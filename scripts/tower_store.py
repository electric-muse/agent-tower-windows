"""Small shared JSON maps: merge under a process/thread lock, publish atomically.

Existing JSON state stays readable; malformed state is never silently overwritten.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading

from tower_runtime import private_json
from tower_platform import lock_file, unlock_file

_LOCK = threading.RLock()
_PATH_LOCKS = {}


def path_lock(path):
    with _LOCK:
        return _PATH_LOCKS.setdefault(str(Path(path).resolve()), threading.RLock())


class JsonMap:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        try:
            with self.path.open(encoding='utf-8') as file:
                value = json.load(file)
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict):
            raise ValueError('state map is not an object')
        return value

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path_lock(self.path):
            with os.fdopen(os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600), 'r+b') as file:
                lock_file(file)
                try:
                    yield
                finally:
                    unlock_file(file)

    def update(self, change):
        with self.locked():
            value = self.read()
            result = change(value)
            private_json(self.path, value)
            return result

    def put(self, key, value):
        return self.update(lambda data: data.__setitem__(key, value))


def register_route(path, mid, entry):
    if mid:
        JsonMap(path).put(mid, entry)


def prune_routes(path, alive):
    def change(data):
        dead = [mid for mid, entry in data.items()
                if entry.get('kind') == 'job' and entry.get('jid') not in alive]
        for mid in dead:
            del data[mid]
        return len(dead)
    return JsonMap(path).update(change)
