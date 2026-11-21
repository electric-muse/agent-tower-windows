"""波次一修复的离线单测 — mock网络与投递, 验证逻辑正确性"""
import sys, os, ast, json, types, time, tempfile, shutil
os.environ.setdefault("SYNC_DRY", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tower_sync as fs
import tower_ctl as fc

# W5-5: 状态文件全部重定向到临时目录。此前单测直接删真实 state/.jid-seq.j(把W1-6的
#       单调水位线归零, 正是它要验的不变量)、对真实 card-mids.json 跑 prune、
#       经 record_sent 追加真实 .text_sent — 跑一次测试就是一次生产写入。
TMP = tempfile.mkdtemp(prefix="wave1-state-")
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

def t(name, cond, detail=""):
    PASS.append((name, bool(cond), detail))

t("W5-5 状态路径已重定向(不写生产state)",
  fc.STATED == TMP and fc.CMIDF.startswith(TMP) and fs.TEXTSENT.startswith(TMP))

# ===== W1-1: else分支恢复(AST断言) =====
tree = ast.parse(open(os.path.join(HERE, 'tower_sync.py'), encoding='utf-8').read())
class V(ast.NodeVisitor):
    found = None
    def visit_If(self, node):
        # 找含 seg_mode 的顶层投递 if: elif app_mode 的 orelse 应有内容
        src = ast.unparse(node)
        if 'app_mode()' in src and 'roll_gap' in src:
            for o in ast.walk(node):
                if isinstance(o, ast.If) and 'app_mode' in ast.unparse(o.test):
                    if o.orelse:
                        V.found = len(o.orelse)
        self.generic_visit(node)
V().visit(tree)
t("W1-1 webhook else恢复为真分支", V.found and V.found > 0, f"orelse stmts={V.found}")

# ===== W1-2: msg created后record_sent / fail重试 =====
P = "/x/y/z.jsonl"
st = {"seg": None, "tn": 1, "proj": "p", "armed": True}
fs.DRY = True; fs._SENT = set(); fs._MSGPEND.clear(); fs._MSGTRY.clear()
cfg = dict(fs.conf())
h = fs.msg_hash("测试消息ABC")
fs._MSGPEND[h] = True; st["_msgh"] = h        # 模拟事件循环预置
fs.send_msg_card(P, st, "测试消息ABC", "host", cfg)
key = ("msg", P, st["_msgseq"])
t("W1-2 enqueue即pending未占hash", fs._MSGTRY.get(key) and key[0] == "msg")
# 模拟created回收
fs.SENDER.results[key] = ("created", "om_test1")
fs.harvest_results({P: st})
t("W1-2 created→hash入去重库", h in fs.sent_set())
# 模拟create-fail重试
st2 = {"seg": None, "tn": 2, "proj": "p", "armed": True}
h2 = fs.msg_hash("重试消息DEF")
fs._MSGPEND[h2] = True; st2["_msgh"] = h2
fs.send_msg_card(P, st2, "重试消息DEF", "host", cfg)
k2 = ("msg", P, st2["_msgseq"])
fs.SENDER.results[k2] = ("create-fail",)
fs.harvest_results({P: st2})
t("W1-2 fail→入队重试≤3", fs._MSGTRY.get(k2) and fs._MSGTRY[k2][2] == 1 and h2 not in fs.sent_set())
# 第4次fail→放弃并占坑
for i in (2, 3):
    fs.SENDER.results[k2] = ("create-fail",); fs.harvest_results({P: st2})
fs.SENDER.results[k2] = ("create-fail",); fs.harvest_results({P: st2})
t("W1-2 fail超3→不伪装已送达", fs._MSGTRY.get(k2) is None and h2 not in fs.sent_set())
# 在途防重: 同文本在途不重复发
fs._MSGPEND.clear(); fs._MSGTRY.clear(); fs._SENT.discard(fs.msg_hash("ABC"))
st3 = {"seg": None, "tn": 3, "proj": "p", "armed": True, "ev": 5, "has_msg": True}

# ===== W1-3: clear_lost_roll 并入 =====
st4 = {"seg": {"no": 3, "roll": [["执行命令", "ls", 2], ["读文件", "f", 1]], "mid": "", "closed": False, "ts": 0},
       "gen": 0}
fs.clear_lost_roll(st4)
t("W1-3 未建卡段roll并入新段", st4["seg"]["no"] == 4 and len(st4["seg"]["roll"]) == 2
  and st4["pending_seg"] is True)
st5 = {"seg": {"no": 3, "roll": [["a", "b", 1]], "mid": "om_has", "closed": False, "ts": 0}}
fs.clear_lost_roll(st5)
t("W1-3 已建卡段no-op(不该被调用方误用)", st5["seg"]["no"] == 3 and st5["seg"]["mid"] == "om_has")

# ===== W1-4: cmd_stop 无pid不炸 =====
jobs = {"j9": {"state": "running", "pid": None, "jid": "j9"},
        "j8": {"state": "running", "pid": 99999999, "jid": "j8"}}
orig_load, orig_save = fc.jobs_load, fc.jobs_save
fc.jobs_load = lambda: json.loads(json.dumps(jobs))
saved = {}
fc.jobs_save = lambda j: saved.update(j)
fc.reply = lambda *a, **k: None
fc.audit = lambda *a, **k: None
try:
    fc.cmd_stop("s", "c", ["all"])
    ok = "j9" in saved  # 全表落盘了
except KeyError as e:
    ok = False
t("W1-4 stop-all 无pid条目不再炸且整表落盘", ok)
fc.jobs_load, fc.jobs_save = orig_load, orig_save

# ===== W1-5: _model_fail 回滚 =====
jobs5 = {"j7": {"state": "switching", "sw_ts": time.time(), "jid": "j7"}}
fc.jobs_load = lambda: json.loads(json.dumps(jobs5))
saved5 = {}
fc.jobs_save = lambda j: saved5.update(j)
fc._model_fail("j7")
t("W1-5 _model_fail→exited", saved5.get("j7", {}).get("state") == "exited"
  and "model_pending" not in saved5.get("j7", {}))
fc.jobs_load, fc.jobs_save = orig_load, orig_save

# ===== W1-6: jid水位线 + prune =====
seqf = fc._JID_SEQF + ".j"
try: os.remove(seqf)
except Exception: pass
fc.jobs_load = lambda: {}
a = fc.new_jid(); b = fc.new_jid()
t("W1-6 jid单调不复用", int(b[1:]) == int(a[1:]) + 1, f"{a}->{b}")
t("W1-6 水位线落盘(重启后仍不复用)",
  int(open(seqf).read().strip()) == int(b[1:]), f"seqf={open(seqf).read().strip()}")
fc.jobs_load = orig_load
# prune: 路由种子直接落盘(只塞内存会被 mt=0 的强制重载丢掉 -> 断言恒真的假绿)
json.dump({"om_gone": {"kind": "job", "jid": "j999"},
           "om_keep": {"kind": "job", "jid": "j1"},
           "om_sess": {"kind": "sess", "sid8": "abcd1234"}},
          open(fc.CMIDF, "w", encoding="utf-8"), ensure_ascii=False)
fc._cmid["mt"] = 0; fc._cmid["d"] = {}
fc.cmid_prune({"j1"})
live = json.load(open(fc.CMIDF, encoding='utf-8'))
t("W1-6 prune摘死jid保活路由",
  "om_gone" not in live and "om_keep" in live and "om_sess" in live, f"live={sorted(live)}")
fc._cmid["mt"] = 0; fc._cmid["d"] = {}

# ===== 汇总 =====
bad = [p for p in PASS if not p[1]]
for name, ok, d in PASS:
    print(("✓" if ok else "✗"), name, d)
print(f"\n{len(PASS)-len(bad)}/{len(PASS)} PASS")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if bad else 0)
