#!/usr/bin/env python3
"""Independent launchd probe: two stale observations before an explicit recovery."""
import argparse
import json
from pathlib import Path
import subprocess
import time

from tower_runtime import private_json, safe_log
from tower_store import JsonMap

STALE_SECONDS = 300
CONFIRM_SECONDS = 45  # Allow scheduling jitter around launchd's 60-second interval.
MAX_PROBE_GAP = 180
COOLDOWN_SECONDS = 600


def stalled(snapshot, now):
    fields = ['updated_at', 'ctl_progress_at', 'sync_progress_at']
    if snapshot.get('sender_required', True):
        fields.append('sender_progress_at')
    return any(not isinstance(snapshot.get(key), (int, float)) or
               not 0 <= now - snapshot[key] <= STALE_SECONDS for key in fields)


def check(root, label, recover=False, now=None, runner=subprocess.run):
    root = Path(root)
    now = time.time() if now is None else now
    store = JsonMap(root / 'state/watchdog-probe.json')
    with store.locked():
        probe = store.read()
        try:
            snapshot = json.loads((root / 'state/health.json').read_text())
            if not isinstance(snapshot, dict):
                snapshot = {}
        except (OSError, ValueError):
            snapshot = {}
        previous = probe.get('checked_at', 0)
        continuous = CONFIRM_SECONDS <= now - previous <= MAX_PROBE_GAP
        unhealthy = stalled(snapshot, now)
        # A long probe gap may be host sleep; require a fresh confirmation after wake.
        confirmed = unhealthy and probe.get('stalled', False) and continuous
        ready = confirmed and now - probe.get('recovery_at', 0) >= COOLDOWN_SECONDS
        result = 'healthy' if not unhealthy else 'confirmed' if confirmed else 'suspected'
        probe.update(checked_at=now, stalled=unhealthy)
        if recover and ready:
            # Persist cooldown first, even if launchctl fails; never create a restart storm.
            probe['recovery_at'] = now
            private_json(store.path, probe)
            try:
                action = runner(['/bin/launchctl', 'kickstart', '-k', label],
                                capture_output=True, timeout=20, check=False)
                result = 'restarted' if action.returncode == 0 else 'restart_failed'
            except (OSError, subprocess.TimeoutExpired):
                result = 'restart_failed'
            safe_log(root / 'logs/watchdog.log', f'watchdog {result}')
        private_json(store.path, probe)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recover', action='store_true', help='允许重启已注册的塔台服务')
    parser.add_argument('--label', required=True, help='完整launchd目标，例如gui/501/com.user.agent-tower')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    result = check(root, args.label, recover=args.recover)
    print('watchdog:', result)
    return 0 if result in ('healthy', 'restarted') else 1


if __name__ == '__main__':
    raise SystemExit(main())
