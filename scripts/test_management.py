"""Management regressions with temporary state and mocked outbound APIs."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import os
import time
import queue
import types
import copy
import unittest
from unittest.mock import patch

import tower_ctl as ctl
import tower_sync as sync
from tower_store import JsonMap
from tower_delivery import Outbox, CommandInbox
from tower_runtime import health_issues


class Management(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_finished_round_wins_over_stale_windows_pid(self):
        rc = self.root / 'round.rc'
        rc.write_text('1')
        jobs = {'dj1': {'jid': 'dj1', 'state': 'running', 'pid': 123,
                        'rc_file': str(rc), 'chat_id': '', 'sess': ''}}
        with patch.object(ctl, 'jobs_load', return_value=jobs), \
             patch.object(ctl, 'jobs_save'), patch.object(ctl, 'alive', return_value=True), \
             patch.object(ctl, 'log'):
            ctl.poll_jobs()
        self.assertEqual(jobs['dj1']['state'], 'exited')
        self.assertEqual(jobs['dj1']['exit'], '1')

    def test_route_merge_survives_sync_restart(self):
        path = str(self.root / 'routes.json')
        with patch.object(ctl, 'CMIDF', path), patch.object(sync, 'CMIDF', path), \
             patch.object(sync, '_cmid_cache', {'d': {}, 'n': 0}), \
             patch.object(sync, 'jid_of', return_value='aaaaaaaa'), \
             patch.object(sync, 'sid8', return_value='aaaaaaaa'):
            ctl.cmid_register('root', 'j1')
            sync.register_mid('message', 'fixture')
            self.assertEqual(ctl.cmid_lookup('root'), {'kind': 'job', 'jid': 'j1'})
            self.assertEqual(ctl.cmid_lookup('message')['kind'], 'sess')
            ctl.cmid_prune(set())
            self.assertIsNone(ctl.cmid_lookup('root'))
            self.assertIsNotNone(ctl.cmid_lookup('message'))

    def test_concurrent_map_updates_do_not_lose_entries(self):
        store = JsonMap(self.root / 'map.json')
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda n: store.put(str(n), n), range(100)))
        self.assertEqual(len(store.read()), 100)

    def test_corrupt_map_is_preserved(self):
        path = self.root / 'map.json'
        path.write_text('broken')
        with self.assertRaises(ValueError):
            JsonMap(path).put('new', 'value')
        self.assertEqual(path.read_text(), 'broken')

    def transcript(self, sid):
        path = self.root / '.codex/sessions/2026/10/07' / f'rollout-2026-10-07T10-00-00-{sid}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(' ' * (51 * 1024))
        return str(path)

    def test_unwatch_survives_restart_and_manual_watch_clears_ignore(self):
        sid = 'aaaaaaaa-1111-7111-8111-111111111111'
        path = self.transcript(sid)
        topics = self.root / 'sess-topics.json'
        topics.write_text(json.dumps({sid: 'root'}))
        with patch.object(ctl, 'STOPICF', str(topics)), patch.object(ctl, 'reply'), \
             patch.object(ctl, 'audit'), patch.object(ctl, 'conf', return_value=dict(ctl.DEF)), \
             patch.object(ctl, '_scan_active', return_value=[('🖥', sid, '', '', time.time(), False)]), \
             patch.object(ctl, 'find_rollout', return_value=(path, sid)), \
             patch.object(ctl, 'watch_session') as watch:
            ctl.cmd_unwatch('fixture', 'fixture', [sid[:8]])
            ctl.auto_watch_tick()
            watch.assert_not_called()
            self.assertTrue(ctl.watch_settings().read()[sid]['ignored'])
        # Existing mapping is sufficient to exercise a manual re-enable without sending.
        topics.write_text(json.dumps({sid: 'root'}))
        with patch.object(ctl, 'STOPICF', str(topics)), \
             patch.object(ctl, 'find_rollout', return_value=(path, sid)):
            ctl.watch_session('fixture', sid, sender='fixture')
            self.assertFalse(ctl.watch_settings().read()[sid]['ignored'])

    def test_unwatch_callback_accepts_full_id_and_rejects_ambiguous_prefix(self):
        ids = ['aaaaaaaa-1111-7111-8111-111111111111', 'aaaaaaaa-2222-7222-8222-222222222222']
        topics = self.root / 'sess-topics.json'
        topics.write_text(json.dumps(dict.fromkeys(ids, 'root')))
        with patch.object(ctl, 'STOPICF', str(topics)), patch.object(ctl, 'reply') as reply, \
             patch.object(ctl, 'audit'), patch.object(ctl, 'conf', return_value={'allow_ou': 'fixture'}), \
             patch.object(ctl, 'rate_ok', return_value=True):
            ctl.handle_ui('fixture', 'fixture', {'act': 'unwatch_sess', 'sess8': ids[0][:8]})
            self.assertEqual(set(ctl._st_load()), set(ids))
            self.assertIn('歧义', reply.call_args.args[1])
            ctl.handle_ui('fixture', 'fixture', {'act': 'unwatch_sess', 'sess8': ids[0]})
            self.assertEqual(set(ctl._st_load()), {ids[1]})
            self.assertTrue(ctl.watch_settings().read()[ids[0]]['ignored'])
            ctl.handle_ui('fixture', 'fixture', {'act': 'unwatch_sess', 'sess8': 'all'})
            self.assertEqual(set(ctl._st_load()), {ids[1]})
            self.assertIn('无效', reply.call_args.args[1])

    def test_scan_does_not_starve_unmanaged_sessions(self):
        ids = [f'{n:08x}-1111-7111-8111-111111111111' for n in range(9)]
        for sid in ids:
            self.transcript(sid)
        (self.root / 'sess-topics.json').write_text(json.dumps({sid: 'root' for sid in ids[:8]}))
        with patch.object(ctl, 'HOME', str(self.root)), patch.object(ctl, 'STATED', str(self.root)), \
             patch.object(ctl, 'jobs_load', return_value={}), patch.object(ctl.harness, 'files', return_value=[]):
            rows = ctl._scan_active()
        self.assertEqual(len(rows), 9)
        self.assertEqual([row[1] for row in rows if not row[5]], ids[8:])

    def test_short_id_ambiguity_never_picks_first_file(self):
        ids = ['aaaaaaaa-1111-7111-8111-111111111111', 'aaaaaaaa-2222-7222-8222-222222222222']
        paths = [self.transcript(sid) for sid in ids]
        with patch.dict(os.environ, HOME=str(self.root)), patch.object(ctl.harness, 'files', return_value=[]):
            self.assertEqual(ctl.find_rollout('aaaaaaaa'), ('', ''))
            for path, sid in zip(paths, ids):
                self.assertEqual(ctl.find_rollout(sid), (path, sid))

    def test_event_identity_distinguishes_rounds_sessions_and_replays(self):
        first = self.transcript('aaaaaaaa-1111-7111-8111-111111111111')
        second = self.transcript('bbbbbbbb-1111-7111-8111-111111111111')
        h = sync.event_hash(first, 100)
        self.assertEqual(h, sync.event_hash(first, 100))
        self.assertNotEqual(h, sync.event_hash(first, 200))
        self.assertNotEqual(h, sync.event_hash(second, 100))
        self.assertNotEqual(h, sync.event_hash(first, 100, 1))
        box = Outbox(self.root / 'delivery.sqlite')
        old = box.put(('msg', first, h), {'answer': '已完成'}, 'root')
        box.finish(old['id'], 'message')
        replay = box.put(('msg', first, h), {'answer': '已完成'}, 'root')
        next_round = box.put(('msg', first, sync.event_hash(first, 200)), {'answer': '已完成'}, 'root')
        self.assertEqual(replay['status'], 'sent')
        self.assertEqual(next_round['status'], 'pending')
        self.assertNotEqual(old['id'], next_round['id'])

    def test_topic_reply_failure_reuses_root_and_stable_tokens(self):
        topics = self.root / 'sess-topics.json'
        with patch.object(ctl, 'STOPICF', str(topics)), \
             patch.object(ctl, 'action_card', return_value='root') as root, \
             patch.object(ctl, 'api', side_effect=[{'code': 500}, {'code': 0, 'data': {'thread_id': 'thread'}}]) as api:
            first = ctl.create_topic('watch:fixture', 'chat', 'title', [], 'anchor', 'body')
            self.assertFalse(first[0])
            builds = JsonMap(self.root / 'topic-builds.json')
            builds.update(lambda data: data['watch:fixture'].update(next_at=0))
            second = ctl.create_topic('watch:fixture', 'chat', 'title', [], 'anchor', 'body')
            self.assertEqual(second, ('root', 'thread'))
            root.assert_called_once()
            self.assertEqual(api.call_args_list[0].args[2]['uuid'], api.call_args_list[1].args[2]['uuid'])
            self.assertEqual(builds.read()['watch:fixture']['phase'], 'ready')

    def test_topic_uncertain_root_response_reuses_token_after_restart(self):
        with patch.object(ctl, 'STOPICF', str(self.root / 'sess-topics.json')), \
             patch.object(ctl, 'action_card', side_effect=['', 'root']) as root, \
             patch.object(ctl, 'api', return_value={'code': 0, 'data': {'message_id': 'anchor'}}):
            ctl.create_topic('watch:fixture', 'chat', 'title', [], 'anchor', 'body')
            JsonMap(self.root / 'topic-builds.json').update(lambda data: data['watch:fixture'].update(next_at=0))
            self.assertEqual(ctl.create_topic('watch:fixture', 'chat', 'title', [], 'anchor', 'body')[0], 'root')
            self.assertEqual(root.call_args_list[0].kwargs['delivery_id'], root.call_args_list[1].kwargs['delivery_id'])

    def test_long_message_keeps_tail_and_balances_code_fences(self):
        text = 'intro\n\n```python\n' + 'print("content")\n' * 100 + '```\n\nFINAL_TAIL'
        parts = sync.split_message(text, 300)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 300 for part in parts))
        self.assertTrue(parts[-1].endswith('FINAL_TAIL'))
        self.assertEqual(sum(part.count('print("content")') for part in parts), 100)
        self.assertTrue(all(sum(line.startswith('```') for line in part.splitlines()) % 2 == 0 for part in parts))
        plain = '段落内容\n\n' * 300 + '结尾'
        self.assertEqual(''.join(sync.split_message(plain, 300)), plain)

    def test_long_message_partial_delivery_replay_preserves_remaining_parts(self):
        path = self.transcript('aaaaaaaa-1111-7111-8111-111111111111')
        sender = sync.Sender()
        box = Outbox(self.root / 'delivery.sqlite')
        text = '内容' * 600 + 'FINAL_TAIL'
        cfg = {**sync.DEF, 'max_len': 300}
        with patch.object(sync, 'DRY', False), patch.object(sync, 'SENDER', sender), \
             patch.object(sender, 'outbox', return_value=box), patch.object(sync, '_MSGTRY', {}), \
             patch.object(sync, '_MSGPEND', {}), patch.object(sync, 'value_root', return_value='root'), \
             patch.object(sync, 'title_of', return_value='fixture'), patch.object(sync, 'note_of2', return_value='fixture'):
            sync.send_msg_card(path, {'_msgh': 'event'}, text, 'fixture', cfg)
            rows = box.due(100)
            self.assertGreater(len(rows), 1)
            box.finish(rows[0]['id'], 'sent-part-one')
            sync.send_msg_card(path, {'_msgh': 'event'}, text, 'fixture', cfg)
            self.assertEqual(len(box.due(100)), len(rows) - 1)
            self.assertEqual(box.summary()['counts']['sent'], 1)
            self.assertTrue(any('FINAL_TAIL' in row['payload'] for row in box.due(100)))

    def test_incoming_message_is_deduplicated_across_restart(self):
        event = types.SimpleNamespace(event=types.SimpleNamespace(
            sender=types.SimpleNamespace(sender_id=types.SimpleNamespace(open_id='fixture')),
            message=types.SimpleNamespace(chat_id='fixture', chat_type='group', content=json.dumps({'text': '#new PRIVATE_PROMPT'}),
                                          message_id='fixture-event', parent_id='', thread_id='')))
        with patch.object(ctl, 'STATED', str(self.root)), patch.object(ctl, 'Q', queue.Queue()) as q, \
             patch.object(ctl, 'conf', return_value=dict(ctl.DEF)), patch.object(ctl, 'log'):
            ctl.on_message(event)
            ctl.on_message(event)
            self.assertEqual(q.qsize(), 1)
            ident = q.get_nowait()[-1]['command_id']
            self.assertEqual(ctl.command_inbox().recover(), 1)
            ctl.on_message(event)
            self.assertTrue(q.empty())
            self.assertEqual(ctl.command_inbox().summary()['counts'], {'interrupted': 1})
        self.assertNotIn(b'PRIVATE_PROMPT', (self.root / 'command-inbox.sqlite').read_bytes())
        self.assertNotIn(b'fixture-event', (self.root / 'command-inbox.sqlite').read_bytes())

    def test_queue_full_rejects_without_consuming_command_identity(self):
        with patch.object(ctl, 'STATED', str(self.root)), patch.object(ctl, 'Q', queue.Queue()) as q, \
             patch.object(ctl, 'conf', return_value={**ctl.DEF, 'command_queue_max': 1}):
            q.put(('fixture',))
            self.assertEqual(ctl.enqueue_inbound(('fixture',), 'message', 'event'), 'full')
            q.get_nowait()
            self.assertEqual(ctl.enqueue_inbound(('fixture',), 'message', 'event'), 'queued')

    def test_worker_persists_command_handled_and_runs_only_once(self):
        class Stop(BaseException):
            pass
        class Once(queue.Queue):
            def get(self, *args, **kwargs):
                if self.empty():
                    raise Stop()
                return super().get(*args, **kwargs)
        with patch.object(ctl, 'STATED', str(self.root)), patch.object(ctl, 'Q', Once()), \
             patch.object(ctl, 'conf', return_value={**ctl.DEF, 'allow_ou': 'fixture'}), \
             patch.object(ctl, 'rate_ok', return_value=True), patch.object(ctl, 'log'), \
             patch.object(ctl, 'audit'), patch.object(ctl, 'wiz_get', return_value=None), \
             patch.dict(ctl.CMDS, {'fixture': unittest.mock.Mock()}) as commands:
            ctl.enqueue_inbound(('fixture', 'chat', '#fixture', '', ''), 'message', 'event')
            with self.assertRaises(Stop):
                ctl.worker()
            commands['fixture'].assert_called_once()
            self.assertEqual(ctl.command_inbox().summary()['counts'], {'handled': 1})
            self.assertEqual(ctl.enqueue_inbound(('fixture',), 'message', 'event'), 'duplicate')

    def scan_fixture(self, events, policy, dry=True, source="codex"):
        sid = 'aaaaaaaa-1111-7111-8111-111111111111'
        path = self.transcript(sid)
        if source != 'codex':
            path = str(self.root / f'.{source}/projects/demo/{sid}.jsonl')
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(''.join(json.dumps(event) + '\n' for event in events))
        snapshot = {path: {'off': 0, 'armed': True, 'task': 'fixture', 'proj': 'demo', 'ev': 0,
                           'lev_ts': time.time(), 'seg': None}}
        cfg = {**sync.DEF, 'save_gap': 0}
        sender = unittest.mock.Mock()
        with patch.object(sync, 'STATE', str(self.root / 'sync.json')), \
             patch.object(sync, 'LOGD', str(self.root)), patch.object(sync, 'LOG', str(self.root / 'sync.log')), \
             patch.object(sync, 'conf', return_value=cfg), patch.object(sync, 'DRY', dry), \
             patch.object(sync, 'app_mode', return_value=False), \
             patch.object(sync, 'session_preferences', return_value={sid: policy}), \
             patch.object(sync, 'session_policy', side_effect=lambda *args: {'mode': 'full', **policy}), \
             patch.object(sync, 'load_state', return_value=snapshot), \
             patch.object(sync, '_scan_files', return_value=[path]), patch.object(sync, 'SENDER', sender), \
             patch.object(sync, 'harvest_results'), patch.object(sync, 'sent_set', return_value=set()), \
             patch.object(sync, '_MSGPEND', {}), patch.object(sync, 'log'), \
             patch.object(sync, 'send_msg_card') as send, \
             patch.dict(os.environ, SYNC_TEST_SECS='-1'):
            sync.scan_dir(test_dir=str(self.root))
        return send, snapshot[path], Path(path).stat().st_size

    def message(self, text, channel='commentary'):
        return {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'channel': channel,
                                                   'content': [{'type': 'output_text', 'text': text}]}}

    def test_pending_questions_bypass_result_errors_and_warmup_but_respect_pause(self):
        question = {'type': 'response_item', 'payload': {'type': 'function_call',
                    'name': 'request_user_input', 'arguments': json.dumps({'questions': [
                        {'question': '请选择下一步', 'options': [{'label': '继续'}]}], 'private': 'PRIVATE'})}}
        for mode in ('full', 'result', 'errors'):
            send, _, _ = self.scan_fixture([question], {'mode': mode})
            self.assertEqual(send.call_count, 1)
            self.assertIn('请选择下一步', send.call_args.args[2])
            self.assertNotIn('PRIVATE', send.call_args.args[2])
        send, _, _ = self.scan_fixture([question], {'paused': True})
        send.assert_not_called()

    def test_result_mode_emits_only_final_reply(self):
        send, state, size = self.scan_fixture([self.message('intermediate'), self.message('FINAL', 'final')], {'mode': 'result'})
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[2], 'FINAL')
        self.assertEqual(state['off'], size)
        self.assertNotIn('result_pending', state)

    def test_errors_mode_emits_fixed_error_without_raw_details(self):
        send, _, _ = self.scan_fixture([self.message('success', 'final'), {'type': 'error', 'message': 'PRIVATE_ERROR'}], {'mode': 'errors'})
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.kwargs['template'], 'red')
        self.assertNotIn('PRIVATE_ERROR', str(send.call_args))

    def test_pause_consumes_new_events_without_broadcast_or_backfill(self):
        send, state, size = self.scan_fixture([self.message('private paused message', 'final')], {'paused': True})
        send.assert_not_called()
        self.assertEqual(state['off'], size)
        self.assertNotIn('result_pending', state)

    def test_observer_title_pause_modes_and_full_uuid_buttons(self):
        sid = 'aaaaaaaa-1111-7111-8111-111111111111'
        JsonMap(self.root / 'sess-topics.json').put(sid, 'root')
        with patch.object(ctl, 'STOPICF', str(self.root / 'sess-topics.json')), \
             patch.object(ctl, 'jobs_load', return_value={}), patch.object(ctl, 'conf', return_value=dict(ctl.DEF)), \
             patch.object(ctl, 'reply'), patch.object(ctl, 'audit'), \
             patch.object(ctl, 'api', return_value={'code': 0}) as api:
            ctl.cmd_observe('fixture', 'chat', [sid[:8], 'title', '观察测试'])
            ctl.cmd_observe('fixture', 'chat', [sid, 'pause'])
            ctl.cmd_observe('fixture', 'chat', [sid, 'mode', 'errors'])
            policy = ctl.watch_settings().read()[sid]
            self.assertEqual(policy['title'], '观察测试')
            self.assertTrue(policy['paused'])
            self.assertEqual(policy['mode'], 'errors')
            self.assertEqual(api.call_args.args[:2], ('PATCH', '/im/v1/messages/root'))
            _, elements, _ = ctl.watch_card_parts(sid, policy)
            self.assertIn(sid, json.dumps(elements))
            ctl.cmd_observe('fixture', 'chat', [sid, 'resume'])
            self.assertFalse(ctl.watch_settings().read()[sid]['paused'])

    def test_pending_delivery_waits_for_resume(self):
        sid = 'aaaaaaaa-1111-7111-8111-111111111111'
        path = self.transcript(sid)
        box = Outbox(self.root / 'delivery.sqlite')
        box.put(('msg', path, 'event'), {'body': 'fixture'}, 'root')
        preferences = JsonMap(self.root / 'session-settings.json')
        preferences.put(sid, {'paused': True})
        sender = sync.Sender()
        with patch.object(sync, 'STATE', str(self.root / 'sync.json')), patch.object(sync, 'DRY', False), \
             patch.object(sync, 'conf', return_value={**sync.DEF, 'watched_only': 0}), \
             patch.object(sender, 'outbox', return_value=box), patch.object(sync, 'jid_of', return_value=sid[:8]), \
             patch.object(sync, '_MSGTRY', {}), patch.object(sync, 'app_send', return_value='message') as send, \
             patch.object(sync.time, 'sleep'):
            sender.replay_due()
            send.assert_not_called()
            self.assertEqual(box.summary()['counts'], {'pending': 1})
            preferences.put(sid, {'paused': False})
            sender.replay_due()
            send.assert_called_once()
            self.assertEqual(box.summary()['counts'], {'sent': 1})

    def test_watched_only_scope_requires_manual_topic_or_tower_job(self):
        sid = 'aaaaaaaa-1111-7111-8111-111111111111'
        topics, jobs = {}, {}
        with patch.object(sync, 'conf', return_value={**sync.DEF, 'watched_only': 1}), \
             patch.object(sync, 'sess_uuid_of', return_value=sid), \
             patch.object(sync, 'jid_of', return_value='j1'), \
             patch.object(sync, 'sess_topics', side_effect=lambda: topics), \
             patch.object(sync, 'jobs_raw', side_effect=lambda: jobs):
            self.assertTrue(sync.session_policy('source', {})['scope_excluded'])
            topics[sid] = 'root'
            self.assertFalse(sync.session_policy('source', {}).get('paused'))
            self.assertTrue(sync.session_policy('source', {sid: {'paused': True}})['paused'])
            topics.clear()
            jobs['j1'] = {'root_mid': 'job-root'}
            self.assertFalse(sync.session_policy('source', {}).get('paused'))
            jobs.clear()
            with patch.object(sync, 'conf', return_value={**sync.DEF, 'watched_only': 0}):
                self.assertFalse(sync.session_policy('source', {}).get('paused'))

    def test_paused_backlog_does_not_starve_active_delivery(self):
        box = Outbox(self.root / 'delivery.sqlite')
        for index in range(25):
            box.put(('msg', 'paused', str(index)), {'body': 'fixture'}, 'root')
        box.put(('msg', 'active', 'event'), {'body': 'fixture'}, 'root')
        sender = sync.Sender()
        with patch.object(sync, 'DRY', False), patch.object(sender, 'outbox', return_value=box), \
             patch.object(sync, 'session_policy', side_effect=lambda path, *args: {'paused': path == 'paused'}), \
             patch.object(sync, 'app_send', return_value='message') as send, patch.object(sync, '_MSGTRY', {}), \
             patch.object(sync.time, 'sleep'):
            sender.replay_due()
            sender.replay_due()
            send.assert_called_once()
        self.assertEqual(box.summary()['counts'], {'pending': 25, 'sent': 1})

    def test_unknown_end_uses_explicitly_unconfirmed_idle_summary(self):
        state = {'result_pending': {'text': 'last reply', 'identity': 'fixture', 'at': 0}}
        with patch.object(sync, 'send_msg_card') as send:
            sync.emit_result('fixture', state, 'fixture', sync.DEF, preview=True)
        self.assertIn('未确认结束', send.call_args.kwargs['suffix'])
        self.assertNotIn('result_pending', state)
        self.assertEqual(sync.source_signals(json.dumps({'type': 'error', 'message': 'PRIVATE'})), (False, True))

    def test_observed_panel_preserves_engine_and_preference(self):
        sid = 'aaaaaaaa-1111-7111-8111-111111111111'
        values, _, _, _ = ctl.task_panel.rows({}, {sid: 'root'}, [], {}, 'watch', preferences={sid: {'title': '备注', 'agent': 'qoder', 'paused': True}})
        self.assertEqual(values[0]['id'], sid)
        self.assertEqual(values[0]['agent'], 'qoder')
        self.assertEqual(values[0]['title'], '备注')
        self.assertIn('暂停', values[0]['status']['label'])
        self.assertFalse(values[0]['status']['can_go'])

    def healthy_status(self):
        now = time.time()
        return {'updated_at': now, 'ws_connected': True, 'ctl_alive': True, 'sync_alive': True,
                'sender_alive': True, 'command_worker_alive': True,
                'ctl_progress_at': now, 'sync_progress_at': now, 'sender_progress_at': now}

    def test_history_is_not_current_failure(self):
        status = {**self.healthy_status(), 'delivery_counts': {'failed': 6},
                  'state_write_failed_at': time.time()-600}
        self.assertEqual(health_issues(status), [])
        status['state_write_failed_at'] = time.time()
        self.assertTrue(any('运行异常' in issue for issue in health_issues(status)))

    def test_liveness_cannot_hide_stalled_sender_or_command_queue(self):
        status = {**self.healthy_status(), 'sender_progress_at': time.time()-200,
                  'command_oldest_wait_seconds': 301}
        issues = health_issues(status)
        self.assertIn('投递线程进度超时', issues)
        self.assertIn('命令排队超过300秒', issues)
        status['sender_alive'] = False
        self.assertIn('投递线程未运行/未确认', health_issues(status))

    def test_outbox_recent_failure_window(self):
        box = Outbox(self.root / 'box.sqlite')
        row = box.put(('msg', 'fixture', 'one'), {}, 'root')
        box.finish(row['id'], error={'retryable': False})
        self.assertEqual(box.summary()['recent_failed'], 1)
        with box.connect() as connection:
            connection.execute('UPDATE deliveries SET updated_at=?', (time.time()-600,))
        self.assertEqual(box.summary()['recent_failed'], 0)
        self.assertEqual(box.summary()['counts'], {'failed': 1})

    def test_paused_pending_is_excluded_from_current_backlog(self):
        box = Outbox(self.root / 'box.sqlite')
        box.put(('msg', 'paused', 'one'), {}, 'root')
        with box.connect() as connection:
            connection.execute('UPDATE deliveries SET created_at=?', (time.time()-600,))
        sender = sync.Sender()
        with patch.object(sender, 'outbox', return_value=box), patch.object(sync, 'DRY', False), \
             patch.object(sync, 'session_policy', return_value={'paused': True}), \
             patch.object(sync, 'session_preferences', return_value={}), \
             patch.dict(sync.STATUS, {}, clear=True):
            sender.replay_due()
            self.assertEqual(sync.STATUS['delivery_active_oldest_seconds'], 0)
            self.assertEqual(sync.STATUS['delivery_counts']['pending'], 1)

    def test_observation_patch_cannot_acknowledge_newer_title(self):
        sid = 'aaaaaaaa-1111-7111-8111-111111111111'
        topics = self.root / 'sess-topics.json'
        topics.write_text(json.dumps({sid: 'root'}))
        with patch.object(ctl, 'STOPICF', str(topics)), patch.object(ctl, 'conf', return_value=ctl.DEF):
            ctl.update_watch_settings(sid, title='旧标题', root_update_pending=True)
            def changed(*args):
                ctl.update_watch_settings(sid, title='新标题', root_update_pending=True)
                return {'code': 0}
            with patch.object(ctl, 'api', side_effect=changed):
                ctl.refresh_observed_titles()
            self.assertTrue(ctl.watch_settings().read()[sid]['root_update_pending'])
            self.assertEqual(ctl.watch_settings().read()[sid]['title'], '新标题')
            with patch.object(ctl, 'api', return_value={'code': 0}):
                ctl.refresh_observed_titles()
            self.assertFalse(ctl.watch_settings().read()[sid]['root_update_pending'])

    def test_fragment_strategy_survives_config_change(self):
        box = Outbox(self.root / 'box.sqlite')
        sender = sync.Sender()
        text = '正文' * 700 + 'END_TAIL'
        with patch.object(sync, 'SENDER', sender), patch.object(sender, 'outbox', return_value=box), \
             patch.object(sync, 'DRY', False), patch.object(sync, 'app_mode', return_value=True), \
             patch.object(sync, 'value_root', return_value='root'), \
             patch.object(sync, 'title_of', return_value='fixture'), patch.object(sync, 'note_of2', return_value='fixture'), \
             patch.object(sync, '_MSGPEND', {}), patch.object(sync, '_MSGTRY', {}):
            sync.send_msg_card('fixture', {'_msgh': 'event'}, text, 'host', {**sync.DEF, 'max_len': 300})
            first = box.due(100)
            box.finish(first[0]['id'], 'sent-one')
            sync.send_msg_card('fixture', {'_msgh': 'event'}, text, 'host', {**sync.DEF, 'max_len': 1200})
            self.assertEqual(len(box.due(100)), len(first)-1)
            self.assertEqual(box.summary()['counts']['sent'], 1)
            self.assertTrue(any('END_TAIL' in row['payload'] for row in box.due(100)))
            with self.assertRaisesRegex(ValueError, 'content changed'):
                sync.send_msg_card('fixture', {'_msgh': 'event'}, 'changed', 'host', sync.DEF)
            self.assertNotIn('event', sync._MSGPEND)

    def test_webhook_full_result_errors_and_pause_use_common_policy(self):
        events = [self.message('重复正文') for _ in range(15)]
        send, _, _ = self.scan_fixture(events, {'mode': 'full'}, dry=False)
        self.assertEqual(send.call_count, 15)
        send, _, _ = self.scan_fixture([self.message('草稿'), self.message('完整结果', 'final')], {'mode': 'result'}, dry=False)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[2], '完整结果')
        send, _, _ = self.scan_fixture([self.message('正文'), {'type': 'error', 'message': 'PRIVATE_DETAIL'}], {'mode': 'errors'}, dry=False)
        self.assertEqual(send.call_count, 1)
        self.assertNotIn('PRIVATE_DETAIL', send.call_args.args[2])
        send, state, size = self.scan_fixture(events, {'paused': True}, dry=False)
        send.assert_not_called()
        self.assertEqual(state['off'], size)

    def test_webhook_partial_delivery_is_durable_and_not_a_reply_route(self):
        box = Outbox(self.root / 'box.sqlite')
        sender = sync.Sender()
        text = '正文' * 700 + 'END_TAIL'
        with patch.object(sync, 'SENDER', sender), patch.object(sender, 'outbox', return_value=box), \
             patch.object(sync, 'DRY', False), patch.object(sync, 'app_mode', return_value=False), \
             patch.object(sync, 'session_policy', return_value={'mode': 'full'}), \
             patch.object(sync, 'value_root', return_value=''), patch.object(sync, 'title_of', return_value='fixture'), \
             patch.object(sync, 'note_of2', return_value='fixture'), patch.object(sync.time, 'sleep'), \
             patch.object(sync, '_MSGPEND', {}), patch.object(sync, '_MSGTRY', {}):
            sync.send_msg_card('fixture', {'_msgh': 'event'}, text, 'host', {**sync.DEF, 'max_len': 300})
            with patch.object(sync, 'post', side_effect=[True, False, True, True]) as post, \
                 patch.object(sync, 'app_send') as app_send:
                sender.replay_due()
                self.assertEqual(post.call_count, 4)
                app_send.assert_not_called()
            self.assertEqual(box.summary()['counts']['sent'], 3)
            self.assertGreater(box.summary()['counts']['pending'], 0)
            with patch.object(sync, 'register_route') as route:
                sync.register_mid('hook_local_receipt', 'fixture')
                route.assert_not_called()
            with box.connect() as connection:
                connection.execute("UPDATE deliveries SET next_at=0 WHERE status='pending'")
            with patch.object(sync, 'post', return_value=True):
                for _ in range(3): sender.replay_due()
            self.assertEqual(box.summary()['counts'].get('pending', 0), 0)

    def test_new_user_turn_cannot_claim_previous_result_completed(self):
        events = [self.message('草稿'), {'type': 'event_msg', 'payload': {'type': 'task_started'}}]
        send, _, _ = self.scan_fixture(events, {'mode': 'result'})
        self.assertEqual(send.call_count, 1)
        self.assertIn('未确认结束', send.call_args.kwargs['suffix'])

    def test_result_keeps_all_text_blocks_in_native_message(self):
        for source in ('claude', 'workbuddy'):
            with self.subTest(source=source):
                content = [{'type': 'text', 'text': '第一段结果'},
                           {'type': 'thinking', 'thinking': 'PRIVATE_REASONING'},
                           {'type': 'text', 'text': '第二段尾部'}]
                event = ({'type': 'assistant', 'message': {'content': content, 'stop_reason': 'end_turn'}}
                         if source == 'claude' else
                         {'type': 'message', 'role': 'assistant', 'channel': 'final', 'content': content})
                send, _, _ = self.scan_fixture([event], {'mode': 'result'}, source=source)
                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.args[2], '第一段结果\n\n第二段尾部')
                self.assertNotIn('PRIVATE_REASONING', str(send.call_args))
                send, _, _ = self.scan_fixture([event], {'mode': 'full'}, source=source)
                self.assertEqual(send.call_count, 2)

    def test_durable_webhook_preserves_configured_rate_limit(self):
        box = Outbox(self.root / 'box.sqlite')
        box.put(('hook', 'fixture', 'one'), {}, '')
        box.put(('msg', 'fixture', 'two'), {}, 'root')
        sender = sync.Sender()
        with patch.object(sender, 'outbox', return_value=box), patch.object(sync, 'DRY', False), \
             patch.object(sync, 'conf', return_value={**sync.DEF, 'gap': 2, 'api_gap': .5}), \
             patch.object(sync, 'session_policy', return_value={'mode': 'full'}), \
             patch.object(sync, 'post', return_value=True), patch.object(sync, 'app_send', return_value='app-mid'), \
             patch.object(sync.time, 'sleep') as sleep:
            sender.replay_due()
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, .5])

    def test_root_routes_recover_legacy_overwrite_and_preserve_reply_routes(self):
        topics = self.root / 'topics.json'
        routes = self.root / 'routes.json'
        topics.write_text(json.dumps({'aaaaaaaa-1111-7111-8111-111111111111': 'watch-root',
                                      'bbbbbbbb-1111-7111-8111-111111111111': 'adopted-root'}))
        routes.write_text(json.dumps({'reply': {'kind': 'sess', 'sid8': 'existing'},
                                      'watch-root': {'kind': 'job', 'jid': 'deleted'}}))
        with patch.object(ctl, 'STOPICF', str(topics)), patch.object(ctl, 'CMIDF', str(routes)), \
             patch.object(ctl, 'jobs_load', return_value={'j1': {'root_mid': 'adopted-root'}}):
            self.assertEqual(ctl.repair_root_routes(), 2)
            self.assertEqual(ctl.cmid_lookup('watch-root'), {'kind': 'sess', 'sid8': 'aaaaaaaa-1111-7111-8111-111111111111'})
            self.assertEqual(ctl.cmid_lookup('adopted-root'), {'kind': 'job', 'jid': 'j1'})
            self.assertEqual(ctl.cmid_lookup('reply'), {'kind': 'sess', 'sid8': 'existing'})
            self.assertEqual(ctl.repair_root_routes(), 0)
            routes.write_text('broken')
            with self.assertRaises(ValueError): ctl.repair_root_routes()
            self.assertEqual(routes.read_text(), 'broken')

    def test_existing_watch_restores_root_route_without_rebuilding(self):
        sid = 'aaaaaaaa-1111-7111-8111-111111111111'
        path = self.transcript(sid)
        topics = self.root / 'topics.json'
        topics.write_text(json.dumps({sid: 'root'}))
        with patch.object(ctl, 'STOPICF', str(topics)), patch.object(ctl, 'CMIDF', str(self.root/'routes.json')), \
             patch.object(ctl, 'find_rollout', return_value=(path, sid)), patch.object(ctl, 'create_topic') as create:
            self.assertEqual(ctl.watch_session('fixture', sid), (None, 'dup'))
            self.assertEqual(ctl.cmid_lookup('root')['sid8'], sid)
            with patch.object(ctl, 'jobs_load', return_value={'j1': {'root_mid': 'root'}}):
                ctl.watch_session('fixture', sid)
            self.assertEqual(ctl.cmid_lookup('root'), {'kind': 'job', 'jid': 'j1'})
            create.assert_not_called()

    def test_manual_watch_reuses_existing_desktop_control_topic(self):
        sid = 'aaaaaaaa-1111-7111-8111-111111111111'
        path = self.transcript(sid)
        binding = {'sid': sid, 'chat_id': 'control-chat', 'root_mid': 'control-root',
                   'thread_id': 'control-thread'}
        with patch.object(ctl, 'STATED', str(self.root)), \
             patch.object(ctl, 'STOPICF', str(self.root / 'sess-topics.json')), \
             patch.object(ctl, 'CMIDF', str(self.root / 'card-mids.json')), \
             patch.object(ctl, 'find_rollout', return_value=(path, sid)), \
             patch.object(ctl, 'desktop_bindings', return_value=type('Bindings', (), {'read': lambda self: {'binding': binding}})()), \
             patch.object(ctl, 'api', return_value={'code': 0, 'data': {'items': [{'deleted': False}]}}), \
             patch.object(ctl, 'create_topic') as create, patch.object(ctl, 'audit'), \
             patch.object(ctl, 'reply'):
            self.assertEqual(ctl.watch_session('control-chat', sid, sender='user', quiet=True),
                             ('control-root', 'control-thread'))
            self.assertEqual(ctl._st_load()[sid], 'control-root')
            create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
