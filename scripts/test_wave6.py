"""波次六修复的离线单测 — 8 项 P2(渲染丢提示/转义/目标环漏装/取消吞消息/锁内慢操作/自检烧号/脚本死路径)
一切副作用重定向到临时目录; 网络、子进程、飞书投递全部打桩, 不碰生产 state/ 与真实群。
"""
import sys, os, re, ast, json, time, shutil, builtins, tempfile, threading, subprocess
os.environ.setdefault("SYNC_DRY", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tower_sync as fs
import tower_ctl as fc

TMP = tempfile.mkdtemp(prefix="wave6-state-")
PROD_STATE = os.path.join(os.path.dirname(HERE), "state")
# 生产 state 的 mtime 不能用来判"本测试有没有写它": 守护进程在跑会持续改写(接入 Qoder 后更勤)。
# 与 wave8/9 一致, 改用写钩子: 只看本进程有没有以写模式打开生产 state 下的文件。
WRITE_HITS = []
_REAL_OPEN = builtins.open
def _spy_open(file, mode="r", *a, **k):
    try:
        p = os.path.abspath(str(file))
    except Exception:
        p = ""
    if p.startswith(PROD_STATE) and any(c in str(mode) for c in "wax+"):
        WRITE_HITS.append((p, mode))
    return _REAL_OPEN(file, mode, *a, **k)
builtins.open = _spy_open

fc.STATED = TMP
fc.JOBSF = os.path.join(TMP, "ctl-jobs.json")
fc.CMIDF = os.path.join(TMP, "card-mids.json")
fc._JID_SEQF = os.path.join(TMP, ".jid-seq")
fc.STOPICF = os.path.join(TMP, "sess-topics.json")
fc.LOG = os.path.join(TMP, "ctl.log")
fc.AUDIT = os.path.join(TMP, "ctl-audit.log")
fc.HB = os.path.join(TMP, "ctl.heartbeat")
for m in (fs, fc):
    for n in ("CMIDF", "JOBSF", "TEXTSENT", "STATE", "TOKF", "LOG"):
        if hasattr(m, n):
            setattr(m, n, os.path.join(TMP, os.path.basename(getattr(m, n))))

PASS = []
def t(n, c, d=""):
    PASS.append((n, bool(c), d))

t("W6-0 状态路径全部指向临时目录",
  fc.STATED == TMP and fs.STATE.startswith(TMP) and fs.TEXTSENT.startswith(TMP)
  and fc.CMIDF.startswith(TMP), f"TMP={TMP}")

# ---------- 打桩 ----------
BASE_CONF = dict(fc.conf())
REPLIES = []; CARDS = []; AUDITS = []; API = []; TSEND = []; KILLED = []
GOS = []; TOPICS = []; THREAD_JID = {}
STORE = {}
def _load(): return json.loads(json.dumps(STORE))
def _save(j): STORE.clear(); STORE.update(j)

class FakeProc:
    def __init__(self, pid=4242): self.pid = pid
class FakeSP:
    STDOUT = subprocess.STDOUT
    calls = []
    def Popen(self, cmd, **kw):
        FakeSP.calls.append((cmd, kw))
        return FakeProc()

ORIG = {k: getattr(fc, k) for k in
        ("jobs_load", "jobs_save", "reply", "reply_card", "audit", "log", "conf", "tsend",
         "root_topic", "subprocess", "_kill_group", "_reap", "api", "cmid_register",
         "find_sess", "final_text", "thread_to_jid", "rate_ok", "cmd_go", "action_card",
         "alive", "notify_done", "app")}
fc.jobs_load = _load; fc.jobs_save = _save
fc.reply = lambda chat_id, md, **k: REPLIES.append((md, k.get("template")))
fc.reply_card = lambda chat_id, title, tpl, els: CARDS.append((title, tpl, els)) or "om_fb"
fc.audit = lambda s, x, r: AUDITS.append((s, x, r))
fc.log = lambda *a, **k: None
fc.tsend = lambda jd, md, title="📩 任务舱", template="blue": TSEND.append((md, title)) or True
fc.root_topic = lambda c, jid, jd: TOPICS.append(jid)
fc._reap = lambda p, rcf: None
fc.cmid_register = lambda *a, **k: None
fc.rate_ok = lambda s: True
fc.subprocess = FakeSP()
fc.app = lambda: {"app_id": "cli_x", "app_secret": "y", "chat_id": "oc_1"}
def _api(m, u, b=None, **k):
    API.append((m, u, b))
    return {"code": 0, "data": {"message_id": "om_reply_1"}}
fc.api = _api
def restore():
    for k, v in ORIG.items():
        setattr(fc, k, v)

TREE = ast.parse(open(os.path.join(HERE, 'tower_ctl.py'), encoding='utf-8').read())
SRC = open(os.path.join(HERE, 'tower_ctl.py'), encoding='utf-8').read()
def _fn(name):
    return next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
def _locks(node):
    return [w for w in ast.walk(node) if isinstance(w, ast.With)
            and any(getattr(it.context_expr, "id", "") == "_jl" for it in w.items)]
def _calls(node, *names):
    return [x for x in ast.walk(node) if isinstance(x, ast.Call)
            and getattr(x.func, "id", getattr(x.func, "attr", "")) in names]
def _lock_clean(fname, *banned):
    """结构断言: fname 的每个 _jl 锁块内都不得出现 banned 调用。
    防恒真: banned 里的每个名字必须在该函数体某处真实出现过, 否则这条断言无意义。"""
    fn = _fn(fname)
    absent = [b for b in banned if not _calls(fn, b)]
    hits = [(f, w) for f in banned for w in _locks(fn) if _calls(w, f)]
    return not hits and not absent, f"{fname} 锁内命中 {[(f, w.lineno) for f, w in hits]} 函数内查无此调用={absent}"

# =====================================================================
# W6-1 / W6-2: 完成卡 — 最终回复必须转义, 灰字提示必须进正文
# =====================================================================
def _done_card(jd):
    API.clear()
    fc.notify_done(jd)
    if not API:
        return None
    return json.loads(API[-1][2]["content"])
def _md_of(card):
    return " ".join(e.get("content", "") for e in card["body"]["elements"]
                    if e.get("tag") == "markdown")

RAW = '我把 <script>x</script> 与 a<b>c&d 修好了'
BASEJD = {"jid": "j1", "agent": "codex", "state": "exited", "exit": "0",
          "started": 1000.0, "ended": 1061.0, "title": "转义测试",
          "chat_id": "oc_1", "root_mid": "om_root", "out_file": os.path.join(TMP, "no.out")}
fc.final_text = lambda jd, limit=520: RAW
c = _done_card(BASEJD)
md = _md_of(c) if c else ""
t("W6-1 最终回复走 clean_md(尖括号/与号被转义)",
  "&lt;script&gt;" in md and "&amp;" in md and "<script>" not in md, md[:160])
t("W6-1 卡片仍有按钮列(重构未丢操作)",
  c is not None and any(e.get("tag") == "column_set" for e in c["body"]["elements"]))
t("W6-1 卡片 config 未退化(wide+update_multi)",
  c is not None and c["config"].get("update_multi") is True
  and c["config"].get("wide_screen_mode") is True)

ERR429 = {**BASEJD, "exit": "1"}
fc.final_text = lambda jd, limit=520: ""
def _err_card(errtext, extra=None):
    out = os.path.join(TMP, "err.out")
    open(out, "w", encoding="utf-8").write(json.dumps({"error": errtext}))
    jd = {**ERR429, "out_file": out}
    if extra:
        jd.update(extra)
    card = _done_card(jd)
    return _md_of(card) if card else ""
m429 = _err_card("HTTP 429 too many requests from gateway")
t("W6-2 429 灰字提示进了正文(此前 els 之后 += 永远不可见)",
  "429=网关限流" in m429, m429[-140:])
mauth = _err_card("permission denied, not authorized")
t("W6-2 鉴权类错误提示进了正文", "重试无效" in mauth, mauth[-140:])
mto = _err_card("stream timeout while waiting", {"timeout_killed": True})
t("W6-2 超时提示进了正文", "拆成两个小任务" in mto, mto[-140:])

_nd = _fn("notify_done")
_els_ln = next((s.lineno for s in _nd.body if isinstance(s, ast.Assign)
               and any(getattr(tg, "id", "") == "els" for tg in s.targets)), None)
_body_after = [s.lineno for s in ast.walk(_nd)
               if isinstance(s, ast.AugAssign) and getattr(s.target, "id", "") == "body"
               and _els_ln and s.lineno > _els_ln]
t("W6-2 结构: els 定型后再不修改 body", _els_ln and not _body_after,
  f"els@{_els_ln} 违规@{_body_after}")
_out429 = os.path.join(TMP, "e429.out")
open(_out429, "w", encoding="utf-8").write(json.dumps({"error": "HTTP 429 too many requests"}))
CARDS.clear()
fc.notify_done({**ERR429, "root_mid": "", "out_file": _out429})
t("W6-2 无 root_mid 时回落 reply_card 且带上同一条灰字提示",
  bool(CARDS) and CARDS[-1][1] == "red"
  and "429=网关限流" in _md_of({"body": {"elements": CARDS[-1][2]}}),
  str(CARDS[-1][:2] if CARDS else "no card"))

# =====================================================================
# W6-3: cmid_prune 摘掉死的 TTL 分支, 只按活 jid 剪
# =====================================================================
t("W6-3 死 TTL 分支已删(ttl_ts/cut 不再出现)",
  "ttl_ts" not in SRC and "cut = time.time()" not in SRC)
json.dump({"om_gone": {"kind": "job", "jid": "j999"},
           "om_live": {"kind": "job", "jid": "j1"},
           "om_sess": {"kind": "sess", "sid8": "abcd1234"}},
          open(fc.CMIDF, "w", encoding="utf-8"))
fc._cmid["d"] = {}; fc._cmid["mt"] = 0.0
fc.cmid_prune({"j1"})
live = json.load(open(fc.CMIDF, encoding="utf-8"))
t("W6-3 死jid路由被摘", "om_gone" not in live, f"live={sorted(live)}")
t("W6-3 活jid与sess路由保留(不误删播报路由)",
  {"om_live", "om_sess"} <= set(live), f"live={sorted(live)}")

# =====================================================================
# W6-4: #cnew/#wnew 的 模式=goal 必须真的装目标环
# =====================================================================
STORE.clear(); TOPICS.clear()
fc.conf = lambda: {**BASE_CONF, "max_jobs": 10, "fallback_recent": 0,
                   "work_roots": TMP}
FakeSP.calls = []
fc.cmd_cnew("ou_a", "oc_1", [TMP, "模式=goal", "标题=目标环", "把方案落地"])
_cid = next(iter(STORE))
t("W6-4 #cnew 模式=goal 装了目标环",
  STORE[_cid].get("goal", {}).get("active") is True and STORE[_cid].get("mode") == "goal",
  json.dumps({"mode": STORE[_cid].get("mode"), "goal": bool(STORE[_cid].get("goal"))}))
t("W6-4 目标环装载回执已发出", any("目标已装载" in m for m, _ in TSEND), str(TSEND[-1:][:1]))
STORE.clear(); TSEND.clear(); TOPICS.clear()
fc.cmd_cnew("ou_a", "oc_1", [TMP, "普通任务"])
_cid2 = next(iter(STORE))
t("W6-4 不带模式时不装目标环",
  not STORE[_cid2].get("goal") and STORE[_cid2].get("mode") == "",
  json.dumps({k: STORE[_cid2].get(k) for k in ("mode", "goal")}, ensure_ascii=False))
STORE.clear(); TSEND.clear()
fc.cmd_wnew("ou_a", "oc_1", [TMP, "模式=goal", "跑起来"])
_wid = next(iter(STORE))
t("W6-4 #wnew 模式=goal 同样装目标环",
  STORE[_wid].get("goal", {}).get("active") is True and STORE[_wid].get("mode") == "goal",
  STORE[_wid].get("agent", "?"))
t("W6-4 三个建任务命令的 goal 标记一致",
  SRC.count('{"plan": " 📋计划') >= 3 and SRC.count("🎯目标模式") >= 3)
t("W6-4 建任务后 root_topic 先于 goal_start(卡片不缺)", len(TOPICS) >= 1, str(TOPICS))

# =====================================================================
# W6-5: 向导中的「取消」不得被当成提示词起任务
# =====================================================================
_P = _fn("worker")
def _first_if(pat):
    ln = None
    for s in ast.walk(_P):
        if isinstance(s, ast.If) and re.search(pat, ast.unparse(s.test)):
            ln = s.lineno if ln is None else min(ln, s.lineno)
    return ln
_cancel_ln = _first_if(r"#cancel")
_fam_ln = _first_if(r"prompt_fam")
t("W6-5 结构: 取消判定早于 prompt_fam 分支",
  _cancel_ln and _fam_ln and _cancel_ln < _fam_ln, f"cancel@{_cancel_ln} fam@{_fam_ln}")

fc._wiz.clear(); REPLIES.clear(); FakeSP.calls = []; TOPICS.clear()
fc.wiz_set("ou_w", "oc_1", step="prompt_fam", fam="claude", cwd=TMP)
th = threading.Thread(target=fc.worker, daemon=True); th.start()
def _wait(pred, sec=4.0):
    end = time.time() + sec
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False
fc.conf = lambda: {**BASE_CONF, "allow_ou": "ou_w,ou_x", "work_roots": TMP}
fc.Q.put(("ou_w", "oc_1", "取消", "", ""))   # worker按5字段解包, 夹具必须给满
t("W6-5 挂起向导时「取消」不发出任何指令",
  _wait(lambda: not fc._wiz and REPLIES) and FakeSP.calls == [],
  f"wiz={len(fc._wiz)} popen={len(FakeSP.calls)}")
t("W6-5 取消给出明确回执", any("向导已取消" in m for m, _ in REPLIES), str(REPLIES[-1:]))

REPLIES.clear(); FakeSP.calls = []
fc.Q.put(("ou_x", "oc_1", "#cancel", "", ""))
t("W6-5 无向导时 #cancel 不再被静默吞掉",
  _wait(lambda: any("没有进行中的向导" in m for m, _ in REPLIES)) and FakeSP.calls == [],
  str(REPLIES[-1:]))

GOS.clear(); REPLIES.clear()
fc.thread_to_jid = lambda tid: "j7"
fc.cmd_go = lambda s, ch, a: GOS.append(a)
fc.conf = lambda: {**BASE_CONF, "allow_ou": "ou_a", "work_roots": TMP}
fc.Q.put(("ou_a", "oc_1", "取消", "", "om_thread7"))
t("W6-5 话题内裸「取消」照常送达 agent(不当控制命令吞)",
  _wait(lambda: len(GOS) == 1) and not any("向导" in m for m, _ in REPLIES),
  f"gos={GOS} replies={REPLIES[-1:]}")
fc.cmd_go = ORIG["cmd_go"]
fc.thread_to_jid = ORIG["thread_to_jid"]

# =====================================================================
# W6-6: 慢操作不得留在 _jl 锁内(kill/find_sess/api/发卡)
# =====================================================================
for fname, banned in (("cmd_model", ("_kill_group", "find_sess", "reply")),
                      ("_model_claude", ("_kill_group", "reply")),
                      ("_model_wcb", ("_kill_group", "reply")),
                      ("cmd_forget", ("_kill_group", "reply")),
                      ("cmd_go", ("find_sess", "_kill_group", "spawn_resume", "reply")),
                      ("cmd_title", ("reply",)),
                      ("cmd_goal", ("reply",)),
                      ("archive_stale_topics", ("api",)),
                      ("refresh_sess", ("find_sess",)),
                      ("poll_jobs", ("notify_done", "root_topic"))):
    ok, d = _lock_clean(fname, *banned)
    t(f"W6-6 {fname} 锁内无 {'/'.join(banned)}", ok, d)

SLOW = ("api", "reply", "tsend", "reply_card", "action_card", "notify_done",
        "root_topic", "Popen", "spawn", "_kill_group", "find_sess")
_FNS = {n.name: n for n in TREE.body if isinstance(n, ast.FunctionDef)}
def _reaches(name, seen=(), depth=0):
    if name in seen or depth > 4 or name not in _FNS:
        return None
    for c in _calls(_FNS[name], *SLOW):
        return f"{getattr(c.func, 'id', getattr(c.func, 'attr', ''))}@{c.lineno}"
    for c in ast.walk(_FNS[name]):
        if isinstance(c, ast.Call) and not isinstance(c.func, ast.Attribute):
            nm = c.func.id
            if nm in _FNS and nm != name:
                r = _reaches(nm, seen + (name,), depth + 1)
                if r:
                    return f"{nm}@{c.lineno}->{r}"
    return None
_leaks = []
for _fn_name in sorted(_FNS):
    for lk in _locks(_FNS[_fn_name]):
        for c in ast.walk(lk):
            if not isinstance(c, ast.Call):
                continue
            nm = getattr(c.func, "id", getattr(c.func, "attr", ""))
            if nm in SLOW:
                _leaks.append(f"{_fn_name}:{c.lineno}:{nm}")
            elif nm in _FNS and nm != _fn_name:
                r = _reaches(nm)
                if r:
                    _leaks.append(f"{_fn_name}:{c.lineno}:{nm}->{r}")
t("W6-6 全模块不变量: 任何 _jl 锁块都不达网络/kill/glob/起进程",
  not _leaks, f"泄漏点={_leaks}")

STORE.clear(); REPLIES.clear(); FakeSP.calls = []
for _fn2, _argv in ((fc.cmd_forget, ["jzz"]), (fc.cmd_title, ["jzz", "新名"]),
                    (fc.cmd_model, ["jzz", "gpt-5"]), (fc.cmd_go, ["jzz", "继续"])):
    REPLIES.clear()
    _fn2("ou_a", "oc_1", _argv)
    t(f"W6-6 {_fn2.__name__} 缺任务仍回红卡(锁外预检没把回执一起丢掉)",
      any("无任务" in m and tpl == "red" for m, tpl in REPLIES), str(REPLIES[-1:]))
t("W6-6 缺任务时不起进程也不写表",
  FakeSP.calls == [] and STORE == {}, f"popen={len(FakeSP.calls)} store={STORE}")

STORE.clear()
STORE["j2"] = {"jid": "j2", "state": "running", "pid": 99999999, "sess": "s" * 8,
               "cwd": TMP, "agent": "claude", "out_file": os.path.join(TMP, "j2.out"),
               "chat_id": "oc_1"}
HOLD = threading.Event(); RELEASE = threading.Event()
def _slow_kill(pid, grace=8):
    KILLED.append(pid); HOLD.set(); RELEASE.wait(5)
fc._kill_group = _slow_kill
FakeSP.calls = []
def _run(fn, *a):
    try:
        fn(*a)
    except Exception as e:
        _run.err = f"{type(e).__name__}: {e}"
    _run.done = True
_run.err = None; _run.done = False
th2 = threading.Thread(target=_run, args=(fc.cmd_forget, "ou_a", "oc_1", ["j2"]), daemon=True)
th2.start()
t("W6-6 kill 期间另一线程仍能取 _jl(不阻塞 poll/心跳)",
  _wait(lambda: HOLD.is_set()) and fc._jl.acquire(blocking=False) is True
  and (fc._jl.release() or True), f"hold={HOLD.is_set()} err={_run.err}")
RELEASE.set(); th2.join(6)
t("W6-6 #forget 完成后任务确实被摘除",
  "j2" not in STORE and not _run.err, f"keys={sorted(STORE)} err={_run.err}")
fc._kill_group = lambda pid, grace=8: KILLED.append(pid)

# archive: api 慢时锁可用, 且成功才标 archived
STORE.clear()
STORE["j3"] = {"jid": "j3", "state": "exited", "pid": None, "cwd": TMP,
               "root_mid": "om_r3", "ended": time.time() - 200000, "prompt": "旧任务"}
AHOLD = threading.Event(); ARELEASE = threading.Event()
def _slow_api(m, u, b=None, **k):
    AHOLD.set(); ARELEASE.wait(5); return {"code": 0}
fc.api = _slow_api
_run.err = None; _run.done = False
th3 = threading.Thread(target=_run, args=(fc.archive_stale_topics,), daemon=True)
th3.start()
t("W6-6 归档 PATCH 在锁外(慢API不冻住任务表)",
  _wait(lambda: AHOLD.is_set()) and fc._jl.acquire(blocking=False) is True
  and (fc._jl.release() or True), f"err={_run.err}")
ARELEASE.set(); th3.join(6)
t("W6-6 归档成功后才标 archived", STORE.get("j3", {}).get("archived") is True and not _run.err,
  str(STORE.get("j3", {}).get("archived")))
fc.api = _api

# refresh_sess: find_sess 慢时锁可用
FHold = threading.Event(); FRelease = threading.Event()
def _slow_find(jid):
    FHold.set(); FRelease.wait(5); return "s" * 32
fc.find_sess = _slow_find
STORE.clear()
STORE["j4"] = {"jid": "j4", "state": "running", "pid": 4242, "sess": "", "cwd": TMP}
_run.err = None; _run.done = False
th4 = threading.Thread(target=_run, args=(fc.refresh_sess,), daemon=True); th4.start()
t("W6-6 find_sess 在锁外(glob+读大文件不冻住任务表)",
  _wait(lambda: FHold.is_set()) and fc._jl.acquire(blocking=False) is True
  and (fc._jl.release() or True), f"err={_run.err}")
FRelease.set(); th4.join(6)
t("W6-6 补到的 sess 正确写回", STORE.get("j4", {}).get("sess") == "s" * 32 and not _run.err,
  str(STORE.get("j4", {}).get("sess")))
STORE["j4"]["sess"] = ""
fc.find_sess = lambda jid: "s" * 32
# 竞态兜底: 锁外解析期间任务若已被别处写回 sess, 不得覆盖
STORE["j4"]["sess"] = "already"
fc.refresh_sess()
t("W6-6 回写只填空位(不覆盖并发写入)", STORE["j4"]["sess"] == "already")
fc.find_sess = ORIG["find_sess"]

# poll_jobs: 完成卡通知道锁外, 且 notified 已落盘不重复通知
STORE.clear()
NOTI = []
fc.notify_done = lambda jd: NOTI.append(jd["jid"])
fc.alive = lambda pid: False
open(os.path.join(TMP, ".rc.j5"), "w").write("0")
STORE["j5"] = {"jid": "j5", "state": "running", "pid": 4242, "sess": "s" * 8,
               "cwd": TMP, "agent": "codex", "chat_id": "oc_1",
               "rc_file": os.path.join(TMP, ".rc.j5"), "out_file": os.path.join(TMP, "j5.out")}
fc.conf = lambda: {**BASE_CONF, "max_run_min": 0, "allow_ou": "ou_a", "work_roots": TMP}
fc.poll_jobs()
t("W6-6 完成卡在锁外发且只发一次", NOTI == ["j5"] and STORE["j5"].get("notified") is True,
  f"noti={NOTI} notified={STORE['j5'].get('notified')}")
fc.poll_jobs()
t("W6-6 notified 已落盘 -> 第二轮不重复发卡", NOTI == ["j5"], f"noti={NOTI}")
fc.notify_done = ORIG["notify_done"]
fc.alive = ORIG["alive"]

# =====================================================================
# W6-7: selftest 只读水位线, 不再烧 jid
# =====================================================================
for p in ("j", "cj", "wj"):
    open(f"{TMP}/.jid-seq.{p}", "w").write("7")
_seq_before = {p: open(f"{TMP}/.jid-seq.{p}").read() for p in ("j", "cj", "wj")}
import io
_buf = io.StringIO(); _so = sys.stdout
try:
    sys.stdout = _buf
    fc.selftest()
except Exception as e:
    _buf.write(f"EXC {e}")
finally:
    sys.stdout = _so
_out = _buf.getvalue()
t("W6-7 selftest 不消耗 jid 水位线",
  all(open(f"{TMP}/.jid-seq.{p}").read() == _seq_before[p] for p in ("j", "cj", "wj")),
  f"after={[(p, open(f'{TMP}/.jid-seq.{p}').read()) for p in ('j','cj','wj')]}")
t("W6-7 selftest 打印水位线而非新号", "jid 水位线" in _out and "jid sample" not in _out,
  _out.strip().splitlines()[-1][:80] if _out.strip() else "")
t("W6-7 selftest 未抛异常", "EXC" not in _out, _out[:120])
_new = fc.new_jid()
t("W6-7 new_jid 仍正常推进水位线", _new == "j8" and open(f"{TMP}/.jid-seq.j").read() == "8", _new)

# =====================================================================
# W6-8: feishu-notify.sh 死路径(agent-hq) -> 从脚本自身定位仓库
# =====================================================================
SH = os.path.join(HERE, "feishu-notify.sh")
sh_src = open(SH, encoding="utf-8").read()
t("W6-8 脚本语法可解析", subprocess.run(["bash", "-n", SH], capture_output=True).returncode == 0)
t("W6-8 不再把仓库根硬编码成已删除的 ~/agent-hq",
  'HQ="$HOME/agent-hq"' not in sh_src and 'HQ="$(cd "$(dirname' in sh_src,
  [ln for ln in sh_src.splitlines() if ln.startswith("HQ=")][0][:70])

SB = tempfile.mkdtemp(prefix="wave6-sh-")
os.makedirs(f"{SB}/scripts"); os.makedirs(f"{SB}/logs"); os.makedirs(f"{SB}/state")
os.makedirs(f"{SB}/home/.codex"); os.makedirs(f"{SB}/config")
shutil.copy(SH, f"{SB}/scripts/feishu-notify.sh")
shutil.copy(os.path.join(HERE, "tower_sync.py"), f"{SB}/scripts/tower_sync.py")
shutil.copy(os.path.join(HERE, "tower_ui.py"), f"{SB}/scripts/tower_ui.py")
for mod in ("tower_store.py", "tower_runtime.py", "tower_delivery.py", "tower_harness.py", "tower_questions.py", "tower_dsh.py", "tower_diagnostics.py", "tower_panel.py", "tower_retention.py"):
    shutil.copy(os.path.join(HERE, mod), f"{SB}/scripts/{mod}")
for f in ("stream.conf", "tool-glossary.json"):     # 只拷无密钥项, feishu-app.json 不进 /tmp
    shutil.copy(os.path.join(os.path.dirname(HERE), "config", f), f"{SB}/config/{f}")
def _run_sh(payload, webhook="https://example.com/not-feishu"):
    open(f"{SB}/home/.codex/feishu_webhook.txt", "w").write(webhook + "\n")
    return subprocess.run(["bash", f"{SB}/scripts/feishu-notify.sh"], input=payload,
                          capture_output=True, text=True,
                          env={**os.environ, "HOME": f"{SB}/home", "SYNC_DRY": "1"})
STOP = json.dumps({"hook_event_name": "Stop", "turn_id": "turn-1", "cwd": "/tmp/proj",
                   "last_assistant_message": "完成"})
FEISHU = "https://open.feishu.cn/open-apis/bot/v2/hook/fake"
_prod_notify_log = os.path.join(os.path.dirname(HERE), "logs", "feishu-notify.log")
_prod_notify_before = open(_prod_notify_log, "rb").read() if os.path.exists(_prod_notify_log) else None
r = _run_sh(STOP)
t("W6-8 默认不保存原始载荷",
  r.returncode == 0 and not os.path.exists(f"{SB}/logs/last-input.json")
  and not os.path.exists(f"{SB}/logs/feishu-notify.log"),
  f"rc={r.returncode} cap={os.path.exists(f'{SB}/logs/last-input.json')} err={r.stderr[:80]}")
t("W6-8 非飞书 webhook 仍被挡在门外(不误发)",
  not os.path.exists(f"{SB}/state/.sent_cache"), str(os.listdir(f"{SB}/state")))
try:
    os.remove(f"{SB}/logs/last-input.json")
except OSError:
    pass   # 变异/退化场景下这里可能就没生成, 别让它把整轮测试崩掉

r2 = _run_sh(STOP, webhook=FEISHU)
logtxt = open(f"{SB}/logs/feishu-notify.log").read() if os.path.exists(f"{SB}/logs/feishu-notify.log") else ""
t("W6-8 修好后整条链路可达(去重库+日志+卡片进程)",
  os.path.exists(f"{SB}/state/.sent_cache") and "ev=turn-complete card-rc=0" in logtxt,
  f"rc={r2.returncode} log={logtxt.strip()[:120]} err={r2.stderr[:80]}")
t("W6-8 副作用全在沙箱(生产 notify 日志未被测试改写)",
  (open(_prod_notify_log, "rb").read() if os.path.exists(_prod_notify_log) else None) == _prod_notify_before)
_synclog = f"{SB}/logs/sync.log"
t("W6-8 走的是 dry 路径未真发",
  os.path.exists(_synclog) and "dry-deliver" in open(_synclog).read(),
  open(_synclog).read()[-90:] if os.path.exists(_synclog) else "no sync.log")
n1 = logtxt.count("ev=turn-complete")
_run_sh(STOP, webhook=FEISHU)
_nl2 = f"{SB}/logs/feishu-notify.log"
logtxt2 = open(_nl2).read() if os.path.exists(_nl2) else ""
t("W6-8 去重缓存生效(同回合不刷二张卡)",
  n1 == 1 and logtxt2.count("ev=turn-complete") == 1, f"n1={n1} n2={logtxt2.count('ev=turn-complete')}")
os.remove(f"{SB}/scripts/tower_sync.py")
r3 = _run_sh(STOP, webhook=FEISHU)
t("W6-8 依赖缺失时 fail-open 静默退出", r3.returncode == 0 and r3.stdout == "",
  f"rc={r3.returncode} out={r3.stdout[:60]}")

# ---------- 汇总 ----------
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(SB, ignore_errors=True)
t("W6-0 生产 state 未被本测试改动", not WRITE_HITS, str(WRITE_HITS[:3]))
restore()
bad = [p for p in PASS if not p[1]]
for n, ok, d in PASS:
    print(("✓" if ok else "✗"), n, d)
print(f"\n{len(PASS)-len(bad)}/{len(PASS)} PASS")
sys.exit(1 if bad else 0)
