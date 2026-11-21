"""波次九: 第四引擎 Qoder CLI 接入 — 纳管(播报) + 触发(#qnew)
一切副作用重定向到临时目录; 网络与飞书投递全部打桩(SYNC_DRY=1), 不碰生产 state/ 与真实群。
"""
import sys, os, re, ast, json, time, uuid, builtins, tempfile, subprocess
os.environ.setdefault("SYNC_DRY", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tower_sync as fs
import tower_ctl as fc

TMP = tempfile.mkdtemp(prefix="wave9-state-")
CTL_SRC = os.path.join(HERE, "tower_ctl.py")
SYNC_SRC = os.path.join(HERE, "tower_sync.py")
_ctl = open(CTL_SRC, encoding="utf-8").read()
_sync = open(SYNC_SRC, encoding="utf-8").read()
CTREE = ast.parse(_ctl)
STREE = ast.parse(_sync)

PROD_STATE = os.path.join(os.path.dirname(HERE), "state")
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
builtins.open = _spy_open             # 生产守护进程自己会写 state, 故只判"本进程有没有写开"

fc.STATED = TMP
fc.JOBSF = os.path.join(TMP, "ctl-jobs.json")
fc.CMIDF = os.path.join(TMP, "card-mids.json")
fc._JID_SEQF = os.path.join(TMP, ".jid-seq")
fc.STOPICF = os.path.join(TMP, "sess-topics.json")
fc.LOG = os.path.join(TMP, "ctl.log")
fc.AUDIT = os.path.join(TMP, "ctl-audit.log")
fc.HB = os.path.join(TMP, "ctl.heartbeat")
for _m in (fs, fc):
    for _n in ("CMIDF", "JOBSF", "TEXTSENT", "STATE", "TOKF", "LOG"):
        if hasattr(_m, _n):
            setattr(_m, _n, os.path.join(TMP, os.path.basename(getattr(_m, _n))))

PASS = []
def t(n, c, d=""):
    PASS.append((n, bool(c), d))

def _fn(tree, name):
    return next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name), None)

def _seg(src, name):
    i = src.find(f"def {name}")
    return src[i:src.find("\ndef ", i + 10)] if i > 0 else ""

# ---------- 打桩 ----------
BASE_CONF = dict(fc.conf())
REPLIES = []; TOPICS = []; AUDITS = []; GOALS = []; POPEC = []; KILLED = []
STORE = {}
def _load(): return json.loads(json.dumps(STORE))
def _save(j): STORE.clear(); STORE.update(json.loads(json.dumps(j)))
class FakeProc:
    def __init__(self, pid=4242): self.pid = pid
class FakeSP:
    STDOUT = subprocess.STDOUT
    def Popen(self, cmd, **kw):
        POPEC.append((cmd, kw))
        return FakeProc()
class FakeThread:
    def __init__(self, target=None, args=(), daemon=None): pass
    def start(self): pass
fc.threading = type("_T", (), {"Thread": FakeThread})
fc.jobs_load = _load; fc.jobs_save = _save
fc.reply = lambda chat_id, md, **k: REPLIES.append((md, k.get("template"))) or "om_fb"
fc.reply_card = lambda chat_id, title, tpl, els: REPLIES.append((title, tpl)) or "om_fb"
fc.audit = lambda s, x, r: AUDITS.append((s, x, r))
fc.log = lambda *a, **k: None
fc.tsend = lambda *a, **k: True
fc.root_topic = lambda c, jid, jd: TOPICS.append(jid)
fc.goal_start = lambda c, jid, p, s: GOALS.append(jid)
fc._reap = lambda p, rcf: None
fc.cmid_register = lambda *a, **k: None
fc.rate_ok = lambda s: True
fc.subprocess = FakeSP()
fc._kill_group = lambda pid: KILLED.append(pid)
fc.api = lambda m, u, b=None, **k: {"code": 0, "data": {"message_id": "om_reply_1"}}
fs.api = fc.api
fs.token = lambda: "t"

# Qoder CLI 可执行文件桩(只需存在, 不真跑)。各引擎桩路径必须互不相同,
# 否则"换模型时把 agent 写死回 claude"这类串味变异看不出来。
def _bin(name):
    p = os.path.join(TMP, name)
    _REAL_OPEN(p, "w").close()
    return p
FAKE_BIN = _bin("qoderclicn-fake")
CLAUDE_BIN = _bin("claude-fake")
WCB_BIN = _bin("codebuddy-fake")
MISSING_BIN = os.path.join(TMP, "no-such-qoder-bin")
PROJ = os.path.realpath(tempfile.mkdtemp(prefix="wave9-proj-"))   # 避免 /var→/private/var 软链假红
def cfg(**kw):
    c = {**BASE_CONF, "max_jobs": 10, "fallback_recent": 0, "work_roots": PROJ,
         "qoder_bin": FAKE_BIN, "claude_bin": CLAUDE_BIN, "wcb_bin": WCB_BIN, **kw}
    fc.conf = lambda: c
cfg()

t("W9-0 落盘路径全部指向临时目录",
  fc.STATED == TMP and fs.STATE.startswith(TMP) and fs.TEXTSENT.startswith(TMP)
  and fc.JOBSF.startswith(TMP), f"TMP={TMP}")

# =====================================================================
# W9-1/2 纳管谓词: is_qoder 独立判定 + is_claude 扩为"claude 形态"
# =====================================================================
QID = str(uuid.uuid4())
QP = f"{TMP}/home/.qoder-cn/projects/-proj-x-demo/{QID}.jsonl"
CP = f"{TMP}/home/.claude/projects/-proj-x-demo/{str(uuid.uuid4())}.jsonl"
WP = f"{TMP}/home/.workbuddy/projects/w-demo/{str(uuid.uuid4())}.jsonl"
XP = f"{TMP}/home/.codex/sessions/2026/09/rollout-2026-01a0abcd-{str(uuid.uuid4())}.jsonl"
t("W9-1 is_qoder 只认 qoder 目录",
  fs.is_qoder(QP) and not fs.is_qoder(CP) and not fs.is_qoder(WP) and not fs.is_qoder(XP))
t("W9-2 is_claude 涵盖 claude+qoder 两种形态",
  fs.is_claude(CP) and fs.is_claude(QP) and not fs.is_claude(WP) and not fs.is_claude(XP))
# 扩谓词的意义: 下游按 is_claude/is_wb 分派的站点自动接住 qoder, 不需要第4个谓词逐处补
_mo = _seg(_sync, "meta_of")
t("W9-2b 解析路由仍走 is_claude/is_wb 分派(未漏改)",
  "if is_wb(path):" in _mo and "if is_claude(path):" in _mo and "is_qoder" not in _mo)
_qcalls = [n for n in ast.walk(STREE) if isinstance(n, ast.Call)
           and getattr(n.func, "id", "") == "is_qoder"]
t("W9-2c is_qoder 只在图标兜底处出现(解析路由不逐处补)",
  len(_qcalls) == 2, str([n.lineno for n in _qcalls]))
t("W9-2d qoder 走 claude 的 uuid 命名规则",
  fs.sess_uuid_of(QP) == QID and fs.sid8(QP) == QID[:8])

# =====================================================================
# W9-3 扫描目录: _scan_files 把 qsession 纳入 glob, 且缓存签名跟着它变
# =====================================================================
import inspect
_params = list(inspect.signature(fs._scan_files).parameters)
t("W9-3a _scan_files 形参含 qsession",
  "qsession" in _params and _params.index("qsession") < _params.index("test_dir"), str(_params))
PATS = []
_real_glob = fs.glob.glob
fs.glob.glob = lambda pat: (PATS.append(pat), [])[1]
_qd = tempfile.mkdtemp()
fs._scan_files(_qd, _qd, f"{_qd}/qoder", _qd, _qd, None)
fs.glob.glob = _real_glob
t("W9-3b 实际 glob 了 qsession 目录", any("qoder" in p for p in PATS), " | ".join(PATS)[:130])
_getm = [n for n in ast.walk(_fn(STREE, "_scan_files"))
         if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "getmtime"]
t("W9-3c 目录签名含 qsession mtime(否则新会话被缓存吃掉)",
  any(getattr(a, "id", "") == "qsession" for n in _getm for a in n.args),
  str([getattr(a, "id", "?") for n in _getm for a in n.args]))
t("W9-3d scan_dir 用 QSESS 实参调用",
  "_scan_files(root, CSESS, QSESS, WBSESS, CBSESS, test_dir)" in _sync)

# =====================================================================
# W9-4 解析: Qoder 转录经 claude 解析器出事件与标题
# =====================================================================
os.makedirs(os.path.dirname(QP), exist_ok=True)
LINE_USER = json.dumps({"type": "user", "cwd": "/proj/x/demo", "sessionId": QID,
                        "gitBranch": "main", "message": {"role": "user", "content": "把纳管跑通"}})
LINE_ASST = json.dumps({"type": "assistant", "sessionId": QID,
                        "message": {"role": "assistant",
                                    "content": [{"type": "text", "text": "已改完 4 处"}]}})
LINE_TOOL = json.dumps({"type": "assistant", "sessionId": QID,
                        "message": {"role": "assistant",
                                    "content": [{"type": "tool_use", "name": "Bash",
                                                 "input": {"command": "pytest"}}]}})
LINE_SIDE = json.dumps({"type": "assistant", "isSidechain": True,
                        "message": {"role": "assistant",
                                    "content": [{"type": "text", "text": "不该出现"}]}})
with _REAL_OPEN(QP, "w", encoding="utf-8") as f:
    f.write("\n".join([LINE_USER, LINE_ASST, LINE_TOOL, LINE_SIDE]) + "\n")
t("W9-4a Qoder user 行算回合", list(fs.events_of(LINE_USER, QP)) == [("turn", "")],
  str(list(fs.events_of(LINE_USER, QP))))
t("W9-4b Qoder assistant 出正文",
  list(fs.events_of(LINE_ASST, QP)) == [("msg", "已改完 4 处")],
  str(list(fs.events_of(LINE_ASST, QP))))
_ev_tool = list(fs.events_of(LINE_TOOL, QP))
t("W9-4c Qoder tool_use 出工具卡",
  _ev_tool and _ev_tool[0][0] == "tool" and "Bash" in _ev_tool[0][1], str(_ev_tool))
t("W9-4d sidechain 不外播", list(fs.events_of(LINE_SIDE, QP)) == [],
  str(list(fs.events_of(LINE_SIDE, QP))))
proj, task = fs.meta_of(QP)
t("W9-4e meta_of 反解项目名+首条指令", proj == "demo" and task == "把纳管跑通", f"{proj}/{task}")

# =====================================================================
# W9-5 图标与投递路由
# =====================================================================
_real_jid_map, _real_jobs_raw = fs.jid_map, fs.jobs_raw
fs.jid_map = lambda: {QID: "qj3"}
STORE.update({"qj3": {"jid": "qj3", "agent": "qoder", "state": "running",
                      "root_mid": "om_root_qj3", "title": "纳管测试"}})
fs.jobs_raw = _load
t("W9-5a 遥控 qj 任务图标 🧩", fs.card_kind(QP) == "🧩" and fs.jid_of(QP) == "qj3",
  f"{fs.card_kind(QP)}/{fs.jid_of(QP)}")
t("W9-5b custom_title 认 qj 编号(正则含 q)", fs.custom_title(QP) == "纳管测试",
  repr(fs.custom_title(QP)))
t("W9-5c value_root 把 qj 投递锚回任务话题",
  fs.value_root(("k", QP)) == "om_root_qj3", repr(fs.value_root(("k", QP))))
fs._cmid_cache = {"d": {}, "n": 0}
fs.register_mid("om_new_1", QP)
t("W9-5d register_mid 把 qj 记成 job(不是 sess)",
  fs._cmid_cache["d"].get("om_new_1") == {"kind": "job", "jid": "qj3"},
  json.dumps(fs._cmid_cache["d"], ensure_ascii=False))
fs.jid_map = lambda: {}
t("W9-5e 桌面 Qoder 会话也标 🧩(非 🖥)", fs.card_kind(QP) == "🧩", fs.card_kind(QP))
t("W9-5f 其他引擎图标未漂移",
  fs.card_kind(CP) == "🖥" and fs.card_kind(WP) == "🦞" and fs.card_kind(XP) == "🖥",
  f"{fs.card_kind(CP)}{fs.card_kind(WP)}{fs.card_kind(XP)}")
fs.jid_map, fs.jobs_raw = _real_jid_map, _real_jobs_raw

# =====================================================================
# W9-6 #qnew 命令构造
# =====================================================================
def clear():
    for L in (POPEC, STORE, REPLIES, AUDITS, TOPICS, GOALS, KILLED):
        L.clear()

clear()
fc.cmd_qnew("ou_a", "oc_1", [PROJ, "标题=纳管冒烟", "把 Qoder 接进来"])
cmd, kw = POPEC[-1] if POPEC else ([], {})
jd = STORE.get("qj1", {})
t("W9-6a 启动命令带 -p/--session-id/-o json",
  cmd[:2] == [FAKE_BIN, "-p"] and "--session-id" in cmd and "-o" in cmd and "json" in cmd,
  " ".join(str(x)[:26] for x in cmd))
t("W9-6b 默认权限=bypass_permissions",
  "--permission-mode" in cmd and cmd[cmd.index("--permission-mode") + 1] == "bypass_permissions",
  str(cmd))
t("W9-6c sid 是预分配 uuid(与转录文件名对得上)",
  bool(re.match(r"^[0-9a-f-]{36}$", str(jd.get("sess")))) and jd["sess"] in cmd, str(jd.get("sess")))
t("W9-6d jobs 记录 agent=qoder + jid 前缀 qj",
  list(STORE) == ["qj1"] and jd.get("agent") == "qoder" and jd.get("state") == "running",
  json.dumps({k: jd.get(k) for k in ("jid", "agent", "state")}, ensure_ascii=False))
t("W9-6e 工作目录/标题/提示词入库",
  jd.get("cwd") == PROJ and jd.get("title") == "纳管冒烟" and jd.get("prompt") == "把 Qoder 接进来",
  json.dumps({k: jd.get(k) for k in ("cwd", "title", "prompt")}, ensure_ascii=False))
t("W9-6f 起进程在锁外(cwd/stdout 落临时目录)",
  kw.get("cwd") == PROJ and str(kw.get("stdout")) .startswith("<")
  and jd.get("out_file", "").startswith(TMP), str(kw.get("cwd")))
t("W9-6g 先建话题舱", TOPICS == ["qj1"], str(TOPICS))
t("W9-6h 审计留痕 #qnew", any("#qnew" in str(a[1]) and "qj1" in str(a[2]) for a in AUDITS),
  str(AUDITS)[:120])
t("W9-6i 水位线文件写在临时目录",
  os.path.exists(os.path.join(TMP, ".jid-seq.qj")) and fc.STATED == TMP, fc.STATED)

clear()
fc.cmd_qnew("ou_a", "oc_1", [PROJ, "标题=s", "权限=safe", "只读跑"])
cmd = POPEC[-1][0]; jid = next(iter(STORE))
t("W9-6j 权限=safe 降级为 accept_edits(下划线式)",
  cmd[cmd.index("--permission-mode") + 1] == "accept_edits" and "acceptEdits" not in cmd
  and STORE[jid]["perm"] == "safe", str(cmd))
clear()
fc.cmd_qnew("ou_a", "oc_1", [PROJ, "模式=plan", "帮我规划"])
cmd = POPEC[-1][0]; jid = next(iter(STORE))
t("W9-6k 模式=plan 透传 plan 权限",
  cmd[cmd.index("--permission-mode") + 1] == "plan" and STORE[jid]["mode"] == "plan", str(cmd))
clear()
fc.cmd_qnew("ou_a", "oc_1", [PROJ, "模式=goal", "装目标环"])
jid = next(iter(STORE))
t("W9-6l 模式=goal 先建舱再装目标环", TOPICS == [jid] and GOALS == [jid], f"{TOPICS}/{GOALS}")
clear()
fc.cmd_qnew("ou_a", "oc_1", [PROJ, "标题=x", "权限=safe", "模式=plan", "只做规划"])
jd4 = STORE[next(iter(STORE))]
t("W9-6m 首 token 不被当模型吞掉(cnew/wnew 的模型位不残留)",
  jd4.get("prompt") == "只做规划" and jd4.get("perm") == "safe" and jd4.get("mode") == "plan",
  json.dumps({k: jd4.get(k) for k in ("prompt", "perm", "mode")}, ensure_ascii=False))

# =====================================================================
# W9-7 失败路径
# =====================================================================
clear()
cfg(qoder_bin=MISSING_BIN)
fc.cmd_qnew("ou_a", "oc_1", [PROJ, "干活"])
t("W9-7a 未装 CLI: 红卡 + 不 Popen + 不建任务",
  REPLIES and "npm i -g @qodercn-ai/qoderclicn" in REPLIES[-1][0] and REPLIES[-1][1] == "red"
  and not POPEC and not STORE, str(REPLIES)[-110:])
cfg()
clear()
fc.cmd_qnew("ou_a", "oc_1", [PROJ, "标题=只有标题没有提示词"])
t("W9-7b 缺提示词红卡且不启动", REPLIES and "缺少提示词" in REPLIES[-1][0] and not POPEC,
  str(REPLIES)[-80:])
clear()
STORE.update({f"j{i}": {"jid": f"j{i}", "state": "running"} for i in range(3)})
cfg(max_jobs=3)
REPLIES.clear(); POPEC.clear()
fc.cmd_qnew("ou_a", "oc_1", [PROJ, "超并发"])
t("W9-7c 并发上限对 qnew 同样生效",
  REPLIES and "上限" in REPLIES[-1][0] and not POPEC and not [k for k in STORE if k.startswith("qj")],
  str(REPLIES)[-80:])
cfg(max_jobs=10)
clear()
fc.cmd_qnew("ou_a", "oc_1", [])
t("W9-7d #qnew 空参转项目选择(不报缺参数)",
  REPLIES and "项目" in REPLIES[-1][0] and not POPEC, str(REPLIES)[:110])

# =====================================================================
# W9-8 换模型: 复用 _model_claude 但不得退回硬编码 claude
# =====================================================================
def mkjob(jid, agent, pid, model="(默认)"):
    outf = os.path.join(TMP, f"job-{jid}.out")
    _REAL_OPEN(outf, "wb").close()
    STORE[jid] = {"jid": jid, "agent": agent, "state": "running", "pid": pid, "sess": QID,
                  "cwd": PROJ, "perm": "full", "out_file": outf, "model": model,
                  "chat_id": "oc_1", "started": time.time(), "notified": False}

clear()
mkjob("qj5", "qoder", 777)
fc.cmd_model("ou_a", "oc_1", ["qj5", "qoder-max"])
cmd = POPEC[-1][0] if POPEC else []
t("W9-8a qoder 换模型走 resume --model 且仍用 qoder 二进制/下划线权限名",
  cmd[:1] == [FAKE_BIN] and "--resume" in cmd and "qoder-max" in cmd
  and cmd[cmd.index("--permission-mode") + 1] == "bypass_permissions"
  and "bypassPermissions" not in cmd, " ".join(map(str, cmd))[:130])
t("W9-8b 先 kill 旧进程组再重启", KILLED == [777] and len(POPEC) == 1, str(KILLED))
t("W9-8c 回执/审计用 qoder 图标与引擎名",
  any("🧩" in r[0] for r in REPLIES) and any("qoder resumed" in str(a[2]) for a in AUDITS),
  f"{str(REPLIES)[-90:]} / {str(AUDITS)[-90:]}")
t("W9-8d 换模型后 model 落库", STORE.get("qj5", {}).get("model") == "qoder-max",
  str(STORE.get("qj5", {}).get("model")))
clear()
mkjob("qj6", "qoder", 778)
fc.cmd_model("ou_a", "oc_1", ["qj6", "   "])
t("W9-8e 空模型名给查询指引(不拿空串启 resume)",
  REPLIES and "--list-models" in REPLIES[-1][0] and not POPEC, str(REPLIES)[-90:])
clear()
mkjob("cj9", "claude", 888, model="sonnet")
fc.cmd_model("ou_a", "oc_1", ["cj9", "opus"])
cmd = POPEC[-1][0] if POPEC else []
t("W9-8f claude 任务换模型未被 qoder 分支带偏(仍 claude 二进制+驼峰权限名)",
  cmd[:1] == [CLAUDE_BIN] and "bypassPermissions" in cmd and "bypass_permissions" not in cmd
  and any("🤖" in r[0] for r in REPLIES), str(cmd)[:130])

# =====================================================================
# W9-9 结果回收: qoder 的 result JSON 与 claude 同构
# =====================================================================
clear()
qid2 = str(uuid.uuid4())
qfile = os.path.join(TMP, "job-qj9.out")
with _REAL_OPEN(qfile, "w", encoding="utf-8") as f:
    f.write(json.dumps({"type": "result", "is_error": False, "result": "全部完成",
                        "session_id": qid2, "total_cost_usd": 0.12, "num_turns": 3,
                        "duration_ms": 4500}) + "\n")
STORE.update({"qj9": {"jid": "qj9", "agent": "qoder", "state": "exited", "sess": qid2,
                      "out_file": qfile, "cwd": PROJ, "chat_id": "oc_1", "rc_file": "",
                      "started": time.time() - 60, "ended": time.time(), "notified": False,
                      "title": "回收测试", "prompt": "跑完", "exit": "0"}})
_meta = fc.claude_meta(STORE["qj9"])
t("W9-9a qoder 结果 JSON 提取到成本/回合/耗时",
  "💰" in _meta and "3 turns" in _meta and "秒" in _meta, _meta)
_ft = fc.final_text(STORE["qj9"])
t("W9-9b final_text 对 qoder 走 result 字段", _ft == "全部完成", repr(_ft))
REPLIES.clear()
fc.notify_done(STORE["qj9"])
t("W9-9c notify_done 对 qoder 出完成卡(不静默)", bool(REPLIES), str(REPLIES)[:110])

# =====================================================================
# W9-10 引擎清单一致性(新增引擎最容易漏的表)
# =====================================================================
t("W9-10a AGENT_STYLE 有 qoder 图标+黄色",
  fc.AGENT_STYLE.get("qoder") == ("🧩", "yellow"), str(fc.AGENT_STYLE.get("qoder")))
t("W9-10b FAM_CMD/FAM_IC 与五引擎同键",
  set(fc.FAM_CMD) == set(fc.FAM_IC) == {"codex", "claude", "wcb", "qoder", "dsh"}
  and fc.FAM_IC["qoder"] == fc.AGENT_STYLE["qoder"][0], str(fc.FAM_IC))
t("W9-10c CMDS 注册 qnew → cmd_qnew", fc.CMDS.get("qnew") is fc.cmd_qnew)
_wk = _fn(CTREE, "worker")
_disp = {}
for n in ast.walk(_wk):
    if (isinstance(n, ast.Dict) and n.keys
            and all(isinstance(k, ast.Constant) and k.value in fc.FAM_CMD for k in n.keys)
            and len(n.keys) >= 3):
        _disp = {k.value: v.id for k, v in zip(n.keys, n.values)
                 if isinstance(k, ast.Constant) and isinstance(v, ast.Name)}
t("W9-10d 卡片派发五引擎齐全且指向各自实现",
  _disp == {"codex": "cmd_new", "claude": "cmd_cnew", "wcb": "cmd_wnew", "qoder": "cmd_qnew", "dsh": "cmd_dnew"},
  str(_disp))
t("W9-10e DEF 暴露 qoder_bin(可配置不写死)", "qoder_bin" in fc.DEF, str(list(fc.DEF))[:130])
t("W9-10f 引擎按钮含 Qoder", 'btn("🧩 Qoder CLI", "fam_proj", {"fam": "qoder"})' in _ctl)
_ad, _ftxt, _cm = _seg(_ctl, "notify_done"), _seg(_ctl, "final_text"), _seg(_ctl, "cmd_model")
t("W9-10g 三处 agent 判定各自接住 qoder",
  "qoder" in _ad and "qoder" in _ftxt and "qoder" in _cm,
  str([("qoder" in _ad), ("qoder" in _ftxt), ("qoder" in _cm)]))
t("W9-10h find_rollout 覆盖 qoder 目录",
  '~/.qoder-cn/projects/*/*{sess8}*.jsonl' in _ctl)
_sa = _seg(_ctl, "_scan_active")
t("W9-10i _scan_active 四源含 qoder + 🧩", '.qoder-cn/projects' in _sa and "🧩" in _sa)
_sat = _fn(CTREE, "_scan_active")
_ttl_assigns = [n for n in ast.walk(_sat) if isinstance(n, ast.Assign)
                and any(getattr(x, "id", "") == "ttl" for x in n.targets)]
t("W9-10i2 #scan 标题走 _rollout_task(兜底才在线上路径)",
  len(_ttl_assigns) == 1 and isinstance(_ttl_assigns[0].value, ast.Call)
  and getattr(_ttl_assigns[0].value.func, "id", "") == "_rollout_task"
  and '"aiTitle"' not in _sa, f"ttl 赋值{len(_ttl_assigns)}处")
_st = _seg(_ctl, "selftest")
t("W9-10j selftest 检查 qoder 二进制与 qj 水位",
  "qoder_bin" in _st and '"qj"' in _st, _st[:90].replace("\n", " "))
t("W9-10k 帮助/图例文案四个前缀齐全",
  "qjN=" in _ctl and "#qnew" in _ctl and "五个编码引擎" in _ctl)

# =====================================================================
# W9-11 _rollout_task: Qoder 不写 ai-title 时的标题兜底(#scan 不退化成空)
# =====================================================================
def rt(lines):
    """fixture 落成真实 .qoder-cn/projects/ 目录结构, 走 qoder 判定分支"""
    d = os.path.join(TMP, "home", ".qoder-cn", "projects", "-proj-x-demo")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, f"fx{len(d) % 1}-{abs(hash(str(lines))) % 10**8}.jsonl")
    with _REAL_OPEN(dst, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return fc._rollout_task(dst)
t("W9-11a 无 ai-title → 取首条用户消息", rt([LINE_USER, LINE_ASST]) == "把纳管跑通",
  repr(rt([LINE_USER, LINE_ASST])))
t("W9-11b 有 ai-title 仍优先(不抢 claude/wb 行为)",
  rt([json.dumps({"type": "ai-title", "aiTitle": "起个标题"}), LINE_USER]) == "起个标题",
  repr(rt([json.dumps({"type": "ai-title", "aiTitle": "起个标题"}), LINE_USER])))
_LI = json.dumps({"type": "user", "isSidechain": True, "message": {"content": "子代理指令"}})
_L2 = json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "列表里的指令"}]}})
t("W9-11c sidechain 跳过 + 列表型 content 能取", rt([_LI, _L2]) == "列表里的指令", repr(rt([_LI, _L2])))
t("W9-11d 超长标题截断带省略号",
  rt([json.dumps({"type": "user", "message": {"content": "一" * 40}})]) == "一" * 20 + "…",
  repr(rt([json.dumps({"type": "user", "message": {"content": "一" * 40}})])))
t("W9-11e 空文件不崩(回空串)", rt([]) == "", repr(rt([])))
_LN1 = json.dumps({"type": "user", "message": {"content": "<local-command-caveat>Caveat: 回显"}})
_LN2 = json.dumps({"type": "user", "message": {"content": '{"codesec_review":{"budget_secs":120}}'}})
t("W9-11f 命令回显/JSON 载荷跳过后取真实消息",
  rt([_LN1, _LN2, LINE_USER]) == "把纳管跑通", repr(rt([_LN1, _LN2, LINE_USER])))
t("W9-11g 全是控件文本则回空串(不当标题)", rt([_LN1, _LN2]) == "", repr(rt([_LN1, _LN2])))

# =====================================================================
# 汇总
# =====================================================================
t("W9-X 未写生产 state 目录", not WRITE_HITS, str(WRITE_HITS[:3]))

bad = [p for p in PASS if not p[1]]
for n, ok, d in PASS:
    print(("✓" if ok else "✗"), n, str(d)[:150])
print(f"\n{len(PASS)-len(bad)}/{len(PASS)} PASS")
sys.exit(1 if bad else 0)
