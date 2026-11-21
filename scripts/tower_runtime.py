"""Private runtime IO, redacted logs, transport diagnostics and health snapshots."""
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import urllib.error

_LOCK = threading.RLock()
STATUS = {"ws_connected": False, "ws_changed_at": 0}


def redact(text):
    text = re.sub(r'(?i)\b(?:https?|wss?)://[^\s<>]+',
                  lambda m: m.group().split('?')[0] + ('?[REDACTED]' if '?' in m.group() else ''), str(text))
    text = re.sub(r'(?i)((?:access[_-]?key|ticket|token|secret|password|authorization|cookie)\s*[=:]\s*)[^\s,;]+',
                  r'\1[REDACTED]', text)
    text = re.sub(r'''(?i)(["'](?:access[_-]?key|ticket|token|secret|password|authorization|cookie)["']\s*:\s*)["'][^"']*["']''', r'\1"[REDACTED]"', text)
    return text


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + f'.{threading.get_ident()}.tmp')
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        try: tmp.unlink()
        except FileNotFoundError: pass


def safe_log(path, message, limit=400000):
    try:
        with _LOCK:
            p = Path(path)
            if p.exists() and p.stat().st_size > limit:
                os.replace(p, str(p)+'.1')
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            if hasattr(os, 'fchmod'):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as f:
                f.write(time.strftime('%F %T ') + redact(message) + '\n')
    except OSError:
        pass  # Nonessential logs must not stop a task loop.


def http_failure(exc):
    """No response body/headers are logged; allowlist only machine-readable identifiers."""
    status = getattr(exc, 'code', None)
    result = {'kind': 'network', 'retryable': True, 'http_status': status,
              'exc': re.sub(r'[^A-Za-z0-9_. ]', '', type(exc).__name__ + ':' + str(exc)[:60])}
    if isinstance(exc, urllib.error.HTTPError):
        result.update(kind='http', retryable=status in (408, 425, 429) or status >= 500)
        try:
            body = json.loads(exc.read(65536))
            result['code'] = str(body.get('code', ''))[:32]
            # Feishu request IDs help diagnostics without copying possibly sensitive msg/body.
            result['request_id'] = re.sub(r'[^a-zA-Z0-9_-]', '',
                str((body.get('error') or {}).get('log_id', '')))[:100]
        except (ValueError, AttributeError, TypeError):
            pass
        try: result['retry_after'] = min(3600, max(1, int(exc.headers.get('Retry-After', '0'))))
        except (ValueError, TypeError, AttributeError): pass
    return result


def health_issues(status, now=None):
    """Current faults only; old counters remain diagnostic history."""
    now = time.time() if now is None else now
    issues = []
    if now - status.get('updated_at', 0) > 30:
        issues.append('健康快照超过30秒')
    if not status.get('ws_connected'):
        issues.append('WebSocket未连接/未确认')
    if status.get('disk_low'):
        issues.append('磁盘可用低于1GiB')
    for name, label in (('ctl', '控制循环'), ('sync', '来源扫描'), ('sender', '投递线程')):
        if name == 'sender' and not status.get('sender_required', True):
            continue
        if not status.get(name + '_alive'):
            issues.append(label + '未运行/未确认')
        elif now - status.get(name + '_progress_at', 0) > status.get(name + '_budget_seconds', 120):
            issues.append(label + '进度超时')
    if not status.get('command_worker_alive'):
        issues.append('命令处理线程未运行/未确认')
    if status.get('command_worker_busy_since') and now - status['command_worker_busy_since'] > 300:
        issues.append('命令执行超过300秒，请检查是否阻塞')
    if status.get('command_oldest_wait_seconds', 0) > 300:
        issues.append('命令排队超过300秒')
    if status.get('delivery_recent_failed', 0):
        issues.append('最近5分钟有失败投递')
    if status.get('delivery_active_oldest_seconds', 0) > 300:
        issues.append('未暂停来源投递积压超过300秒')
    recent = sorted(key for key, value in status.items()
                    if key.endswith(('_failed_at', '_error_at')) and isinstance(value, (int, float))
                    and value > 0 and 0 <= now - value < 300)
    if recent:
        issues.append('最近5分钟运行异常：' + ', '.join(recent))
    return issues


def health_tick(state_dir, log_dir):
    """Liveness is separate from transport, delivery and disk health."""
    try:
        free = shutil.disk_usage(state_dir).free
    except OSError:
        STATUS['state_write_failed_at'] = time.time()
        return
    status = {**STATUS, 'updated_at': time.time(), 'disk_free_bytes': free,
              'disk_low': free < 1024**3}
    status['issues'] = health_issues(status)
    try:
        private_json(Path(state_dir)/'health.json', status)
    except OSError:
        STATUS['state_write_failed_at'] = time.time()
    if status['disk_low'] and time.time() - STATUS.get('disk_warn_at', 0) > 300:
        STATUS['disk_warn_at'] = time.time()
        safe_log(Path(log_dir)/'ctl.log', f'WARN disk-low free_bytes={free}')


class RedactedStream:
    """Capture SDK output before launchd receives it. Also track WS connection evidence."""
    def __init__(self, stream, log_path):
        self.stream, self.log_path = stream, log_path
        self.pending = ''
        self.lock = threading.Lock()
        self.discarding = False
    def write(self, text):
        with self.lock:
            self.pending += text
            while '\n' in self.pending:
                line, self.pending = self.pending.split('\n', 1)
                if self.discarding:
                    self.discarding = False
                    continue
                if 'disconnected to ' in line or 'connect failed' in line:
                    STATUS.update(ws_connected=False, ws_changed_at=time.time())
                elif ' connected to ' in line:
                    STATUS.update(ws_connected=True, ws_changed_at=time.time())
                if line.strip(): safe_log(self.log_path, line, limit=1000000)
            # Never flush arbitrary fragments: a secret/key may span writes.
            if len(self.pending) > 65536:
                self.pending = ''
                self.discarding = True
        return len(text)
    def flush(self): pass
    def __getattr__(self, name): return getattr(self.stream, name)


def install_runtime(root):
    import sys
    os.umask(0o077)
    for name in ('state', 'logs'):
        p = Path(root)/name
        p.mkdir(exist_ok=True, mode=0o700)
        p.chmod(0o700)
    sys.stdout = RedactedStream(sys.stdout, str(Path(root)/'logs/sdk.log'))
    sys.stderr = RedactedStream(sys.stderr, str(Path(root)/'logs/sdk.log'))
