"""波次七修复的离线单测 — 状态瘦身(B1) / 去重表摊销压缩(B2) / 任务表读取缓存(B3)
落盘全部重定向到临时目录, 网络在 SYNC_DRY=1 下短路, 不碰生产 state/ 与飞书。
"""
import sys, os, ast, io, json, time, builtins, tempfile, shutil, threading, subprocess
os.environ.setdefault("SYNC_DRY", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tower_sync as fs

TMP = tempfile.mkdtemp(prefix="wave7-state-")
SRC = os.path.join(HERE, "tower_sync.py")
_src = open(SRC, encoding="utf-8").read()

for _m in (fs,):
    for _k in ("CMIDF", "JOBSF", "TEXTSENT", "STATE", "TOKF", "LOG", "STATED"):
        if hasattr(_m, _k):
            setattr(_m, _k, os.path.join(TMP, os.path.basename(getattr(_m, _k))))
SID = "01a0ab12-3cd4-5ef6-8090-aabbccddee01"
PASS = []
def t(n, c, d=""):
    PASS.append((n, bool(c), d))

# ---------- 通用打桩 ----------
OPEN_LOG = []
REAL_OPEN = builtins.open
def counting_open(path, mode="r", *a, **k):
    OPEN_LOG.append((str(path), mode))
    return REAL_OPEN(path, mode, *a, **k)
def reads_of(target):
    return sum(1 for p, m in OPEN_LOG if p == target and "r" in m and "w" not in m and "a" not in m)

# =====================================================================
# W7-1a: prune_state 剪掉只写不读的死键, 且不动任何活字段
# =====================================================================
LIVE = {"off": 123, "proj": "p", "task": "干活", "tdeep": True, "roll": [], "roll_ts": 1.0,
        "lev_ts": 2.0, "ev": 7, "has_msg": True, "armed": True, "tn": 3, "_msgseq": 2,
        "prev_seg": {"mid": "om_1", "no": 1, "total": 4, "peek": "ls"}, "gen": 1}
ST = dict(LIVE)
ST.update({"hist": ["一条很长的历史消息" * 30] * 5, "mdone": 9, "msgs_sent": 9,
           "dirty": True, "live_mid": "om_x", "live_ts": 9.0,
           "seg": {"no": 2, "mid": "om_2", "closed": False, "ts": 1.0, "gen": 1,
                   "roll": [["ls", "ls -l", 3], ["git", "git st", 1]]},
           "pending_seg": True})
n = fs.prune_state({"p1": ST})
t("W7-1 死键被剪光", not any(k in ST for k in fs.DEAD_ST_KEYS) and n == 6, f"n={n}")
t("W7-1 活字段一个没动", all(ST.get(k) == v for k, v in LIVE.items()),
  json.dumps({k: ST.get(k) for k, v in LIVE.items() if ST.get(k) != v}, ensure_ascii=False)[:120])
t("W7-1 未定格段的 roll 保留(还要渲染)", ST["seg"]["roll"] and len(ST["seg"]["roll"]) == 2,
  json.dumps(ST["seg"]["roll"])[:80])
# 已定格段: roll 清空, 但摘要 prev_seg / mid 留着
ST2 = {"seg": {"no": 1, "mid": "om_1", "closed": True, "ts": 1.0,
               "roll": [["a", "b", 9]] * 40}, "prev_seg": {"mid": "om_1", "total": 360}}
fs.prune_state({"p": ST2})
t("W7-1 已定格段的 roll 被回收", ST2["seg"]["roll"] == [] and ST2["seg"]["closed"] is True)
t("W7-1 回收 roll 不牵连 prev_seg 摘要", ST2.get("prev_seg", {}).get("total") == 360,
  json.dumps(ST2, ensure_ascii=False)[:90])
t("W7-1 空表/坏值不炸", fs.prune_state({}) == 0 and fs.prune_state(None) == 0
  and fs.prune_state({"x": "字符串"}) == 0)

# =====================================================================
# W7-1b: close_seg 定格后不再把 roll 留在状态里, 但最终卡必须已按全量渲染
# =====================================================================
ENQ = []
class FakeSender:
    class _Q:
        def put(self, x): ENQ.append(x)
    q = _Q()
    def enqueue(self, key, op, mid=None, card=None):
        ENQ.append((key, op, mid, card))
ORIG = {k: getattr(fs, k) for k in ("SENDER", "load_state", "save_state", "conf")}
fs.SENDER = FakeSender()
CFG = dict(fs.conf())
host = "h"
roll = [["ls", "ls -la", 5], ["grep", "grep x", 2], ["git", "git diff", 1]]
seg = {"no": 4, "mid": "om_seg4", "closed": False, "ts": time.time(), "gen": 0,
       "roll": [list(r) for r in roll]}
st3 = {"armed": True, "seg": seg, "proj": "p", "tn": 1}
fs.close_seg("p", st3, host, CFG)
body = json.dumps(ENQ[-1][3], ensure_ascii=False)
t("W7-1 定格卡按全量 roll 渲染(8项3类)", "已执行 **8** 项 · 3 类" in body, body[:120])
t("W7-1 定格卡里含命令明细", "ls -la" in body and "git diff" in body)
t("W7-1 定格后 roll 清空", st3["seg"]["roll"] == [] and st3["seg"]["closed"] is True)
t("W7-1 摘要 prev_seg 仍带 total/peek(收拢旧卡要用)",
  st3["prev_seg"]["total"] == 8 and st3["prev_seg"]["peek"] == "ls", str(st3["prev_seg"])[:80])
_no_mid = {"armed": True, "tn": 1, "proj": "p",
           "seg": {"no": 1, "mid": "", "closed": False, "ts": 0, "gen": 0,
                   "roll": [["x", "y", 2]]}}
fs.close_seg("p2", _no_mid, host, CFG)
t("W7-1 无 mid 的段仍走 clear_lost_roll 不丢命令",
  _no_mid["seg"]["roll"] == [["x", "y", 2]] and _no_mid["seg"]["no"] == 2
  and _no_mid["seg"]["mid"] == "", json.dumps(_no_mid, ensure_ascii=False)[:110])

# =====================================================================
# W7-1c: 源码不变量 — 这些名字只允许出现在 DEAD_ST_KEYS 里
# =====================================================================
TREE = ast.parse(_src)
def _calls(node, fname):
    return [x for x in ast.walk(node) if isinstance(x, ast.Call)
            and getattr(x.func, "id", getattr(x.func, "attr", "")) == fname]
def _fn(name):
    return next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
t("W7-1 load_state 加载即剪枝(AST)", bool(_calls(_fn("load_state"), "prune_state")),
  [ast.unparse(x) for x in _fn("load_state").body if isinstance(x, ast.Expr)][:3])
t("W7-1 死键不再被写进状态",
  all(f'st["{k}"]' not in _src and f'st.get("{k}"' not in _src and f'setdefault("{k}"' not in _src
      for k in fs.DEAD_ST_KEYS),
  [k for k in fs.DEAD_ST_KEYS if f'st["{k}"]' in _src])
t("W7-1 hist 队列与其配置项一起退役",
  '"hist_n"' not in _src and "keepq" not in _src and "hist_n" not in fs.DEF)
t("W7-1 消息事件分支仍能直达 send_msg_card(限 scan_dir 内)",
  bool(_calls(_fn("scan_dir"), "send_msg_card")) and bool(_calls(_fn("scan_dir"), "close_seg")))
_dicts = [n for n in ast.walk(_fn("scan_dir")) if isinstance(n, ast.Dict)]
_dead_init = [ast.unparse(n)[:40] for n in _dicts
              if any(isinstance(k, ast.Constant) and k.value in fs.DEAD_ST_KEYS for k in n.keys)]
t("W7-1 会话初始字典字面量里不再出现死键", bool(_dicts) and not _dead_init,
  f"字典字面量={len(_dicts)} 命中={_dead_init[:1]}")

# =====================================================================
# W7-1d: 端到端 — 沙箱里跑真守护: 预置脏状态 -> 事件 -> 定格 -> 落盘
# =====================================================================
SB = tempfile.mkdtemp(prefix="wave7-e2e-")
os.makedirs(f"{SB}/scripts"); os.makedirs(f"{SB}/config")
os.makedirs(f"{SB}/logs"); os.makedirs(f"{SB}/state"); os.makedirs(f"{SB}/home/.codex")
os.makedirs(f"{SB}/sess/proj")
shutil.copy(SRC, f"{SB}/scripts/tower_sync.py")
shutil.copy(os.path.join(HERE, "tower_ui.py"), f"{SB}/scripts/tower_ui.py")
for mod in ("tower_store.py", "tower_runtime.py", "tower_delivery.py", "tower_harness.py", "tower_questions.py", "tower_dsh.py", "tower_diagnostics.py", "tower_panel.py", "tower_retention.py"):
    shutil.copy(os.path.join(HERE, mod), f"{SB}/scripts/{mod}")
for f in ("tool-glossary.json",):
    shutil.copy(os.path.join(os.path.dirname(HERE), "config", f), f"{SB}/config/{f}")
with open(f"{SB}/config/stream.conf", "w", encoding="utf-8") as f:
    f.write("save_gap=1\narm_events=1\npoll=0.3\nlive_gap=0.2\nseg_mode=1\n")   # 不复制 feishu-app.json
RP = f"{SB}/sess/proj/rollout-2026-09-18-session-{SID}.jsonl"
def _ln(o): return json.dumps(o, ensure_ascii=False)
events = [
    {"type": "turn_context"},
    {"type": "response_item", "payload": {"type": "function_call", "name": "shell",
        "arguments": _ln({"command": ["ls", "-la"]})}},
    {"type": "response_item", "payload": {"type": "function_call", "name": "shell",
        "arguments": _ln({"command": ["git", "status"]})}},
    {"type": "response_item", "payload": {"type": "message", "role": "assistant",
        "content": [{"text": "本轮做完了"}]}},
]
REAL_OPEN(RP, "w", encoding="utf-8").write("\n".join(map(_ln, events)) + "\n")
# 预置"老状态": 带全部死键 + 一个已定格却仍挂着 roll 的段
seed_state = {"v": fs.VERSION, "files": {RP: {
    "off": 0, "proj": "p", "task": "旧任务", "tdeep": True, "roll": [], "roll_ts": 0,
    "hist": ["旧历史" * 200] * 5, "mdone": 12, "dirty": True, "live_mid": "om_z", "live_ts": 1.0,
    "msgs_sent": 8, "lev_ts": 0, "ev": 3, "has_msg": True, "armed": True, "tn": 1,
    "seg": {"no": 1, "mid": "om_old", "closed": True, "ts": 0, "gen": 0,
            "roll": [["ls", "ls", 400]] * 40}}}}
json.dump(seed_state, open(f"{SB}/state/sync-state.json", "w", encoding="utf-8"))
seed_bytes = len(json.dumps(seed_state))
env = {**os.environ, "HOME": f"{SB}/home", "CODEX_SESS_DIR": f"{SB}/sess",
       "SYNC_DRY": "1", "PATH": os.environ.get("PATH", "")}
p = subprocess.Popen([sys.executable, f"{SB}/scripts/tower_sync.py"], env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(6)
p.terminate()
try:
    out = p.communicate(timeout=5)[0]
except Exception:
    p.kill(); out = "TIMEOUT"
savelog = f"{SB}/logs/sync.log"
logtxt = REAL_OPEN(savelog, encoding="utf-8").read() if os.path.exists(savelog) else ""
after_state = json.load(REAL_OPEN(f"{SB}/state/sync-state.json", encoding="utf-8"))["files"]
stt = after_state.get(RP, {})
t("W7-1 端到端守护无异常退出", "Traceback" not in out and "watcher start" in logtxt,
  f"rc={p.returncode} out={out.strip()[:120]} log={logtxt.strip()[:80]}")
t("W7-1 端到端事件被消费(游标/回合号推进)",
  stt.get("off") == os.path.getsize(RP) and stt.get("tn") == 2 and stt.get("ev") == 7,
  json.dumps({k: stt.get(k) for k in ("off", "ev", "tn")}, ensure_ascii=False))
t("W7-1 端到端消息卡走完整链路(占坑需 Sender 确认)",
  os.path.exists(f"{SB}/state/.text_sent")
  and bool(stt.get('last_message_event'))
  and stt['last_message_event'] in REAL_OPEN(f"{SB}/state/.text_sent", encoding="utf-8").read(),
  REAL_OPEN(f"{SB}/state/.text_sent", encoding="utf-8").read()[:40] if os.path.exists(f"{SB}/state/.text_sent") else "无")
t("W7-1 端到端落盘状态已无死键",
  all(k not in stt for k in fs.DEAD_ST_KEYS),
  json.dumps(sorted(stt), ensure_ascii=False)[:130])
t("W7-1 端到端已定格段不再挂 roll",
  all(not (s.get("seg") or {}).get("roll") or not (s.get("seg") or {}).get("closed")
      for s in after_state.values()),
  json.dumps((stt.get("seg") or {}).get("roll"), ensure_ascii=False)[:80])
t("W7-1 端到端状态体积明显下降",
  len(json.dumps({"v": fs.VERSION, "files": after_state})) < seed_bytes,
  f"{seed_bytes} -> {len(json.dumps({'v': fs.VERSION, 'files': after_state}))}")
t("W7-1 端到端进程自我定位在沙箱(HQ 由脚本位置推导)",
  os.path.exists(f"{SB}/logs/sync.log") and os.path.exists(f"{SB}/state/sync-state.json"))

# =====================================================================
# W7-2: .text_sent 不再每卡整表重写
# =====================================================================
def reset_sent(lines=4000):
    fs.TEXTSENT = os.path.join(TMP, ".text_sent")
    with REAL_OPEN(fs.TEXTSENT, "w", encoding="utf-8") as f:
        f.write("".join(f"{fs.msg_hash('seed%d' % i)} 1758000000\n" for i in range(lines)))
    fs._SENT = None
    fs._SENT_LINES = 0
    fs.sent_set()                       # 预热: 冷启动那一次整表读是允许的
reset_sent()
size0 = os.path.getsize(fs.TEXTSENT)
builtins.open = counting_open
OPEN_LOG.clear()
for i in range(300):
    fs.record_sent(fs.msg_hash(f"card{i}"))
n_r = reads_of(fs.TEXTSENT)
n_w = sum(1 for p, m in OPEN_LOG if p == fs.TEXTSENT and "w" in m)
builtins.open = REAL_OPEN
t("W7-2 300张卡零整表读+零整表重写", n_r == 0 and n_w == 0, f"读{n_r} 整表写{n_w}")
ln = REAL_OPEN(fs.TEXTSENT, encoding="utf-8").read().splitlines()
newh = {fs.msg_hash(f"card{i}") for i in range(300)}
t("W7-2 300条新哈希全部落库", len(ln) == 4300 and newh <= {x.split(" ", 1)[0] for x in ln},
  f"lines={len(ln)} 缺{len(newh - {x.split(' ', 1)[0] for x in ln})}")
t("W7-2 追加量线性(~28B/条)", os.path.getsize(fs.TEXTSENT) - size0 in range(300*26, 300*34),
  f"+{os.path.getsize(fs.TEXTSENT)-size0}B")
# 阈值压缩: 小窗口下 40 张卡, 旧实现整表重写 40 次, 现在只在越线时重写
K, M = fs.SENT_KEEP, fs.SENT_MAX
fs.SENT_KEEP, fs.SENT_MAX = 5, 6
reset_sent(3)
seedmem = {fs.msg_hash("seed0"), fs.msg_hash("seed2")}
builtins.open = counting_open
OPEN_LOG.clear()
for i in range(40):
    fs.record_sent(fs.msg_hash(f"z{i}"))
n_w2 = sum(1 for p, m in OPEN_LOG if p == fs.TEXTSENT and "w" in m)
builtins.open = REAL_OPEN
ln2 = REAL_OPEN(fs.TEXTSENT, encoding="utf-8").read().splitlines()
t("W7-2 压缩按阈值摊销(40张卡 <40 次重写)", 1 <= n_w2 < 40, f"整表重写={n_w2}")
t("W7-2 文件行数始终被压在阈值内", len(ln2) <= fs.SENT_MAX and fs._SENT_LINES == len(ln2),
  f"lines={len(ln2)} n={fs._SENT_LINES}")
t("W7-2 压缩后内存去重集不缩水(与旧行为一致)",
  seedmem <= fs.sent_set() and {fs.msg_hash(f"z{i}") for i in range(40)} <= fs.sent_set(),
  f"mem={len(fs._SENT)} file={len(ln2)}")
fs.SENT_KEEP, fs.SENT_MAX = K, M
# 语义不变: 重复哈希返回 False / 首次 True
reset_sent(10)
h = fs.msg_hash("dup")
t("W7-2 去重语义不变(首True再False)", fs.record_sent(h) is True and fs.record_sent(h) is False)
# 重启窗口等价: 文件比窗口长时, 内存仍取最新 SENT_KEEP 条(旧实现文件已被压到同样尾部)
reset_sent(4000)
for i in range(2000):
    fs.record_sent(fs.msg_hash(f"tail{i}"))
allln = REAL_OPEN(fs.TEXTSENT, encoding="utf-8").read().splitlines()
fs._SENT = None
fs._SENT_LINES = 0
restarted = fs.sent_set()
t("W7-2 重启后去重窗口=最新 SENT_KEEP 条",
  len(allln) > fs.SENT_KEEP and restarted == {x.split(" ", 1)[0] for x in allln[-fs.SENT_KEEP:]}
  and len(restarted) == fs.SENT_KEEP,
  f"mem={len(restarted)} file={len(allln)} keep={fs.SENT_KEEP}")
# 文件不可写时仍不抛(fail-open), 且不虚增计数
BAD = os.path.join(TMP, "no-such-dir", ".text_sent")
keep, fs.TEXTSENT = fs.TEXTSENT, BAD
fs._SENT = None; fs._SENT_LINES = 0
err = None
try:
    ok = fs.record_sent("abc123")
except Exception as e:
    err = f"{type(e).__name__}: {e}"
t("W7-2 写盘失败不抛异常(fail-open)", err is None and ok is True, str(err))
t("W7-2 写失败不虚增行数", fs._SENT_LINES == 0, f"n={fs._SENT_LINES}")
fs.TEXTSENT = keep
# fd 泄漏: 旧实现 open(...,"w") 从不关闭, 每次重写漏一个 fd
reset_sent(4000)
fs.SENT_KEEP, fs.SENT_MAX = 5, 6
fds0 = len(os.listdir("/dev/fd"))
for i in range(40):
    fs.record_sent(fs.msg_hash(f"fd{i}"))
fds1 = len(os.listdir("/dev/fd"))
fs.SENT_KEEP, fs.SENT_MAX = K, M
t("W7-2 压缩路径不漏 fd", fds1 - fds0 <= 2, f"fd {fds0}->{fds1}")

# =====================================================================
# W7-3: ctl-jobs.json 的读取收敛到 mtime 缓存
# =====================================================================
JF = fs.JOBSF
def write_jobs(d, bump=1.0):
    with REAL_OPEN(JF + ".tmp", "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(JF + ".tmp", JF)
    st = os.stat(JF)
    os.utime(JF, (st.st_atime + bump, st.st_mtime + bump))     # 确保 mtime 一定变化
def warm():
    fs._JOBS_RAW = None; fs._JOBS_MT = 0.0
    fs._JIDS = {}; fs._JIDS_MT = 0.0; fs._JIDS_T = 0.0
RP2 = f"/tmp/x/rollout-2026-09-18-session-{SID}.jsonl"
JOBS = {"j1": {"jid": "j1", "title": "标题甲", "root_mid": "om_root1", "sess": SID, "out_file": ""}}
write_jobs(JOBS); warm()
t("W7-3 jid 反查仍命中(删死代码不回归)", fs.jid_of(RP2) == "j1", fs.jid_of(RP2))
t("W7-3 标题与话题归属都取到", fs.custom_title(RP2) == "标题甲"
  and fs.value_root(("msg", RP2, 1)) == "om_root1",
  f"{fs.custom_title(RP2)}|{fs.value_root(('msg', RP2, 1))}")
builtins.open = counting_open
OPEN_LOG.clear()
for _ in range(30):
    fs.custom_title(RP2)
    fs.value_root(("msg", RP2, 1))
n3 = reads_of(JF)
builtins.open = REAL_OPEN
t("W7-3 稳态 60 次渲染零整表读取", n3 == 0, f"reads={n3}")
builtins.open = counting_open
OPEN_LOG.clear()
warm()                                              # 两份缓存全清 = 真冷启动
fs.custom_title(RP2)
n_cold = reads_of(JF)
builtins.open = REAL_OPEN
t("W7-3 冷启动只解析一次(两处读取合一)", n_cold == 1, f"reads={n_cold}")
# mtime 变了必须立刻看见(缓存不能吃掉 #title / root_mid 更新)
write_jobs({**JOBS, "j1": {**JOBS["j1"], "title": "标题乙", "root_mid": "om_root2"}})
t("W7-3 任务表更新后缓存即时失效", fs.custom_title(RP2) == "标题乙"
  and fs.value_root(("msg", RP2, 1)) == "om_root2", fs.custom_title(RP2))
builtins.open = REAL_OPEN
OPEN_LOG.clear()
builtins.open = counting_open
fs.custom_title(RP2)
fs.value_root(("msg", RP2, 1))
n_same = reads_of(JF)
builtins.open = REAL_OPEN
t("W7-3 mtime 未变时不重复解析", n_same == 0, f"reads={n_same}")
# 文件缺失/坏 JSON: 沿用上次缓存, 首次则空表(旧行为是异常->"")
keep_jf = fs.JOBSF
fs.JOBSF = os.path.join(TMP, "gone.json")
t("W7-3 任务表消失时沿用缓存不崩",
  isinstance(fs.jobs_raw(), dict) and fs.value_root(("msg", RP2, 1)) == "om_root2",
  str(fs.jobs_raw())[:60])
fs._JOBS_RAW = None; fs._JOBS_MT = 0.0; fs._JIDS = {}; fs._JIDS_MT = 0.0; fs._JIDS_T = 0.0
t("W7-3 无文件且无缓存→空表且标题/话题为空",
  fs.jobs_raw() == {} and fs.custom_title(RP2) == "" and fs.value_root(("msg", RP2, 1)) == "",
  str(fs.jobs_raw())[:40])
fs.JOBSF = keep_jf
warm()                                              # 连 jid_map 的 5s 空表缓存一起清
write_jobs({**JOBS, "j1": {**JOBS["j1"], "title": "标题甲"}})
t("W7-3 回到可读文件先固化缓存", fs.custom_title(RP2) == "标题甲", fs.custom_title(RP2))
REAL_OPEN(keep_jf, "w", encoding="utf-8").write("{trun")     # 模拟 ctl 写盘被读到半截
st = os.stat(keep_jf)
os.utime(keep_jf, (st.st_atime + 3, st.st_mtime + 3))
t("W7-3 半截JSON沿用上次缓存(不被写盘瞬间打穿)",
  fs.custom_title(RP2) == "标题甲" and fs.jobs_raw().get("j1", {}).get("title") == "标题甲",
  fs.custom_title(RP2))
write_jobs({**JOBS, "j1": {**JOBS["j1"], "title": "标题丙"}})
t("W7-3 文件修好后立刻读到新内容", fs.custom_title(RP2) == "标题丙", fs.custom_title(RP2))
# 双线程(扫描线程 + Sender 线程)并发只读不打架
write_jobs(JOBS)
warm()
ERRS = []
def _thr():
    try:
        for _ in range(200):
            fs.custom_title(RP2); fs.value_root(("msg", RP2, 1))
    except Exception as e:
        ERRS.append(repr(e))
def _writer():
    try:
        for i in range(60):
            write_jobs({**JOBS, "j1": {**JOBS["j1"], "title": f"并发改{i}"}}, bump=0.0)
    except Exception as e:
        ERRS.append(repr(e))
ths = [threading.Thread(target=_thr) for _ in range(4)] + [threading.Thread(target=_writer)]
[x.start() for x in ths]; [x.join(10) for x in ths]
t("W7-3 扫描/Sender 双线程并发读无异常", not ERRS and not any(x.is_alive() for x in ths), str(ERRS[:1]))
t("W7-3 并发写后仍能读到最终值", fs.custom_title(RP2).startswith("并发改"), fs.custom_title(RP2))
t("W7-3 jobs_raw 之外不再有裸读",
  _src.count("json.load(open(JOBSF") == 1 and "def jobs_raw" in _src,
  f'裸读={_src.count("json.load(open(JOBSF")}')
t("W7-3 jid_map 里那段没人用的 out_file 映射已删", "m[f] = jid" not in _src)

# ---------- 隔离与汇总 ----------
fs.SENDER = ORIG["SENDER"]
PROD = os.path.join(os.path.dirname(HERE), "state")
t("W7 全部落盘路径在沙箱内",
  all(getattr(fs, k).startswith(TMP) or getattr(fs, k).startswith(SB)
      for k in ("JOBSF", "TEXTSENT", "STATE", "CMIDF", "TOKF", "LOG")),
  str([getattr(fs, k) for k in ("JOBSF", "TEXTSENT", "STATE")]))
t("W7 生产 state 未被本轮测试写入",
  not os.path.exists(os.path.join(PROD, "sync-state.json.unittest"))
  and fs.STATE != os.path.join(PROD, "sync-state.json"))
bad = [p for p in PASS if not p[1]]
for nm, ok, d in PASS:
    print(("✓" if ok else "✗"), nm, d)
print(f"\n{len(PASS)-len(bad)}/{len(PASS)} PASS")
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(SB, ignore_errors=True)
sys.exit(1 if bad else 0)
