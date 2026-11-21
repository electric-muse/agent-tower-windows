"""Small OS-specific primitives used by the Tower runtime."""
import os
from pathlib import Path
import subprocess


IS_WINDOWS = os.name == 'nt'


def lock_file(file, *, nonblocking=False):
    if not IS_WINDOWS:
        import fcntl
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        fcntl.flock(file.fileno(), flags)
        return
    import msvcrt
    file.seek(0)
    try:
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK if nonblocking else msvcrt.LK_LOCK, 1)
    except OSError as error:
        if nonblocking:
            raise BlockingIOError from error
        raise


def unlock_file(file):
    if not IS_WINDOWS:
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        return
    import msvcrt
    file.seek(0)
    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)


def process_flags():
    return ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS
            else {'start_new_session': True})


def stop_tree(pid):
    if IS_WINDOWS:
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                       capture_output=True, timeout=15, check=False)
        return
    import signal
    import time
    for sig, wait_s in ((signal.SIGINT, 8), (signal.SIGTERM, 4), (signal.SIGKILL, 2)):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        end = time.time() + wait_s
        while time.time() < end:
            try:
                os.kill(pid, 0)
                time.sleep(0.4)
            except ProcessLookupError:
                return


def codex_executable():
    """Use the native exe on Windows so prompts never pass through cmd.exe."""
    import shutil
    if not IS_WINDOWS:
        return shutil.which('codex') or '/opt/homebrew/bin/codex'
    npm = Path(os.environ.get('APPDATA', '')) / 'npm/node_modules/@openai/codex/node_modules'
    matches = sorted(npm.glob('@openai/codex-win32-*/vendor/*/bin/codex.exe'))
    return str(matches[0]) if matches else ''


def open_codex_thread(session_id):
    """Activate only a canonical local thread link, without passing shell text."""
    import uuid
    import sys
    try:
        canonical = str(uuid.UUID(session_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Invalid Codex desktop thread ID') from None
    uri = f'codex://threads/{canonical}'
    if IS_WINDOWS:
        os.startfile(uri)
    else:
        command = 'open' if sys.platform == 'darwin' else 'xdg-open'
        try:
            subprocess.run([command, uri], capture_output=True, timeout=5, check=True)
        except (OSError, subprocess.SubprocessError):
            raise OSError('Codex desktop link could not be opened') from None
