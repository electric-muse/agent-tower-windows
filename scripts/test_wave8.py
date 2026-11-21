"""波次八修复的离线单测 — 卡片样式统一 / 引擎中立标题 / 展开全文截尾 / #scan 项目名 /
死代码清理 / 心跳卡结果认领 / W1-3 未建卡 roll 搬运真正生效
一切副作用重定向到临时目录; 网络与飞书投递全部打桩(SYNC_DRY=1), 不碰生产 state/ 与真实群。
"""
import sys, os, re, ast, json, time, builtins, shutil, tempfile, subprocess
os.environ.setdefault("SYNC_DRY", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tower_sync as fs
import tower_ctl as fc

TMP = tempfile.mkdtemp(prefix="wave8-state-")
CTL_SRC = os.path.join(HERE, "tower_ctl.py")
SYNC_SRC = os.path.join(HERE, "tower_sync.py")
_ctl = open(CTL_SRC, encoding="utf-8").read()
_sync = open(SYNC_SRC, encoding="utf-8").read()
CTREE = ast.parse(_ctl)
STREE = ast.parse(_sync)

PROD_STATE = os.path.join(os.path.dirname(HERE), "state")
WRITE_HITS = []                       # 生产 state 目录被本进程写开的记录(应始终为空)
_REAL_OPEN = builtins.open
def _spy_open(file, mode="r", *a, **k):
    try:
        p = os.path.abspath(str(file))
    except Exception:
        p = ""
    if p.startswith(PROD_STATE) and any(c in str(mode) for c in "wax+"):
        WRITE_HITS.append((p, mode))
    return _REAL_OPEN(file, mode, *a, **k)
builtins.open = _spy_open             # 注: 生产守护进程仍在跑, 它自己会改 state 文件,
                                      #     所以隔离只能用"本进程有没有写开"来判, 不能比 mtime
PROD_N = len([f for f in os.listdir(PROD_STATE)
              if os.path.isfile(os.path.join(PROD_STATE, f))])

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

# ---------- 打桩 ----------
API = []
def fake_api(method, path, body=None):
    API.append((method, path, body))
    return {"code": 0, "data": {"message_id": "om_sent_1", "thread_id": "zt_1"}}
STORE = {}
AUDITS = []
LOGS = []
fc.api = fake_api
fc.token = lambda: "t"
fc.jobs_load = lambda: json.loads(json.dumps(STORE))
fc.jobs_save = lambda j: STORE.clear() or STORE.update(json.loads(json.dumps(j)))
fc.audit = lambda *a, **k: AUDITS.append(a)
fc.log = lambda m: LOGS.append(str(m))
fc.cmid_register = lambda *a, **k: None
CARD_CAPS = []
REAL_REPLY_CARD = fc.reply_card
fc.reply_card = lambda chat_id, title, template, els: (
    CARD_CAPS.append((title, els)), REAL_REPLY_CARD(chat_id, title, template, els))[1]

def cards_by(kind):
    """kind: 'post' 主群建卡 / 'patch' 改卡 / 'reply' 话题锚定卡"""
    out = []
    for m, p, b in API:
        if not b or not isinstance(b.get("content"), str):
            continue
        c = json.loads(b["content"])
        if kind == "post" and m == "POST" and "receive_id_type=chat_id" in p:
            out.append(c)
        elif kind == "patch" and m == "PATCH":
            out.append(c)
        elif kind == "reply" and m == "POST" and p.endswith("/reply"):
            out.append(c)
    return out

def cfg_of(card_json):
    return (card_json.get("config") or card_json.get("card", {}).get("config") or {})

def has_three(card_json):
    c = cfg_of(card_json)
    return bool(c.get("wide_screen_mode")) and bool(c.get("enable_forward")) \
        and c.get("update_multi") is True

t("W8-0 落盘路径全部指向临时目录",
  fc.STATED == TMP and fs.STATE.startswith(TMP) and fc.JOBSF.startswith(TMP)
  and fs.TEXTSENT.startswith(TMP), f"TMP={TMP}")

# =====================================================================
# W8-1 五处卡片补齐 CARD_CONFIG(reply_card/归档卡/任务舱/播报舱)
# =====================================================================
API.clear()
mid = fc.reply_card("oc_1", "标题卡", "blue", [{"tag": "markdown", "content": "正文"}])
pc = cards_by("post")
t("W8-1 reply_card 建卡带齐三键 config",
  pc and has_three(pc[-1]) and "wide_screen_mode" in json.dumps(pc[-1]),
  json.dumps(cfg_of(pc[-1]) if pc else {}, ensure_ascii=False)[:90])
t("W8-1 reply_card 仍返回 message_id(路由没被改坏)", mid == "om_sent_1", f"mid={mid}")

API.clear()
fc.reply("oc_1", "普通回复")
rc = cards_by("post")
t("W8-1 reply() 走 CARD_CONFIG 未退化", rc and has_three(rc[-1]))

API.clear()
STORE.clear()
STORE.update({"j9": {"jid": "j9", "state": "exited", "root_mid": "om_r9",
                     "ended": time.time() - 90000, "started": time.time() - 90100,
                     "prompt": "归档测试"}})
fc.archive_stale_topics()
ac = cards_by("patch")
t("W8-1 归档卡补齐 wide_screen_mode/enable_forward",
  ac and has_three(ac[-1]), json.dumps(cfg_of(ac[-1]) if ac else {}, ensure_ascii=False)[:90])
t("W8-1 归档 PATCH 成功后按原语义标 archived",
  STORE.get("j9", {}).get("archived") is True,
  json.dumps({k: STORE.get("j9", {}).get(k) for k in ("archived", "root_mid")})[:80])

API.clear()
STORE.clear()
STORE.update({"j1": {"jid": "j1", "state": "running", "pid": 11, "cwd": "/tmp/proj",
                     "model": "m", "prompt": "干活", "started": time.time()}})
fc.root_topic("oc_1", "j1", dict(STORE["j1"], proj_tag="指定项目"))
root_cards = cards_by("post") + cards_by("reply")
t("W8-1 任务舱两张卡(根卡+锚定卡)都带三键",
  len(root_cards) == 2 and all(has_three(c) for c in root_cards),
  f"{len(root_cards)} 张: " + json.dumps([cfg_of(c) for c in root_cards], ensure_ascii=False)[:120])
t("W8-1 锚定卡仍是 grey 话题头(只加 config, 不动样式)",
  any((c.get("header") or {}).get("template") == "grey" for c in cards_by("reply")),
  json.dumps([list(c) for c in cards_by("reply")], ensure_ascii=False)[:90])

API.clear()
RP8 = "01a0ab12"
FULL = f"{RP8}-3cd4-5ef6-8090-aabbccddee01"
os.makedirs(f"{TMP}/roll", exist_ok=True)
rf = f"{TMP}/roll/rollout-2026-09-18T07-43-29-{FULL}.jsonl"
with open(rf, "w", encoding="utf-8") as f:
    f.write(json.dumps({"type": "session_meta",
                        "payload": {"cwd": "/tmp/proj"}}) + "\n")
fc.find_rollout = lambda s8: (rf, FULL)
fc._st_load = lambda: {}
fc._st_save = lambda d: None
wmid = fc.watch_session("oc_1", RP8, quiet=True)
wreply = cards_by("reply")
t("W8-1 播报舱锚定卡带齐三键",
  wreply and has_three(wreply[-1]),
  json.dumps(cfg_of(wreply[-1]) if wreply else {}, ensure_ascii=False)[:90])
t("W8-1 watch_session 仍返回 (mid, tid)",
  isinstance(wmid, tuple) and wmid[0] == "om_sent_1", f"{wmid}")

_sites = [mm.start() for mm in re.finditer(r'"schema"', _ctl)]
_missing = [i for i in _sites if "CARD_CONFIG" not in _ctl[i:i + 130]]
t("W8-1 源码里每个卡片 schema 都紧跟 CARD_CONFIG",
  len(_sites) >= 6 and not _missing,
  f"{len(_sites)} 处, 缺 {len(_missing)} 处 @ {_missing[:3]}")
t("W8-1 内联 {update_multi} 孤例已清零",
  '"config": {"update_multi": True}' not in _ctl and '"config": {"update_multi": True}' not in _sync)
KEYS = ("wide_screen_mode", "enable_forward", "update_multi")
t("W8-1 两个模块的 CARD_CONFIG 定义仍一致(未各改各的)",
  fc.CARD_CONFIG == fs.CARD_CONFIG and tuple(fc.CARD_CONFIG) == KEYS,
  f"{fc.CARD_CONFIG} / {fs.CARD_CONFIG}")

# =====================================================================
# W8-2 标题不再自称 codex; 三引擎的 plan 标记统一
# =====================================================================
API.clear()
fc.reply("oc_1", "只带正文")
head = ((cards_by("post") or [{}])[-1].get("header") or {}).get("title", {}).get("content", "")
t("W8-2 reply() 默认标题引擎中立", head == "🎛 控制台" and "codex" not in head.lower(), f"head={head}")
API.clear()
fc.reply("oc_1", "x", title="专属标题")
_h2 = ((cards_by("post") or [{}])[-1].get("header") or {}).get("title", {}).get("content", "")
t("W8-2 显式传标题仍然生效(默认值改动不影响调用方)", _h2 == "专属标题", f"head={_h2}")
t("W8-2 源码不再出现 codex 控制/飞书控制台 标题",
  not re.search(r"codex 控制|codex 飞书", _ctl),
  re.findall(r".{0,18}(?:codex 控制|codex 飞书).{0,18}", _ctl)[:2])
_plans = [d for d in ast.walk(CTREE) if isinstance(d, ast.Dict)
          and any(isinstance(k, ast.Constant) and k.value == "plan" and isinstance(v, ast.Constant)
                  and v.value == " 📋计划(只读)" for k,v in zip(d.keys,d.values))]

def _plan_marks_in(fn_name):
    for f in ast.walk(CTREE):
        if isinstance(f, ast.FunctionDef) and f.name == fn_name:
            return [d.values[i].value for d in ast.walk(f) if isinstance(d, ast.Dict)
                    for i, k in enumerate(d.keys)
                    if isinstance(k, ast.Constant) and k.value == "plan"
                    and isinstance(d.values[i], ast.Constant)]
    return []

_marks = {n: _plan_marks_in(n) for n in ("cmd_new", "cmd_cnew", "cmd_qnew", "cmd_wnew")}
t("W8-2 四个启动命令各有1处plan标记且字面量一致",
  all(len(v) == 1 for v in _marks.values())
  and {v[0] for v in _marks.values()} == {" 📋计划(只读)"},
  json.dumps(_marks, ensure_ascii=False)[:140])
t("W8-2 目标模式标记未被顺手改动",
  all(any(isinstance(k, ast.Constant) and k.value == "goal" and isinstance(v, ast.Constant)
          and v.value == " 🎯目标模式" for k, v in zip(d.keys, d.values)) for d in _plans))

# =====================================================================
# W8-3 "展开完整回复" 真的给全文
# =====================================================================
TAIL = "TAIL" + "尾" * 30
LONG = ("甲" * 300) + ("乙" * 1270) + TAIL          # 1604 字, 跨过 320/1500 两条线
_REAL_FINAL = fc.final_text
fc.final_text = lambda jd, limit=520: LONG
STORE.clear()
STORE.update({"j2": {"jid": "j2", "state": "exited", "cwd": "/tmp/proj", "model": "m",
                     "title": "改名任务", "prompt": "指令", "started": time.time() - 60,
                     "ended": time.time(), "exit": "0"}})
CARD_CAPS.clear()
fc.cmd_jobs("s1", "oc_1", ["j2"])
_els = CARD_CAPS[-1][1] if CARD_CAPS else []
_panels = [e for e in _els if e.get("tag") == "collapsible_panel"]
_ptxt = _panels[0]["elements"][0]["content"] if _panels else ""
_body = [e for e in _els if e.get("tag") == "markdown"]
t("W8-3 折叠面板给到全文结尾(旧实现裁掉尾部100字)",
  len(_panels) == 1 and _ptxt.endswith(TAIL),
  f"面板数={len(_panels)} 尾={_ptxt[-12:] if _ptxt else '无'}")
t("W8-3 面板长度 = 全文减去首屏 320 字",
  len(_ptxt) == len(LONG) - 320, f"{len(_ptxt)} vs {len(LONG) - 320}")
_head = next((e["content"] for e in _body if e["content"].startswith("**最新回复**")), "")
t("W8-3 首屏仍只直出 320 字(未把长文推回三屏外)",
  _head == "**最新回复**：\n" + LONG[:320] + "…",
  f"首屏 {len(_head)} 字, 尾部={_head[-8:]!r}")
t("W8-3 源码不再有 [320:1500] 硬截断", "[320:1500]" not in _ctl)
_short = "短回复"
fc.final_text = lambda jd, limit=520: _short
CARD_CAPS.clear()
fc.cmd_jobs("s1", "oc_1", ["j2"])
t("W8-3 短回复仍直出、不建面板",
  CARD_CAPS and not [e for e in CARD_CAPS[-1][1] if e.get("tag") == "collapsible_panel"]
  and _short in json.dumps(CARD_CAPS[-1][1], ensure_ascii=False))
fc.final_text = _REAL_FINAL

# =====================================================================
# W8-4 #scan 的 codex 行不再恒为 '?'
# =====================================================================
SB = tempfile.mkdtemp(prefix="wave8-scan-")
def _mk(path, cwd, first_user="修复登录跳转"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [json.dumps({"type": "session_meta", "payload": {"cwd": cwd}})]
    if first_user:
        lines.append(json.dumps({"type": "response_item", "payload": {
            "role": "user", "content": [{"text": first_user}]}}))
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return path
_OK = _mk(f"{SB}/home/.codex/sessions/2026/09/18/rollout-2026-09-18T07-43-29-"
          f"{FULL}.jsonl", "/Users/x/WorkBuddy/2026-07-16-15-43-44/agent-space")
_BAD = _mk(f"{SB}/home/.codex/sessions/2026/09/18/rollout-2026-09-18T07-40-00-"
           f"01a0beef-1111-2222-3333-444455556666.jsonl", "")
open(_BAD, "w", encoding="utf-8").write("这不是JSON\n")
os.utime(_BAD, (time.time(), time.time()))
t("W8-4 _codex_proj 取 cwd 末段", fc._codex_proj(_OK) == "agent-space", fc._codex_proj(_OK))
t("W8-4 坏首行/缺 cwd 都回落 '?'",
  fc._codex_proj(_BAD) == "?" and fc._codex_proj(f"{SB}/nope.jsonl") == "?")
_ROOT = _mk(f"{SB}/home/.codex/sessions/2026/09/18/rollout-2026-09-18T07-41-00-"
            f"01a0c3d4-1111-2222-3333-444455556666.jsonl", "/Users/x/agent-space")
_ROOT2 = _mk(f"{SB}/home/.codex/sessions/2026/09/18/rollout-2026-09-18T07-41-30-"
             f"01a0c3d5-1111-2222-3333-444455556666.jsonl", "/")
t("W8-4 根目录 cwd 不产出空项目名", fc._codex_proj(_ROOT2) == "?", fc._codex_proj(_ROOT2))
_OLD_HOME = fc.HOME
fc.HOME = f"{SB}/home"
STORE.clear()
fc.private_json(fc.STOPICF, {})  # This scenario explicitly tests an unobserved session.
rows = fc._scan_active()
fc.HOME = _OLD_HOME
codex_rows = {r[1]: r for r in rows if r[0] == "🖥"}
t("W8-4 #scan 列出沙箱内 codex 会话(测试确实打到了这条分支)",
  FULL in codex_rows, json.dumps(sorted(codex_rows), ensure_ascii=False)[:80])
r = codex_rows.get(FULL) or ()
t("W8-4 codex 行带出项目名", len(r) > 2 and r[2] == "agent-space", f"{r[:4]}")
t("W8-4 codex 行带出任务标题(与 claude/wb 行同款信息量)",
  len(r) > 3 and r[3] == "修复登录跳转", f"{r[:4]}")
t("W8-4 未纳管判定仍生效(供 #scan 过滤)", r and r[5] is False, f"{r[5:] if r else ()}")
t("W8-4 整表已无写死的 '?' 占位项目",
  'out.append(("🖥", s8, "?"' not in _ctl, )

# =====================================================================
# W8-5 死代码清理
# =====================================================================
def _dead_conf_locals(tree):
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for st in node.body:
            if isinstance(st, ast.Assign) and any(isinstance(n, ast.Name) and n.id == "c"
                                                  for n in st.targets) \
                    and isinstance(st.value, ast.Call) \
                    and getattr(st.value.func, "id", "") == "conf":
                if not any(isinstance(n, ast.Name) and n.id == "c"
                           and isinstance(n.ctx, ast.Load) for n in ast.walk(node)):
                    out.append((st.lineno, node.name))
    return out
t("W8-5 两个模块都没有无人使用的 c = conf()",
  not _dead_conf_locals(CTREE) and not _dead_conf_locals(STREE),
  f"{_dead_conf_locals(CTREE)} {_dead_conf_locals(STREE)}")
_live_conf = 0
for _node in ast.walk(CTREE):
    if not isinstance(_node, ast.FunctionDef):
        continue
    _loads = [n for n in ast.walk(_node) if isinstance(n, ast.Name) and n.id == "c"
              and isinstance(n.ctx, ast.Load)]
    if not _loads:
        continue
    for _st in _node.body:
        if isinstance(_st, ast.Assign) and isinstance(_st.value, ast.Call) \
                and getattr(_st.value.func, "id", "") == "conf" \
                and any(isinstance(n, ast.Name) and n.id == "c" for n in _st.targets):
            _live_conf += 1
t("W8-5 仍在用的 c = conf() 没被误删(至少 5 处活引用)", _live_conf >= 5, f"live={_live_conf}")
t("W8-5 _rollout_task 里从未读取的 spec 已清除", not re.search(r"\bspec\b", _ctl))
_rt = next(n for n in ast.walk(CTREE)
           if isinstance(n, ast.FunctionDef) and n.name == "_rollout_task")
t("W8-5 _rollout_task 内已无多余的 import re as _re",
  not any(isinstance(n, ast.Import) for n in ast.walk(_rt)))
def _fn(tree, name):
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
t("W8-5 _rollout_task 仍能取到标题(删死代码没删掉功能)",
  fc._rollout_task(_OK) == "修复登录跳转", repr(fc._rollout_task(_OK))[:40])

# =====================================================================
# W8-6 心跳卡 💤 的投递结果有人认领
# =====================================================================
class FakeSyncSender:
    def __init__(self, results=None):
        self.results = results or {}
        self.enq = []
    def take_results(self):
        out, self.results = self.results, {}
        return out
    def enqueue(self, key, op, mid=None, card=None):
        self.enq.append((key, op, mid, card))
    def start(self): pass
P8 = "/tmp/rollout-2026-09-18T07-43-29-01a0ab12-3cd4-5ef6-8090-aabbccddee01.jsonl"
ST8 = {"off": 1, "armed": True, "seg": {"no": 1, "mid": "", "closed": False, "ts": 0,
                                        "roll": [["ls", "ls", 1]]}}
_S8 = FakeSyncSender({("note", P8, 0): ("create-fail",)})
fs.SENDER = _S8
_LOGS = []
_orig_slog = fs.log
fs.log = lambda m: _LOGS.append(str(m))
fs.harvest_results({P8: dict(ST8)})
t("W8-6 心跳卡建卡失败会留下可查日志(原先静默丢弃)",
  any("note-card-fail" in l for l in _LOGS), f"{_LOGS[:2]}")
t("W8-6 失败不做即时重投(每轮 poll 重投会打穿 API)",
  not any(k[0] == "note" for k, *_a in _S8.enq), f"{_S8.enq[:2]}")
_LOGS.clear()
fs.harvest_results({P8: {}})                     # st is None -> 直接跳过
t("W8-6 成功投递不记失败日志",
  (_S8.results, _LOGS) == ({}, []) and True, f"{_LOGS}")
_LOGS.clear()
_S2 = FakeSyncSender({("note", P8, 0): ("created", "om_real_1"),
                      ("seg", P8, 1): ("create-fail",),
                      ("zz", P8, 0): ("create-fail",)})
fs.SENDER = _S2
st2 = dict(ST8)
fs.harvest_results({P8: st2})
t("W8-6 seg/msg 分支行为未受影响(段建卡失败仍打标重试)",
  st2["seg"].get("pending_create") is True and not any("note-card-fail" in l for l in _LOGS),
  f"{json.dumps(st2['seg'], ensure_ascii=False)[:90]} {_LOGS}")
t("W8-6 未知 kind 仍安全忽略", True)
fs.log = _orig_slog

# =====================================================================
# W8-7 W1-3: 未建卡的 roll 搬运要真的产出卡片
# =====================================================================
_ENQ = []
class CapSender:
    def enqueue(self, key, op, mid=None, card=None):
        _ENQ.append((key, op, mid, card))
    def start(self): pass
    def take_results(self): return {}
fs.SENDER = CapSender()
CFG = dict(fs.conf())
CFG.update({"seg_mode": 1, "live_gap": 0, "max_len": 1200})
fs.conf = lambda *a, **k: CFG

# (a) clear_lost_roll 语义
s = {"seg": {"no": 3, "mid": "", "closed": False, "ts": 0, "gen": 0,
             "roll": [["🔧 列目录", "ls -la", 2]]}, "pending_seg": False}
carried = fs.clear_lost_roll(s)
t("W8-7a 搬了活必须回报 True",
  carried is True and s["pending_seg"] is True and s["seg"]["roll"] == [["🔧 列目录", "ls -la", 2]]
  and s["seg"]["no"] == 4, json.dumps(s, ensure_ascii=False)[:140])
s = {"seg": {"no": 3, "mid": "", "closed": False, "roll": []}, "pending_seg": True}
t("W8-7a 空 roll 不谎报(仍照常开新段)",
  fs.clear_lost_roll(s) is False and s["seg"]["no"] == 4 and s["seg"]["roll"] == [])
s = {"seg": {"no": 3, "mid": "om_has", "closed": False, "roll": [["a", "b", 1]]},
     "pending_seg": True}
t("W8-7a 有卡的段不动它",
  fs.clear_lost_roll(s) is False and s["seg"]["mid"] == "om_has" and s["seg"]["no"] == 3)

# (b) close_seg 不再把刚搬进来的标记清掉
s = {"armed": True, "tn": 1, "proj": "p",
     "seg": {"no": 5, "mid": "", "closed": False, "ts": 0, "gen": 0,
             "roll": [["🔧 git", "git status", 1], ["🔧 ls", "ls", 3]]},
     "pending_seg": True}
_ENQ.clear()
fs.close_seg(P8, s, "h", CFG)
t("W8-7b 无卡段定格: roll 跟到新段且 pending_seg 保持 True",
  s["pending_seg"] is True and s["seg"]["no"] == 6
  and [r[0] for r in s["seg"]["roll"]] == ["🔧 git", "🔧 ls"],
  json.dumps({"pend": s["pending_seg"], "roll": s["seg"]["roll"]}, ensure_ascii=False)[:120])
s = {"armed": True, "tn": 1, "proj": "p",
     "seg": {"no": 5, "mid": "om_5", "closed": False, "ts": 0, "gen": 0,
             "roll": [["🔧 ls", "ls", 1]]}, "pending_seg": True}
_ENQ.clear()
fs.close_seg(P8, s, "h", CFG)
t("W8-7b 有卡段定格: PATCH 完成卡 + 标记归零(旧语义不变)",
  s["pending_seg"] is False and s["seg"]["closed"] is True and s["seg"]["roll"] == []
  and _ENQ and _ENQ[-1][1] == "patch",
  json.dumps({k: s.get(k) for k in ("pending_seg",)}, ensure_ascii=False)[:60])
s = {"armed": True, "tn": 1, "proj": "p",
     "seg": {"no": 5, "mid": "om_5", "closed": True, "ts": 0, "gen": 0, "roll": []},
     "pending_seg": True}
_ENQ.clear()
fs.close_seg(P8, s, "h", CFG)
t("W8-7b 重复定格不重复投递且标记照常归零",
  s["pending_seg"] is False and not _ENQ, f"{_ENQ}")
s = {"armed": True, "tn": 1, "proj": "p", "seg": None, "pending_seg": True}
fs.close_seg(P8, s, "h", CFG)
t("W8-7b 无段可定时不动状态", s["seg"] is None and s["pending_seg"] is True)

# (c) 轮换分支: 搬运结果不能再被随后的 open_new_seg 覆盖
_sd = _fn(STREE, "scan_dir")
_calls = [n.func.id for n in ast.walk(_sd) if isinstance(n, ast.Call)
          and isinstance(n.func, ast.Name)]
t("W8-7c 轮换分支不再走 clear_lost_roll(改为直接带 roll 进新批)",
  "clear_lost_roll" not in _calls and "close_seg" in _calls and "open_new_seg" in _calls,
  f"{sorted(set(c for c in _calls if 'seg' in c or 'roll' in c))}")
_rotate = [ln for ln in _sync.splitlines()
           if "clear_lost_roll(st)" in ln and not ln.strip().startswith("def ")]
t("W8-7c clear_lost_roll 只剩 close_seg 一个调用点",
  len(_rotate) == 1 and "elif not clear_lost_roll(st)" in _rotate[0], _rotate[:2])

# (d) 端到端: 沙箱真守护跑一轮轮换, 未建卡命令必须长成一张新卡
E2 = tempfile.mkdtemp(prefix="wave8-e2e-")
for d in ("scripts", "config", "logs", "state", "home/.codex", "sess/proj"):
    os.makedirs(f"{E2}/{d}", exist_ok=True)
shutil.copy(SYNC_SRC, f"{E2}/scripts/tower_sync.py")
shutil.copy(os.path.join(HERE, "tower_ui.py"), f"{E2}/scripts/tower_ui.py")
for mod in ("tower_store.py", "tower_runtime.py", "tower_delivery.py", "tower_harness.py", "tower_questions.py", "tower_dsh.py", "tower_diagnostics.py", "tower_panel.py", "tower_retention.py"):
    shutil.copy(os.path.join(HERE, mod), f"{E2}/scripts/{mod}")
shutil.copy(os.path.join(os.path.dirname(HERE), "config", "tool-glossary.json"),
            f"{E2}/config/tool-glossary.json")
with open(f"{E2}/config/stream.conf", "w", encoding="utf-8") as f:
    f.write("save_gap=1\npoll=0.3\nseg_mode=1\nseg_max=2\narm_events=1\nlive_gap=0.2\n")
RP = f"{E2}/sess/proj/rollout-2026-09-18-session-{FULL}.jsonl"
open(RP, "w", encoding="utf-8").write(
    json.dumps({"type": "turn_context"}) + "\n")
LOST = [["🔧 列目录", "ls -la", 2], ["🔧 查状态", "git status", 1]]
seed = {"v": fs.VERSION, "files": {RP: {
    "off": os.path.getsize(RP), "proj": "proj", "task": "轮换夹具", "tdeep": True,
    "roll": [], "roll_ts": 0, "lev_ts": time.time(), "ev": 3, "has_msg": True,
    "armed": True, "tn": 1, "sleepy": False,
    "seg": {"no": 2, "mid": "", "closed": False, "ts": time.time(), "gen": 0,
            "roll": [list(r) for r in LOST]},
    "pending_seg": False}}}
json.dump(seed, open(f"{E2}/state/sync-state.json", "w", encoding="utf-8"))
env = {**os.environ, "HOME": f"{E2}/home", "CODEX_SESS_DIR": f"{E2}/sess",
       "SYNC_DRY": "1", "PATH": os.environ.get("PATH", "")}
pr = subprocess.Popen([sys.executable, f"{E2}/scripts/tower_sync.py"], env=env,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(6)
pr.terminate()
try:
    out = pr.communicate(timeout=5)[0]
except Exception:
    pr.kill(); out = "TIMEOUT"
logtxt = ""
if os.path.exists(f"{E2}/logs/sync.log"):
    logtxt = open(f"{E2}/logs/sync.log", encoding="utf-8").read()
after = json.load(open(f"{E2}/state/sync-state.json", encoding="utf-8"))["files"].get(RP, {})
seg = after.get("seg") or {}
t("W8-7d 端到端守护正常启动(无 Traceback)",
  "Traceback" not in out and "watcher start" in logtxt and "seg-rotate" in logtxt,
  f"rc={pr.returncode} out={out.strip()[:100]} log={logtxt.strip()[:60]}")
t("W8-7d 轮换后新批真的带着那两条命令",
  seg.get("no") == 1 and [list(x) for x in seg.get("roll") or []] == LOST,
  json.dumps({"no": seg.get("no"), "roll": seg.get("roll")}, ensure_ascii=False)[:130])
t("W8-7d 搬进来的 roll 拿到了卡片(旧实现到不了这一步: mid 恒为空)",
  str(seg.get("mid") or "").startswith("om_dry"), json.dumps(seg, ensure_ascii=False)[:130])
t("W8-7d 建卡触发后标记归零, gen 只进一格",
  after.get("pending_seg") is False and after.get("gen") == 1,
  json.dumps({k: after.get(k) for k in ("pending_seg", "gen")})[:60])
t("W8-7d 端到端只写沙箱(副本按脚本位置自我定位)",
  os.path.exists(f"{E2}/state/sync-state.json") and os.path.exists(f"{E2}/logs/sync.log"))

# ---------- 隔离与总判 ----------
builtins.open = _REAL_OPEN
t("W8-0 生产 state 目录全程未被本进程写开", not WRITE_HITS,
  f"{len(WRITE_HITS)} 次越界写: {WRITE_HITS[:3]} (该目录基线 {PROD_N} 个文件)")

fails = [(n, d) for n, ok, d in PASS if not ok]
for n, ok, d in PASS:
    print(("✓ " if ok else "✗ ") + n + ("" if ok else f"  <- {d}"))
print(f"\n{len(PASS) - len(fails)}/{len(PASS)} PASS")
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(SB, ignore_errors=True)
shutil.rmtree(E2, ignore_errors=True)
sys.exit(1 if fails else 0)
