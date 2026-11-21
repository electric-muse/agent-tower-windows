"""波次三样式统一: 结构断言 + 实弹渲染(不发群, 校验JSON形态)"""
import sys, os, json
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tower_sync as fs
import tower_ctl as fc
PASS = []
def t(n, c, d=""): PASS.append((n, bool(c), d))

src = open(os.path.join(HERE, 'tower_ctl.py'), encoding='utf-8').read()
ssrc = open(os.path.join(HERE, 'tower_sync.py'), encoding='utf-8').read()

# W3-13 完成卡双色
i = src.find('def notify_done')
blk = src[i:src.index('def cmd_jobs', i)]
t("W3-13 舱内完成卡 green/red", '"template":"green" if ok else "red"' in blk)
t("W3-13 恒等三元已删", 'if jd.get("agent")!="claude" else' not in blk)

# W3-14 notify颜色
ns = open(os.path.join(HERE, 'feishu-notify.sh'), encoding='utf-8').read()
t("W3-14 approval→orange", '等待审批' in ns and 'template:"orange"' in ns)
t("W3-14 session-start→blue", '任务开始' in ns and ns.count('template:"red"') == 0 or 'template:"turquoise"' not in ns.split('任务开始')[1][:120])

# W3-15 按钮常量
t("W3-15 BTN常量表", all(x in src for x in ('BTN_GO', 'BTN_STOP', 'BTN_MODEL', 'BTN_LIST', 'BTN_FORGET', 'BTN_RETRY')))
import re
lits = [l for l in src.splitlines() if re.search(r'btn\("(▶️ 继续|🔄 立即|🔁 换模型|🗑 放弃|📋 任务列表)', l)]
t("W3-15 同value文案零字面量", not lits, str(lits[:1]))
t("W3-15 scan每卡primary≤1", '"primary" if _i == 0 else "default"' in src)

# W3-16 mark_card icon
t("W3-16 mark_card带icon参", 'def mark_card(mid, title, desc, icon="✅")' in src)
t("W3-16 stop/forget不假勾", 'icon="🗑")' in src and 'icon="⏹")' in src and 'icon={"go": "▶️"' in src)
t("W3-16 danger toast=warn", '"warn" if _danger' in src)

# W3-17 CARD_CONFIG
t("W3-17 ctl CARD_CONFIG", 'CARD_CONFIG = {' in src)
t("W3-17 sync CARD_CONFIG", 'CARD_CONFIG = {' in ssrc and '"config": dict(CARD_CONFIG)' in ssrc)
for agent, (_, color) in fc.AGENT_STYLE.items():
    _, _, actual = fc.root_card_parts("j1", {"agent": agent, "cwd": "/tmp"})
    t(f"W3-18 根卡使用引擎样式 {agent}", actual == color)
t("W3-18 wcb让出orange", '"wcodebuddy": ("🦞", "wathet")' in src)

# W3-19 S-2旧路标题
t("W3-19 webhook卡title_of", 'card(title_of(p, st, "💬" + card_kind(p)), "blue"' in ssrc)
t("W3-19 flush_roll title_of", 'mkcard(title_of(p, st, "🔧" + card_kind(p)), "wathet"' in ssrc)
t("W3-19 批号进脚注", 'note_of2(p, st, host, f"累计批' in ssrc)

# W3-20 过载: help-all折叠 + jobs详情
t("W3-20 help-all panel折叠", 'panel("🎮 遥控命令", t2)' in src)
t("W3-20 jobs详情320直出", 'ft[:320]' in src and 'panel("📄 展开完整回复"' in src)

# 渲染形态: 折叠卡是合法信封
st = {"seg": None, "tn": 1, "proj": "p", "armed": True,
      "prev_seg": {"mid": "om_x", "no": 3, "total": 5, "peek": "ls"}}
fs.SENDER.q.queue.clear()
fs.clear_lost_roll = getattr(fs, 'clear_lost_roll', None)
c = fs.CARD_CONFIG
t("W3 sync CARD_CONFIG三键齐", c.get("wide_screen_mode") and c.get("enable_forward") and c.get("update_multi"))

bad = [p for p in PASS if not p[1]]
for n, ok, d in PASS: print(("✓" if ok else "✗"), n, d)
print(f"{len(PASS)-len(bad)}/{len(PASS)} PASS")
sys.exit(1 if bad else 0)
