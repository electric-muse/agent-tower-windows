#!/usr/bin/env python3
# agent-tower 单入口: 一个进程内跑 遥控(ctl) + 播报(sync) 双循环
# 用法: tower.py [--selftest]  (守护模式为默认)
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import setproctitle
    setproctitle.setproctitle("agent-tower")
except ImportError:
    pass

import tower_ctl as ctl    # noqa: E402
import tower_sync as sync  # noqa: E402


def _safe(fn, name):
    try:
        fn()
        raise RuntimeError(f"{name} loop returned unexpectedly")
    except Exception as e:  # 任一循环崩溃 => 进程退出交给launchd重启
        ctl.log(f"FATAL {name} loop died: {e}")
        os._exit(3)


def selftest():
    ctl.selftest()
    print("sync module OK:", bool(sync.STATE and sync.LOG))


def main():
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    t = threading.Thread(target=_safe, args=(sync.scan_dir, "sync"),
                         name="tower-sync", daemon=True)
    t.start()
    ctl.log("tower: sync loop thread started")
    _safe(ctl.main_loop, "ctl")  # ctl主循环占主线程


if __name__ == "__main__":
    if "--selftest" not in sys.argv:
        from tower_runtime import install_runtime
        install_runtime(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    main()
