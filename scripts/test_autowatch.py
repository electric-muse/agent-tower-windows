"""Offline regressions for UUID prefix rollover and watch routing."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import tower_ctl as ctl
import tower_sync as sync


class AutoWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def transcript(self, sid):
        path = self.root / '.codex/sessions/2026/10/07' / f'rollout-2026-10-07T10-00-00-{sid}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(' ' * (51 * 1024))
        return str(path)

    def test_find_and_route_any_uuid_prefix(self):
        for prefix in ('01a0abcd', '01a1abcd', '01a2abcd', 'deadbeef'):
            sid = prefix + '-1111-7111-8111-111111111111'
            path = self.transcript(sid)
            with patch.dict(os.environ, HOME=str(self.root)), patch.object(ctl.harness, 'files', return_value=[]):
                self.assertEqual(ctl.find_rollout(prefix), (path, sid))
                self.assertEqual(ctl.find_rollout(sid), (path, sid))
            self.assertEqual(sync.sess_uuid_of(path), sid)
            with patch.object(sync, 'jid_map', return_value={sid: 'j1'}):
                self.assertEqual(sync.jid_of(path), 'j1')
            with patch.object(sync, 'sess_topics', return_value={sid: 'root'}):
                self.assertEqual(sync.value_root(('create', path)), 'root')

    def test_fork_keeps_parent_identity_and_child_job_lookup(self):
        parent = '01a1abcd-1111-7111-8111-111111111111'
        child = '01a2abcd-2222-7222-8222-222222222222'
        path = self.transcript(parent + '_' + child)
        self.assertEqual(sync.sess_uuid_of(path), parent)
        with patch.object(sync, 'jid_map', return_value={child: 'j2'}):
            self.assertEqual(sync.jid_of(path), 'j2')

    def test_active_new_prefix_reaches_watch(self):
        sid = '01a1abcd-1111-7111-8111-111111111111'
        self.transcript(sid)
        with patch.dict(os.environ, HOME=str(self.root)), \
             patch.object(ctl, 'HOME', str(self.root)), \
             patch.object(ctl.harness, 'files', return_value=[]), \
             patch.object(ctl, 'jobs_load', return_value={}), \
             patch.object(ctl, 'STATED', str(self.root)), \
             patch.object(ctl, 'conf', return_value=dict(ctl.DEF)), \
             patch.object(ctl, 'app', return_value={'chat_id': 'fixture'}), \
             patch.object(ctl, 'watch_session', return_value=('root', 'thread')) as watch, \
             patch.object(ctl, 'log'):
            ctl.auto_watch_tick()
            watch.assert_called_once_with('fixture', sid, sender='auto-watch', quiet=True)

    def test_creation_failure_is_logged(self):
        path = self.transcript('01a1abcd-1111-7111-8111-111111111111')
        with patch.object(ctl, 'conf', return_value=dict(ctl.DEF)), \
             patch.object(ctl, '_scan_active', return_value=[('🖥', '01a1abcd', '', '', time.time(), False)]), \
             patch.object(ctl, 'find_rollout', return_value=(path, 'sid')), \
             patch.object(ctl, 'app', return_value={'chat_id': 'fixture'}), \
             patch.object(ctl, 'watch_session', return_value=(None, '建卡失败')), \
             patch.object(ctl, 'log') as log:
            ctl.auto_watch_tick()
            log.assert_called_once_with('auto-watch-failed 01a1abcd reason=建卡失败')


if __name__ == '__main__':
    unittest.main()
