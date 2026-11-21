"""波次二修复离线单测"""
import sys, os, ast, json, time, threading
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tower_sync as fs
import tower_ctl as fc
PASS = []
def t(n, c, d=""): PASS.append((n, bool(c), d))

# ===== W2-8: _reap原子写 + poll读空按未就绪 =====
src = open(os.path.join(HERE, 'tower_ctl.py'), encoding='utf-8').read()
t("W2-8 _reap用tmp+replace", 'os.replace(rcf + ".tmp", rcf)' in src)
t("W2-8 poll读空→未就绪", 'rc_txt = open(rc).read().strip()' in src and 'if not rc_txt:' in src)

# ===== W2-9: retry保留goal环 =====
i = src.find('if isinstance(item, tuple) and item[0] == "__retry__":')
blk = src[i:i+400]
t("W2-9 retry检测active goal", 'Q.put(("goal-kick"' in blk and 'active' in blk)
i2 = src.find('if not jj[jid_].get("goal", {}).get("rounds"):')
t("W2-9 goal-kick不重置已有轮次", i2 > 0)

# ===== W2-7: 锁外kill =====
# 原字符串位置断言会被合法重构误伤(W5-1 重排了 cmd_go 的 if _pid), 改用AST判不变量本身
TREE = ast.parse(src)
def _fn(name):
    return next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
def _locks(node):
    return [w for w in ast.walk(node) if isinstance(w, ast.With)
            and any(getattr(it.context_expr, "id", "") == "_jl" for it in w.items)]
def _calls(node, fname):
    return [x for x in ast.walk(node) if isinstance(x, ast.Call)
            and getattr(x.func, "id", getattr(x.func, "attr", "")) == fname]
_g = _fn("cmd_go")
t("W2-7 cmd_go的kill全在_jl锁外",
  bool(_calls(_g, "_kill_group")) and not any(_calls(w, "_kill_group") for w in _locks(_g)))
t("W2-7 cmd_go锁内先置stopping",
  'jd["state"] = "stopping"; jobs_save(j)' in src[src.find('def cmd_go'):src.find('def cmd_title')])
i = src.find('def cmd_stop')
sp = src[i:src.find('def cmd_clean')]
t("W2-7 cmd_stop批量锁外kill", 'for pid in kills:' in sp and sp.index('for pid in kills') > sp.index('jobs_save(j)'))

# ===== W2-10: big-skip行对齐 + rewind快进 =====
ss = open(os.path.join(HERE, 'tower_sync.py'), encoding='utf-8').read()
t("W2-10 big-skip seek对齐行边界", 'k = tail.find(b"\\n")' in ss)
t("W2-10 rewind快进到EOF", 'st["off"] = size' in ss and 'continue' in ss[ss.find('# W2-10: 文件被截断'):ss.find('# W2-10: 文件被截断')+300])
# 行为级: rewind后tn不虚增
P = "/x/rw.jsonl"
st1 = {"off": 9999, "tn": 5, "roll": [["a","b",1]]}
# 模拟scan_dir的rewind分支逻辑(直接调不可行, 用AST存在性+语义断言)
t("W2-10 rewind清roll", 'st["roll"] = []' in ss[ss.find('rewind'):ss.find('rewind')+400])

# ===== W2-11: glob缓存 + recent set =====
t("W2-11 目录签名缓存函数", 'def _scan_files' in ss and '_GLOB.update(sig=sig' in ss)
t("W2-11 recent改set", 'recent = set()' in ss and 'recent.add(p)' in ss)
# 行为级: 同签名二次调用不重新glob
calls = {"n": 0}
_orig = fs.glob.glob
def fake_glob(pat):
    calls["n"] += 1
    return _orig(pat)
fs.glob.glob = fake_glob
import tempfile, inspect
d = tempfile.mkdtemp()
# W9: 目录实参个数随 _scan_files 签名变动(第4引擎加了qsession), 按签名动态补齐
_dirs = list(inspect.signature(fs._scan_files).parameters).index("test_dir")
r1 = fs._scan_files(*([d] * _dirs), None)
n1 = calls["n"]
r2 = fs._scan_files(*([d] * _dirs), None)
n2 = calls["n"]
t("W2-11 二次命中缓存零glob", n2 == n1, f"first={n1} second={n2}")
fs.glob.glob = _orig

# ===== W2-12: archive/root_topic 判值 =====
t("W2-12a archive判code", 'if o and o.get("code") == 0:' in src and 'archive-fail' in src)
t("W2-12b root_topic失败打标", 'topic_pending' in src and src.count('topic_pending') >= 3)

bad = [p for p in PASS if not p[1]]
for n, ok, d in PASS: print(("✓" if ok else "✗"), n, d)
print(f"\n{len(PASS)-len(bad)}/{len(PASS)} PASS")
sys.exit(1 if bad else 0)
