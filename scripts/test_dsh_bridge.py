#!/usr/bin/env python3
"""Offline protocol regression: in-memory HTTP envelopes and multiplexed frames."""
import json
import io
import http.cookiejar
from copy import deepcopy
from urllib.error import HTTPError
from urllib.parse import urlsplit
from unittest.mock import patch
import unittest
from collections import deque
from tower_dsh_bridge import DshBridge, DshBridgeError

Q = {'callId': 'call-1', 'state': 'open', 'questions': [{'id': 'q1', 'question': 'Choose'}]}
A = [{'id': 'q1', 'selected': [], 'custom': 'hello'}]

class MockOpener:
    """In-memory HTTP transport; no sockets, tokens or credentials leave tests."""
    def __init__(self, bridge):
        self.bridge = bridge
        self.requests = []
        self.state = {'active': [deepcopy(Q)], 'settled': []}
        self.reply = deepcopy(A)
        self.settle = True
        self.accepted = True
        self.bad_id = False
        self.override = None
        self.reject = False

    def open(self, request, timeout=None):
        if isinstance(request, str):
            cookie = http.cookiejar.Cookie(0, 'dsh-auth-test', 'fake', None, False,
                    '127.0.0.1', False, False, '/', True, False, None, True, None, None, {})
            self.bridge.cookies.set_cookie(cookie)
            raise HTTPError(request, 303, 'Redirect', {'Location': './'}, io.BytesIO())
        self.bridge.cookies.add_cookie_header(request)
        body = json.loads(request.data)
        self.requests.append((urlsplit(request.full_url).path, body, request.get_header('Cookie')))
        endpoint = body['method']
        if endpoint == 'session/projections':
            value = {'values': {'userQuestions': {'questions': self.state}}}
        elif endpoint == '$events/result':
            if self.settle and body['payload']['args']['outcome']['kind'] == 'result':
                self.state['active'] = []
                self.state['settled'] = [{'callId': 'call-1', 'answers': self.reply}]
            value = None
        else:
            value = self.accepted
        envelope = {'type': 'server-response', 'rpcId': body['rpcId'],
                    'result': {'ok': True, 'value': value}}
        if self.bad_id:
            envelope['rpcId'] = 'wrong'
        if self.reject:
            envelope['result'] = {'ok': False, 'error': {'code': 'BAD_ANSWER'}}
        if self.override is not None:
            envelope = self.override
        return io.BytesIO(json.dumps(envelope).encode())

class Mux:
    def __init__(self, frames): self.frames = deque(frames)
    def recv(self, timeout=None):
        if not self.frames: raise TimeoutError
        return json.dumps({'type': 'item', 'streamId': 'questions', 'value': self.frames.popleft()})
    def close(self): pass

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = DshBridge('http://127.0.0.1:4567/?token=fake', session_ids={'session-1'})
        self.server = MockOpener(self.bridge)
        self.bridge.http = self.server
        self.bridge.authenticate()
        self.bridge.ws = Mux([
            {'type': 'ready', 'clientId': 'client-1'},
            {'type': 'waterfall', 'event': 'user-questions/request', 'eventId': 'event-1',
             'agentId': 'session-1', 'request': {'wait': {'callId': 'call-1'}, 'questions': Q['questions']}}
        ])
        self.bridge.poll()
    def tearDown(self):
        self.bridge.close()
    def test_mock_auth_rpc_and_confirmed_answer(self):
        self.assertEqual(self.bridge.answer('session-1', 'call-1', A), {'status': 'answered'})
        request = next(r for r in self.server.requests if r[0] == '/api/$events/result')
        self.assertIn('dsh-auth-test=fake', request[2])
        self.assertEqual(request[1]['payload']['args']['outcome']['value'], {'answers': A})
        self.assertNotIn('fake', self.bridge._url)
    def test_stale_delivery_200_is_not_success(self):
        self.server.settle = False
        self.assertEqual(self.bridge.answer('session-1', 'call-1', A, confirm_timeout=0),
                         {'status': 'unconfirmed'})
    def test_desktop_won_race(self):
        self.server.reply = [{'id': 'q1', 'selected': ['desktop']}]
        self.assertEqual(self.bridge.answer('session-1', 'call-1', A), {'status': 'expired'})
    def test_continued_is_queued(self):
        self.server.state['active'][0]['state'] = 'continued'
        self.assertEqual(self.bridge.answer('session-1', 'call-1', A), {'status': 'queued'})
        self.assertTrue(any(r[0] == '/api/userQuestions/answer' for r in self.server.requests))
    def test_reject_incomplete_batch_before_write(self):
        with self.assertRaises(DshBridgeError): self.bridge.answer('session-1', 'call-1', [])
        self.assertFalse(any(r[0] == '/api/$events/result' for r in self.server.requests))
    def test_expired_never_writes(self):
        self.server.state['active'] = []
        self.assertEqual(self.bridge.answer('session-1', 'call-1', A), {'status': 'expired'})
        self.assertEqual(len(self.server.requests), 1)
    def test_mux_cancellation(self):
        self.bridge.poll()
        self.bridge.ws.frames.append({'type': 'cancel', 'eventId': 'event-1'})
        self.bridge.poll()
        self.assertEqual(self.bridge.events, {})
    def test_unmanaged_question_is_delegated(self):
        self.bridge.ws.frames.clear()
        self.bridge.ws.frames.append({'type': 'waterfall', 'event': 'user-questions/request',
                                     'eventId': 'foreign', 'agentId': 'other', 'request': {}})
        self.bridge.poll()
        self.assertNotIn('foreign', self.bridge.events)
        self.assertEqual(self.server.requests[-1][1]['payload']['args']['outcome'], {'kind': 'next'})
    def test_rpc_correlation(self):
        self.server.bad_id = True
        with self.assertRaisesRegex(DshBridgeError, 'correlation'): self.bridge.pending('session-1')
    def test_pending_cannot_expand_managed_scope(self):
        with self.assertRaisesRegex(DshBridgeError, 'scope'):
            self.bridge.pending('other')
        with self.assertRaisesRegex(DshBridgeError, 'scope'):
            self.bridge.answer('other', 'call-1', A)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.bridge.session_ids, {'session-1'})

    def test_legacy_question_without_projection_is_unsupported(self):
        self.server.state['active'] = []
        self.bridge.ws.frames.clear()
        self.bridge.ws.frames.append({'type': 'waterfall', 'event': 'user-questions/request',
            'eventId': 'legacy', 'agentId': 'session-1', 'request': {'questions': Q['questions']}})
        self.assertEqual(self.bridge.answer('session-1', 'call-1', A), {'status': 'unsupported'})
        self.assertFalse(any(r[0] == '/api/$events/result' for r in self.server.requests))

    def test_answer_schema_rejected_before_transport(self):
        for answers in [None, {}, [None], [{'id': 'q1'}],
                [{'id': 'q1', 'selected': 'x'}], [{'id': 'q1', 'selected': [1]}],
                [{'id': 'q1', 'selected': [], 'custom': None}],
                [{'id': 'q1', 'selected': [], 'extra': True}], A + A]:
            with self.subTest(answers=answers), self.assertRaises(DshBridgeError):
                self.bridge.answer('session-1', 'call-1', answers)
        self.assertEqual(self.server.requests, [])

    def test_rpc_protocol_errors(self):
        for invalid in [[], {'type': 'server-response', 'rpcId': 'wrong', 'result': []}]:
            self.server.override = invalid
            with self.assertRaises(DshBridgeError):
                self.bridge.pending('session-1')
        self.server.override = None
        self.server.reject = True
        with self.assertRaisesRegex(DshBridgeError, 'BAD_ANSWER'):
            self.bridge.pending('session-1')

    def test_projection_unknown_state_rejected(self):
        self.server.state['active'][0]['state'] = 'pending'
        with self.assertRaisesRegex(DshBridgeError, 'projection'):
            self.bridge.pending('session-1')

    def test_invalid_continued_acknowledgement(self):
        self.server.state['active'][0]['state'] = 'continued'
        self.server.accepted = {'ok': True}
        with self.assertRaisesRegex(DshBridgeError, 'acknowledgement'):
            self.bridge.answer('session-1', 'call-1', A)

    def test_ended_stream_discards_stale_events(self):
        self.bridge.poll()
        self.bridge.ws.recv = lambda timeout=None: json.dumps({'type': 'end', 'streamId': 'questions'})
        with self.assertRaises(DshBridgeError):
            self.bridge.poll()
        self.assertEqual(self.bridge.events, {})
        self.assertIsNone(self.bridge.client_id)

    def test_on_demand_connection_and_cleanup(self):
        self.bridge.close()
        self.bridge._url = 'http://127.0.0.1:4567/?token=fake'
        self.bridge.authenticate()
        self.bridge.pending('session-1')
        mux = Mux([{'type': 'ready', 'clientId': 'client-1'},
                   {'type': 'waterfall', 'event': 'user-questions/request', 'eventId': 'event-1',
                    'agentId': 'session-1', 'request': {'wait': {'callId': 'call-1'}, 'questions': Q['questions']}}])
        mux.send = lambda frame: None
        with patch('websockets.sync.client.connect', return_value=mux):
            self.bridge.connect_events()
            self.assertEqual(self.bridge.answer('session-1', 'call-1', A), {'status': 'answered'})
        self.bridge.close()
        self.assertFalse(self.bridge.authenticated)
        self.assertEqual(list(self.bridge.cookies), [])

    def test_direct_rpc_cannot_bypass_scope(self):
        for endpoint, args in [('session/projections', {'request': {'sessionId': 'other'}}),
                               ('userQuestions/answer', {'agentId': 'other'})]:
            with self.assertRaisesRegex(DshBridgeError, 'scope'):
                self.bridge.rpc(endpoint, args)
        self.assertEqual(self.server.requests, [])

    def test_malformed_event_reports_protocol_error(self):
        self.bridge.ws.recv = lambda timeout=None: '[]'
        with self.assertRaisesRegex(DshBridgeError, 'frame'):
            self.bridge.poll()
        self.bridge.ws.recv = lambda timeout=None: json.dumps({'type': 'item', 'streamId': 'questions',
            'value': {'type': 'waterfall', 'eventId': 'x', 'event': 'user-questions/request',
                      'agentId': 'session-1', 'request': {'wait': 'invalid', 'questions': Q['questions']}}})
        with self.assertRaisesRegex(DshBridgeError, 'request'):
            self.bridge.poll()

    def test_open_without_event_stream_is_unavailable(self):
        self.bridge.ws = None
        self.assertEqual(self.bridge.answer('session-1', 'call-1', A), {'status': 'unavailable'})
        self.assertFalse(any(r[0] == '/api/$events/result' for r in self.server.requests))

    def test_continued_false_is_expired(self):
        self.server.state['active'][0]['state'] = 'continued'
        self.server.accepted = False
        self.assertEqual(self.bridge.answer('session-1', 'call-1', A), {'status': 'expired'})

    def test_loopback_only(self):
        for url in ['http://example.com/', 'http://127.0.0.1@evil.test/', 'https://127.0.0.1/',
                    'http://127.0.0.1/x', 'http://127.0.0.1/#token']:
            with self.assertRaises(DshBridgeError): DshBridge(url)

if __name__ == '__main__': unittest.main()
