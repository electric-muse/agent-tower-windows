"""波次五修复的离线单测 — P0续跑断链/话题舱死锁 + P1鉴权后门/unwatch全清/单测隔离
全部副作用重定向到临时目录, 网络与子进程打桩, 不碰生产 state/ 与飞书。
"""
import sys, os, ast, json, time, tempfile, shutil, threading, subprocess, queue
os.environ.setdefault("SYNC_DRY", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tower_sync as fs
import tower_ctl as fc

TMP = tempfile.mkdtemp(prefix="wave5-state-")
fc.STATED = TMP
fc.JOBSF = os.path.join(TMP, "ctl-jobs.json")
fc.CMIDF = os.path.join(TMP, "card-mids.json")
fc._JID_SEQF = os.path.join(TMP, ".jid-seq")
fc.STOPICF = os.path.join(TMP, "sess-topics.json")
fc.LOG = os.path.join(TMP, "ctl.log")
fc.AUDIT = os.path.join(TMP, "ctl-audit.log")
fc.HB = os.path.join(TMP, "ctl.heartbeat")
fs.CMIDF = os.path.join(TMP, "card-mids.json")
fs.JOBSF = os.path.join(TMP, "ctl-jobs.json")
fs.TEXTSENT = os.path.join(TMP, ".text_sent")
fs.STATE = os.path.join(TMP, "sync-state.json")
fs.TOKF = os.path.join(TMP, ".token-cache.json")
fs.LOG = os.path.join(TMP, "sync.log")

PASS = []
def t(n, c, d=""):
    PASS.append((n, bool(c), d))

# ---------- 打桩 ----------
BASE_CONF = {**fc.DEF, "work_roots": TMP}
fc.conf = lambda: dict(BASE_CONF)
AUDITS = []
REPLIES = []
ORIG = {k: getattr(fc, k) for k in
        ("jobs_load", "jobs_save", "reply", "audit", "log", "conf", "tsend",
         "root_topic", "subprocess", "_kill_group", "_reap", "cmd_scan",
         "mark_card", "action_card", "api", "cmid_register", "find_sess")}

STORE = {}
def _load():
    return json.loads(json.dumps(STORE))
def _save(j):
    STORE.clear(); STORE.update(j)
fc.jobs_load = _load
fc.jobs_save = _save
fc.reply = lambda chat_id, md, **k: REPLIES.append((md, k.get("template")))
fc.audit = lambda s, x, r: AUDITS.append((s, x, r))
fc.log = lambda *a, **k: None
fc.tsend = lambda *a, **k: True
fc._reap = lambda p, rcf: None
fc.mark_card = lambda *a, **k: None
fc.cmid_register = lambda *a, **k: None

SESS = "0f3a1b2c-1111-2222-3333-444455556666"

class FakeProc:
    def __init__(self, pid=4242): self.pid = pid
class FakeSP:
    """顶掉 tower_ctl.subprocess: 记录命令, 不起真进程"""
    STDOUT = subprocess.STDOUT
    calls = []
    hook = None
    def Popen(self, cmd, **kw):
        FakeSP.calls.append((cmd, kw))
        if FakeSP.hook: FakeSP.hook()
        return FakeProc()

def restore():
    for k, v in ORIG.items():
        setattr(fc, k, v)

# =====================================================================
# W5-1: #go 对已结束任务必须真的续跑(此前整段被误嵌进 `if _pid:`)
# =====================================================================
STORE.clear()
STORE["j1"] = {"jid": "j1", "state": "exited", "pid": None, "sess": SESS,
               "cwd": TMP, "agent": "codex", "title": "旧任务", "perm": "safe"}
fc.subprocess = FakeSP()
FakeSP.calls = []; FakeSP.hook = None
TOPICS = []
fc.root_topic = lambda c, jid, jd: TOPICS.append(jid)
err = None
try:
    fc.cmd_go("ou_a", "oc_1", ["j1", "继续干活"])
except Exception as e:
    err = f"{type(e).__name__}: {e}"
t("W5-1 exited任务 #go 不抛异常", err is None, str(err))
t("W5-1 exited任务 #go 真的起了进程", len(FakeSP.calls) == 1,
  f"popen={len(FakeSP.calls)}")
t("W5-1 起进程后落盘 state=running/pid", STORE.get("j1", {}).get("state") == "running"
  and STORE["j1"].get("pid") == 4242, json.dumps(STORE.get("j1", {}), ensure_ascii=False)[:120])
t("W5-1 续跑指令进了 argv", FakeSP.calls and FakeSP.calls[0][0][-2:] == ["--", "继续干活"],
  str(FakeSP.calls[0][0][-3:]) if FakeSP.calls else "")
t("W5-1 未残留 exit/retry 脏字段",
  not any(k in STORE.get("j1", {}) for k in ("exit", "retry_cnt", "retry_at")))
t("W5-1 无root_mid时补建话题舱", TOPICS == ["j1"], str(TOPICS))

# W5-1b: 起进程期间的并发写入不能被锁内的陈旧快照覆盖
STORE.clear()
STORE["j1"] = {"jid": "j1", "state": "exited", "pid": None, "sess": SESS,
               "cwd": TMP, "agent": "codex"}
def _concurrent_writer():
    STORE["j2"] = {"jid": "j2", "state": "exited", "note": "poll期间写入"}
FakeSP.calls = []; FakeSP.hook = _concurrent_writer
err = None
try:
    fc.cmd_go("ou_a", "oc_1", ["j1", "x"])
except Exception as e:
    err = f"{type(e).__name__}: {e}"
t("W5-1b Popen期间的并发写入不被覆盖", "j2" in STORE and err is None,
  f"keys={sorted(STORE)} err={err}")
FakeSP.hook = None

def _go(args):
    """裸调 cmd_go 会把回归异常抛成 traceback(看不到汇总), 统一转成字符串"""
    try:
        fc.cmd_go("ou_a", "oc_1", args)
        return ""
    except Exception as e:
        return f"{type(e).__name__}: {e}"

# W5-1c: running 任务仍要先 kill 再续跑(W2-7 行为不回退)
STORE.clear()
STORE["j3"] = {"jid": "j3", "state": "running", "pid": 99999999, "sess": SESS,
               "cwd": TMP, "agent": "codex"}
KILLED = []
fc._kill_group = lambda pid, grace=8: KILLED.append(pid)
FakeSP.calls = []
e1c = _go(["j3", "换个方向"])
t("W5-1c running任务 #go 仍先kill再续跑",
  not e1c and KILLED == [99999999] and len(FakeSP.calls) == 1,
  f"err={e1c} killed={KILLED} popen={len(FakeSP.calls)}")
fc._kill_group = ORIG["_kill_group"]

# W5-1d: 无 session 时给明确回执, 不起空进程
STORE.clear()
STORE["j4"] = {"jid": "j4", "state": "exited", "pid": None, "sess": "", "cwd": TMP,
               "agent": "codex"}
fc.find_sess = lambda jid: ""
REPLIES.clear(); FakeSP.calls = []
e1d = _go(["j4", "x"])
t("W5-1d 无session→红卡回执且不起进程",
  not e1d and len(FakeSP.calls) == 0 and REPLIES and "没有可续跑" in REPLIES[-1][0],
  f"err={e1d} popen={len(FakeSP.calls)} replies={REPLIES[-1:]}")

# W5-1f: 显式权限既要进命令行也要落盘(重载落盘不能把它丢掉)
STORE.clear()
STORE["j6"] = {"jid": "j6", "state": "exited", "pid": None, "sess": SESS,
               "cwd": TMP, "agent": "codex", "perm": "full"}
FakeSP.calls = []
e1f = _go(["j6", "干活", "权限=safe"])
_cmd = " ".join(FakeSP.calls[0][0]) if FakeSP.calls else ""
t("W5-1f 权限=safe 进命令行且写回任务表",
  not e1f and "danger-full-access" not in _cmd and STORE.get("j6", {}).get("perm") == "safe",
  f"err={e1f} perm={STORE.get('j6', {}).get('perm')} cmd={_cmd[:90]}")
FakeSP.calls = []
e1f2 = _go(["j6", "再干一轮"])
_cmd2 = " ".join(FakeSP.calls[0][0]) if FakeSP.calls else ""
t("W5-1f 后续不带权限的 #go 沿用已落盘 safe",
  not e1f2 and "danger-full-access" not in _cmd2 and STORE["j6"].get("perm") == "safe",
  f"err={e1f2} cmd={_cmd2[:90]}")

# W5-1e: 结构不变量 — `if _pid:` 只包 kill, Popen 在锁外且不在该分支内
TREE = ast.parse(open(os.path.join(HERE, 'tower_ctl.py'), encoding='utf-8').read())
def _fn(name):
    return next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
def _locks(node):
    return [w for w in ast.walk(node)
            if isinstance(w, ast.With)
            and any(getattr(it.context_expr, "id", "") == "_jl" for it in w.items)]
def _calls(node, fname):
    return [x for x in ast.walk(node) if isinstance(x, ast.Call)
            and getattr(x.func, "id", getattr(x.func, "attr", "")) == fname]
_g = _fn("cmd_go")
_pids = [n for n in _g.body if isinstance(n, ast.If)
         and getattr(n.test, "id", "") == "_pid"]
t("W5-1e if _pid 分支内只有kill没有Popen",
  bool(_pids) and all(_calls(w, "_kill_group") and not _calls(w, "Popen") and not _calls(w, "spawn_resume") for w in _pids))
t("W5-1e Popen不在任何 _jl 锁内",
  bool(_calls(_g, "spawn_resume")) and bool(_calls(_fn("spawn_resume"), "Popen"))
  and not any(_calls(w, "spawn_resume") for w in _locks(_g))
  and not any(_calls(w, "Popen") for w in _locks(_fn("spawn_resume"))))

# =====================================================================
# W5-2: poll_jobs 补建话题舱不得在 _jl 锁内调 root_topic(同线程重入死锁)
# =====================================================================
fc.subprocess = ORIG["subprocess"]
fc.root_topic = ORIG["root_topic"]   # W5-1 的桩不能漏到这里, 否则测的是假root_topic
_p = _fn("poll_jobs")
t("W5-2 poll_jobs 锁内不调 root_topic",
  not any(_calls(w, "root_topic") for w in _locks(_p)) and bool(_calls(_p, "root_topic")))

STORE.clear()
STORE["j5"] = {"jid": "j5", "state": "exited", "pid": None, "sess": SESS,
               "cwd": TMP, "agent": "codex", "chat_id": "oc_1",
               "topic_pending": True, "notified": True, "title": "舱丢了"}
fc.conf = lambda: {**BASE_CONF, "max_run_min": 0}
fc.action_card = lambda *a, **k: "om_root5"
fc.api = lambda m, u, b=None, **k: {"code": 0, "data": {"thread_id": "om_thread5"}}

def _run_poll():
    try:
        fc.poll_jobs()
    except Exception as e:
        _run_poll.err = f"{type(e).__name__}: {e}"
_run_poll.err = None
th = threading.Thread(target=_run_poll, daemon=True)
th.start(); th.join(8)
if th.is_alive():
    for n, ok, d in PASS:
        print(("✓" if ok else "✗"), n, d)
    print("✗ W5-2 poll_jobs 未在8秒内返回 — 仍与 root_topic 死锁")
    print(f"{len(PASS) - 1}/{len(PASS) + 1} PASS")
    shutil.rmtree(TMP, ignore_errors=True)
    os._exit(1)
t("W5-2 poll_jobs 带topic_pending时正常返回(无死锁)", _run_poll.err is None, str(_run_poll.err))
t("W5-2 话题舱补建成功并清标记",
  STORE.get("j5", {}).get("root_mid") == "om_root5"
  and not STORE.get("j5", {}).get("topic_pending"),
  json.dumps(STORE.get("j5", {}), ensure_ascii=False)[:140])

# 补建失败 -> 保留标记由下轮重试
STORE["j5"].pop("root_mid", None); STORE["j5"]["topic_pending"] = True
# Simulate a fresh build, rather than a previously acknowledged ready topic.
fc.private_json(os.path.join(TMP, 'topic-builds.json'), {})
fc.action_card = lambda *a, **k: ""
th = threading.Thread(target=_run_poll, daemon=True); th.start(); th.join(8)
t("W5-2 建卡失败保留topic_pending(下轮再试)",
  not th.is_alive() and STORE.get("j5", {}).get("topic_pending") is True
  and not STORE.get("j5", {}).get("root_mid"))
fc.action_card = lambda *a, **k: "om_root5"
builds = fc.JsonMap(os.path.join(TMP, 'topic-builds.json'))
builds.update(lambda data: data['job:j5'].update(next_at=0))
th = threading.Thread(target=_run_poll, daemon=True); th.start(); th.join(8)
t("W5-2 下轮补建自愈", not th.is_alive() and STORE.get("j5", {}).get("root_mid") == "om_root5")
fc.conf = ORIG["conf"]

# =====================================================================
# W5-3: 卡片侧鉴权与文字命令同语义 — allow_ou 为空 = 拒绝一切
# =====================================================================
SCANS = []
fc.cmd_scan = lambda *a, **k: SCANS.append(a)
AUDITS.clear()

fc.conf = lambda: {**BASE_CONF, "allow_ou": ""}
fc.handle_ui("ou_a", "oc_1", {"act": "scan_now"}, "om_c1")
t("W5-3 空白名单→卡片动作被拒", SCANS == [] and any("DENY" in a[2] for a in AUDITS),
  f"scans={len(SCANS)} audits={AUDITS[-1:]}")

fc.conf = lambda: {**BASE_CONF, "allow_ou": "ou_ok, ou_b"}
AUDITS.clear()
fc.handle_ui("ou_a", "oc_1", {"act": "scan_now"}, "om_c1")
t("W5-3 名单外 open_id 仍被拒", SCANS == [] and any("DENY" in a[2] for a in AUDITS))
fc.handle_ui("ou_ok", "oc_1", {"act": "scan_now"}, "om_c1")
t("W5-3 名单内放行(带空格也能匹配)", len(SCANS) == 1, f"scans={len(SCANS)}")

# 回调 toast: 未授权不再回「收到,处理中…」
def _ev(open_id, act="scan_now"):
    return {"action": {"value": {"act": act},
                       "context": {"open_chat_id": "oc_1", "open_message_id": "om_c1"}},
            "operator": {"open_id": open_id}}
while not fc.Q.empty():
    fc.Q.get_nowait()
fc.conf = lambda: {**BASE_CONF, "allow_ou": ""}
r = fc.on_card_action(_ev("ou_a"))
t("W5-3 空白名单回调→未授权toast",
  isinstance(r, dict) and r.get("toast", {}).get("type") == "warn"
  and "未授权" in r["toast"]["content"], str(r))
t("W5-3 回调缺字段不再 UnboundLocalError", r is not None)

fc.conf = lambda: {**BASE_CONF, "allow_ou": "ou_ok"}
while not fc.Q.empty():      # 未授权那次也入队(真闸口在handle_ui), 先清空再数
    fc.Q.get_nowait()
r = fc.on_card_action(_ev("ou_ok"))
t("W5-3 授权回调→处理中toast且已入队",
  r.get("toast", {}).get("content") == "收到,处理中…" and fc.Q.qsize() == 1, str(r))
while not fc.Q.empty():
    fc.Q.get_nowait()
r = fc.on_card_action(_ev("ou_ok", "job_forget"))
t("W5-3 危险动作→warn toast", r.get("toast", {}).get("type") == "warn", str(r))
while not fc.Q.empty():
    fc.Q.get_nowait()
fc.conf = ORIG["conf"]

# =====================================================================
# W5-4: #unwatch 空参不得静默清空纳管
# =====================================================================
U = {"11111111-aaaa-bbbb-cccc-000000000001": {"mid": "om_a"},
     "22222222-aaaa-bbbb-cccc-000000000002": {"mid": "om_b"},
     "33333333-aaaa-bbbb-cccc-000000000003": {"mid": "om_c"}}
json.dump(U, open(fc.STOPICF, "w", encoding="utf-8"))
REPLIES.clear()
fc.cmd_unwatch("ou_a", "oc_1", [])
after = json.load(open(fc.STOPICF, encoding="utf-8"))
t("W5-4 空参不动纳管表", len(after) == 3, f"left={len(after)}")
t("W5-4 空参回用法提示", any("用法" in m for m, _ in REPLIES), str(REPLIES[-1:]))
REPLIES.clear()
fc.cmd_unwatch("ou_a", "oc_1", ["nope123"])
after = json.load(open(fc.STOPICF, encoding="utf-8"))
t("W5-4 未命中前缀不动表且提示", len(after) == 3 and any("未纳管" in m for m, _ in REPLIES))
fc.cmd_unwatch("ou_a", "oc_1", ["11111111"])
after = json.load(open(fc.STOPICF, encoding="utf-8"))
t("W5-4 单个前缀只删自己", len(after) == 2 and not any(k.startswith("11111111") for k in after),
  f"left={sorted(after)}")
fc.cmd_unwatch("ou_a", "oc_1", ["all"])
after = json.load(open(fc.STOPICF, encoding="utf-8"))
t("W5-4 显式 all 才全清", after == {}, str(after))
src = open(os.path.join(HERE, 'tower_ctl.py'), encoding='utf-8').read()
t("W5-4 help 文案与实现一致", "#unwatch <sess8 或 all>" in src)

# =====================================================================
# W5-5: 本套单测自身不得写生产 state/
# =====================================================================
PROD = os.path.join(os.path.dirname(HERE), "state")
t("W5-5 生产 state 路径未被本测试触碰",
  fc.STATED == TMP and fc.STOPICF.startswith(TMP) and fs.TEXTSENT.startswith(TMP)
  and not any(str(x).startswith(PROD) for x in (fc.JOBSF, fc.CMIDF, fc.LOG, fs.STATE)))
t("W5-5 无 .unittest 垃圾文件残留",
  not os.path.exists(os.path.join(PROD, "card-mids.json.unittest")))

# ---------- 汇总 ----------
restore()
bad = [p for p in PASS if not p[1]]
for n, ok, d in PASS:
    print(("✓" if ok else "✗"), n, d)
print(f"\n{len(PASS)-len(bad)}/{len(PASS)} PASS")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if bad else 0)
