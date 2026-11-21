#!/usr/bin/env python3
"""Render launchd paths using plistlib so spaces and XML characters stay valid."""
import getpass
import os
from pathlib import Path
import plistlib
import sys


def render(root, home, user, watchdog=False, uid=None):
    name = 'agent-tower-watchdog.plist.example' if watchdog else 'agent-tower.plist.example'
    with (Path(root) / 'launchd' / name).open("rb") as f:
        data = plistlib.load(f)
    def replace(value):
        if isinstance(value, str):
            return value.replace("__ROOT__", str(root)).replace("__HOME__", str(home)).replace("__USER__", user).replace('__UID__', str(os.getuid() if uid is None else uid))
        if isinstance(value, list):
            return [replace(v) for v in value]
        if isinstance(value, dict):
            return {k: replace(v) for k, v in value.items()}
        return value
    return plistlib.dumps(replace(data))


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    target = Path(sys.argv[1])
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = render(root, Path.home(), getpass.getuser(), watchdog='--watchdog' in sys.argv[2:])
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(target)
