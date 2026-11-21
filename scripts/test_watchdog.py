"""Offline supervisor regression; recovery commands are always mocked."""
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from tower_watchdog import check
from render_launchd import render


class Watchdog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'state').mkdir()
        (self.root / 'logs').mkdir()
        self.runner = Mock(return_value=Mock(returncode=0))

    def snapshot(self, now, **changes):
        data = dict.fromkeys(['updated_at', 'ctl_progress_at', 'sync_progress_at', 'sender_progress_at'], now)
        data.update(changes)
        (self.root / 'state/health.json').write_text(json.dumps(data))

    def probe(self, now, recover=True):
        return check(self.root, 'gui/501/com.fixture.agent-tower', recover, now, self.runner)

    def test_two_stale_checks_recover_once_then_cooldown(self):
        self.snapshot(1000)
        self.assertEqual(self.probe(1400), 'suspected')
        self.assertEqual(self.probe(1460), 'restarted')
        self.assertEqual(self.probe(1520), 'confirmed')
        self.runner.assert_called_once()
        self.assertEqual(self.runner.call_args.args[0], ['/bin/launchctl', 'kickstart', '-k', 'gui/501/com.fixture.agent-tower'])

    def test_host_sleep_requires_new_confirmation(self):
        self.snapshot(1000)
        self.probe(1400)
        self.assertEqual(self.probe(30000), 'suspected')
        self.runner.assert_not_called()
        self.assertEqual(self.probe(30060), 'restarted')

    def test_interval_jitter_still_confirms_stall(self):
        self.snapshot(1000)
        self.probe(1400)
        self.assertEqual(self.probe(1459.5), 'restarted')

    def test_fresh_snapshot_does_not_hide_stalled_loop(self):
        self.snapshot(1400, sync_progress_at=1000)
        self.probe(1400)
        self.snapshot(1460, sync_progress_at=1000)
        self.assertEqual(self.probe(1460), 'restarted')

    def test_healthy_reset_and_disabled_sender(self):
        self.snapshot(1000)
        self.probe(1400)
        self.snapshot(1460, sender_required=False, sender_progress_at=1000, ws_connected=False)
        self.assertEqual(self.probe(1460), 'healthy')
        self.snapshot(1000)
        self.assertEqual(self.probe(1520), 'suspected')
        self.runner.assert_not_called()

    def test_check_mode_never_restarts(self):
        self.snapshot(1000)
        self.probe(1400, False)
        self.assertEqual(self.probe(1460, False), 'confirmed')
        self.runner.assert_not_called()

    def test_missing_health_and_restart_failure_are_bounded(self):
        self.assertEqual(self.probe(1400), 'suspected')
        self.runner.side_effect = subprocess.TimeoutExpired('launchctl', 20)
        self.assertEqual(self.probe(1460), 'restart_failed')
        self.probe(1520)
        self.runner.assert_called_once()

    def test_corrupt_probe_is_not_overwritten(self):
        path = self.root / 'state/watchdog-probe.json'
        path.write_text('corrupt')
        with self.assertRaises(ValueError):
            self.probe(1400)
        self.assertEqual(path.read_text(), 'corrupt')
        self.runner.assert_not_called()

    def test_watchdog_plist_is_independent_and_uses_correct_target(self):
        repo = Path(__file__).resolve().parent.parent
        data = plistlib.loads(render(repo, self.root, 'fixture', watchdog=True, uid=501))
        self.assertEqual(data['Label'], 'com.fixture.agent-tower-watchdog')
        self.assertEqual(data['StartInterval'], 60)
        self.assertNotIn('KeepAlive', data)
        self.assertIn('gui/501/com.fixture.agent-tower', data['ProgramArguments'])
        self.assertIn('--recover', data['ProgramArguments'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
