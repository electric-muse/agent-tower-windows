import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tower_platform import IS_WINDOWS, codex_executable, lock_file, process_flags, stop_tree, unlock_file
from tower_store import JsonMap


class PlatformTests(unittest.TestCase):
    def test_json_map_uses_a_file_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            store = JsonMap(path)
            for value in range(3):
                store.put('count', value)
            self.assertEqual(store.read(), {'count': 2})
            with (Path(directory) / 'lock').open('w+b') as file:
                lock_file(file)
                try:
                    with (Path(directory) / 'lock').open('r+b') as other:
                        with self.assertRaises(BlockingIOError):
                            lock_file(other, nonblocking=True)
                finally:
                    unlock_file(file)

    def test_spawn_and_stop(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                   stdout=subprocess.DEVNULL, **process_flags())
        try:
            self.assertIsNone(process.poll())
            stop_tree(process.pid)
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()

    @unittest.skipUnless(IS_WINDOWS, 'Windows Codex native binary discovery')
    def test_windows_codex_binary(self):
        binary = codex_executable()
        if not binary:
            self.skipTest('Codex CLI is not installed')
        self.assertTrue(Path(binary).is_file())
        result = subprocess.run([binary, '--version'], capture_output=True,
                                text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('codex', result.stdout.lower())


if __name__ == '__main__':
    unittest.main()
