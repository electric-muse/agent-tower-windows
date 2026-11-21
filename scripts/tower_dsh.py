"""DeepSeek headless process adapter. No transcript mutation or credential access.

CLI contract verified against locally installed dsh 0.1.7-rc.2 (headless --help).
All children stay in the Tower-owned process group; stdin carries task text.
Only session identity and final assistant text may reach the private job output.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from tower_diagnostics import Capture, classify, diagnostic, prefer
from tower_platform import IS_WINDOWS, lock_file, unlock_file, process_flags

SESSION = re.compile(r'(?:session-)?[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
MAX_LINE = 8 * 1024 * 1024


def valid_session(value):
    return isinstance(value, str) and SESSION.fullmatch(value) is not None


def assert_available(path):
    """Advisory preflight only. dsh's own write-open lock is authoritative.

    Never create/remove a source lock. A race after this check is rejected by dsh.
    """
    if IS_WINDOWS:
        raise ValueError('Windows 暂不支持接管现有 Harness 会话')
    lock = Path(path).parent / 'session.lock'
    try:
        with lock.open('rb') as file:
            try:
                lock_file(file, nonblocking=True)
            except BlockingIOError:
                raise ValueError('会话被 Harness 占用；请先在桌面端关闭该会话，再显式接管') from None
            finally:
                unlock_file(file)
    except FileNotFoundError:
        pass


def runtime_env(perm='safe', mode='', node_bin=''):
    env = dict(os.environ)
    # launchd does not inherit interactive shell PATH, including keg-only Node.
    candidates = ([node_bin] if node_bin else [shutil.which('node'),
                  '/opt/homebrew/opt/node@24/bin/node', '/opt/homebrew/bin/node',
                  '/usr/local/opt/node@24/bin/node', '/usr/local/bin/node'])
    node = next((p for p in candidates if p and Path(p).is_file() and os.access(p, os.X_OK)), '')
    if not node:
        raise ValueError('未找到 Node.js，请配置 dsh_node_bin')
    env['PATH'] = str(Path(node).parent) + os.pathsep + env.get('PATH', '/usr/bin:/bin')
    env['PYTHONIOENCODING'] = 'utf-8'
    env['DSH_PERMISSION_MODE'] = ('read-only' if mode == 'plan' else
                                  'danger-full-access' if perm == 'full' else 'workspace-write')
    return env


def spawn(binary, cwd, prompt, output, *, session='', perm='safe', mode='new', node_bin='', config_patch=''):
    if session and not valid_session(session):
        raise ValueError('无效的 Harness session ID')
    if not Path(binary).is_file() or not os.access(binary, os.X_OK):
        raise ValueError('未找到可执行 dsh；请配置 dsh_bin')
    if perm not in ('safe', 'full'):
        raise ValueError('未知权限模式')
    env = runtime_env(perm, mode, node_bin)
    cmd = [sys.executable, str(Path(__file__).resolve()), '--bin', binary]
    if session:
        cmd += ['--session-id', session]
    if config_patch:
        patch_path=Path(config_patch).expanduser().resolve()
        if not patch_path.is_file():raise ValueError('配置补丁文件不存在')
        cmd += ['--config-patch',str(patch_path)]
    # No task in argv, shell interpolation, persistent prompt file, or pipe deadlock.
    with tempfile.TemporaryFile() as task:
        task.write(prompt.encode('utf-8')); task.seek(0)
        return subprocess.Popen(cmd, cwd=cwd, stdin=task, stdout=output,
                                stderr=subprocess.DEVNULL, env=env, **process_flags())


def run(binary, session='', config_patch=''):
    if session and not valid_session(session):
        return 2
    if IS_WINDOWS and Path(binary).suffix.lower() == '.js':
        node = shutil.which('node')
        if not node:
            raise ValueError('未找到 Node.js')
        cmd = [node, binary]
    else:
        cmd = [binary]
    if config_patch:cmd += ['--patch',config_patch]
    cmd += ['--profile', 'headless', '--json']
    if session: cmd += ['--session-id', session]
    cmd += ['-']
    def emit(event): print(json.dumps(event, ensure_ascii=False), flush=True)
    emit({'type':'run_start', 'at':time.time()})
    stderr = Capture()
    def drain(pipe):
        try:
            while chunk := pipe.read1(4096): stderr.feed(chunk)
        finally: pipe.close()
    # Never store raw stderr. Drain concurrently so a noisy child cannot deadlock.
    with subprocess.Popen(cmd, stdin=sys.stdin.buffer, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as child:
        reader = threading.Thread(target=drain, args=(child.stderr,), daemon=True)
        reader.start()
        have_session = have_final = False
        category = None
        phase = None
        def status(value):
            nonlocal phase
            if value != phase:
                phase = value
                emit({'type':'status','phase':value,'at':time.time()})
        for line in iter(lambda: child.stdout.readline(MAX_LINE + 1), b''):
            if len(line) > MAX_LINE:
                category = 'output_limit'
                child.terminate()
                try: child.wait(timeout=3)
                except subprocess.TimeoutExpired: child.kill()
                break
            try: event = json.loads(line)
            except (ValueError, UnicodeError): continue
            if not isinstance(event, dict): continue
            kind = event.get('type')
            if kind == 'session' and valid_session(event.get('sessionId')):
                have_session = True
                emit({'type':'session','sessionId':event['sessionId']})
                status('running')
            elif kind == 'final' and isinstance(event.get('text'), str):
                have_final = True
                emit({'type':'final','text':event['text']})
            elif kind == 'error':
                category = prefer(category, classify(event))
            elif kind == 'status':
                reason = event.get('reason') or {}
                if isinstance(reason,dict) and reason.get('kind') == 'error':
                    category = prefer(category, classify(reason))
                elif event.get('phase') in ('turn_start','step_start','step_end'):
                    status('running')
            elif kind == 'tool_call':
                tool = str(event.get('tool') or '').lower()
                status('waiting_input' if tool in ('ask_user_question','request_user_input') else
                       'waiting_approval' if tool in ('request_approval','request_user_approval') else 'tool_running')
            elif kind == 'tool_result': status('running')
        code = child.wait()
        reader.join(timeout=2)
    if not code and not (have_session and have_final): code = 2
    if code:
        category = ('output_limit' if category == 'output_limit' else 'interrupted' if code < 0 else
                    prefer(category, stderr.result()['category']))
        emit(diagnostic(category))
        emit({'type':'error','error':f'DSH_RUN_FAILED_RC_{code}'})
    else: status('completed')
    stderr.clear()
    return code


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bin', required=True)
    parser.add_argument('--session-id', default='')
    parser.add_argument('--config-patch', default='')
    options = parser.parse_args()
    try:
        sys.exit(run(options.bin, options.session_id, options.config_patch))
    except (OSError, ValueError):
        print(json.dumps(diagnostic('configuration'), ensure_ascii=False), flush=True)
        print(json.dumps({'type':'error','error':'DSH_LAUNCH_FAILED'}), flush=True)
        sys.exit(2)
