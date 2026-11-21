"""Allowlisted diagnostics: classify in memory, persist only fixed vocabulary.

Never return raw provider messages, stderr, tool arguments, credentials or URLs.
Unknown Harness failures are intentionally non-retryable, unlike legacy engines.
"""
import json
from pathlib import Path
import re
import threading

# category -> (label, retryable, operator action)
KINDS = {
    'auth': ('认证失败', False, '在本机检查 Harness 登录状态或认证配置，再手动重试'),
    'quota': ('配额不足', False, '检查账户额度；恢复额度后再手动重试'),
    'session_busy': ('会话被占用', False, '先在原客户端关闭该会话；不会强抢或删除写锁'),
    'session_missing': ('会话不存在', False, '确认原始会话仍在；新任务请使用 #dnew'),
    'session_incompatible': ('会话不兼容', False, 'preset/派生会话不能直接接管，请使用 #dnew'),
    'approval_required': ('需要本机审批', False, '在本机处理审批或调整任务范围；不会自动提升权限'),
    'permission': ('权限受限', False, '检查工作目录与沙箱权限；不会自动提升权限'),
    'configuration': ('配置或依赖异常', False, '检查本机 headless profile、模型配置和 Node.js'),
    'rate_limit': ('服务限流', True, '等待退避后重试；如持续出现，请检查服务限额'),
    'network': ('网络暂时异常', True, '检查网络；系统最多自动重试两次'),
    'upstream': ('上游暂时异常', True, '等待服务恢复；系统最多自动重试两次'),
    'output_limit': ('输出超过保护上限', False, '缩小任务输出后手动重试'),
    'interrupted': ('任务被中断', False, '不会自动重试；需要时发送新的续跑指令'),
    'unknown': ('未分类失败', False, '请在本机检查 Harness；未知错误不自动重试'),
}
# Permanent errors take precedence if a single record contains multiple symptoms.
_PATTERNS = [
    ('session_busy', r'SessionAlreadyOwned|already (?:owned|locked)|write.?lock|session.{0,100}(?:is live|occupied|in use|busy)|exclusive run interval'),
    ('session_incompatible', r'cannot be (?:adopted|driven)|agent preset|subagent or forked|not .{0,80}cwd|recorded in .{0,100}not'),
    ('session_missing', r'(?:session.{0,100}(?:does not exist|not found)|no such session|SESSION_QUERY_SESSION_NOT_FOUND)'),
    ('auth', r'invalid.{0,20}(?:api.?key|token)|unauthenticated|unauthorized|not authorized|authentication|not logged in|login required|\b401\b'),
    ('quota', r'insufficient.{0,20}(?:balance|quota|credit)|quota exceeded|payment required|\b402\b'),
    ('approval_required', r'approval.{0,60}(?:required|unavailable|denied|reject|pending|disabled)|(?:no|missing).{0,20}answerer'),
    ('permission', r'permission denied|access denied|sandbox.{0,40}(?:denied|forbidden)|\bEACCES\b|\bEPERM\b|\b403\b'),
    ('configuration', r'model not found|unknown model|invalid.{0,20}(?:config|profile)|module not found|cannot find (?:module|package)|ENOENT|DSH_LAUNCH_FAILED'),
    ('output_limit', r'DSH_OUTPUT_TOO_LARGE'),
    ('rate_limit', r'\b429\b|too many requests|rate.?limit'),
    ('network', r'timed? ?out|timeout|ECONN|ENOTFOUND|EAI_AGAIN|connection reset|network error|fetch failed|DNS'),
    ('upstream', r'\b50[0234]\b|overloaded|service unavailable|bad gateway'),
]
_PATTERNS = [(name, re.compile(pattern, re.I)) for name, pattern in _PATTERNS]


def diagnostic(category='unknown'):
    category = category if category in KINDS else 'unknown'
    label, retryable, action = KINDS[category]
    return {'type':'diagnostic', 'category':category, 'label':label,
            'retryable':retryable, 'action':action}


def classify(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text[:65536]
    return next((name for name, pattern in _PATTERNS if pattern.search(text)), 'unknown')


def prefer(*categories):
    known=[c for c in categories if c in KINDS and c!='unknown']
    return next((c for c in known if not KINDS[c][1]), known[-1] if known else 'unknown')


class Capture:
    """Bounded rolling stderr classification. Raw bytes never leave this object."""
    def __init__(self):
        self.category = 'unknown'
        self._tail = ''
        self._lock = threading.Lock()
    def feed(self, chunk):
        with self._lock:
            text = self._tail + chunk.decode('utf-8', 'replace') if isinstance(chunk, bytes) else self._tail + str(chunk)
            category = classify(text)
            self.category = prefer(self.category,category)
            self._tail = text[-1024:]
    def result(self):
        with self._lock: return diagnostic(self.category)
    def clear(self):
        with self._lock: self._tail = ''


PHASES = {'starting', 'running', 'tool_running', 'waiting_input', 'waiting_approval', 'completed'}


def read_run(path):
    """Read a bounded tail of the current round, ignoring malformed/legacy records.

    Every new wrapper process emits a run_start record. No stale previous-round
    error or final text may authorize a retry or label the current round.
    """
    result = {'phase':'starting', 'diagnostic':None}
    try:
        with Path(path).open('rb') as file:
            file.seek(max(0, Path(path).stat().st_size - 128*1024))
            lines = file.read(128*1024).decode('utf-8', 'replace').splitlines()
        for line in lines:
            try: event = json.loads(line)
            except ValueError: continue
            if not isinstance(event, dict): continue
            kind = event.get('type')
            if kind in ('run_start', 'session'):
                result = {'phase':'starting' if kind == 'run_start' else 'running', 'diagnostic':None}
            elif kind == 'status' and event.get('phase') in PHASES:
                result['phase'] = event['phase']
            elif kind == 'diagnostic':
                result['diagnostic'] = diagnostic(event.get('category'))
            elif kind == 'final':
                result['phase'] = 'completed'
    except (OSError, TypeError, ValueError): pass
    return result
