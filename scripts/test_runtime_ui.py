"""Runtime, authorization and Feishu UI regressions. All IO is temporary/mocked."""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import queue
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

os.environ["SYNC_DRY"] = "1"
import tower_ctl as ctl
import tower_sync as sync
import tower
from tower_ui import format_title, normalize_title, title_config
from render_launchd import render


class StopLoop(BaseException):
    pass


class OneQueue:
    def __init__(self, items): self.items = list(items)
    def get(self):
        if not self.items: raise StopLoop()
        return self.items.pop(0)
    def put(self, item): self.items.append(item)


class RuntimeUI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="runtime-ui-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = {**ctl.DEF, "allow_ou": "ou_test", "work_roots": str(self.root),
                       "auto_watch": 0, "rate_n": 100, "max_run_min": 0}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for key, value in {"conf": lambda: self.config, "_wiz": {}, "_rate": {},
                           "_root_retry": {}, "_PROJ": {"ts": 0, "list": []},
                           "STATED": str(self.root), "HOME": str(self.root),
                           "JOBSF": str(self.root / "jobs.json"),
                           "HB": str(self.root / "heartbeat"),
                           "log": Mock(), "audit": Mock(), "reply": Mock(),
                           "api": Mock(return_value={"code": 0}), "tsend": Mock()}.items():
            self.stack.enter_context(patch.object(ctl, key, value))
        ctl.jobs_save({})

    def process(self, *items):
        with patch.object(ctl, "Q", OneQueue(items)), self.assertRaises(StopLoop):
            ctl.worker()

    def job(self, jid="j1", **kw):
        jd = {"jid": jid, "cwd": str(self.root), "state": "exited", "title": "旧标题",
              "prompt": "测试任务", "root_mid": "om_root", "chat_id": "oc_test",
              "exit": "0", "sess": "fixture", "agent": "codex", **kw}
        ctl.jobs_save({jid: jd})
        return jd

    def test_unified_entry_starts_all_threads_and_polls_repeatedly(self):
        import lark_oapi as lark
        clock = types.SimpleNamespace(value=1000, steps=0)
        threads = []
        class Thread:
            def __init__(self, target, name=None, **kw):
                self.target, self.name, self.started = target, name, False
                threads.append(self)
            def start(self): self.started = True
            def is_alive(self): return self.started
        def sleep(seconds):
            clock.value += seconds; clock.steps += 1
            if clock.steps >= 20: raise StopLoop()
        with patch.object(lark.ws, "Client", return_value=types.SimpleNamespace(start=lambda: None)), \
             patch.object(ctl, "app", return_value={"app_id": "fixture", "app_secret": "fixture"}), \
             patch.object(ctl.threading, "Thread", Thread), \
             patch.object(ctl.time, "monotonic", side_effect=lambda: clock.value), \
             patch.object(ctl.time, "sleep", side_effect=sleep), \
             patch.object(ctl, "poll_jobs") as poll, patch.object(ctl, "refresh_sess") as refresh, \
             patch.object(ctl, "archive_stale_topics"), patch.object(ctl, "auto_watch_tick"), \
             patch.object(ctl, "refresh_observed_titles") as observed, \
             patch.object(tower.sys, "argv", ["tower.py"]), self.assertRaises(StopLoop):
            tower.main()
        self.assertEqual({t.name for t in threads if t.started}, {"tower-sync", "tower-ws", "tower-worker"})
        self.assertEqual(poll.call_count, 5)
        self.assertEqual(refresh.call_count, 5)
        self.assertEqual(observed.call_count, 5)

    def test_dead_worker_fails_main_loop(self):
        import lark_oapi as lark
        thread = Mock(); thread.is_alive.return_value = False
        with patch.object(lark.ws, "Client"), patch.object(ctl.threading, "Thread", return_value=thread), \
             patch.object(ctl, "app", return_value={"app_id": "x", "app_secret": "x"}), \
             self.assertRaisesRegex(RuntimeError, "关键线程"):
            ctl.main_loop()

    def test_unexpected_loop_return_exits_for_supervisor(self):
        with patch.object(tower.os, "_exit", side_effect=StopLoop) as exit_, self.assertRaises(StopLoop):
            tower._safe(lambda: None, "test")
        exit_.assert_called_once_with(3)

    def test_symlink_cannot_escape_roots_or_project_lookup(self):
        allowed = self.root / "allowed"; allowed.mkdir()
        outside = self.root / "outside"; (outside / ".git").mkdir(parents=True)
        (allowed / "linked").symlink_to(outside, target_is_directory=True)
        self.config["work_roots"] = str(allowed)
        self.assertFalse(ctl.allowed_cwd(str(allowed / "linked")))
        self.assertEqual(ctl.projects(), [])
        self.assertIsNone(ctl.resolve_target("outside"))
        with self.assertRaises(ValueError): ctl.require_cwd(str(outside))

    def test_cached_projects_rechecked_after_roots_change(self):
        ctl._PROJ.update(ts=ctl.time.time(), list=[("root", str(self.root), 0)])
        self.config["work_roots"] = str(self.root / "other")
        self.assertEqual(ctl.projects(), [])

    def test_every_process_launch_rechecks_directory(self):
        tree = ast.parse(Path(ctl.__file__).read_text())
        launches = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute) and n.func.attr == "Popen"]
        self.assertEqual(len(launches), 8)  # go/goal/kick share one validated spawn_resume
        self.assertIn("require_cwd", __import__("inspect").getsource(ctl.spawn_resume))
        self.assertIn("require_cwd", __import__("inspect").getsource(ctl._start_dsh))
        for call in launches:
            cwd = next(k.value for k in call.keywords if k.arg == "cwd")
            self.assertIsInstance(cwd, ast.Call)
            self.assertEqual(cwd.func.id, "require_cwd")

    def test_explicit_invalid_path_does_not_fall_back(self):
        with patch.object(ctl, "projects", return_value=[("recent", str(self.root), 0)]), \
             patch.object(ctl, "known_models", return_value=[]), patch.object(ctl.subprocess, "Popen") as proc:
            ctl.cmd_new("ou_test", "oc_test", [str(self.root / "missing"), "任务"])
        proc.assert_not_called()
        self.assertIn("指定目录", ctl.reply.call_args.args[1])

    def test_revoked_wizard_cannot_launch(self):
        ctl.wiz_set("ou_revoked", "oc_test", step="prompt", cwd=str(self.root))
        with patch.object(ctl, "cmd_new") as new:
            self.process(("ou_revoked", "oc_test", "开始任务", "", ""))
        new.assert_not_called()
        self.assertIn("无权", ctl.reply.call_args.args[1])

    def test_wizard_respects_rate_limit(self):
        ctl.wiz_set("ou_test", "oc_test", step="prompt", cwd=str(self.root))
        self.config["rate_n"] = 0
        with patch.object(ctl, "cmd_new") as new:
            self.process(("ou_test", "oc_test", "开始任务", "", ""))
        new.assert_not_called()

    def test_sdk_callback_context_enqueues_and_toasts(self):
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger
        event = P2CardActionTrigger({"event": {"operator": {"open_id": "ou_test"},
            "action": {"value": {"act": "job_detail", "jid": "qj1"}},
            "context": {"open_chat_id": "oc_test", "open_message_id": "om_root"}}})
        with patch.object(ctl, "Q", queue.Queue()) as q:
            response = ctl.on_card_action(event)
            self.assertEqual(q.get_nowait(), ("__ui__", "ou_test", "oc_test", {"act": "job_detail", "jid": "qj1"}, "om_root"))
        self.assertEqual(response["toast"]["type"], "info")

    def test_button_rate_limit(self):
        self.config["rate_n"] = 0
        with patch.object(ctl, "cmd_jobs") as jobs:
            ctl.handle_ui("ou_test", "oc_test", {"act": "job_detail", "jid": "j1"})
        jobs.assert_not_called()

    def test_thread_title_routing_all_families(self):
        for jid in ("j1", "cj2", "wj3", "qj4"):
            with self.subTest(jid=jid), patch.object(ctl, "thread_to_jid", return_value=jid), \
                 patch.dict(ctl.CMDS, {"title": Mock()}) as cmds:
                self.process(("ou_test", "oc_test", f"#title {jid} 显式标题", "", "omt_test"),
                             ("ou_test", "oc_test", "#title 自动标题", "", "omt_test"))
                self.assertEqual(cmds["title"].call_args_list[0].args[2], [jid, "显式标题"])
                self.assertEqual(cmds["title"].call_args_list[1].args[2], [jid, "自动标题"])

    def test_title_tokens_with_spaces_and_wizard_input(self):
        expected = ("带 空格标题", "safe", "plan", ["don't", "change", "code"])
        text = '''标题="带 空格标题" 权限=safe 模式=plan don't change code'''
        self.assertEqual(ctl.parse_new_tokens(text.split()), expected)
        wizard = ctl.parse_new_tokens([text])
        self.assertEqual(wizard[:3], expected[:3])
        self.assertEqual(" ".join(wizard[3]), " ".join(expected[3]))
        multiline = "修复代码：\ndef f():\n    return 1"
        self.assertEqual(ctl.parse_new_tokens([multiline])[3], [multiline])
        self.assertEqual(ctl.parse_new_tokens(["标题=多行 " + multiline])[3], [multiline])
        self.assertEqual(ctl.parse_new_tokens(["模式=planet", "hi"])[2:], ("", ["模式=planet", "hi"]))

    def test_rename_patches_root_and_keeps_controls(self):
        self.job()
        ctl.cmd_title("ou_test", "oc_test", ["j1", "新的 标题"])
        jd = ctl.jobs_load()["j1"]
        self.assertEqual(jd["title"], "新的 标题")
        self.assertNotIn("root_update_pending", jd)
        method, path, payload = ctl.api.call_args.args
        self.assertEqual((method, path), ("PATCH", "/im/v1/messages/om_root"))
        card = json.loads(payload["content"])
        self.assertIn("新的 标题", card["header"]["title"]["content"])
        raw = json.dumps(card, ensure_ascii=False)
        self.assertIn("修改标题", raw)
        self.assertIn("pick_job", raw)

    def test_rename_failure_remains_pending_and_poll_retries(self):
        self.job()
        ctl.api.return_value = None
        ctl.cmd_title("ou_test", "oc_test", ["j1", "新标题"])
        self.assertTrue(ctl.jobs_load()["j1"]["root_update_pending"])
        self.assertIn("待自动同步", ctl.reply.call_args.args[1])
        ctl.api.return_value = {"code": 0}
        ctl.poll_jobs()
        self.assertNotIn("root_update_pending", ctl.jobs_load()["j1"])

    def test_persistent_root_not_replaced_by_click_receipt(self):
        self.job()
        ctl.mark_card("om_root", "处理中", "测试")
        ctl.api.assert_not_called()
        ctl.mark_card("om_picker", "处理中", "测试")
        self.assertEqual(ctl.api.call_args.args[:2], ("PATCH", "/im/v1/messages/om_picker"))

    def test_rename_button_wizard_dispatch(self):
        self.job()
        ctl.handle_ui("ou_test", "oc_test", {"act": "pick_job", "jid": "j1", "kind": "title"}, "om_root")
        self.assertEqual(ctl.wiz_get("ou_test")["act"], "title")
        self.process(("ou_test", "oc_test", "手机改名 含空格", "", ""))
        self.assertEqual(ctl.jobs_load()["j1"]["title"], "手机改名 含空格")

    def test_new_same_project_retains_agent(self):
        for agent, fam in (("codex", "codex"), ("claude", "claude"), ("wcodebuddy", "wcb"), ("qoder", "qoder")):
            with self.subTest(agent=agent):
                jd = self.job(agent=agent)
                _, els, _ = ctl.root_card_parts("j1", jd)
                values = [b["behaviors"][0]["value"] for row in els if row["tag"] == "column_set"
                          for col in row["columns"] for b in col["elements"] if b["tag"] == "button"]
                value = next(v for v in values if v["act"] == "new_in_proj")
                self.assertEqual(value["fam"], fam)
                with patch.object(ctl, "action_card", return_value="om_prompt"):
                    ctl.handle_ui("ou_test", "oc_test", value, "om_root")
                self.assertEqual(ctl.wiz_get("ou_test")["fam"], fam)

    def test_button_rows_have_no_extra_empty_row(self):
        self.assertEqual(ctl.btn_row(), [])
        for count in range(1, 7):
            rows = ctl.btn_row(*(ctl.btn("测试", "test", {}) for _ in range(count)))
            self.assertEqual(len(rows), (count + 1) // 2)
            self.assertTrue(all(len(row["columns"]) == 2 for row in rows))

    def test_wizard_displays_configured_permission(self):
        self.config["default_perm"] = "safe"
        with patch.object(ctl, "action_card", return_value="om_prompt") as card:
            ctl.ui_prompt_fam("ou_test", "oc_test", "qoder", str(self.root))
        self.assertIn("safe(默认)", str(card.call_args))
        self.assertNotIn("full(默认)", str(card.call_args))

    def test_title_template_fallback_and_limits(self):
        values = dict(icon="🚀", jid="j1", title="任务", project="demo", agent="codex")
        self.assertEqual(format_title("topic", {"topic_title_template": "{project} / {title}"}, **values), "demo / 任务")
        for invalid in ("{unknown}", "{title.__class__}", "{title:999999999}", "{", "   "):
            self.assertEqual(format_title("topic", {"topic_title_template": invalid}, **values), "🚀 j1 · 任务")
        self.assertEqual(normalize_title(" a\n b "), "a b")
        self.assertEqual(len(normalize_title("字" * 100, {"title_max_len": 999})), 80)
        self.assertEqual(len(normalize_title("字" * 100, {"title_max_len": "invalid"})), 24)

    def test_title_config_hot_reload(self):
        path = self.root / "ctl.conf"
        path.write_text("title_max_len=30\nstream_title_template={project} · {title}\n")
        self.assertEqual(title_config(str(path))["title_max_len"], "30")
        path.write_text("title_max_len=48\nstream_title_template={title}\n")
        self.assertEqual(title_config(str(path))["title_max_len"], "48")

    def test_stream_uses_custom_title_for_all_families(self):
        (self.root / "ctl.conf").write_text("stream_title_template={project} / {title} / {jid}\n")
        with patch.object(sync, "CFGD", str(self.root)):
            for jid in ("j1", "cj1", "wj1", "qj1"):
                with patch.object(sync, "jid_of", return_value=jid), \
                     patch.object(sync, "jobs_raw", return_value={jid: {"title": "自定义"}}):
                    self.assertEqual(sync.title_of("fixture", {"proj": "demo", "task": "旧名"}), f"demo / 自定义 / {jid}")

    def test_goal_done_requires_last_line_and_success(self):
        for final, code, done in (("尚未达到 GOAL_DONE 条件", "0", False),
                                  ("GOAL_DONE\n仍未完成", "0", False),
                                  ("完成\nGOAL_DONE\n", "0", True),
                                  ("GOAL_DONE", "1", False)):
            with self.subTest(final=final, code=code):
                jd = self.job(exit=code, goal={"text": "目标", "active": True, "max": 1})
                with patch.object(ctl, "final_text", return_value=final):
                    ctl.goal_tick(jd)
                self.assertEqual("目标完成" in str(ctl.tsend.call_args), done)

    def test_manual_stop_and_timeout_do_not_auto_retry(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                rc = self.root / "stop.rc"; rc.write_text("130")
                self.job(state="stopping", pid=123, rc_file=str(rc), timeout_killed=timeout,
                         goal={"active": True, "text": "不应续跑"})
                with patch.object(ctl, "alive", return_value=False), patch.object(ctl, "notify_done"), \
                     patch.object(ctl, "goal_tick") as goal:
                    ctl.poll_jobs()
                saved = ctl.jobs_load()["j1"]
                self.assertEqual(saved["state"], "stopped")
                self.assertNotIn("retry_at", saved)
                self.assertFalse(saved["goal"]["active"])
                goal.assert_not_called()

    def test_stop_cancels_pending_and_queued_retry(self):
        self.job(retry_at=ctl.time.time() + 60, retry_cnt=1)
        ctl.cmd_stop("ou_test", "oc_test", ["j1"])
        with patch.object(ctl, "cmd_go") as go:
            self.process(("__retry__", "j1"))
        go.assert_not_called()
        self.assertNotIn("retry_at", ctl.jobs_load()["j1"])
        self.assertEqual(ctl.jobs_load()["j1"]["state"], "stopped")

    def test_automatic_resume_preserves_retry_budget(self):
        self.job(retry_cnt=2, first_dead_see=1, timeout_killed=True)
        with patch.object(ctl.subprocess, "Popen", return_value=types.SimpleNamespace(pid=777)), \
             patch.object(ctl.threading, "Thread"):
            ctl.cmd_go("ctl-retry", "oc_test", ["j1", "重试"])
        saved = ctl.jobs_load()["j1"]
        self.assertEqual(saved["retry_cnt"], 2)
        self.assertNotIn("first_dead_see", saved)
        self.assertNotIn("timeout_killed", saved)
        Path(saved["rc_file"]).write_text("1")
        with patch.object(ctl, "alive", return_value=False), patch.object(ctl, "notify_done"):
            ctl.poll_jobs()
        self.assertNotIn("retry_at", ctl.jobs_load()["j1"])

    def test_missing_rc_grace_starts_at_observed_death(self):
        self.job(state="running", pid=123, started=1, rc_file=str(self.root / "missing"))
        with patch.object(ctl, "alive", return_value=False), patch.object(ctl.time, "time", return_value=1000), \
             patch.object(ctl, "notify_done"):
            ctl.poll_jobs()
        saved = ctl.jobs_load()["j1"]
        self.assertEqual(saved["state"], "running")
        self.assertEqual(saved["first_dead_see"], 1000)
        with patch.object(ctl, "alive", return_value=False), patch.object(ctl.time, "time", return_value=1091), \
             patch.object(ctl, "notify_done"):
            ctl.poll_jobs()
        self.assertEqual(ctl.jobs_load()["j1"]["exit"], "lost")

    def test_each_round_has_independent_exit_code(self):
        old, new = ctl.new_round_rc("j1"), ctl.new_round_rc("j1")
        self.assertNotEqual(old, new)
        ctl._reap(types.SimpleNamespace(wait=lambda: 0), new)
        ctl._reap(types.SimpleNamespace(wait=lambda: 130), old)
        self.assertEqual(Path(new).read_text(), "0")
        self.assertEqual(Path(old).read_text(), "130")

    def test_cleanup_only_selects_own_round_artifacts(self):
        own = [Path(ctl.new_round_rc("j1")) for _ in range(2)]
        other = Path(ctl.new_round_rc("j10"))
        for p in own + [other]: p.write_text("0")
        self.assertEqual(ctl.job_artifacts({"jid": "j1"}), {str(p) for p in own})

    def test_plist_uses_checkout_and_escapes_xml(self):
        checkout = self.root / "repo & with spaces"
        (checkout / "launchd").mkdir(parents=True)
        source = Path(__file__).resolve().parent.parent / "launchd/agent-tower.plist.example"
        (checkout / "launchd/agent-tower.plist.example").write_bytes(source.read_bytes())
        data = plistlib.loads(render(checkout, self.root, "test-user"))
        self.assertEqual(data["ProgramArguments"][0], str(checkout / ".venv/bin/python"))
        self.assertEqual(data["Label"], "com.test-user.agent-tower")

    def test_selftest_rejects_missing_credentials(self):
        with patch.object(ctl, "app", return_value={"app_id": "fixture"}), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "app_id/app_secret"):
            ctl.selftest()


if __name__ == "__main__":
    unittest.main(verbosity=2)
