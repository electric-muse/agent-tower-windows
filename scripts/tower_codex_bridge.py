"""Native Codex desktop IPC follower for explicitly scoped local threads.

Private desktop protocol observed in the bundled Codex desktop client.
Only explicitly scoped local threads are used.
"""
import json
import os
from pathlib import Path
import socket
import stat
import struct
import time
import uuid
from tower_platform import open_codex_thread

MAX_FRAME = 32 * 1024 * 1024
INPUT_METHOD = 'item/tool/requestUserInput'


class CodexBridgeError(RuntimeError):
    def __init__(self, message, *, code=None, stage=None):
        super().__init__(message)
        self.code = code
        self.stage = stage


class WindowsPipe:
    """Socket-like byte transport for the Codex desktop named pipe."""
    def __init__(self, path, timeout):
        import ctypes
        import msvcrt

        self.file = open(path, 'r+b', buffering=0)
        self.handle = msvcrt.get_osfhandle(self.file.fileno())
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.GetNamedPipeServerProcessId.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        self.kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        self.kernel.OpenProcess.restype = ctypes.c_void_p
        self.kernel.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.PeekNamedPipe.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
        self.timeout = timeout
        pid = ctypes.c_ulong()
        if not self.kernel.GetNamedPipeServerProcessId(self.handle, ctypes.byref(pid)):
            self.close()
            raise CodexBridgeError('Codex pipe server identity is unavailable')
        process = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not process:
            self.close()
            raise CodexBridgeError('Codex pipe server cannot be verified')
        try:
            size = ctypes.c_ulong(32768)
            name = ctypes.create_unicode_buffer(size.value)
            if not self.kernel.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(size)):
                raise CodexBridgeError('Codex pipe server path is unavailable')
            executable = name.value.lower().replace('/', '\\')
            trusted_backend = (executable.endswith('\\codex.exe')
                               and '\\openai\\codex\\' in executable)
            trusted_frontend = (executable.endswith('\\chatgpt.exe')
                                and '\\windowsapps\\openai.codex_' in executable)
            if not (trusted_backend or trusted_frontend):
                raise CodexBridgeError('Codex pipe server is not the desktop app')
        except Exception:
            self.close()
            raise
        finally:
            self.kernel.CloseHandle(process)

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        while data:
            written = self.file.write(data)
            if not written:
                raise OSError('Codex pipe write failed')
            data = data[written:]

    def recv(self, size):
        import ctypes

        deadline = time.monotonic() + self.timeout
        available = ctypes.c_ulong()
        while True:
            if not self.kernel.PeekNamedPipe(self.handle, None, 0, None,
                                             ctypes.byref(available), None):
                raise OSError(ctypes.get_last_error(), 'Codex pipe disconnected')
            if available.value:
                return self.file.read(min(size, available.value))
            if time.monotonic() >= deadline:
                raise TimeoutError
            time.sleep(0.01)

    def close(self):
        self.file.close()


def input_projection(state):
    """Drop history and unrelated approvals; keep question/answer evidence only."""
    pending = [r for r in state.get('requests', []) if isinstance(r, dict)
               and r.get('method') == INPUT_METHOD]
    settled = []
    stack = [state.get('turns', []), state.get('turnHistory', {})]
    budget = 100000
    while stack and budget:
        budget -= 1
        value = stack.pop()
        if isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, dict):
            if value.get('type') == 'userInputResponse':
                settled.append({k: value.get(k) for k in ('requestId', 'answers', 'completed')})
            else:
                stack.extend(v for v in value.values() if isinstance(v, (dict, list)))
    return {'pending': pending, 'settled': settled}


def _history_turns(state):
    history = ((state.get('turnHistory') or {}).get('history') or {})
    entities = history.get('entitiesByKey') or {}
    for island in reversed(history.get('islands') or []):
        for entry in reversed(island.get('entries') or []):
            turn = entities.get(entry.get('value')) or {}
            if turn.get('turnId'):
                messages = [item for item in turn.get('items') or []
                            if item.get('type') == 'agentMessage' and item.get('phase') == 'final_answer']
                user = next((item for item in turn.get('items') or []
                             if item.get('type') == 'userMessage'), {})
                yield {'id': turn['turnId'], 'status': turn.get('status'),
                       'client_id': user.get('clientId') or '',
                       'text': '\n'.join(item.get('text') or '' for item in messages).strip()}


def latest_turn(state):
    return next(_history_turns(state), {'id': '', 'status': '', 'client_id': '', 'text': ''})


def turn_by_client(state, client_id):
    for turn in _history_turns(state):
        if turn['client_id'] == client_id:
            return turn
    return {'id': '', 'status': '', 'client_id': '', 'text': ''}


class CodexBridge:
    def __init__(self, *, session_ids, socket_path=None, timeout=2, auto_open=False):
        self.session_ids = set(session_ids)
        self.path = Path(socket_path or (r'\\.\pipe\codex-ipc' if os.name == 'nt'
                                         else Path.home() / '.codex/ipc/ipc.sock'))
        self.timeout = timeout
        self.socket = None
        self.client_id = 'initializing-client'
        self.buffer = bytearray()
        self.followed = {}
        self.snapshots = {}
        self.auto_open = auto_open
        self.opened_threads = set()

    def connect(self):
        stage = 'open'
        try:
            if os.name == 'nt':
                self.socket = WindowsPipe(str(self.path), self.timeout)
            else:
                info = self.path.lstat()
                parent = self.path.parent.lstat()
                if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                        or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                        or info.st_mode & 0o077 or parent.st_mode & 0o022):
                    raise CodexBridgeError('Codex IPC socket ownership or permissions are unsafe')
                self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.socket.settimeout(self.timeout)
                self.socket.connect(str(self.path))
            stage = 'initialize'
            response = self.request('initialize', {'clientType': 'agent-tower'}, version=0)
            self.client_id = response['result']['clientId']
        except CodexBridgeError:
            self.close()
            raise
        except (OSError, KeyError) as error:
            self.close()
            code = getattr(error, 'winerror', None)
            raise CodexBridgeError(
                f'Codex desktop IPC {stage} failed: {type(error).__name__} winerror={code}'
            ) from None

    def _send(self, message):
        raw = json.dumps(message, ensure_ascii=False).encode()
        if not self.socket or len(raw) > MAX_FRAME:
            raise CodexBridgeError('Codex IPC connection or frame is unavailable')
        self.socket.sendall(struct.pack('<I', len(raw)) + raw)

    def _receive(self, deadline):
        while True:
            if len(self.buffer) >= 4:
                length = struct.unpack('<I', self.buffer[:4])[0]
                if not 0 < length <= MAX_FRAME:
                    raise CodexBridgeError('Codex IPC frame size is invalid')
                if len(self.buffer) >= length + 4:
                    raw = bytes(self.buffer[4:length + 4])
                    del self.buffer[:length + 4]
                    try:
                        value = json.loads(raw)
                    except (ValueError, UnicodeError):
                        raise CodexBridgeError('Codex IPC frame is invalid') from None
                    if not isinstance(value, dict):
                        raise CodexBridgeError('Codex IPC message is invalid')
                    return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self.socket.settimeout(remaining)
            chunk = self.socket.recv(65536)
            if not chunk:
                raise CodexBridgeError('Codex desktop IPC disconnected')
            self.buffer.extend(chunk)

    def _handle(self, message):
        if message.get('type') == 'client-discovery-request':
            self._send({'type': 'client-discovery-response', 'requestId': message['requestId'],
                        'response': {'canHandle': False}})
            return
        if (message.get('type') != 'broadcast' or message.get('method') != 'thread-stream-state-changed'
                or message.get('version') not in (11, 13)):
            return
        params = message.get('params') or {}
        sid = params.get('conversationId')
        change = params.get('change') or {}
        if (sid not in self.session_ids or params.get('hostId') != 'local'
                or message.get('sourceClientId') != self.followed.get(sid)
                or change.get('type') != 'snapshot'):
            return
        state = change.get('conversationState') or {}
        if state.get('id') != sid:
            raise CodexBridgeError('Codex snapshot thread identity mismatch')
        self.snapshots[sid] = state

    def request(self, method, params, *, version=1, owner=None):
        allowed = {'initialize', 'thread-owner-discovery', 'thread-follower-submit-user-input',
                   'thread-follower-start-turn'}
        if method not in allowed:
            raise CodexBridgeError('Unsupported Codex bridge operation')
        if method != 'initialize':
            if not isinstance(params, dict) or params.get('conversationId') not in self.session_ids:
                raise CodexBridgeError('Thread is outside the explicitly watched scope')
            if method == 'thread-owner-discovery' and params.get('hostId') != 'local':
                raise CodexBridgeError('Only the local desktop host is supported')
            if method in ('thread-follower-submit-user-input', 'thread-follower-start-turn') and (not owner
                    or owner != self.followed.get(params['conversationId'])):
                raise CodexBridgeError('Request must target the discovered thread owner')
        request_id = str(uuid.uuid4())
        message = {'type': 'request', 'requestId': request_id, 'sourceClientId': self.client_id,
                   'method': method, 'params': params, 'version': version,
                   'timeoutMs': int(self.timeout * 1000)}
        if owner:
            message['targetClientId'] = owner
        try:
            self._send(message)
        except OSError:
            raise CodexBridgeError('Codex IPC request timed out or disconnected') from None
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                reply = self._receive(deadline)
                if reply.get('type') == 'response' and reply.get('requestId') == request_id:
                    if reply.get('resultType') != 'success':
                        if method == 'thread-owner-discovery' and reply.get('error') == 'no-client-found':
                            raise CodexBridgeError('Codex desktop thread owner is unavailable',
                                                   code='thread-not-loaded', stage=method)
                        raise CodexBridgeError('Codex owner could not handle this request',
                                               code='request-rejected', stage=method)
                    return reply
                self._handle(reply)
        except (TimeoutError, OSError):
            raise CodexBridgeError('Codex IPC request timed out or disconnected') from None

    def _following(self, sid, owner, value):
        self._send({'type': 'broadcast', 'method': 'thread-stream-following-changed', 'version': 1,
                    'sourceClientId': self.client_id, 'targetClientIds': [owner],
                    'params': {'hostId': 'local', 'conversationId': sid, 'following': value}})

    def snapshot_state(self, sid):
        if sid not in self.session_ids:
            raise CodexBridgeError('Thread is outside the explicitly watched scope')
        try:
            response = self.request('thread-owner-discovery', {'hostId': 'local', 'conversationId': sid})
        except CodexBridgeError as error:
            if error.code != 'thread-not-loaded' or not self.auto_open or sid in self.opened_threads:
                raise
            self.opened_threads.add(sid)
            try:
                open_codex_thread(sid)
            except (OSError, ValueError, RuntimeError):
                raise CodexBridgeError('Codex desktop thread could not be opened',
                                       code='thread-open-failed', stage='open-thread') from None
            deadline = time.monotonic() + 15
            while True:
                try:
                    response = self.request('thread-owner-discovery', {'hostId': 'local', 'conversationId': sid})
                    break
                except CodexBridgeError as retry_error:
                    if retry_error.code != 'thread-not-loaded':
                        raise
                    if time.monotonic() >= deadline:
                        raise CodexBridgeError('Codex desktop thread did not load in time',
                                               code='thread-open-timeout', stage='open-thread') from None
                    time.sleep(0.25)
        owner = response.get('handledByClientId')
        if not owner:
            raise CodexBridgeError('Codex desktop thread owner is unavailable')
        self.followed[sid] = owner
        # Drain already delivered snapshots before requesting a fresh one.
        try:
            for _ in range(100):
                self._handle(self._receive(time.monotonic() + 0.001))
        except TimeoutError:
            pass
        self.snapshots.pop(sid, None)
        self._following(sid, owner, True)
        deadline = time.monotonic() + self.timeout
        try:
            while sid not in self.snapshots:
                self._handle(self._receive(deadline))
        except (TimeoutError, OSError):
            raise CodexBridgeError('Codex question snapshot is unavailable') from None
        return self.snapshots.pop(sid)

    def snapshot(self, sid):
        return input_projection(self.snapshot_state(sid))

    def pending(self, sid):
        return self.snapshot(sid)['pending']

    def start_turn(self, sid, text, client_message_id=None, on_dispatch=None, images=None):
        if not isinstance(text, str) or not text.strip():
            raise CodexBridgeError('Codex message is empty')
        images = images or []
        if len(images) > 4 or any(not isinstance(path, str) or not os.path.isabs(path) for path in images):
            raise CodexBridgeError('Codex image paths are invalid')
        state = self.snapshot_state(sid)
        if (state.get('threadRuntimeStatus') or {}).get('type') != 'idle':
            raise CodexBridgeError('Codex desktop thread is busy')
        before = latest_turn(state)['id']
        if on_dispatch:
            on_dispatch(before)
        response = self.request('thread-follower-start-turn', {
            'conversationId': sid,
            'turnStart': {'request': {
                'threadId': sid,
                'input': ([{'type': 'text', 'text': text, 'text_elements': []}]
                          + [{'type': 'localImage', 'path': path} for path in images]),
                'clientUserMessageId': client_message_id or str(uuid.uuid4()),
            }, 'context': {}},
        }, version=2, owner=self.followed[sid])
        return {'before': before, 'result': (response.get('result') or {}).get('result')}

    def answer(self, sid, request_id, answers):
        pending = self.pending(sid)
        question = next((r for r in pending if r.get('id') == request_id), None)
        if question is None:
            return {'status': 'expired'}
        expected = {q['id'] for q in question['params']['questions']}
        if (set(answers) != expected or any(not isinstance(a, list) or not a
                or any(not isinstance(text, str) or not text.strip() for text in a) for a in answers.values())):
            raise CodexBridgeError('Answer must include every question exactly once')
        self.request('thread-follower-submit-user-input', {'conversationId': sid, 'requestId': request_id,
                     'response': {'answers': {q: {'answers': a} for q, a in answers.items()}}},
                     owner=self.followed[sid])
        # Desktop returns ok even if its request disappeared. Confirm the native
        # userInputResponse, rather than claiming that an IPC write means success.
        state = self.snapshot(sid)
        settled = next((r for r in state['settled'] if r['requestId'] == request_id and r['completed']), None)
        if settled:
            return {'status': 'answered' if settled['answers'] == answers else 'expired'}
        return {'status': 'unconfirmed'}

    def close(self):
        if self.socket:
            try:
                for sid, owner in self.followed.items():
                    self._following(sid, owner, False)
            except (OSError, CodexBridgeError):
                pass
            self.socket.close()
        self.socket = None
        self.followed.clear()
        self.snapshots.clear()
        self.buffer.clear()
        self.opened_threads.clear()
