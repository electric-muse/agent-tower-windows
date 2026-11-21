#!/usr/bin/env python3
"""Existing DSH Web/Desktop Host interaction adapter.

URLs (including launch tokens) are supplied by the caller in memory only. No
profile boot, session resume, credential-file access, or log rewriting occurs.
Protocol verified statically against DeepSeek Harness 0.2.0-rc.2 app.asar.
"""
from __future__ import annotations

import http.cookiejar
import json
import time
import uuid
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPCookieProcessor, ProxyHandler, HTTPRedirectHandler


class DshBridgeError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DshBridge:
    RPC_ENDPOINTS = frozenset({'session/projections', '$events/result', 'userQuestions/answer'})

    def __init__(self, authenticated_url: str, *, session_ids=(), timeout: float = 5):
        try:
            parts = urlsplit(authenticated_url)
            parts.port
        except (TypeError, ValueError, AttributeError):
            raise DshBridgeError('Invalid DSH authentication URL') from None
        if (parts.scheme != 'http' or parts.hostname not in {'localhost', '127.0.0.1', '::1'}
                or parts.username or parts.password or parts.path not in {'', '/'} or parts.fragment):
            raise DshBridgeError('DSH bridge requires a loopback HTTP root URL')
        self.base = f'http://{parts.netloc}'
        self._url = authenticated_url
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        self.http = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.cookies), _NoRedirect())
        self.ws = None
        self.client_id = None
        self.events = {}
        self.session_ids = frozenset(session_ids)
        if any(not isinstance(s, str) or not s for s in self.session_ids):
            raise DshBridgeError('Invalid managed session scope')
        self.authenticated = False

    def authenticate(self):
        # Launch-token exchange intentionally accepts only its local 303 redirect.
        from urllib.error import HTTPError
        try:
            response = self.http.open(self._url, timeout=self.timeout)
        except HTTPError as exc:
            if exc.code != 303 or exc.headers.get('Location') != './':
                raise DshBridgeError('DSH authentication failed') from None
            exc.close()
        except Exception:
            raise DshBridgeError('DSH authentication transport unavailable') from None
        else:
            response.close()
        finally:
            self._url = self.base
        if not any(cookie.name.startswith('dsh-auth-') for cookie in self.cookies):
            raise DshBridgeError('DSH authentication did not issue a session cookie')
        self.authenticated = True

    def rpc(self, endpoint: str, args: dict):
        if not self.authenticated:
            raise DshBridgeError('DSH bridge is not authenticated')
        if endpoint not in self.RPC_ENDPOINTS:
            raise DshBridgeError('Unsupported DSH RPC endpoint')
        if not isinstance(args, dict):
            raise DshBridgeError('Invalid DSH RPC arguments')
        if endpoint in {'session/projections', 'session/state', 'session/prompt'}:
            request = args.get('request')
            if not isinstance(request, dict):
                raise DshBridgeError('Invalid DSH projection request')
            self._require_scope(request.get('sessionId'))
        elif endpoint == 'session/events':
            request = args.get('request') or {}
            address = request.get('address') or {}
            if address.get('kind') != 'session':
                raise DshBridgeError('History address is outside managed scope')
            self._require_scope(address.get('sessionId'))
        elif endpoint == 'userQuestions/answer':
            self._require_scope(args.get('agentId'))
        rpc_id = str(uuid.uuid4())
        body = {'type': 'client-request', 'rpcId': rpc_id, 'method': endpoint, 'payload': {'args': args}}
        req = Request(f'{self.base}/api/{endpoint}', data=json.dumps(body).encode(),
                      headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with self.http.open(req, timeout=self.timeout) as response:
                envelope = json.load(response)
        except Exception:
            raise DshBridgeError('DSH RPC transport unavailable') from None
        if not isinstance(envelope, dict):
            raise DshBridgeError('Invalid DSH RPC response')
        if envelope.get('type') != 'server-response' or envelope.get('rpcId') != rpc_id:
            raise DshBridgeError('DSH RPC correlation mismatch')
        result = envelope.get('result', {})
        if not isinstance(result, dict):
            raise DshBridgeError('Invalid DSH RPC result')
        if result.get('ok') is not True:
            error = result.get('error')
            code = error.get('code', 'unknown') if isinstance(error, dict) else 'unknown'
            code = code if isinstance(code, str) and code.isidentifier() else 'unknown'
            raise DshBridgeError(f'DSH RPC rejected ({code})')
        return result.get('value')

    def _require_scope(self, session_id):
        if not isinstance(session_id, str) or session_id not in self.session_ids:
            raise DshBridgeError('Session is outside managed scope')

    def _projection(self, session_id):
        self._require_scope(session_id)
        projection = self.rpc('session/projections', {'request': {'sessionId': session_id}})
        try:
            values = projection['values']
            user_questions = values.get('userQuestions')
            state = user_questions['questions'] if user_questions is not None else {'active': [], 'settled': []}
            if not isinstance(state, dict) or not all(isinstance(state.get(k), list) for k in ('active', 'settled')):
                raise ValueError
            seen = set()
            for question in state['active']:
                if (not isinstance(question, dict) or not isinstance(question.get('callId'), str)
                        or not question['callId'] or question['callId'] in seen
                        or question.get('state') not in {'open', 'continued'}
                        or not isinstance(question.get('questions'), list) or not question['questions']):
                    raise ValueError
                seen.add(question['callId'])
                ids = [q['id'] for q in question['questions']]
                if any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
                    raise ValueError
            for question in state['settled']:
                if not isinstance(question, dict) or not isinstance(question.get('callId'), str):
                    raise ValueError
                self._validate_answers(question.get('answers'))
            return state
        except (KeyError, TypeError, AttributeError, ValueError):
            raise DshBridgeError('Invalid DSH question projection') from None

    def pending(self, session_id: str):
        return self._projection(session_id)['active']

    @staticmethod
    def _validate_answers(answers, questions=None):
        if not isinstance(answers, list):
            raise DshBridgeError('Invalid DSH answer batch')
        for answer in answers:
            if (not isinstance(answer, dict) or set(answer) - {'id', 'selected', 'custom'}
                    or not isinstance(answer.get('id'), str)
                    or not isinstance(answer.get('selected'), list)
                    or any(not isinstance(value, str) for value in answer['selected'])
                    or ('custom' in answer and not isinstance(answer['custom'], str))):
                raise DshBridgeError('Invalid DSH answer schema')
        ids = [a['id'] for a in answers]
        if len(set(ids)) != len(ids):
            raise DshBridgeError('Answer must include every question exactly once')
        if questions is not None and set(ids) != {q['id'] for q in questions}:
            raise DshBridgeError('Answer must include every question exactly once')

    def connect_events(self):
        if not self.authenticated or not self.session_ids:
            raise DshBridgeError('Authenticated managed session scope is required')
        from websockets.sync.client import connect
        cookie_req = Request(self.base + '/api/remote.mux')
        self.cookies.add_cookie_header(cookie_req)
        try:
            self.ws = connect(self.base.replace('http:', 'ws:', 1) + '/api/remote.mux',
                              additional_headers={'Cookie': cookie_req.get_header('Cookie', '')},
                              open_timeout=self.timeout, proxy=None, max_size=2**20)
            self.ws.send(json.dumps({'type': 'open', 'streamId': 'questions', 'endpoint': '$events',
                                     'payload': {'args': {}}}))
            deadline = time.monotonic() + self.timeout
            while self.client_id is None:
                self.poll(max(0.001, deadline - time.monotonic()))
                if time.monotonic() >= deadline:
                    raise TimeoutError
        except Exception:
            self.close()
            raise DshBridgeError('DSH question event stream unavailable') from None

    def poll(self, timeout: float = 0.01):
        if self.ws is None:
            raise DshBridgeError('DSH question event stream is not connected')
        try:
            frame = json.loads(self.ws.recv(timeout=timeout))
        except TimeoutError:
            return None
        except Exception:
            self.close()
            raise DshBridgeError('Invalid DSH question event frame') from None
        if not isinstance(frame, dict):
            raise DshBridgeError('Invalid DSH question event frame')
        if frame.get('streamId') != 'questions':
            return None
        if frame.get('type') in {'end', 'error'}:
            self.close()
            raise DshBridgeError('DSH question event stream ended')
        if frame.get('type') != 'item':
            raise DshBridgeError('Invalid DSH question event frame')
        value = frame.get('value', {})
        if not isinstance(value, dict):
            raise DshBridgeError('Invalid DSH question event value')
        kind = value.get('type')
        if kind == 'ready':
            if not isinstance(value.get('clientId'), str) or not value['clientId']:
                raise DshBridgeError('Invalid DSH event client ID')
        elif kind in {'cancel', 'waterfall'}:
            if self.client_id is None or not isinstance(value.get('eventId'), str) or not value['eventId']:
                raise DshBridgeError('Invalid DSH event correlation')
        else:
            raise DshBridgeError('Invalid DSH event type')
        if kind == 'ready':
            self.client_id = value['clientId']
        elif kind == 'cancel':
            self.events.pop(value['eventId'], None)
        elif kind == 'waterfall':
            if not isinstance(value.get('request'), dict):
                raise DshBridgeError('Invalid DSH event request')
            if (value.get('event') == 'user-questions/request'
                    and value.get('agentId') in self.session_ids):
                request = value['request']
                if (not isinstance(request.get('questions'), list)
                        or not request['questions']
                        or ('wait' in request and not isinstance(request['wait'], dict))):
                    raise DshBridgeError('Invalid DSH question event request')
                self.events[value['eventId']] = value
            else:
                # Never claim unrelated approvals or other clients' waterfalls.
                self.rpc('$events/result', {'clientId': self.client_id, 'eventId': value['eventId'],
                                           'outcome': {'kind': 'next'}})
        return value

    def answer(self, session_id: str, call_id: str, answers: list[dict], *, confirm_timeout: float = 3):
        self._require_scope(session_id)
        if not isinstance(confirm_timeout, (int, float)) or not 0 <= confirm_timeout <= 30:
            raise DshBridgeError('Invalid DSH confirmation timeout')
        self._validate_answers(answers)
        if not isinstance(call_id, str) or not call_id:
            raise DshBridgeError('Invalid DSH call ID')
        if self.ws is not None:
            for _ in range(100):
                if self.poll() is None:
                    break
        question = next((q for q in self.pending(session_id) if q['callId'] == call_id), None)
        if question is None:
            legacy = any(e.get('agentId') == session_id
                         and not e.get('request', {}).get('wait') for e in self.events.values())
            return {'status': 'unsupported' if legacy else 'expired'}
        self._validate_answers(answers, question['questions'])
        payload = {'answers': answers}
        if question.get('state') == 'continued':
            accepted = self.rpc('userQuestions/answer', {'agentId': session_id, 'callId': call_id, 'answer': payload})
            if not isinstance(accepted, bool):
                raise DshBridgeError('Invalid DSH answer acknowledgement')
            return {'status': 'queued' if accepted else 'expired'}
        if self.ws is None or self.client_id is None:
            return {'status': 'unavailable'}
        candidates = [e for e in self.events.values() if e.get('agentId') == session_id
                      and (e.get('request', {}).get('wait', {}).get('callId') == call_id
                           or (not e.get('request', {}).get('wait') and
                               e.get('request', {}).get('questions') == question['questions']))]
        if len(candidates) != 1:
            return {'status': 'unavailable'}
        event = candidates[0]
        self.rpc('$events/result', {'clientId': self.client_id, 'eventId': event['eventId'],
                                   'outcome': {'kind': 'result', 'value': payload}})
        # $events/result returns success even for stale delivery. Confirm the
        # exact answer in the authoritative projection; HTTP 200 is insufficient.
        deadline = time.monotonic() + confirm_timeout
        while True:
            state = self._projection(session_id)
            settled = next((q for q in state.get('settled', []) if q['callId'] == call_id), None)
            if settled:
                expected_answers = sorted(answers, key=lambda a: a['id'])
                received_answers = sorted(settled.get('answers', []), key=lambda a: a['id'])
                return {'status': 'answered' if received_answers == expected_answers else 'expired'}
            if time.monotonic() >= deadline:
                return {'status': 'unconfirmed'}
            time.sleep(0.05)

    def close(self):
        try:
            if self.ws is not None:
                self.ws.close()
        finally:
            self.ws = None
            self.client_id = None
            self.events.clear()
            self.cookies.clear()
            self.authenticated = False
            self._url = self.base
