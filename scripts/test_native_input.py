"""Offline tests of request identity, protocol framing, and Feishu scope."""
import json
import queue
import struct
import time
import unittest
from unittest.mock import Mock, patch

import tower_ctl as ctl
from tower_native_input import NativeInput, NativeInputError, parse_answers, question_token
from tower_codex_bridge import CodexBridge, CodexBridgeError, MAX_FRAME, input_projection

Q = [{'id': 'q', 'question': 'choose', 'options': [{'label': 'A'}, {'label': 'B'}]}]


class NativeTests(unittest.TestCase):
    def tearDown(self):
        ctl._REPLY_CONTEXT.route = {}
        ctl._NATIVE_ADVERTISED.clear()

    def test_answers(self):
        self.assertEqual(parse_answers(Q, '2'), [{'id': 'q', 'selected': ['B']}])
        self.assertEqual(parse_answers(Q, 'other')[0]['custom'], 'other')
        for text in ('', '3'):
            with self.assertRaises(NativeInputError):
                parse_answers(Q, text)
        multi = [dict(Q[0], multiSelect=True)]
        self.assertEqual(parse_answers(multi, '1,2')[0]['selected'], ['A', 'B'])
        with self.assertRaises(NativeInputError):
            parse_answers(multi, '1,1')

    def test_complete_multi_question(self):
        questions = Q + [dict(Q[0], id='other')]
        for text in ('1=A', '1=A\n1=B', '1=A\n3=B'):
            with self.assertRaises(NativeInputError):
                parse_answers(questions, text)
        self.assertEqual(len(parse_answers(questions, '1=A\n2=B')), 2)

    def test_token_changes_for_identity_and_question(self):
        req = {'id': 'request', 'questions': Q}
        token = question_token('desktop', 'sid', req)
        for engine, sid, changed in [('codex', 'sid', req), ('desktop', 'other', req),
                ('desktop', 'sid', dict(req, id='new')),
                ('desktop', 'sid', dict(req, questions=[dict(Q[0], question='new')]))]:
            self.assertNotEqual(token, question_token(engine, sid, changed))

    def test_expired_token_never_submits(self):
        native = NativeInput('desktop', 'sid')
        native.bridge = Mock()
        native.bridge.pending.return_value = [{'id': 'new', 'params': {'questions': Q}}]
        self.assertEqual(native.answer('old', 'A')['status'], 'expired')
        native.bridge.answer.assert_not_called()

    def test_codex_native_payload(self):
        native = NativeInput('desktop', 'sid'); native.bridge = Mock()
        native.bridge.pending.return_value = [{'id': 'r', 'params': {'questions': Q}}]
        token = native.pending()[0]['token']
        native.answer(token, '2')
        native.bridge.answer.assert_called_once_with('sid', 'r', {'q': ['B']})

    def test_first_text_only_displays_and_later_text_answers(self):
        native = Mock(); native.__enter__ = Mock(return_value=native); native.__exit__ = Mock()
        native.pending.return_value = [{'id': 'r', 'questions': Q, 'token': 'tok'}]
        native.answer.return_value = {'status': 'answered'}
        with patch.object(ctl, 'native_context', return_value=('sid', 'desktop', 'sid')), \
             patch('tower_native_input.NativeInput', return_value=native), \
             patch.object(ctl, 'reply_card') as card, patch.object(ctl, 'reply') as reply:
            ctl.native_reply('user', 'chat', 'sid', '1')
            native.answer.assert_not_called()
            actions = card.call_args.args[3][1]['actions']
            self.assertEqual(actions[0]['behaviors'][0]['value']['act'], 'native_answer')
            ctl.native_reply('user', 'chat', 'sid', '2')
            native.answer.assert_called_once_with('tok', '2')
            self.assertIn('已确认', reply.call_args.args[1])
            self.assertFalse(ctl._NATIVE_ADVERTISED)

    def test_cross_chat_and_thread_rejected(self):
        with patch.object(ctl, 'find_rollout', return_value=('path', 'sid')), \
             patch.object(ctl, '_st_load', return_value={'sid': 'root'}), \
             patch.object(ctl, 'JsonMap') as store:
            store.return_value.read.return_value = {'watch:sid': {'chat_id': 'chat', 'root_mid': 'root'}}
            with self.assertRaises(NativeInputError): ctl.native_context('other', 'sid')
            ctl._REPLY_CONTEXT.route = {'root_id': 'other-root'}
            with self.assertRaises(NativeInputError): ctl.native_context('chat', 'sid')

    def test_legacy_watch_verifies_chat_from_root_message(self):
        with patch.object(ctl, 'find_rollout', return_value=('path', 'sid')), \
             patch.object(ctl, '_st_load', return_value={'sid': 'root'}), \
             patch.object(ctl, 'source_agent', return_value='desktop'), \
             patch.object(ctl, 'JsonMap') as store, patch.object(ctl, 'api') as api:
            store.return_value.read.return_value = {}
            api.return_value = {'code': 0, 'data': {'items': [{'message_id': 'root', 'chat_id': 'chat'}]}}
            self.assertEqual(ctl.native_context('chat', 'sid')[1], 'desktop')
            api.assert_called_with('GET', '/im/v1/messages/root')
            ctl._REPLY_CONTEXT.route = {}
            with self.assertRaises(NativeInputError): ctl.native_context('other-chat', 'sid')
            api.return_value = {'code': 1}
            with self.assertRaises(NativeInputError): ctl.native_context('chat', 'sid')

    def test_ui_authorization_and_rate(self):
        with patch.object(ctl, 'conf', return_value={'allow_ou': 'user'}), \
             patch.object(ctl, 'audit'), patch.object(ctl, 'rate_ok', return_value=False), \
             patch.object(ctl, 'reply'), patch.object(ctl, 'native_reply') as send:
            ctl.handle_ui('intruder', 'chat', {'act': 'native_answer'})
            ctl.handle_ui('user', 'chat', {'act': 'native_answer'})
            send.assert_not_called()

    def test_worker_preserves_multiline_explicit_answer(self):
        class StopQueue:
            def __init__(self): self.sent = False
            def get(self):
                if self.sent: raise StopIteration
                self.sent = True
                return ('user', 'chat', '#answer sid tok 1=A\n2=B', '', '')
            def qsize(self): return 0
        with patch.object(ctl, 'Q', StopQueue()), \
             patch.object(ctl, 'conf', return_value={'allow_ou': 'user'}), \
             patch.object(ctl, 'rate_ok', return_value=True), patch.object(ctl, 'wiz_get', return_value=None), \
             patch.object(ctl, 'native_reply') as send, patch.object(ctl, 'log'):
            with self.assertRaises(StopIteration): ctl.worker()
            send.assert_called_once_with('user', 'chat', 'sid', '1=A\n2=B', 'tok')
            self.assertFalse(ctl._REPLY_CONTEXT.route)

    def test_worker_recovers_watch_root_from_thread_id(self):
        class StopQueue:
            def __init__(self): self.sent = False
            def get(self):
                if self.sent: raise StopIteration
                self.sent = True
                return ('user', 'chat', '#questions', '', 'topic-id')
            def qsize(self): return 0
        with patch.object(ctl, 'Q', StopQueue()), \
             patch.object(ctl, 'conf', return_value={'allow_ou': 'user'}), \
             patch.object(ctl, 'JsonMap') as store, \
             patch.object(ctl, 'cmid_lookup', return_value={'kind': 'sess', 'sid8': 'sid'}), \
             patch.object(ctl, 'rate_ok', return_value=True), patch.object(ctl, 'wiz_get', return_value=None), \
             patch.object(ctl, 'thread_to_jid', return_value=''), \
             patch.object(ctl, 'native_reply') as send, patch.object(ctl, 'log'):
            store.return_value.read.return_value = {'watch:sid': {'chat_id': 'chat', 'root_mid': 'root', 'thread_id': 'topic-id'}}
            with self.assertRaises(StopIteration): ctl.worker()
            send.assert_called_once_with('user', 'chat', 'sid')

    def test_context_routes_answer_into_registered_topic(self):
        with patch.object(ctl, 'find_rollout', return_value=('path', 'sid')), \
             patch.object(ctl, '_st_load', return_value={'sid': 'root'}), \
             patch.object(ctl, 'source_agent', return_value='desktop'), \
             patch.object(ctl, 'JsonMap') as store:
            store.return_value.read.return_value = {'watch:sid': {'chat_id': 'chat', 'root_mid': 'root'}}
            self.assertEqual(ctl.native_context('chat', 'sid'), ('sid', 'desktop', 'sid'))
            self.assertEqual(ctl._REPLY_CONTEXT.route['message_id'], 'root')

    def test_transport_errors_do_not_leak_credentials(self):
        with patch.object(ctl, 'native_context', side_effect=RuntimeError('secret-token')), \
             patch.object(ctl, 'reply') as reply:
            ctl.native_reply('user', 'chat', 'sid')
            self.assertNotIn('secret-token', str(reply.call_args))


class CodexTests(unittest.TestCase):
    def bridge(self):
        return CodexBridge(session_ids={'sid'})

    def test_fragmented_frames_and_bounds(self):
        bridge = self.bridge(); bridge.socket = Mock()
        raw = json.dumps({'type': 'response'}).encode(); frame = struct.pack('<I', len(raw)) + raw
        bridge.socket.recv.side_effect = [frame[:2], frame[2:7], frame[7:]]
        self.assertEqual(bridge._receive(time.monotonic() + 1), {'type': 'response'})
        bridge.buffer = bytearray(struct.pack('<I', MAX_FRAME + 1))
        with self.assertRaises(CodexBridgeError): bridge._receive(time.monotonic() + 1)

    def test_snapshot_rejects_wrong_owner_host_version_scope(self):
        bridge = self.bridge(); bridge.followed['sid'] = 'owner'
        msg = {'type': 'broadcast', 'method': 'thread-stream-state-changed', 'version': 11,
               'sourceClientId': 'owner', 'params': {'hostId': 'local', 'conversationId': 'sid',
                'change': {'type': 'snapshot', 'conversationState': {'id': 'sid', 'requests': []}}}}
        for changed in [dict(msg, sourceClientId='other'), dict(msg, version=10),
                dict(msg, params=dict(msg['params'], hostId='remote')),
                dict(msg, params=dict(msg['params'], conversationId='other'))]:
            bridge._handle(changed)
            self.assertFalse(bridge.snapshots)
        bridge._handle(msg); self.assertIn('sid', bridge.snapshots)

    def test_ok_noop_is_unconfirmed_and_desktop_race_is_expired(self):
        for settled, expected in [([], 'unconfirmed'),
                ([{'requestId': 'r', 'completed': True, 'answers': {'q': ['B']}}], 'expired'),
                ([{'requestId': 'r', 'completed': True, 'answers': {'q': ['A']}}], 'answered')]:
            bridge = self.bridge(); bridge.followed['sid'] = 'owner'
            with patch.object(bridge, 'pending', return_value=[{'id': 'r', 'params': {'questions': Q}}]), \
                 patch.object(bridge, 'request', return_value={'result': {'ok': True}}) as request, \
                 patch.object(bridge, 'snapshot', return_value={'settled': settled}):
                self.assertEqual(bridge.answer('sid', 'r', {'q': ['A']})['status'], expected)
                self.assertEqual(request.call_args.args[0], 'thread-follower-submit-user-input')

    def test_expired_request_scope_and_whitelist(self):
        bridge = self.bridge()
        with patch.object(bridge, 'pending', return_value=[]), patch.object(bridge, 'request') as request:
            self.assertEqual(bridge.answer('sid', 'r', {'q': ['A']})['status'], 'expired')
            request.assert_not_called()
        with self.assertRaises(CodexBridgeError): bridge.snapshot('other')
        with self.assertRaises(CodexBridgeError): bridge.request('thread/resume', {})
        with self.assertRaises(CodexBridgeError):
            bridge.request('thread-owner-discovery', {'hostId': 'local', 'conversationId': 'other'})
        with self.assertRaises(CodexBridgeError):
            bridge.request('thread-follower-submit-user-input', {'conversationId': 'sid'}, owner='other')

    def test_snapshot_requests_fresh_state_and_unfollows_on_close(self):
        bridge = self.bridge(); bridge.socket = Mock(); bridge.snapshots['sid'] = {'pending': ['stale']}
        def receive(deadline):
            if not bridge.socket.sendall.called: raise TimeoutError
            return {'type': 'broadcast', 'method': 'thread-stream-state-changed', 'version': 11,
                    'sourceClientId': 'owner', 'params': {'hostId': 'local', 'conversationId': 'sid',
                     'change': {'type': 'snapshot', 'conversationState': {'id': 'sid', 'requests': []}}}}
        with patch.object(bridge, 'request', return_value={'handledByClientId': 'owner'}), \
             patch.object(bridge, '_receive', side_effect=receive):
            self.assertEqual(bridge.snapshot('sid')['pending'], [])
        sock = bridge.socket; bridge.close()
        frame = sock.sendall.call_args.args[0]
        self.assertFalse(json.loads(frame[4:])['params']['following'])
        sock.close.assert_called_once()

    def test_projection_drops_transcripts_and_approvals(self):
        projected = input_projection({'requests': [{'method': 'approval'}, {'method': 'item/tool/requestUserInput'}],
            'turns': [{'body': 'private', 'items': [{'type': 'userInputResponse', 'requestId': 'r',
                'answers': {'q': ['A']}, 'completed': True}]}]})
        self.assertNotIn('private', json.dumps(projected)); self.assertEqual(len(projected['pending']), 1)
        self.assertEqual(projected['settled'][0]['requestId'], 'r')


if __name__ == '__main__': unittest.main()
