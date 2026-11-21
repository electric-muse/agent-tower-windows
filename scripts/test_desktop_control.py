"""Offline regressions for Feishu control of an existing Codex desktop thread."""
import json
import io
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import tower_ctl as ctl
import tower_sync as sync
from tower_codex_bridge import CodexBridge, CodexBridgeError, latest_turn, turn_by_client
from tower_desktop_control import attach_title, catalog_matches
from tower_ui import sanitize_message_body


SID = '11111111-1111-4111-8111-111111111111'


def state(turn_id='old', status='completed', text='done', runtime='idle', client_id='event-id'):
    entity = {'turnId': turn_id, 'status': status,
              'items': [{'type': 'userMessage', 'clientId': client_id},
                        {'type': 'agentMessage', 'phase': 'final_answer', 'text': text}]}
    return {'id': SID, 'cwd': '/project', 'threadRuntimeStatus': {'type': runtime},
            'turnHistory': {'history': {'entitiesByKey': {'turn': entity},
                                        'islands': [{'entries': [{'value': 'turn'}]}]}}}


class DesktopControl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        ctl._REPLY_CONTEXT.route = {}

    def tearDown(self):
        ctl._REPLY_CONTEXT.route = {}

    def test_rich_text_code_style_command_is_extracted_once(self):
        content = [[{'tag': 'text', 'text': '#leave', 'style': ['code']}]]
        post = {'title': '', 'content': content, 'content_v2': content}
        self.assertEqual(ctl.inbound_text(post, 'post'), '#leave')
        self.assertEqual(ctl.inbound_text({'zh_cn': post, 'en_us': post}, 'post'), '#leave')
        self.assertEqual(ctl.inbound_text({'text': ' #leave '}, 'text'), '#leave')

    def test_post_paragraphs_preserve_user_text_without_using_link_destinations(self):
        post = {'title': '', 'content': [[{'tag': 'text', 'text': '继续'},
                                         {'tag': 'a', 'text': '学习', 'href': '#leave'}],
                                        [{'tag': 'img', 'image_key': 'img_test'},
                                         {'tag': 'text', 'text': '下一题'}]]}
        self.assertEqual(ctl.inbound_text(post, 'post'), '继续学习\n下一题')
        self.assertEqual(ctl.inbound_text([], 'post'), '')

    def test_rich_text_leave_enters_the_same_command_queue(self):
        content = [[{'tag': 'text', 'text': '#leave', 'style': ['code']}]]
        message = SimpleNamespace(chat_id='chat', chat_type='group', parent_id='',
                                  thread_id='', root_id='', message_id='om_rich_leave',
                                  message_type='post', content=json.dumps({'content': content}))
        event = SimpleNamespace(sender=SimpleNamespace(sender_id=SimpleNamespace(open_id='user')),
                                message=message)
        with patch.object(ctl, 'enqueue_inbound', return_value='queued') as enqueue, \
             patch.object(ctl, 'log'):
            ctl.on_message(SimpleNamespace(event=event))
        self.assertEqual(enqueue.call_args.args[0][2], '#leave')

    def test_local_image_reference_does_not_invalidate_feishu_card(self):
        text = '成图 ![午间小憩](C:/posters/001.png)；资料 [来源](https://example.com)'
        card = {'body': {'elements': [{'tag': 'markdown', 'content': text}]}}
        body = {'msg_type': 'interactive', 'content': json.dumps(card, ensure_ascii=False)}
        cleaned = sanitize_message_body(body)
        content = json.loads(cleaned['content'])['body']['elements'][0]['content']
        self.assertNotIn('![', content)
        self.assertIn('午间小憩', content)
        self.assertIn('C:/posters/001.png', content)
        self.assertIn('[来源](https://example.com)', content)
        self.assertEqual(json.loads(body['content'])['body']['elements'][0]['content'], text)

        for module in (ctl, sync):
            with self.subTest(module=module.__name__), \
                 patch.object(module, 'token', return_value='test'), \
                 patch.object(module.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"code":0}')) as send:
                module.api('POST', '/im/v1/messages?receive_id_type=chat_id', body)
                sent = json.loads(send.call_args.args[0].data)
                self.assertEqual(sent['content'], cleaned['content'])

    def test_title_resolution_is_exact_and_unique(self):
        self.assertEqual(attach_title('我要控制 Codex 里“示例学习助手升级”的任务'), '示例学习助手升级')
        self.assertEqual(attach_title('普通聊天'), '')
        db = self.root / 'catalog.db'
        with closing(sqlite3.connect(db)) as con:
            con.execute('CREATE TABLE local_thread_catalog '
                        '(host_id TEXT, thread_id TEXT, display_title TEXT, cwd TEXT, '
                        'source_kind TEXT, thread_source TEXT)')
            con.executemany('INSERT INTO local_thread_catalog VALUES (?,?,?,?,?,?)', [
                ('local', SID, '示例学习助手升级', '/project', 'vscode', 'user'),
                ('remote', 'other', '示例学习助手升级', '/remote', 'vscode', 'user'),
                ('local', 'cli', '示例学习助手升级', '/cli', 'cli', 'user')])
            con.commit()
        self.assertEqual([x['id'] for x in catalog_matches('示例学习助手升级', db)], [SID])
        self.assertEqual(catalog_matches('示例学习助手', db), [])

    def test_short_aliases_only_resolve_explicitly_allowed_threads(self):
        aliases = {'学习': SID, '学习助手': SID,
                   '其他': '22222222-2222-4222-8222-222222222222'}
        config = {**ctl.DEF, 'attach_sessions': SID,
                  'desktop_aliases': json.dumps(aliases, ensure_ascii=False)}
        with patch.object(ctl, 'conf', return_value=config):
            self.assertEqual(ctl.desktop_aliases(), {'学习': SID, '学习助手': SID})
            self.assertEqual(ctl.desktop_shortcut('进入学习助手'), SID)
            self.assertEqual(ctl.desktop_shortcut('我要控制学习'), SID)
            self.assertEqual(ctl.desktop_shortcut('进入其他'), '')
            self.assertEqual(ctl.desktop_shortcut('进入下一章'), '')
            with patch.object(ctl, 'app', return_value={'chat_id': 'chat'}), \
                 patch.object(ctl, 'catalog_matches', return_value=[]) as catalog, \
                 patch.object(ctl, 'reply'):
                ctl.cmd_attach('user', 'chat', ['学习助手'])
                catalog.assert_called_once_with(SID)

    def test_short_alias_routes_through_the_control_worker(self):
        class Done(BaseException):
            pass

        class OneMessage:
            used = False

            def get(self):
                if self.used:
                    raise Done()
                self.used = True
                return ('user', 'chat', '进入学习助手', '', '')

        config = {**ctl.DEF, 'allow_ou': 'user', 'attach_sessions': SID,
                  'desktop_aliases': json.dumps({'学习助手': SID}, ensure_ascii=False)}
        with patch.object(ctl, 'Q', OneMessage()), \
             patch.object(ctl, 'conf', return_value=config), \
             patch.object(ctl, 'rate_ok', return_value=True), \
             patch.object(ctl, 'cmd_attach') as attach, patch.object(ctl, 'log'):
            with self.assertRaises(Done):
                ctl.worker()
            attach.assert_called_once_with('user', 'chat', [SID])

    def test_attach_error_message_distinguishes_failure_stage(self):
        self.assertIn('尚未打开', ctl.desktop_attach_error(
            CodexBridgeError('Codex desktop thread owner is unavailable')))
        self.assertIn('兼容性', ctl.desktop_attach_error(
            CodexBridgeError('Codex question snapshot is unavailable')))
        self.assertIn('桌面接口', ctl.desktop_attach_error(
            CodexBridgeError('Codex desktop IPC open failed: PermissionError winerror=5')))

    def test_full_title_is_resolved_only_inside_explicit_control_scope(self):
        config = {**ctl.DEF, 'attach_sessions': SID}
        with patch.object(ctl, 'conf', return_value=config), \
             patch.object(ctl, 'dsh_catalog_matches', return_value=[]), \
             patch.object(ctl, 'catalog_matches', return_value=[{'id': SID}]) as catalog:
            self.assertEqual(ctl.desktop_shortcut('进入示例学习助手升级'), SID)
            catalog.assert_called_with('示例学习助手升级')
            catalog.return_value = [{'id': 'unapproved'}]
            self.assertEqual(ctl.desktop_shortcut('进入未授权任务'), '')
            catalog.return_value = [{'id': SID}, {'id': SID}]
            self.assertEqual(ctl.desktop_shortcut('进入重名任务'), '')

    def test_unloaded_thread_uses_precise_protocol_error(self):
        bridge = CodexBridge(session_ids={SID})
        with patch('tower_codex_bridge.uuid.uuid4', return_value='request-id'), \
             patch.object(bridge, '_send'), \
             patch.object(bridge, '_receive', return_value={'type': 'response',
                    'requestId': 'request-id', 'resultType': 'error', 'error': 'no-client-found'}):
            with self.assertRaises(CodexBridgeError) as caught:
                bridge.request('thread-owner-discovery', {'hostId': 'local', 'conversationId': SID})
        self.assertEqual(caught.exception.code, 'thread-not-loaded')
        self.assertIn('尚未打开', ctl.desktop_attach_error(caught.exception))

    def test_authorized_unloaded_thread_is_opened_once_and_identity_still_checked(self):
        bridge = CodexBridge(session_ids={SID}, auto_open=True)
        missing = CodexBridgeError('not loaded', code='thread-not-loaded')
        with patch.object(bridge, 'request', side_effect=[missing, {'handledByClientId': 'owner'}]), \
             patch.object(bridge, '_receive', side_effect=TimeoutError), \
             patch.object(bridge, '_following', side_effect=lambda *args: bridge.snapshots.update({SID: state()})), \
             patch('tower_codex_bridge.open_codex_thread') as open_thread:
            self.assertEqual(bridge.snapshot_state(SID)['id'], SID)
            open_thread.assert_called_once_with(SID)

    def test_observation_and_other_errors_do_not_open_threads(self):
        missing = CodexBridgeError('not loaded', code='thread-not-loaded')
        for auto_open, error in ((False, missing), (True, CodexBridgeError('rejected', code='request-rejected'))):
            bridge = CodexBridge(session_ids={SID}, auto_open=auto_open)
            with patch.object(bridge, 'request', side_effect=error), \
                 patch('tower_codex_bridge.open_codex_thread') as open_thread:
                with self.assertRaises(CodexBridgeError):
                    bridge.snapshot_state(SID)
                open_thread.assert_not_called()
        with patch('tower_codex_bridge.open_codex_thread') as open_thread:
            with self.assertRaises(CodexBridgeError):
                CodexBridge(session_ids={SID}, auto_open=True).snapshot_state('other')
            open_thread.assert_not_called()

    def test_open_thread_retry_is_bounded(self):
        bridge = CodexBridge(session_ids={SID}, auto_open=True)
        missing = CodexBridgeError('not loaded', code='thread-not-loaded')
        with patch.object(bridge, 'request', side_effect=missing), \
             patch('tower_codex_bridge.open_codex_thread') as open_thread, \
             patch('tower_codex_bridge.time.monotonic', side_effect=[0, 16]):
            with self.assertRaises(CodexBridgeError) as caught:
                bridge.snapshot_state(SID)
        self.assertEqual(caught.exception.code, 'thread-open-timeout')
        open_thread.assert_called_once_with(SID)

    def test_attach_requires_explicit_scope_and_verified_thread(self):
        rollout = self.root / 'rollout.jsonl'
        rollout.write_text(json.dumps({'payload': {'id': SID, 'cwd': '/project'}}), encoding='utf-8')
        bridge = Mock()
        bridge.snapshot_state.return_value = state()
        bridge.opened_threads = {SID}
        with patch.object(ctl, 'STATED', str(self.root)), \
             patch.object(ctl, 'app', return_value={'chat_id': 'chat'}), \
             patch.object(ctl, 'catalog_matches', return_value=[{'id': SID, 'title': '示例学习助手升级', 'cwd': '/project'}]), \
             patch.object(ctl, 'allowed_cwd', return_value=False), \
             patch.object(ctl, 'conf', return_value={**ctl.DEF, 'attach_sessions': ''}), \
             patch.object(ctl, 'find_rollout', return_value=(str(rollout), SID)), \
             patch.object(ctl, 'CodexBridge', return_value=bridge) as factory, \
             patch.object(ctl, 'create_topic', return_value=('root', 'thread')) as topic, \
             patch.object(ctl, 'reply') as reply, patch.object(ctl, 'audit'), patch.object(ctl, 'log'):
            ctl.cmd_attach('user', 'chat', ['示例学习助手升级'])
            topic.assert_not_called()
            self.assertIn('尚未授权', reply.call_args.args[1])
            with patch.object(ctl, 'conf', return_value={**ctl.DEF, 'attach_sessions': SID}):
                ctl.cmd_attach('user', 'chat', ['示例学习助手升级'])
            self.assertEqual(ctl.desktop_binding('user', 'chat')['sid'], SID)
            self.assertEqual(ctl.desktop_binding('user', 'chat')['thread_id'], 'thread')
            self.assertTrue(ctl.desktop_binding('user', 'chat')['auto_opened'])
            self.assertTrue(factory.call_args.kwargs['auto_open'])
            self.assertIsNone(ctl.desktop_binding('other', 'chat'))
            topic.reset_mock()
            bridge.snapshot_state.side_effect = CodexBridgeError('Codex desktop thread owner is unavailable',
                                                                 code='thread-not-loaded')
            with patch.object(ctl, 'conf', return_value={**ctl.DEF, 'attach_sessions': SID}):
                ctl.cmd_attach('user', 'chat', ['示例学习助手升级'])
            topic.assert_not_called()
            self.assertIn('尚未打开', reply.call_args.args[1])

    def test_other_chat_cannot_attach_or_send(self):
        bound = {'sid': SID, 'title': '任务', 'cwd': '/project', 'chat_id': 'other'}
        with patch.object(ctl, 'app', return_value={'chat_id': 'chat'}), \
             patch.object(ctl, 'catalog_matches') as catalog, \
             patch.object(ctl, 'CodexBridge') as bridge, \
             patch.object(ctl, 'reply') as reply:
            ctl.cmd_attach('user', 'other', ['任务'])
            ctl.desktop_send('user', 'other', '继续', bound)
            catalog.assert_not_called()
            bridge.assert_not_called()
            self.assertIn('无权控制', reply.call_args.args[1])

    def test_busy_thread_cannot_receive_a_turn(self):
        bridge = CodexBridge(session_ids={SID})
        with patch.object(bridge, 'snapshot_state', return_value=state(runtime='active')), \
             patch.object(bridge, 'request') as request:
            with self.assertRaises(CodexBridgeError):
                bridge.start_turn(SID, 'hello')
            request.assert_not_called()

    def test_ipc_open_failure_identifies_stage_without_changing_user_reply(self):
        bridge = CodexBridge(session_ids={SID})
        with patch('tower_codex_bridge.WindowsPipe', side_effect=PermissionError(13, 'denied')):
            with self.assertRaisesRegex(CodexBridgeError, 'IPC open failed: PermissionError'):
                bridge.connect()

    def test_desktop_snapshot_accepts_current_and_previous_event_versions(self):
        bridge = CodexBridge(session_ids={SID})
        bridge.followed[SID] = 'owner'
        for version in (11, 13):
            message = {'type': 'broadcast', 'method': 'thread-stream-state-changed',
                       'version': version, 'sourceClientId': 'owner',
                       'params': {'hostId': 'local', 'conversationId': SID,
                                  'change': {'type': 'snapshot', 'conversationState': state()}}}
            bridge._handle(message)
            self.assertEqual(bridge.snapshots.pop(SID)['id'], SID)
        bridge._handle({**message, 'version': 12})
        self.assertNotIn(SID, bridge.snapshots)

    def test_pending_record_is_created_before_ipc_dispatch(self):
        bridge = CodexBridge(session_ids={SID})
        bridge.followed[SID] = 'owner'
        sequence = []
        with patch.object(bridge, 'snapshot_state', return_value=state()), \
             patch.object(bridge, 'request', side_effect=lambda *args, **kwargs: sequence.append('ipc') or {'result': {}}):
            bridge.start_turn(SID, '继续', 'event-id', on_dispatch=lambda before: sequence.append('saved'))
        self.assertEqual(sequence, ['saved', 'ipc'])

    def test_desktop_turn_uses_native_text_input_shape(self):
        bridge = CodexBridge(session_ids={SID})
        bridge.followed[SID] = 'owner'
        with patch.object(bridge, 'snapshot_state', return_value=state()), \
             patch.object(bridge, 'request', return_value={'result': {}}) as request:
            bridge.start_turn(SID, '只读测试', 'event-id')
        turn_start = request.call_args.args[1]['turnStart']
        self.assertEqual(turn_start['request']['input'], [
            {'type': 'text', 'text': '只读测试', 'text_elements': []}])

    def test_desktop_turn_includes_local_image(self):
        bridge = CodexBridge(session_ids={SID})
        bridge.followed[SID] = 'owner'
        image = str(self.root / 'screenshot.jpg')
        with patch.object(bridge, 'snapshot_state', return_value=state()), \
             patch.object(bridge, 'request', return_value={'result': {}}) as request:
            bridge.start_turn(SID, '请查看截图', 'event-id', images=[image])
        self.assertEqual(request.call_args.args[1]['turnStart']['request']['input'], [
            {'type': 'text', 'text': '请查看截图', 'text_elements': []},
            {'type': 'localImage', 'path': image}])

    def test_feishu_image_download_is_bounded_and_saved_privately(self):
        content = b'\x89PNG\r\n\x1a\n' + b'image-data'
        with patch.object(ctl, 'STATED', str(self.root)), \
             patch.object(ctl, 'token', return_value='test'), \
             patch.object(ctl.urllib.request, 'urlopen', return_value=io.BytesIO(content)) as fetch:
            path = Path(ctl.download_desktop_image('om_test', 'img_test'))
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(path.suffix, '.png')
        self.assertEqual(path.parent, self.root / 'inbound-images')
        self.assertIn('/im/v1/messages/om_test/resources/img_test?type=image',
                      fetch.call_args.args[0].full_url)
        with patch.object(ctl, 'STATED', str(self.root)), \
             patch.object(ctl, 'token', return_value='test'), \
             patch.object(ctl.urllib.request, 'urlopen', return_value=io.BytesIO(b'not an image')):
            with self.assertRaisesRegex(ValueError, 'unsupported Feishu image type'):
                ctl.download_desktop_image('om_test', 'img_test')

    def test_pure_feishu_image_enters_command_queue(self):
        message = SimpleNamespace(chat_id='chat', chat_type='group', parent_id='',
                                  thread_id='', root_id='', message_id='om_test',
                                  message_type='image', content='{"image_key":"img_test"}')
        event = SimpleNamespace(sender=SimpleNamespace(sender_id=SimpleNamespace(open_id='user')),
                                message=message)
        with patch.object(ctl, 'enqueue_inbound', return_value='queued') as enqueue, \
             patch.object(ctl, 'log'):
            ctl.on_message(SimpleNamespace(event=event))
        self.assertEqual(enqueue.call_args.kwargs['image_key'], 'img_test')
        self.assertEqual(enqueue.call_args.kwargs['event_id'] if 'event_id' in enqueue.call_args.kwargs
                         else enqueue.call_args.args[2], 'om_test')

    def test_new_turn_receipt_and_result_are_bound_to_same_user(self):
        bound = {'sid': SID, 'title': '示例学习助手升级', 'cwd': '/project',
                 'root_mid': 'root', 'thread_id': 'thread', 'chat_id': 'chat', 'sender': 'user'}
        bridge = Mock()
        bridge.start_turn.side_effect = lambda sid, text, client_id, on_dispatch: on_dispatch('old')
        bridge.snapshot_state.return_value = state('new', text='原任务的新回复')
        with patch.object(ctl, 'STATED', str(self.root)), \
             patch.object(ctl, 'app', return_value={'chat_id': 'chat'}), \
             patch.object(ctl, 'desktop_allowed', return_value=True), \
             patch.object(ctl, 'CodexBridge', return_value=bridge), \
             patch.object(ctl, 'reply') as reply, patch.object(ctl, 'audit'), \
             patch.object(ctl, 'api', return_value={'code': 0}) as api:
            ctl.desktop_bindings().put('chat:user', bound)
            ctl.desktop_send('user', 'chat', '继续升级', bound, 'event-id')
            self.assertEqual(bridge.start_turn.call_args.args, (SID, '继续升级', 'event-id'))
            self.assertIn('已送达', reply.call_args.args[1])
            self.assertIsNotNone(ctl.desktop_binding('user', 'chat')['pending'])
            ctl.poll_desktop_bindings()
            self.assertEqual(api.call_args.args[1], '/im/v1/messages?receive_id_type=chat_id')
            self.assertIn('原任务的新回复', api.call_args.args[2]['content'])
            self.assertIsNone(ctl.desktop_binding('user', 'chat').get('pending'))
            self.assertIsNone(ctl.desktop_binding('other', 'chat'))

    def test_bound_image_is_downloaded_and_sent_to_original_thread(self):
        bound = {'sid': SID, 'title': '示例学习助手升级', 'cwd': '/project',
                 'chat_id': 'chat', 'root_mid': 'root', 'sender': 'user'}
        bridge = Mock()
        bridge.start_turn.side_effect = lambda sid, text, client_id, on_dispatch, images: on_dispatch('old')
        image = str(self.root / 'inbound-images' / 'test.jpg')
        with patch.object(ctl, 'STATED', str(self.root)), \
             patch.object(ctl, 'app', return_value={'chat_id': 'chat'}), \
             patch.object(ctl, 'desktop_allowed', return_value=True), \
             patch.object(ctl, 'download_desktop_image', return_value=image) as download, \
             patch.object(ctl, 'CodexBridge', return_value=bridge), \
             patch.object(ctl, 'reply') as reply, patch.object(ctl, 'audit'):
            ctl.desktop_bindings().put('chat:user', bound)
            ctl.desktop_send('user', 'chat', '请查看这张截图。', bound, 'event-id',
                             image=('om_test', 'img_test'))
            download.assert_called_once_with('om_test', 'img_test')
            self.assertEqual(bridge.start_turn.call_args.kwargs['images'], [image])
            self.assertEqual(ctl.desktop_binding('user', 'chat')['pending']['client_id'], 'event-id')
            self.assertIn('图片已送达', reply.call_args.args[1])

    def test_result_waits_for_new_completed_turn_and_retries_delivery(self):
        bound = {'sid': SID, 'title': '任务', 'cwd': '/project', 'root_mid': 'root',
                 'chat_id': 'chat', 'pending': {'before': 'old', 'client_id': 'event-id',
                                              'in_thread': True, 'sent_at': time.time()}}
        bridge = Mock()
        with patch.object(ctl, 'STATED', str(self.root)), \
             patch.object(ctl, 'app', return_value={'chat_id': 'chat'}), \
             patch.object(ctl, 'desktop_allowed', return_value=True), \
             patch.object(ctl, 'CodexBridge', return_value=bridge), \
             patch.object(ctl, 'api', side_effect=[{'code': 1}, {'code': 0}]) as api:
            ctl.desktop_bindings().put('chat:user', bound)
            bridge.snapshot_state.return_value = state('old')
            ctl.poll_desktop_bindings()
            api.assert_not_called()
            bridge.snapshot_state.return_value = state('new', status='inProgress', runtime='active')
            ctl.poll_desktop_bindings()
            api.assert_not_called()
            bridge.snapshot_state.return_value = state('new')
            ctl.poll_desktop_bindings()
            self.assertIsNotNone(ctl.desktop_bindings().read()['chat:user']['pending'])
            ctl.poll_desktop_bindings()
            self.assertIsNone(ctl.desktop_bindings().read()['chat:user'].get('pending'))
            self.assertEqual(api.call_args_list[0].args[2]['uuid'], api.call_args_list[1].args[2]['uuid'])
            self.assertEqual(api.call_args.args[1], '/im/v1/messages/root/reply')

    def test_desktop_result_queues_images_in_the_same_thread(self):
        bound = {'sid': SID, 'title': '任务', 'cwd': str(self.root), 'root_mid': 'root',
                 'chat_id': 'chat', 'pending': {'before': 'old', 'client_id': 'event-id',
                                              'in_thread': True, 'sent_at': time.time()}}
        bridge = Mock()
        bridge.snapshot_state.return_value = state('new', text='成图 ![海报](poster.png)')
        with patch.object(ctl, 'STATED', str(self.root)), \
             patch.object(ctl, 'app', return_value={'chat_id': 'chat'}), \
             patch.object(ctl, 'desktop_allowed', return_value=True), \
             patch.object(ctl, 'CodexBridge', return_value=bridge), \
             patch.object(ctl, 'api', return_value={'code': 0}), \
             patch.object(sync, 'queue_local_images', return_value=1) as queue:
            ctl.desktop_bindings().put('chat:user', bound)
            ctl.poll_desktop_bindings()
            queue.assert_called_once_with(('desktop_image', SID, 'new'),
                                          '成图 ![海报](poster.png)', str(self.root), 'root')
            self.assertIsNone(ctl.desktop_bindings().read()['chat:user'].get('pending'))

    def test_latest_turn_extracts_only_final_answer(self):
        snapshot = state('new')
        items = snapshot['turnHistory']['history']['entitiesByKey']['turn']['items']
        items.insert(0, {'type': 'agentMessage', 'phase': 'commentary', 'text': 'private progress'})
        self.assertEqual(latest_turn(snapshot), {'id': 'new', 'status': 'completed',
                                                 'client_id': 'event-id', 'text': 'done'})
        self.assertEqual(turn_by_client(snapshot, 'other')['id'], '')

    def test_dispatch_uncertainty_keeps_pending_for_reconciliation(self):
        bound = {'sid': SID, 'title': '任务', 'cwd': '/project', 'chat_id': 'chat'}
        bridge = Mock()
        def timeout_after_dispatch(sid, text, client_id, on_dispatch):
            on_dispatch('old')
            raise CodexBridgeError('Codex IPC request timed out or disconnected')
        bridge.start_turn.side_effect = timeout_after_dispatch
        with patch.object(ctl, 'STATED', str(self.root)), \
             patch.object(ctl, 'app', return_value={'chat_id': 'chat'}), \
             patch.object(ctl, 'desktop_allowed', return_value=True), \
             patch.object(ctl, 'CodexBridge', return_value=bridge), \
             patch.object(ctl, 'reply') as reply:
            ctl.desktop_bindings().put('chat:user', bound)
            ctl.desktop_send('user', 'chat', '继续', bound, 'event-id')
            self.assertIn('请勿重复发送', reply.call_args.args[1])
            self.assertEqual(ctl.desktop_binding('user', 'chat')['pending']['client_id'], 'event-id')
            ctl.desktop_send('user', 'chat', '继续', ctl.desktop_binding('user', 'chat'), 'other-id')
            self.assertEqual(bridge.start_turn.call_count, 1)

    def test_worker_routes_plain_chat_and_thread_to_bound_desktop(self):
        class Done(BaseException):
            pass

        class TwoMessages:
            def __init__(self):
                self.items = [('user', 'chat', '继续升级', '', ''),
                              ('user', 'chat', '继续整理真题', '', 'thread')]

            def get(self):
                if self.items:
                    return self.items.pop(0)
                raise Done()

        bound = {'sid': SID, 'title': '示例学习助手升级', 'thread_id': 'thread',
                 'root_mid': 'root', 'chat_id': 'chat', 'sender': 'user'}
        with patch.object(ctl, 'Q', TwoMessages()), \
             patch.object(ctl, 'conf', return_value={**ctl.DEF, 'allow_ou': 'user'}), \
             patch.object(ctl, 'rate_ok', return_value=True), \
             patch.object(ctl, 'wiz_get', return_value=None), \
             patch.object(ctl, 'desktop_binding', return_value=bound), \
             patch.object(ctl, 'desktop_send') as send, \
             patch.object(ctl, 'jobs_load', return_value={}), \
             patch.object(ctl, 'log'):
            with self.assertRaises(Done):
                ctl.worker()
            self.assertEqual([call.args[2] for call in send.call_args_list],
                             ['继续升级', '继续整理真题'])

    def test_worker_routes_image_only_to_current_desktop_binding(self):
        class Done(BaseException):
            pass

        class OneMessage:
            used = False

            def get(self):
                if self.used:
                    raise Done()
                self.used = True
                return ('user', 'chat', '', '', '', {'image_key': 'img_test',
                        'image_message_id': 'om_test', 'command_id': '', 'reply_route': {}})

        bound = {'sid': SID, 'title': '示例学习助手升级', 'chat_id': 'chat'}
        with patch.object(ctl, 'Q', OneMessage()), \
             patch.object(ctl, 'conf', return_value={**ctl.DEF, 'allow_ou': 'user'}), \
             patch.object(ctl, 'rate_ok', return_value=True), \
             patch.object(ctl, 'desktop_binding', return_value=bound), \
             patch.object(ctl, 'desktop_send') as send, patch.object(ctl, 'log'):
            with self.assertRaises(Done):
                ctl.worker()
            self.assertEqual(send.call_args.kwargs['image'], ('om_test', 'img_test'))
            self.assertEqual(send.call_args.args[2], '请查看这张截图。')

    def test_image_in_unrelated_topic_does_not_enter_bound_thread(self):
        class Done(BaseException):
            pass

        class OneMessage:
            used = False

            def get(self):
                if self.used:
                    raise Done()
                self.used = True
                return ('user', 'chat', '', '', 'other-thread', {'image_key': 'img_test',
                        'image_message_id': 'om_test', 'command_id': '',
                        'reply_route': {'chat_id': 'chat', 'root_id': 'other-root'}})

        with patch.object(ctl, 'Q', OneMessage()), \
             patch.object(ctl, 'conf', return_value={**ctl.DEF, 'allow_ou': 'user'}), \
             patch.object(ctl, 'rate_ok', return_value=True), \
             patch.object(ctl, 'desktop_thread_binding', return_value=None), \
             patch.object(ctl, 'desktop_send') as send, \
             patch.object(ctl, 'reply') as reply, patch.object(ctl, 'log'):
            with self.assertRaises(Done):
                ctl.worker()
            send.assert_not_called()
            self.assertIn('控制群主聊天', reply.call_args.args[1])

    def test_leave_removes_only_the_current_chat_binding(self):
        with patch.object(ctl, 'STATED', str(self.root)), patch.object(ctl, 'reply') as reply:
            ctl.desktop_bindings().put('chat:user', {'sid': SID})
            ctl.desktop_bindings().put('other:user', {'sid': 'other'})
            ctl.cmd_leave('user', 'chat', [])
            self.assertIsNone(ctl.desktop_binding('user', 'chat'))
            self.assertEqual(ctl.desktop_binding('user', 'other')['sid'], 'other')
            self.assertIn('已退出', reply.call_args.args[1])

    def test_leave_waits_for_pending_reply_unless_explicitly_forced(self):
        with patch.object(ctl, 'STATED', str(self.root)), patch.object(ctl, 'reply') as reply:
            ctl.desktop_bindings().put('chat:user', {'sid': SID, 'pending': {'client_id': 'event'}})
            ctl.cmd_leave('user', 'chat', [])
            self.assertIsNotNone(ctl.desktop_binding('user', 'chat'))
            self.assertIn('#leave force', reply.call_args.args[1])
            ctl.cmd_leave('user', 'chat', ['force'])
            self.assertIsNone(ctl.desktop_binding('user', 'chat'))

    def test_status_and_sessions_show_only_current_scope(self):
        config = {**ctl.DEF, 'attach_sessions': SID,
                  'desktop_aliases': json.dumps({'学习': SID}, ensure_ascii=False)}
        with patch.object(ctl, 'STATED', str(self.root)), \
             patch.object(ctl, 'conf', return_value=config), \
             patch.object(ctl, 'app', return_value={'chat_id': 'chat'}), \
             patch.object(ctl, 'catalog_matches', return_value=[
                 {'id': SID, 'title': '示例学习助手升级', 'cwd': '/project'}]) as catalog, \
             patch.object(ctl, 'reply') as reply:
            ctl.cmd_status('user', 'chat', [])
            self.assertIn('未控制', reply.call_args.args[1])
            ctl.desktop_bindings().put('chat:user', {'sid': SID, 'title': '示例学习助手升级'})
            ctl.cmd_status('user', 'chat', [])
            self.assertIn('示例学习助手升级', reply.call_args.args[1])
            ctl.cmd_sessions('user', 'chat', [])
            self.assertIn('进入学习', reply.call_args.args[1])
            catalog.assert_called_once_with(SID)
            ctl.cmd_sessions('user', 'other', [])
            self.assertIn('控制群', reply.call_args.args[1])

    def test_idle_unconfirmed_turn_reports_uncertainty_once(self):
        bound = {'sid': SID, 'title': '任务', 'cwd': '/project', 'root_mid': 'root',
                 'chat_id': 'chat', 'pending': {'before': 'old', 'client_id': 'missing',
                                               'in_thread': False, 'sent_at': 1}}
        bridge = Mock()
        bridge.snapshot_state.return_value = state('other', client_id='other')
        with patch.object(ctl, 'STATED', str(self.root)), \
             patch.object(ctl, 'app', return_value={'chat_id': 'chat'}), \
             patch.object(ctl, 'desktop_allowed', return_value=True), \
             patch.object(ctl, 'CodexBridge', return_value=bridge), \
             patch.object(ctl, 'api', return_value={'code': 0}) as api:
            ctl.desktop_bindings().put('chat:user', bound)
            ctl.poll_desktop_bindings()
            self.assertIn('暂未找到', api.call_args.args[2]['content'])
            self.assertIsNone(ctl.desktop_bindings().read()['chat:user'].get('pending'))

    def test_revoked_scope_pauses_result_delivery(self):
        bound = {'sid': SID, 'title': '任务', 'cwd': '/project', 'chat_id': 'chat',
                 'pending': {'before': 'old', 'client_id': 'event-id', 'sent_at': 1}}
        with patch.object(ctl, 'STATED', str(self.root)), \
             patch.object(ctl, 'app', return_value={'chat_id': 'other'}), \
             patch.object(ctl, 'CodexBridge') as bridge, \
             patch.object(ctl, 'api') as api:
            ctl.desktop_bindings().put('chat:user', bound)
            ctl.poll_desktop_bindings()
            bridge.assert_not_called()
            api.assert_not_called()
            self.assertIsNotNone(ctl.desktop_binding('user', 'chat')['pending'])


if __name__ == '__main__':
    unittest.main()
