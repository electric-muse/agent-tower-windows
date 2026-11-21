"""Scoped native questions. No transcripts, credentials or answers are persisted."""
import hashlib
import json
import os


class NativeInputError(ValueError):
    pass


def question_token(engine, sid, request):
    raw = json.dumps([engine, sid, request], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def parse_answers(questions, text):
    """Return labels/custom text, requiring a complete unambiguous answer set."""
    if not questions or len({q['id'] for q in questions}) != len(questions):
        raise NativeInputError('问题标识无效，请重新查看问题。')
    if len(questions) == 1:
        values = {1: text.strip()}
    else:
        values = {}
        for line in text.strip().splitlines():
            key, sep, value = line.partition('=')
            if not sep or not key.strip().isdigit() or int(key) in values:
                raise NativeInputError('多题请逐行回答：1=答案、2=答案；每题只回答一次。')
            values[int(key)] = value.strip()
        if set(values) != set(range(1, len(questions) + 1)):
            raise NativeInputError('请完整回答所有题目，题号从 1 开始。')
    result = []
    for index, question in enumerate(questions, 1):
        value = values[index]
        if not value or len(value) > 8000:
            raise NativeInputError('答案不能为空或超过 8000 字。')
        labels = [o['label'] for o in question.get('options', [])]
        parts = value.split(',') if question.get('multiSelect') else [value]
        selected, custom = [], []
        for part in parts:
            part = part.strip()
            if part.isdigit() and labels:
                number = int(part)
                if not 1 <= number <= len(labels):
                    raise NativeInputError('选项编号超出范围。')
                part = labels[number - 1]
            if part in labels:
                selected.append(part)
            else:
                custom.append(part)
        if len(selected) != len(set(selected)) or len(custom) > 1 or any(not p for p in custom):
            raise NativeInputError('选项不能重复；自定义答案只能填写一次。')
        answer = {'id': question['id'], 'selected': selected}
        if custom:
            answer['custom'] = custom[0]
        result.append(answer)
    return result


class NativeInput:
    def __init__(self, engine, sid):
        self.engine, self.sid = engine, sid
        self.bridge = None

    def __enter__(self):
        try:
            if self.engine in ('desktop', 'codex'):
                from tower_codex_bridge import CodexBridge
                self.bridge = CodexBridge(session_ids={self.sid})
                self.bridge.connect()
            elif self.engine == 'deepseek-harness':
                from tower_dsh_bridge import DshBridge
                url = os.environ.get('TOWER_DSH_URL')
                if not url:
                    raise NativeInputError('dsh 原生回答需要现有 Host 的认证地址，请在守护进程环境中设置 TOWER_DSH_URL。终端会话若没有 Host，仍需在原终端回答。')
                self.bridge = DshBridge(url, session_ids={self.sid})
                self.bridge.authenticate()
            else:
                raise NativeInputError('此引擎尚不支持原生回填，请在原会话回答。')
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.bridge:
            self.bridge.close()

    def pending(self):
        result = []
        for item in self.bridge.pending(self.sid):
            if self.engine in ('desktop', 'codex'):
                params = item.get('params', {})
                if params.get('threadId', self.sid) != self.sid:
                    continue
                request = {'id': item['id'], 'questions': params['questions']}
            else:
                request = {'id': item['callId'], 'questions': item['questions']}
            request['token'] = question_token(self.engine, self.sid, request)
            result.append(request)
        return result

    def answer(self, token, text):
        request = next((q for q in self.pending() if q['token'] == token), None)
        if request is None:
            return {'status': 'expired'}
        answers = parse_answers(request['questions'], text)
        if self.engine in ('desktop', 'codex'):
            payload = {a['id']: a['selected'] + ([a['custom']] if 'custom' in a else []) for a in answers}
        else:
            payload = answers
            self.bridge.connect_events()
        return self.bridge.answer(self.sid, request['id'], payload)
