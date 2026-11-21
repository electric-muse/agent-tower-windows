"""波次四: 命令构造收敛 — 行为级断言"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tower_ctl as m
PASS = []
def t(n, c, d=""): PASS.append((n, bool(c), d))

# build_resume_cmd: 四引擎×两权限
c = m.build_resume_cmd("codex", "S1", "干活", perm="full")
t("codex/full: sandbox放开+argv传令", 'sandbox_mode="danger-full-access"' in " ".join(c) and c[-2:] == ["--", "干活"])
c = m.build_resume_cmd("codex", "S1", "干活", perm="safe")
t("codex/safe: 无full-access且无-C", "danger-full" not in " ".join(c) and "-C" not in c)
c = m.build_resume_cmd("claude", "S1", "x", perm="safe", model="opus")
t("claude/safe: acceptEdits+model", "--permission-mode" in c and "acceptEdits" in c and "opus" in c and "-C" not in c)
c = m.build_resume_cmd("claude", "S1", "x", perm="full")
t("claude/full: bypass", "bypassPermissions" in " ".join(c))
c = m.build_resume_cmd("wcodebuddy", "S1", "x", perm="full", model="glm-5.2")
t("wcb/full: -y+model+无-C", "-y" in c and "--model" in c and "glm-5.2" in c and "-C" not in c)
c = m.build_resume_cmd("wcodebuddy", "S1", "x", perm="safe")
t("wcb/safe: acceptEdits无-y", "acceptEdits" in " ".join(c) and "-y" not in c)
c = m.build_resume_cmd("qoder", "S1", "x", perm="safe", model="auto")
t("qoder/safe: 下划线权限名+model", "--permission-mode" in c and "accept_edits" in c
  and "bypass_permissions" not in c and "acceptEdits" not in c and "--resume" in c and "auto" in c)
c = m.build_resume_cmd("qoder", "S1", "x", perm="full")
t("qoder/full: bypass_permissions", "bypass_permissions" in " ".join(c))

# parse_new_tokens 四引擎统一语义
ti, pe, mo, kp = m.parse_new_tokens(["标题=T", "权限=safe", "模式=plan", "帮我", "写"])
t("tokens: 标题/降级/plan/余部", (ti, pe, mo, kp) == ("T", "safe", "plan", ["帮我", "写"]))
ti, pe, mo, kp = m.parse_new_tokens(["权限=full", "模式=goal", "干活"])
t("tokens: goal模式全域可解析(cnew/wnew旧缺口)", mo == "goal" and kp == ["干活"])

# 调用点收敛
src = open(os.path.join(HERE, 'tower_ctl.py'), encoding='utf-8').read()
t("无裸resume构造残留", src.count('"exec", "resume"') == 1)
# W9: 断言从"全局字面量<=1"改为"全部落在build_resume_cmd内", 否则新增分支会误报
_i = src.find("def build_resume_cmd")
_j = src.find("\ndef ", _i + 10)
_brc = src[_i:_j]
t("resume构造只在build_resume_cmd内",
  _i > 0 and _j > _i and src.count('"--resume"') == _brc.count('"--resume"') >= 1,
  f"total={src.count('--resume')} in_brc={_brc.count('--resume')}")

_LAUNCH = ("cmd_new", "cmd_cnew", "cmd_qnew", "cmd_wnew", "cmd_dnew", "cmd_dresume")
import ast as _ast
_tree = _ast.parse(src)
_all_calls = {_n for _n in _ast.walk(_tree)
              if isinstance(_n, _ast.Call) and getattr(_n.func, "id", "") == "parse_new_tokens"}
_calls = {}
for _f in _ast.walk(_tree):
    if isinstance(_f, _ast.FunctionDef) and _f.name in _LAUNCH:
        _calls[_f.name] = len([_n for _n in _ast.walk(_f) if _n in _all_calls])
t("六入口parse统一",
  set(_calls) == set(_LAUNCH) and all(v == 1 for v in _calls.values())
  and sum(_calls.values()) == len(_all_calls), str(_calls))

bad = [p for p in PASS if not p[1]]
for n, ok, d in PASS: print(("✓" if ok else "✗"), n, d)
print(f"{len(PASS)-len(bad)}/{len(PASS)} PASS")
sys.exit(1 if bad else 0)
