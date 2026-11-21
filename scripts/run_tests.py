#!/usr/bin/env python3
"""Run all offline regressions in a disposable checkout, never with live credentials."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    repo = Path(__file__).resolve().parent.parent
    failed = []
    with tempfile.TemporaryDirectory(prefix="tower-tests-") as tmp:
        root = Path(tmp)
        shutil.copytree(repo / "scripts", root / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(repo / "launchd", root / "launchd", ignore=shutil.ignore_patterns("*.plist"))
        for name in ("config", "state", "logs", "home", "tmp", "guard"):
            (root / name).mkdir()
        for name in ("ctl.conf", "stream.conf"):
            shutil.copy(repo / "config" / (name + ".example"), root / "config" / name)
        shutil.copy(repo / "config/tool-glossary.json", root / "config/tool-glossary.json")
        # Temporary directories on macOS resolve under /private/var, not /tmp.
        with (root / "config/ctl.conf").open("a") as f:
            f.write(f"\nwork_roots={root}\nauto_watch=0\n")
        (root / "guard/sitecustomize.py").write_text(
            "import socket\n"
            "def blocked(*args, **kwargs):\n"
            "    raise RuntimeError('offline test: network connection blocked')\n"
            "socket.socket.connect = blocked\n"
            "socket.socket.connect_ex = blocked\n"
            "socket.create_connection = blocked\n")
        env = {**os.environ, "HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
               "SYNC_DRY": "1", "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": str(root / "guard"),
               "CODEX_SESS_DIR": str(root / "home/.codex/sessions"),
               "SYNC_STATE": str(root / "state/sync-state.json")}
        # E2E fixtures choose their own state path relative to the copied module.
        env.pop("SYNC_STATE")
        tests = sorted((root / "scripts").glob("test_*.py"))
        for test in tests:
            try:
                result = subprocess.run([sys.executable, str(test)], cwd=root, env=env,
                                        capture_output=True, text=True, timeout=120)
                output = result.stdout + result.stderr
                summaries = [line for line in output.splitlines()
                             if "PASS" in line or "Ran " in line or line == "OK"]
                print(f"{'PASS' if result.returncode == 0 else 'FAIL'} {test.name}: "
                      + " | ".join(summaries), flush=True)
                if result.returncode:
                    failed.append(test.name)
                    print(output[-14000:], flush=True)
            except subprocess.TimeoutExpired:
                failed.append(test.name)
                print(f"FAIL {test.name}: timeout", flush=True)
        print(f"Suites: {len(tests)-len(failed)}/{len(tests)} passed", flush=True)
    return bool(failed)


if __name__ == "__main__":
    sys.exit(main())
