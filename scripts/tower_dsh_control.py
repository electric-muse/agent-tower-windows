"""Scoped control of existing desktop DSH sessions through their authenticated Host.

No CLI resume, profile changes, writer-lock changes, or credential-store reads.
"""
import base64
import json
from pathlib import Path
import uuid

import tower_harness as harness
from tower_dsh_bridge import DshBridge, DshBridgeError
from tower_media import checked_image


def catalog_matches(value, allowed):
    result = []
    for sid in dict.fromkeys(allowed):
        bare_id = sid.removeprefix('session-')
        paths = list(harness.root().glob(f'*/session-{bare_id}/session.v4.jsonl.zstd'))
        if len(paths) != 1:
            continue
        path = str(paths[0])
        try:
            head = harness.header(path)
            if (head.get('parentSession') or head.get('isSeeded')
                    or head.get('id') != sid):
                continue
            title = harness.metadata(path)[1]
        except (OSError, ValueError, RuntimeError):
            continue
        if value in (sid, title):
            result.append({'id': sid, 'title': title, 'cwd': head.get('cwd') or ''})
    return result


class DshControlBridge(DshBridge):
    RPC_ENDPOINTS = DshBridge.RPC_ENDPOINTS | {'session/state', 'session/events', 'session/prompt'}

    @classmethod
    def from_config(cls, path, *, session_ids, timeout=5):
        try:
            config = json.loads(Path(path).read_text(encoding='utf-8'))
            url = config['url']
        except (OSError, ValueError, KeyError, TypeError):
            raise DshBridgeError('DSH Host configuration unavailable') from None
        return cls(url, session_ids=session_ids, timeout=timeout)

    def connect(self):
        self.authenticate()

    def state(self, sid):
        value = self.rpc('session/state', {'request': {'sessionId': sid}})
        if (not isinstance(value, dict) or type(value.get('cursor')) is not int
                or not isinstance(value.get('running'), bool)
                or not isinstance(value.get('active'), bool)
                or not isinstance(value.get('queue'), (list, type(None)))):
            raise DshBridgeError('Invalid DSH session state')
        return value

    def start_turn(self, sid, text, client_message_id=None, on_dispatch=None, images=None):
        self._require_scope(sid)
        if not isinstance(text, str) or not text.strip():
            raise DshBridgeError('DSH message is empty')
        current = self.state(sid)
        if current['running'] or current.get('queue') or current.get('pendingInput'):
            raise DshBridgeError('DSH desktop session is busy')
        content = [{'type': 'text', 'text': text}]
        if len(images or []) > 4:
            raise DshBridgeError('Too many DSH images')
        for path in images or []:
            image = checked_image(path, str(Path(path).parent))
            content.append({'type': 'image', 'mediaType': image['mime'],
                            'data': base64.b64encode(Path(image['path']).read_bytes()).decode('ascii')})
        client_id = client_message_id or str(uuid.uuid4())
        if on_dispatch:
            on_dispatch(current['cursor'])
        result = self.rpc('session/prompt', {'request': {
            'sessionId': sid, 'requestId': client_id, 'mode': 'queue',
            'content': content, 'clientTimeZone': 'Asia/Shanghai'}})
        if not isinstance(result, dict) or result.get('accepted') is not True:
            raise DshBridgeError('DSH prompt acknowledgement unavailable')
        return {'before': current['cursor']}

    def result(self, sid, pending):
        current = self.state(sid)
        page = self.rpc('session/events', {'request': {
            'address': {'kind': 'session', 'sessionId': sid},
            'throughSeq': current['cursor'], 'maxMessages': 80}})
        if not isinstance(page, dict) or not isinstance(page.get('records'), list):
            raise DshBridgeError('Invalid DSH history page')
        events = sorted((row['event'] for row in page['records']
                         if isinstance(row, dict) and row.get('type') == 'event'
                         and isinstance(row.get('event'), dict)
                         and type(row['event'].get('seq')) is int
                         and row['event']['seq'] > pending['before']), key=lambda event: event['seq'])
        found, ended, texts = False, False, []
        for event in events:
            data = event.get('data') or {}
            if not isinstance(data, dict):
                continue
            if event.get('type') == 'user/message':
                source = data.get('source') or {}
                if source.get('rpcId') == pending['client_id']:
                    found = True
                elif found and (source.get('kind') in ('user', 'retry') or source.get('rpcId')):
                    break
            elif found and event.get('type') == 'assistant/message':
                content = (data.get('message') or {}).get('content') or []
                texts.extend(part['text'] for part in content if isinstance(part, dict)
                             and part.get('type') == 'text' and isinstance(part.get('text'), str))
            elif found and event.get('type') == 'turn/end':
                ended = True
                break
        turn = {'id': pending['client_id'] if found else '',
                'status': 'completed' if ended else 'inProgress', 'text': '\n'.join(texts)}
        busy = current['running'] or current.get('queue') or current.get('pendingInput')
        return {'threadRuntimeStatus': {'type': 'active' if busy else 'idle'}}, turn
