"""Offline regressions for existing DSH desktop sessions; no real Host or credentials."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import tower_ctl as ctl
import tower_harness as harness
from tower_dsh_bridge import DshBridgeError
from tower_dsh_control import DshControlBridge, catalog_matches


SID = 'session-11111111-2222-4333-8444-555555555555'


def event(seq, kind, data):
    return {'type': 'event', 'event': {'type': kind, 'seq': seq, 'data': data}}


class DshControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bridge = DshControlBridge('http://127.0.0.1:4567/?token=fake', session_ids={SID})
        self.addCleanup(self.bridge.close)
        ctl._REPLY_CONTEXT.route = {}

    def test_missing_host_config_has_no_secret_in_error(self):
        with self.assertRaisesRegex(DshBridgeError, 'configuration unavailable'):
            DshControlBridge.from_config(self.root / 'missing.json', session_ids={SID})

    def test_prompt_is_scoped_and_persists_receipt_before_sending(self):
        order = []
        def rpc(endpoint, args):
            order.append(endpoint)
            if endpoint == 'session/state':
                return {'cursor': 12, 'active': True, 'running': False, 'queue': []}
            self.assertEqual(args['request']['sessionId'], SID)
            self.assertEqual(args['request']['requestId'], 'client-id')
            self.assertEqual(args['request']['content'], [{'type': 'text', 'text': 'hello'}])
            self.assertEqual(args['request']['mode'], 'queue')
            return {'accepted': True}
        with patch.object(self.bridge, 'rpc', side_effect=rpc):
            self.bridge.start_turn(SID, 'hello', 'client-id', on_dispatch=lambda before: order.append(('saved', before)))
        self.assertEqual(order, ['session/state', ('saved', 12), 'session/prompt'])
        with self.assertRaisesRegex(DshBridgeError, 'scope'):
            self.bridge.start_turn('other', 'hello')

    def test_running_queue_and_pending_input_reject_send(self):
        base = {'cursor': 12, 'active': True, 'running': False, 'queue': [], 'pendingInput': []}
        for change in ({'running': True}, {'queue': [{}]}, {'pendingInput': ['approval']}):
            with self.subTest(change=change), patch.object(self.bridge, 'rpc', return_value={**base, **change}) as rpc:
                with self.assertRaisesRegex(DshBridgeError, 'busy'):
                    self.bridge.start_turn(SID, 'hello')
                self.assertEqual(rpc.call_count, 1)

    def test_current_reply_requires_matching_user_receipt_and_turn_end(self):
        pending = {'before': 12, 'client_id': 'client-id'}
        records = [event(14, 'user/message', {'source': {'rpcId': 'client-id'}}),
                   event(15, 'user/message', {'source': {'kind': 'time-context'}}),
                   event(16, 'assistant/message', {'message': {'content': [{'type': 'text', 'text': 'answer'},
                                                                              {'type': 'reasoning', 'text': 'private'}]}})]
        current = {'cursor': 18, 'active': True, 'running': False, 'queue': []}
        def rpc(endpoint, args):
            if endpoint == 'session/state':
                return current
            self.assertEqual(args['request']['address'], {'kind': 'session', 'sessionId': SID})
            return {'records': records}
        with patch.object(self.bridge, 'rpc', side_effect=rpc):
            _, turn = self.bridge.result(SID, pending)
            self.assertEqual(turn['status'], 'inProgress')
            records.append(event(17, 'turn/end', {}))
            _, turn = self.bridge.result(SID, pending)
            self.assertEqual(turn['status'], 'completed')
            self.assertEqual(turn['text'], 'answer')
            pending['client_id'] = 'unrelated'
            _, turn = self.bridge.result(SID, pending)
            self.assertEqual(turn['id'], '')

    def test_does_not_claim_answer_from_next_desktop_message(self):
        records = [event(14, 'user/message', {'source': {'rpcId': 'client-id'}}),
                   event(15, 'user/message', {'source': {'rpcId': 'other'}}),
                   event(16, 'assistant/message', {'message': {'content': [{'type': 'text', 'text': 'other answer'}]}}),
                   event(17, 'turn/end', {})]
        current = {'cursor': 18, 'active': True, 'running': False, 'queue': []}
        with patch.object(self.bridge, 'rpc', side_effect=lambda ep, args: current if ep == 'session/state' else {'records': records}):
            _, turn = self.bridge.result(SID, {'before': 12, 'client_id': 'client-id'})
            self.assertEqual(turn['text'], '')
            self.assertNotEqual(turn['status'], 'completed')

    def test_rpc_rejects_out_of_scope_direct_calls(self):
        self.bridge.authenticated = True
        for endpoint, args in [('session/state', {'request': {'sessionId': 'other'}}),
                               ('session/prompt', {'request': {'sessionId': 'other'}}),
                               ('session/events', {'request': {'address': {'sessionId': 'other'}}})]:
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(DshBridgeError, 'scope'):
                self.bridge.rpc(endpoint, args)

    def test_catalog_uses_only_explicit_ids_and_verified_header(self):
        file = self.root / '2026-01' / SID / 'session.v4.jsonl.zstd'
        file.parent.mkdir(parents=True)
        file.touch()
        with patch('tower_dsh_control.harness.root', return_value=self.root), \
             patch('tower_dsh_control.harness.header', return_value={'id': SID, 'cwd': str(self.root)}), \
             patch('tower_dsh_control.harness.metadata', return_value=('project', '电脑优化')):
            self.assertEqual(catalog_matches('电脑优化', [SID])[0]['id'], SID)
            self.assertEqual(catalog_matches('电脑优化', []), [])
            self.assertEqual(catalog_matches('电脑', [SID]), [])

    def test_windows_catalog_title_reads_utf8(self):
        cache = self.root / (SID + '.json')
        cache.write_text(json.dumps({'record': {'rows': {'title': {'val': '电脑优化'}}}},
                                    ensure_ascii=False), encoding='utf-8')
        source = self.root / SID / 'session.v4.jsonl.zstd'
        with patch.object(harness, 'header', return_value={'cwd': str(self.root)}), \
             patch.object(harness.os.path, 'expanduser', return_value=str(self.root)):
            self.assertEqual(harness.metadata(str(source))[1], '电脑优化')

    def test_attach_leave_and_codex_isolation(self):
        config = {**ctl.DEF, 'dsh_attach_sessions': SID,
                  'desktop_aliases': json.dumps({'电脑优化': SID}, ensure_ascii=False)}
        bridge = Mock()
        target = {'id': SID, 'title': '电脑优化', 'cwd': str(self.root)}
        with patch.object(ctl, 'STATED', str(self.root)), patch.object(ctl, 'conf', return_value=config), \
             patch.object(ctl, 'app', return_value={'chat_id': 'chat'}), \
             patch.object(ctl, 'dsh_catalog_matches', return_value=[target]), \
             patch.object(ctl, 'desktop_bridge', return_value=bridge), \
             patch.object(ctl, 'create_topic', return_value=('root', 'thread')), \
             patch.object(ctl, 'reply') as reply, patch.object(ctl, 'audit'):
            ctl.cmd_attach('user', 'chat', ['电脑优化'])
            bound = ctl.desktop_binding('user', 'chat')
            self.assertEqual(bound['engine'], 'dsh')
            self.assertEqual(bound['sid'], SID)
            self.assertIn('原 DSH', reply.call_args.args[1])
            ctl.cmd_leave('user', 'chat', [])
            self.assertIsNone(ctl.desktop_binding('user', 'chat'))
            self.assertIn('DSH', reply.call_args.args[1])
            bridge.close.assert_called_once()

    def test_dsh_cannot_use_codex_work_roots_as_implicit_scope(self):
        with patch.object(ctl, 'conf', return_value={**ctl.DEF, 'dsh_attach_sessions': ''}), \
             patch.object(ctl, 'desktop_allowed', return_value=True):
            self.assertFalse(ctl.binding_allowed({'engine': 'dsh', 'sid': SID, 'cwd': '/project'}))

    def test_dsh_credential_url_is_not_sent_in_error_or_state(self):
        config = self.root / 'dsh-host.json'
        config.write_text(json.dumps({'url': 'http://127.0.0.1:4567/?token=fake'}), encoding='utf-8')
        bridge = DshControlBridge.from_config(config, session_ids={SID})
        self.addCleanup(bridge.close)
        with patch.object(bridge.http, 'open', side_effect=OSError('http://127.0.0.1/?token=fake')):
            with self.assertRaises(DshBridgeError) as error:
                bridge.connect()
        self.assertNotIn('fake', str(error.exception))
        self.assertNotIn('token=', str(error.exception))

    def test_dsh_result_is_returned_to_same_binding_then_cleared(self):
        bound = {'engine': 'dsh', 'sid': SID, 'title': '电脑优化', 'cwd': str(self.root), 'chat_id': 'chat',
                 'pending': {'before': 12, 'client_id': 'client-id', 'sent_at': 1, 'in_thread': False}}
        bridge = Mock()
        bridge.result.return_value = ({'threadRuntimeStatus': {'type': 'idle'}},
                                      {'id': 'client-id', 'status': 'completed', 'text': '联调成功'})
        with patch.object(ctl, 'STATED', str(self.root)), \
             patch.object(ctl, 'app', return_value={'chat_id': 'chat'}), \
             patch.object(ctl, 'binding_allowed', return_value=True), \
             patch.object(ctl, 'desktop_bridge', return_value=bridge), \
             patch.object(ctl, 'api', return_value={'code': 0}) as api, \
             patch('tower_sync.queue_local_images', return_value=0):
            ctl.desktop_bindings().put('chat:user', bound)
            ctl.poll_desktop_bindings()
            self.assertIn('联调成功', api.call_args.args[2]['content'])
            self.assertIsNone(ctl.desktop_binding('user', 'chat').get('pending'))


if __name__ == '__main__':
    unittest.main()
