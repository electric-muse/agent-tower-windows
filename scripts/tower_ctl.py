#!/usr/bin/env python3
# 飞书遥控 codex exec 任务 — feishu-ctl.py (v1)
# 命令(#前缀, 群内发送, 仅 allow_ou 白名单): #jobs #new #stop #model #models #help
# 本守护任务支持控制；纳管桌面会话仅允许原生用户问题回答
# 状态权威: ~/agent-hq/ctl-jobs.json | 审计: ctl-audit.log | 实时进度卡由 feishu-sync v8 自动接管
# 依赖: ~/agent-hq/ctl-venv (lark-oapi), /opt/homebrew/bin/codex, feishu-app.json(app_id/secret/chat_id)
import os, sys, json, time, uuid, shlex, signal, threading, queue, subprocess, urllib.request, urllib.parse, re, shutil

from pathlib import Path
import tower_harness as harness
import tower_dsh as dsh
from tower_diagnostics import read_run, diagnostic
import tower_panel as task_panel
import tower_retention as retention
from tower_delivery import Outbox, CommandInbox
from tower_runtime import safe_log, http_failure, health_tick, STATUS, private_json, health_issues
from tower_ui import TITLE_DEFAULTS, normalize_title, format_title, sanitize_message_body
from tower_store import JsonMap, register_route, prune_routes
from tower_platform import IS_WINDOWS, codex_executable, process_flags, stop_tree
from tower_codex_bridge import CodexBridge, CodexBridgeError, turn_by_client
from tower_desktop_control import SESSION_ID, attach_title, catalog_matches
from tower_media import MAX_IMAGE_BYTES
from tower_dsh_control import DshControlBridge, catalog_matches as dsh_catalog_matches
from tower_dsh_bridge import DshBridgeError

HOME = os.path.expanduser("~")
HQ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFGD, STATED, LOGD = f"{HQ}/config", f"{HQ}/state", f"{HQ}/logs"

APPF = f"{CFGD}/feishu-app.json"
CONF = f"{CFGD}/ctl.conf"
JOBSF = f"{STATED}/ctl-jobs.json"
AUDIT = f"{LOGD}/ctl-audit.log"
LOG = f"{LOGD}/ctl.log"
HB = f"{LOGD}/ctl.heartbeat"
CODEX = codex_executable()
CLAUDE = shutil.which("claude") or f"{HOME}/.npm-global/bin/claude"
WCB = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy"
DSH = shutil.which("dsh") or f"{HOME}/.npm-global/bin/dsh"
QODER = shutil.which("qoderclicn") or f"{HOME}/.npm-global/bin/qoderclicn"   # npm i -g @qodercn-ai/qoderclicn; 未登录时结果带 is_error
WCB_MODELS = {"auto","hy4-preview","hy3","hy3-x","deepseek-v4.1-flash","deepseek-v4-pro",
              "glm-5.3","glm-5.3-flash","glm-5.2","glm-5.1","glm-5v-turbo","minimax-m3",
              "kimi-k3-1","kimi-k2.8-preview","kimi-k2.7","kimi-k2.6"}
BASE = "https://open.feishu.cn/open-apis"
DEF = {
    **TITLE_DEFAULTS,
    "allow_ou": "",                 # 逗号分隔 open_id 白名单, 空=全部拒绝
    "work_roots": f"{HOME}/WorkBuddy,{HOME}/Desktop/projects,/tmp",
    "codex_bin": CODEX,
    "default_model": "",            # 空=codex 自身默认(gpt-6-astra)
    "sandbox": "workspace-write",
    "max_jobs": 6,
    "fallback_recent": 1,
    "rate_n": 10, "rate_win": 60,
    "models": "",  # ctl.conf 手工维护: codex 可用模型逗号分隔(别名=真名亦可); 空=#models只显示config.toml默认
    "claude_bin": CLAUDE,
    "wcb_bin": WCB,
    "qoder_bin": QODER,
    "dsh_bin": DSH,
    "dsh_node_bin": "",
    "dsh_config_patch": "",
    "attach_sessions": "",       # 允许从飞书控制的已有 Codex 会话 UUID
    "desktop_aliases": "{}",      # JSON: 短名称 -> 已授权会话 UUID
    "dsh_attach_sessions": "",    # 显式授权的现有 DSH session-ID
    "max_run_min": 45,              # 单轮最长运行分钟数, 超时SIGINT+通知(0=关闭)
    "default_perm": "full",         # 任务默认权限: full=放开(单机自用); 命令带 权限=safe 降级沙箱
    "retention_job_days": 30,
    "retention_failed_days": 14,
    "status_card_gap": 30,
    "command_queue_max": 100,
    "auto_watch": 1,                # 自动收编活跃外部会话(10min内有写入且>50KB且未纳管)
    "auto_watch_min_kb": 50,
    "auto_watch_max": 3,            # 单轮最多收编数(防刷屏)
    "dsh_auto_watch_min_kb": 50,     # Harness 按压缩文件大小独立配置
}

# ===== W3: 统一规范(审计v18) =====
AGENT_STYLE = {   # 引擎 -> (图标, 根卡header色)
    "codex":      ("🚀", "blue"),
    "claude":     ("🤖", "purple"),
    "wcodebuddy": ("🦞", "wathet"),
    "qoder":      ("🧩", "yellow"),
    "desktop":    ("🖥", "grey"),
    "deepseek-harness": ("🐋", "turquoise"),
}
# 向导 fam 标识 -> 文字命令名(提示用户可跳过按钮直接打字)
FAM_CMD = {"codex": "new", "claude": "cnew", "wcb": "wnew", "qoder": "qnew", "dsh": "dnew"}
FAM_IC = {"codex": "🚀", "claude": "🤖", "wcb": "🦞", "qoder": "🧩", "dsh": "🐋"}
BTN_GO    = "▶️ 继续下指令"
BTN_RETRY = "🔄 立即重试"
BTN_STOP  = "⏹ 中断"
BTN_MODEL = "🔁 换模型"
BTN_LIST  = "📋 任务列表"
BTN_FORGET = "🗑 放弃"
CARD_CONFIG = {"wide_screen_mode": True, "enable_forward": True,
               "update_multi": True}   # W3-17: 全卡统一config

def agent_style(agent):
    return AGENT_STYLE.get(agent or "codex", AGENT_STYLE["codex"])

def log(m):
    safe_log(LOG, m, limit=300000)


def audit(sender, text, result):
    # Persist action/outcome only, never a user's prompt or credential-like payload.
    first = text.split()[0] if text else ""
    action = first if re.fullmatch(r"#[a-z-]+|watch|new|stop|go|model|title|\[(?:thread|quote)->[cwqd]?j[0-9]+\]", first) else "input"
    safe_log(AUDIT, f"action={action[:32]} result={result.split()[0][:48]}", limit=1000000)


_cfg = None; _cfg_mt = 0
def conf():
    global _cfg, _cfg_mt
    try: mt = os.path.getmtime(CONF)
    except OSError: mt = 0
    if _cfg and mt == _cfg_mt: return _cfg
    c = dict(DEF)
    try:
        for ln in open(CONF, encoding="utf-8"):
            k, _, v = ln.partition("=")
            k, v = k.strip(), v.strip()
            if k in c and v:
                try: c[k] = int(v) if v.isdigit() else v
                except Exception: c[k] = v
    except FileNotFoundError:
        pass
    _cfg, _cfg_mt = c, mt
    return c

_app = None
def app():
    global _app
    if _app is None:
        with open(APPF, encoding="utf-8") as file:
            _app = json.load(file)
    return _app

_tok = {"v": "", "exp": 0}
def token():
    if _tok["v"] and _tok["exp"] - 60 > time.time():
        return _tok["v"]
    a = app()
    req = urllib.request.Request(BASE + "/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": a["app_id"], "app_secret": a["app_secret"]}).encode(),
        headers={"Content-Type": "application/json"})
    o = json.loads(urllib.request.urlopen(req, timeout=8).read().decode())
    _tok["v"] = o["tenant_access_token"]; _tok["exp"] = time.time() + int(o.get("expire") or 7000)
    return _tok["v"]

def api(method, path, body=None):
    body = sanitize_message_body(body)
    for attempt in (1, 2):
        req = urllib.request.Request(BASE + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method, headers={"Content-Type": "application/json",
                                    "Authorization": f"Bearer {token()}"})
        try:
            o = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
        except Exception as e:
            log(f"api-err {json.dumps(http_failure(e))}"); return None
        if o.get("code") in (99991661, 99991663) and attempt == 1:
            _tok["v"] = ""; continue
        return o
    return o

_REPLY_CONTEXT = threading.local()


def send_control_card(chat_id, card):
    route = getattr(_REPLY_CONTEXT, 'route', {})
    body = {'msg_type': 'interactive', 'content': json.dumps(card, ensure_ascii=False)}
    if route.get('chat_id') == chat_id and route.get('message_id'):
        return api('POST', f"/im/v1/messages/{route['message_id']}/reply",
                   {**body, 'reply_in_thread': True})
    return api('POST', '/im/v1/messages?receive_id_type=chat_id',
               {**body, 'receive_id': chat_id})


def reply(chat_id, md, title="🎛 控制台", template="turquoise"):
    card = {"msg_type": "interactive", "card": {"schema": "2.0",
        "config": CARD_CONFIG,
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "body": {"elements": [{"tag": "markdown", "content": md or " "}]}}}
    send_control_card(chat_id, card['card'])

def panel(title, md, expanded=False):
    return {"tag": "collapsible_panel", "expanded": expanded,
            "header": {"title": {"tag": "markdown", "content": title}},
            "elements": [{"tag": "markdown", "content": md}]}

def reply_card(chat_id, title, template, els):
    card = {"msg_type": "interactive", "card": {"schema": "2.0",
        "config": CARD_CONFIG,
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "body": {"elements": els}}}
    o = send_control_card(chat_id, card['card'])
    return ((o or {}).get("data") or {}).get("message_id", "")

# ---------- card-mids 引用路由 ----------
CMIDF = f"{STATED}/card-mids.json"
_jl = threading.Lock()
_cmid = {"mt": 0.0, "d": {}}
def cmid_register(mid, jid):
    """ctl发的根卡/完成卡登记 mid->job, 引用即路由"""
    if not mid:
        return
    try:
        register_route(CMIDF, mid, {"kind": "job", "jid": jid})
    except Exception:
        STATUS['route_write_failed_at'] = time.time()
        log('route-write-failed')

def cmid_prune(alive_jids):
    """W1-6: 摘除指向已删jid的mid路由(文件里不存 per-mid 时间, 无从按 TTL 判, 只按活jid剪)"""
    try:
        count = prune_routes(CMIDF, alive_jids)
        if count:
            log(f"cmid-prune {count}")
    except Exception as e:
        log(f"cmid-prune-err {e}")

def cmid_lookup(mid):
    try:
        return JsonMap(CMIDF).read().get(mid)
    except Exception:
        STATUS['route_read_failed_at'] = time.time()
        return None


def repair_root_routes():
    """Recover authoritative roots after legacy route overwrites; preserve replies."""
    with _WATCH_LOCK:
        roots = {mid: {'kind': 'sess', 'sid8': sid} for sid, mid in _st_load().items()
                 if isinstance(mid, str) and mid}
        # A root explicitly adopted by a controlled job retains that job's route.
        roots.update({job['root_mid']: {'kind': 'job', 'jid': jid}
                      for jid, job in jobs_load().items() if job.get('root_mid')})
        def restore(data):
            changed = 0
            for mid, entry in roots.items():
                if data.get(mid) != entry:
                    data[mid] = entry
                    changed += 1
            return changed
        return JsonMap(CMIDF).update(restore)

def tsend(jd, md, title="📩 任务舱", template="blue"):
    """回执优先进该任务话题(有root_mid), 否则回主群"""
    root = (jd or {}).get("root_mid", "")
    if root:
        card={"schema":"2.0","config":CARD_CONFIG,
              "header":{"template":template,"title":{"tag":"plain_text","content":title}},
              "body":{"elements":[{"tag":"markdown","content":md}]}}
        return api("POST", f"/im/v1/messages/{root}/reply",
                   {"content": json.dumps(card, ensure_ascii=False),
                    "msg_type": "interactive", "reply_in_thread": True})
    return None

def parse_new_tokens(rest):
    """五引擎/向导共用选项解析；标题可加引号，其他文本不做 shell 解释。"""
    title = ""; perm = conf().get("default_perm", "full"); mode = ""
    pattern = re.compile(
        r'''(?<!\S)(?:标题=(?:"([^"\n]*)"|'([^'\n]*)'|([^\s"']+))|'''
        r'''(?:权限|perm)=(safe|full)|(?:模式|mode)=(plan|goal))(?=\s|$)''')
    def consume(match):
        nonlocal title, perm, mode
        if any(v is not None for v in match.group(1, 2, 3)):
            title = normalize_title(next(v for v in match.group(1, 2, 3) if v is not None), conf())
        elif match.group(4):
            perm = match.group(4)
        else:
            mode = match.group(5)
        return " "
    text = " ".join(rest)
    if not pattern.search(text):
        return title, perm, mode, list(rest)  # 向导中的多行代码/缩进原样保留
    prompt = pattern.sub(consume, text).strip()
    keep = ([prompt] if prompt else []) if len(rest) == 1 else prompt.split()
    return title, perm, mode, keep


def build_resume_cmd(agent, sess, text, perm="full", model=None):
    """W4: resume构造唯一入口。agent: codex|claude|wcodebuddy|qoder; perm: full|safe"""
    c = conf()
    safe = perm == "safe"
    if agent == "wcodebuddy":
        cmd = [c.get("wcb_bin", WCB), "-r", sess, "-p", text, "--output-format", "json"]
        cmd += ["--permission-mode", "acceptEdits"] if safe else ["-y"]
        if model: cmd += ["--model", model]
    elif agent == "claude":
        cmd = [c.get("claude_bin", CLAUDE), "-p", text, "--resume", sess,
               "--output-format", "json",
               "--permission-mode", "acceptEdits" if safe else "bypassPermissions"]
        if model: cmd += ["--model", model]
    elif agent == "qoder":
        # Qoder CLI 权限名是下划线式(accept_edits/bypass_permissions), 照抄 claude 驼峰会被拒
        cmd = [c.get("qoder_bin", QODER), "-p", text, "--resume", sess, "-o", "json",
               "--permission-mode", "accept_edits" if safe else "bypass_permissions"]
        if model: cmd += ["--model", model]
    elif agent == "deepseek-harness":
        raise ValueError("Harness 使用 stdin 进程适配器，不构造含提示词的命令行")
    else:  # codex
        cmd = [c["codex_bin"], "exec", "resume", sess, "--skip-git-repo-check"]
        if model: cmd += ["-c", f"model={model}"]
        cmd += ["--json"]
        if not safe:
            cmd += ["-c", 'sandbox_mode="danger-full-access"']
        cmd += ["--", text]      # resume 不支持 -C, 工作目录由 Popen(cwd=) 指定
    return cmd

def spawn_resume(jd, sess, text, output, perm=None):
    cwd = require_cwd(jd["cwd"])
    effective = perm or jd.get("perm") or "full"
    if jd.get("agent") == "deepseek-harness":
        return dsh.spawn(conf().get("dsh_bin", DSH), cwd, text, output,
                         session=sess, perm=effective, mode=jd.get("mode", "new"),
                         node_bin=conf().get("dsh_node_bin", ""), config_patch=conf().get("dsh_config_patch", ""))
    cmd = build_resume_cmd(jd.get("agent"), sess, text, perm=effective)
    return subprocess.Popen(cmd, cwd=require_cwd(cwd), stdout=output,
                            stderr=subprocess.STDOUT, **process_flags())


def cmd_go(sender, chat_id, args):
    if len(args) < 2:
        return reply(chat_id, "用法: `#go <jid> 指令…`", template="red")
    jid = args[0]
    perm = ""; toks = []
    for tk in args[1:]:
        if tk in ("权限=safe", "perm=safe"):
            perm = "safe"
        elif tk in ("权限=full", "perm=full"):
            perm = "full"
        else:
            toks.append(tk)
    text = " ".join(toks)
    if jid not in jobs_load():        # W6-6: 存在性预检在锁外, 缺任务的红卡不占 _jl
        return reply(chat_id, f"❌ 无任务 {jid}", template="red")
    before = jobs_load().get(jid) or {}
    if before.get("agent") == "deepseek-harness":
        if not text.strip(): return reply(chat_id, "❌ 缺少指令", template="red")
        try: require_cwd(before.get("cwd", ""))
        except ValueError: return reply(chat_id, "❌ 目录已不在 work_roots 白名单内，任务未被中断", template="red")
        if not before.get("sess") and before.get("state") == "running":
            return reply(chat_id, "Harness 正在初始化，请稍后再续跑；未启动并发会话", template="orange")
        running = sum(v.get("state") in ("running", "stopping", "switching")
                      for key, v in jobs_load().items() if key != jid)
        if running >= int(conf()["max_jobs"]):
            return reply(chat_id, "❌ 并发任务已达上限", template="red")
    with _jl:
        j = jobs_load(); jd = j.get(jid)
        if not jd:
            return                    # 兜竞态: 宁可不回, 也绝不持锁发网络
        # W2-7: 锁内只置stopping+记pid, 慢kill挪锁外(避免持锁15s阻塞poll/心跳/其它命令)
        # W5-1: if _pid 只包kill; resume对已结束任务同样要走(此前整段被误嵌进去, 续跑全断)
        _pid = jd.get("pid") if jd.get("state") == "running" and jd.get("sess") else None
        if _pid:
            jd["state"] = "stopping"; jobs_save(j)
        sess = jd.get("sess")   # W6-6: 锁内只取快照, find_sess兜底在锁外
    if _pid:
        try: _kill_group(_pid)
        except Exception as e: log(f"go-kill-err {jid} {e}")
        time.sleep(1.0)
    if not sess:
        sess = find_sess(jid)   # W6-6: glob+读大文件的兜底解析挪锁外
    if not sess:
        return reply(chat_id, f"❌ {jid} 没有可续跑的 session(任务太新?),稍后再发一次", template="red")
    rcf = new_round_rc(jid)
    outf = f"{STATED}/job-{jid}.out"
    try:
        with open(outf, "ab") as of:
            pr = spawn_resume(jd, sess, text, of, perm)
    except Exception as e:
        return reply(chat_id, f"❌ 续跑失败: {e}", template="red")
    threading.Thread(target=_reap, args=(pr, rcf), daemon=True).start()
    # W5-1: 锁外起进程后重新取锁+重载落盘, 不用陈旧快照覆盖poll期间的并发写入
    with _jl:
        j = jobs_load()
        jd = j.get(jid) or jd
        jd.update(pid=pr.pid, state="running", sess=sess, started=time.time(),
                  rc_file=rcf, out_file=outf, notified=False)
        reset_run_flags(jd)
        if perm:
            jd["perm"] = perm     # 显式降级/放开要落盘, 下次不带权限参数的#go沿用
        jd.pop("exit", None); jd.pop("retry_at", None)
        if sender != "ctl-retry":
            jd.pop("retry_cnt", None)
        _unarch = bool(jd.pop("archived", None))
        j[jid] = jd; jobs_save(j)
    audit(sender, f"#go {jid} {text[:60]}", f"resumed pid={pr.pid} sess={sess[:8]}")
    if _unarch:
        log(f"unarchive {jid}")
    if not jd.get("root_mid"):
        try:
            root_topic(chat_id, jid, {**jd, "proj_tag": "续跑补建舱"})
        except Exception as e:
            log(f"late-topic-err {e}")
        jd = jobs_load().get(jid, jd)
    if not tsend(jd, f"➡️ 指令已接收:{clean_md(text)[:120]}", f"📩 {jid} · 新指令", "green"):
        reply(chat_id, f"➡️ 已转给 🚀**{jid}**:{clean_md(text)[:80]}", template="green")


def desktop_bindings():
    return JsonMap(Path(STATED) / 'desktop-bindings.json')


def desktop_binding_key(sender, chat_id):
    return f'{chat_id}:{sender}'


def desktop_binding(sender, chat_id):
    return desktop_bindings().read().get(desktop_binding_key(sender, chat_id))


def desktop_allowed(sid, cwd):
    explicit = {value.strip() for value in str(conf()['attach_sessions']).split(',') if value.strip()}
    return sid in explicit or allowed_cwd(cwd)


def desktop_aliases():
    try:
        aliases = json.loads(str(conf().get('desktop_aliases') or '{}'))
    except ValueError:
        return {}
    if not isinstance(aliases, dict):
        return {}
    allowed = {sid.strip() for sid in str(conf()['attach_sessions']).split(',') if sid.strip()}
    allowed.update(dsh_attach_ids())
    return {name.strip(): sid for name, sid in aliases.items()
            if isinstance(name, str) and re.fullmatch(r'[\w-]{1,24}', name.strip())
            and isinstance(sid, str) and SESSION_ID.fullmatch(sid.removeprefix('session-')) and sid in allowed}


def dsh_attach_ids():
    return {sid.strip() for sid in str(conf().get('dsh_attach_sessions') or '').split(',')
            if sid.strip().startswith('session-')
            and SESSION_ID.fullmatch(sid.strip().removeprefix('session-'))}


def binding_allowed(bound):
    if bound.get('engine') == 'dsh':
        return bound.get('sid') in dsh_attach_ids()
    return desktop_allowed(bound['sid'], bound['cwd'])


def desktop_bridge(bound, timeout, *, auto_open=False):
    if bound.get('engine') == 'dsh':
        return DshControlBridge.from_config(Path(CFGD) / 'dsh-host.json',
                                           session_ids={bound['sid']}, timeout=timeout)
    return CodexBridge(session_ids={bound['sid']}, timeout=timeout, auto_open=auto_open)


def dsh_attach_title(text):
    match = re.fullmatch(r'\s*(?:我要|我想)?\s*(?:进入|控制|接管)\s*DSH\s*(?:里|中)?\s*(.+?)\s*(?:的任务|任务)?\s*', text, re.I)
    return match.group(1).strip('“”「」 ') if match else ''


def desktop_shortcut(text):
    match = re.fullmatch(r'\s*(?:我要|我想)?\s*(?:进入|控制|接管)\s*(\S+)\s*', text)
    if not match:
        return ''
    name = match.group(1)
    alias = desktop_aliases().get(name)
    if alias:
        return alias
    allowed = {sid.strip() for sid in str(conf()['attach_sessions']).split(',') if sid.strip()}
    matches = [target for target in catalog_matches(name) if target['id'] in allowed]
    matches += dsh_catalog_matches(name, dsh_attach_ids())
    return matches[0]['id'] if len(matches) == 1 else ''


def desktop_chat_allowed(chat_id):
    return bool(app().get('chat_id')) and chat_id == app()['chat_id']


def desktop_attach_error(error):
    reason = str(error)
    if error.code in ('thread-open-failed', 'thread-open-timeout'):
        return '已尝试打开目标 Codex 会话，但尚未完成加载。请稍后重试，或在电脑端查看该聊天的页面提示。'
    if 'thread owner is unavailable' in reason:
        return 'Codex 客户端尚未打开这个任务。请先在电脑端打开后重试。'
    if 'snapshot' in reason:
        return '已连接 Codex，但未能读取该任务状态。请检查塔台与当前客户端的兼容性。'
    if 'identity mismatch' in reason:
        return 'Codex 返回的会话身份或工作目录不一致，未接管。'
    return '塔台无法连接 Codex 桌面接口。请确认客户端正在运行，并查看本机连接日志。'


def cmd_attach(sender, chat_id, args):
    if not desktop_chat_allowed(chat_id):
        return reply(chat_id, '只能在已配置的飞书控制群接管桌面任务。', template='orange')
    title = ' '.join(args).strip()
    if not title:
        return reply(chat_id, '用法：`#attach 示例学习助手升级`，或直接说「我要控制 Codex 里示例学习助手升级的任务」')
    title = desktop_aliases().get(title, title)
    dsh_title = re.sub(r'^DSH\s+', '', title, flags=re.I)
    dsh_matches = dsh_catalog_matches(dsh_title, dsh_attach_ids())
    if dsh_matches or title.startswith('session-') or dsh_title != title:
        return attach_dsh(sender, chat_id, dsh_matches)
    matches = catalog_matches(title)
    if len(matches) != 1:
        return reply(chat_id, '找不到唯一的本机桌面 Codex 任务。请使用完整标题或完整会话 ID；同名任务不会自动选中。', template='orange')
    target = matches[0]
    sid, cwd = target['id'], target['cwd']
    if not desktop_allowed(sid, cwd):
        return reply(chat_id, '此任务尚未授权手机控制：需将它的完整会话 ID 加入 `attach_sessions`，或将工作目录加入 `work_roots`。', template='orange')
    path, found = find_rollout(sid)
    if found != sid or not path:
        return reply(chat_id, '找不到该任务唯一的本机会话文件，未绑定。', template='orange')
    try:
        with open(path, encoding='utf-8') as file:
            header = json.loads(file.readline())
        meta = header.get('payload') or {}
        if (meta.get('id') != sid and meta.get('session_id') != sid) or (
                os.path.realpath(meta.get('cwd') or '') != os.path.realpath(cwd)):
            return reply(chat_id, '会话文件与任务目录不一致，未绑定。', template='red')
    except (OSError, ValueError):
        return reply(chat_id, '会话文件无法验证，未绑定。', template='red')
    key = desktop_binding_key(sender, chat_id)
    current = desktop_bindings().read()
    bound = current.get(key)
    if bound and bound.get('pending'):
        return reply(chat_id, '当前会话仍在回复，完成后再切换控制目标。', template='orange')
    if any(value.get('sid') == sid and name != key for name, value in current.items()):
        return reply(chat_id, '此桌面会话已绑定到另一位用户或聊天，未重复绑定。', template='orange')
    try:
        bridge = CodexBridge(session_ids={sid}, timeout=3, auto_open=True)
        bridge.connect()
        try:
            state = bridge.snapshot_state(sid)
            opened_threads = getattr(bridge, 'opened_threads', None)
            auto_opened = isinstance(opened_threads, set) and sid in opened_threads
        finally:
            bridge.close()
        if state.get('id') != sid or os.path.realpath(state.get('cwd') or '') != os.path.realpath(cwd):
            raise CodexBridgeError('Codex desktop thread identity mismatch')
    except CodexBridgeError as error:
        log(f'desktop-attach-failed stage={error.stage or "unknown"} code={error.code or "unknown"} {error}')
        return reply(chat_id, desktop_attach_error(error), template='orange')
    name = target['title']
    elements = [{'tag': 'markdown', 'content': f'**{clean_md(name)}**\n\n直接在群里或本话题发中文、截图，即可继续原桌面会话。发「退出控制」解除绑定。'}]
    root, thread = create_topic(f'desktop:{sid}:{chat_id}:{sender}', chat_id, f'🖥 {name}', elements,
                                '🧵 原 Codex 会话', '本话题接收原会话的回复。', tag='blue')
    if not root:
        return reply(chat_id, '飞书任务话题创建失败，未绑定；请稍后重试。', template='orange')
    desktop_bindings().put(key, {'sid': sid, 'title': name, 'cwd': cwd,
                                 'root_mid': root, 'thread_id': thread,
                                 'chat_id': chat_id, 'sender': sender,
                                 'auto_opened': auto_opened, 'connection_checked_at': time.time()})
    log(f'desktop-attach-ok auto_opened={int(auto_opened)}')
    audit(sender, '#attach', 'bound')
    reply(chat_id, f'已进入原 Codex 任务「{clean_md(name)}」。下一条直接发中文；退出请发「退出控制」。', template='green')


def attach_dsh(sender, chat_id, matches):
    if len(matches) != 1:
        return reply(chat_id, '找不到唯一的已授权 DSH 会话，请发 `#sessions` 查看任务。', template='orange')
    target = matches[0]
    sid = target['id']
    if sid not in dsh_attach_ids():
        return reply(chat_id, '该 DSH 会话未授权手机控制。', template='orange')
    key = desktop_binding_key(sender, chat_id)
    bindings = desktop_bindings().read()
    if bindings.get(key, {}).get('pending'):
        return reply(chat_id, '上一条消息仍在等待回复，完成后再切换任务。', template='orange')
    if any(name != key and value.get('sid') == sid for name, value in bindings.items()):
        return reply(chat_id, '该 DSH 会话已被其他用户或聊天绑定。', template='orange')
    bound = {**target, 'sid': sid, 'engine': 'dsh', 'chat_id': chat_id, 'sender': sender}
    try:
        bridge = desktop_bridge(bound, 5)
        try:
            bridge.connect()
            bridge.state(sid)
        finally:
            bridge.close()
    except DshBridgeError:
        log('dsh-attach-failed')
        return reply(chat_id, '无法连接 DSH Host。请保持 DSH 打开，并更新本机 `config/dsh-host.json` 认证地址。', template='orange')
    root, thread = create_topic(f'dsh-desktop:{sid}:{chat_id}:{sender}', chat_id,
                                f'🐋 {target["title"]}', [{'tag': 'markdown', 'content':
                                '在控制群主聊天或本控制话题发中文、截图，继续原 DSH 会话；退出发「退出控制」。'}],
                                '原 DSH 会话', '原会话回复', tag='blue')
    if not root:
        return reply(chat_id, 'DSH 控制话题创建失败，未绑定。', template='orange')
    desktop_bindings().put(key, {**bound, 'root_mid': root, 'thread_id': thread})
    audit(sender, '#attach', 'dsh-bound')
    reply(chat_id, f'已进入原 DSH 任务「{clean_md(target["title"])}」。下一条直接发中文；退出请发「退出控制」。', template='green')


def cmd_leave(sender, chat_id, args):
    key = desktop_binding_key(sender, chat_id)
    bound = desktop_binding(sender, chat_id)
    if bound and bound.get('pending') and args != ['force']:
        return reply(chat_id, '上一条指令仍在等待回复。收到回复后再发 `#leave`；若确定放弃这次回传，发 `#leave force`。', template='orange')
    removed = desktop_bindings().update(lambda data: data.pop(key, None))
    engine = 'DSH' if (bound or {}).get('engine') == 'dsh' else 'Codex'
    reply(chat_id, f'已退出原 {engine} 任务控制。' if removed else '当前没有绑定桌面任务。', template='grey')


def cmd_status(sender, chat_id, args):
    if not desktop_chat_allowed(chat_id):
        return reply(chat_id, '请在飞书控制群查看桌面接管状态。', template='orange')
    bound = desktop_binding(sender, chat_id)
    if not bound:
        return reply(chat_id, '当前未控制桌面任务。发 `#sessions` 查看可进入的任务。')
    state = '上一条指令仍在等待回复。' if bound.get('pending') else '没有待回传的指令。'
    recovery = '\n上次进入时，塔台已自动打开电脑端的原任务。' if bound.get('auto_opened') else ''
    reply(chat_id, f'当前控制「{clean_md(bound.get("title") or "Codex 任务")}」。\n{state}{recovery}\n退出：`#leave`。')


def cmd_sessions(sender, chat_id, args):
    if not desktop_chat_allowed(chat_id):
        return reply(chat_id, '请在飞书控制群查看可接管任务。', template='orange')
    allowed = [sid.strip() for sid in str(conf()['attach_sessions']).split(',') if sid.strip()]
    aliases = desktop_aliases()
    lines = []
    for sid in dict.fromkeys(allowed):
        matches = catalog_matches(sid)
        if len(matches) != 1:
            continue
        title = clean_md(matches[0]['title'])
        names = [name for name, target in aliases.items() if target == sid]
        command = f'进入{names[0]}' if names else f'#attach {title}'
        lines.append(f'· {title}：`{command}`')
    for sid in sorted(dsh_attach_ids()):
        matches = dsh_catalog_matches(sid, {sid})
        if len(matches) == 1:
            names = [name for name, target in aliases.items() if target == sid]
            command = f'进入{names[0]}' if names else f'#attach DSH {matches[0]["title"]}'
            lines.append(f'· DSH · {clean_md(matches[0]["title"])}：`{command}`')
    reply(chat_id, '已授权的桌面任务（电脑客户端需运行）：\n' + '\n'.join(lines) if lines
          else '当前没有已授权且可识别的桌面任务。')


def desktop_thread_binding(sender, chat_id, thread_id, root_id):
    bound = desktop_binding(sender, chat_id)
    if bound and (thread_id in (bound.get('thread_id'), bound.get('root_mid'))
                  or root_id == bound.get('root_mid')):
        return bound
    return None


def download_desktop_image(message_id, image_key):
    if (not isinstance(message_id, str) or not 1 <= len(message_id) <= 512
            or not isinstance(image_key, str) or not 1 <= len(image_key) <= 512):
        raise ValueError('invalid Feishu image reference')
    resource = (f'/im/v1/messages/{urllib.parse.quote(message_id, safe="")}/resources/'
                f'{urllib.parse.quote(image_key, safe="")}?type=image')
    request = urllib.request.Request(BASE + resource,
                                     headers={'Authorization': f'Bearer {token()}'})
    with urllib.request.urlopen(request, timeout=12) as response:
        content = response.read(MAX_IMAGE_BYTES + 1)
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ValueError('Feishu image is empty or too large')
    if content.startswith(b'\x89PNG\r\n\x1a\n'):
        suffix = '.png'
    elif content.startswith(b'\xff\xd8\xff'):
        suffix = '.jpg'
    elif content.startswith((b'GIF87a', b'GIF89a')):
        suffix = '.gif'
    else:
        raise ValueError('unsupported Feishu image type')
    folder = Path(STATED) / 'inbound-images'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / (uuid.uuid4().hex + suffix)
    try:
        with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as image:
            image.write(content)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return str(path)


def desktop_send(sender, chat_id, text, bound, message_id='', image=None):
    sid = bound['sid']
    engine = 'DSH' if bound.get('engine') == 'dsh' else 'Codex'
    if not desktop_chat_allowed(chat_id) or bound.get('chat_id') != chat_id:
        return reply(chat_id, '此聊天无权控制原 Codex 任务，消息未发送。', template='orange')
    if not binding_allowed(bound):
        return reply(chat_id, '此会话的手机控制授权已撤销，消息未发送。', template='orange')
    if bound.get('pending'):
        return reply(chat_id, f'原 {engine} 任务正在回复上一条消息，请完成后再发。', template='orange')
    client_id = message_id or str(uuid.uuid4())
    key = desktop_binding_key(sender, chat_id)
    dispatch_recorded = False
    local_image = None

    def mark_pending(before):
        nonlocal dispatch_recorded
        def mark(data):
            current = data.get(key)
            if not current or current.get('sid') != sid or current.get('pending'):
                return False
            current['pending'] = {'before': before, 'client_id': client_id,
                                  'in_thread': bool(getattr(_REPLY_CONTEXT, 'route', {})),
                                  'sent_at': time.time()}
            return True
        if not desktop_bindings().update(mark):
            raise CodexBridgeError('Desktop binding changed before dispatch')
        dispatch_recorded = True

    try:
        if image:
            local_image = download_desktop_image(*image)
        bridge = desktop_bridge(bound, 5, auto_open=True)
        bridge.connect()
        try:
            if local_image:
                bridge.start_turn(sid, text, client_id, on_dispatch=mark_pending,
                                  images=[local_image])
            else:
                bridge.start_turn(sid, text, client_id, on_dispatch=mark_pending)
        finally:
            bridge.close()
    except (CodexBridgeError, DshBridgeError, OSError, ValueError) as error:
        if local_image and not dispatch_recorded:
            Path(local_image).unlink(missing_ok=True)
        if dispatch_recorded:
            return reply(chat_id, f'发送结果尚未确认，正在核对原 {engine} 会话；请勿重复发送这条消息。', template='orange')
        if image and isinstance(error, (OSError, ValueError)):
            log(f'desktop-image-download-failed type={type(error).__name__} code={getattr(error, "code", "") or ""}')
            return reply(chat_id, '无法读取这张图片，请确认飞书图片权限和文件大小（上限 10 MB）后重发。', template='orange')
        if 'busy' in str(error):
            return reply(chat_id, f'原 {engine} 任务正在运行，请等这一轮完成后再发。', template='orange')
        return reply(chat_id, f'无法连接原 {engine} 任务，请确认电脑客户端仍运行。消息未确认送达。', template='orange')
    audit(sender, '#attach-send', 'started')
    reply(chat_id, f'{"图片" if image else "消息"}已送达「{clean_md(bound["title"])}」，完成后会在这里回复。', template='green')


def poll_desktop_bindings():
    for key, bound in desktop_bindings().read().items():
        pending = bound.get('pending') or {}
        if not pending:
            continue
        if not desktop_chat_allowed(bound.get('chat_id')) or not binding_allowed(bound):
            continue
        sid = bound['sid']
        try:
            bridge = desktop_bridge(bound, 3 if bound.get('engine') == 'dsh' else 2)
            bridge.connect()
            try:
                if bound.get('engine') == 'dsh':
                    state, turn = bridge.result(sid, pending)
                else:
                    state = bridge.snapshot_state(sid)
                    turn = turn_by_client(state, pending.get('client_id'))
            finally:
                bridge.close()
        except (CodexBridgeError, DshBridgeError):
            continue
        completed = (turn['id'] and turn['id'] != pending.get('before')
                     and turn['status'] in ('completed', 'failed', 'interrupted'))
        uncertain = (not completed and time.time() - pending.get('sent_at', time.time()) > 120
                     and (state.get('threadRuntimeStatus') or {}).get('type') == 'idle')
        if not completed and not uncertain:
            continue
        text = (turn['text'] or ('本轮已结束，未收到最终文字回复。' if turn['status'] == 'completed'
                                 else f'本轮状态：{turn["status"]}。请在电脑端查看详情。')) if completed else (
            '这条消息已被桌面端接收，但暂未找到对应的完成记录。请在电脑端查看后再决定是否重发。')
        card = {'schema': '2.0', 'config': CARD_CONFIG,
                'header': {'template': 'green' if completed and turn['status'] == 'completed' else 'orange',
                           'title': {'tag': 'plain_text', 'content': f'🖥 {bound["title"]} · 回复'}},
                'body': {'elements': [{'tag': 'markdown', 'content': clean_md(text)[:12000]}]}}
        token = uuid.uuid5(uuid.NAMESPACE_URL, key + ':' + (turn['id'] if completed else pending['client_id'])).hex
        body = {'content': json.dumps(card, ensure_ascii=False),
                'msg_type': 'interactive', 'uuid': token}
        if pending.get('in_thread'):
            result = api('POST', f'/im/v1/messages/{bound["root_mid"]}/reply',
                         {**body, 'reply_in_thread': True})
        else:
            result = api('POST', '/im/v1/messages?receive_id_type=chat_id',
                         {**body, 'receive_id': bound['chat_id']})
        if (result or {}).get('code') != 0:
            continue
        if completed and turn['text']:
            try:
                from tower_sync import queue_local_images
                queue_local_images(('desktop_image', sid, turn['id']), turn['text'], bound['cwd'],
                                   bound['root_mid'] if pending.get('in_thread') else '')
            except (OSError, ValueError):
                log('desktop-image-queue-failed')
                continue
        def clear(data):
            current = data.get(key)
            if current and (current.get('pending') or {}).get('before') == pending.get('before'):
                current.pop('pending', None)
        desktop_bindings().update(clear)

def clean_md(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def cmd_title(sender, chat_id, args):
    if len(args) < 2:
        return reply(chat_id, "用法: `#title <jid> 新任务名`", template="red")
    jid = args[0]
    title = normalize_title(" ".join(args[1:]), conf())
    if not title:
        return reply(chat_id, "❌ 标题不能为空", template="red")
    if jid not in jobs_load():        # W6-6: 存在性预检在锁外, 缺任务的红卡不占 _jl
        return reply(chat_id, f"❌ 无任务 {jid}", template="red")
    with _jl:
        j = jobs_load()
        if jid not in j:
            return                    # 兜竞态: 宁可不回, 也绝不持锁发网络
        j[jid]["title"] = title
        j[jid]["root_update_pending"] = True
        jobs_save(j)
    audit(sender, f"#title {jid} {title}", "renamed")
    updated = refresh_root_card(jid)
    suffix = "话题根卡已同步，后续播报使用新标题" if updated else "后续播报生效；根卡待自动同步"
    reply(chat_id, f"🏷 {jid} 已改名为「{clean_md(title)}」\n{suffix}", template="green")
def jobs_load():
    try:
        with open(JOBSF, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError): return {}
def jobs_save(j):
    private_json(JOBSF, j)

def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # pid可能被系统回收复用: comm必须仍是 agent 相关进程
    try:
        out = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        if out and not any(k in out for k in ("node", "codex", "claude", "Python", "python")):
            return False
    except Exception:
        pass
    return True

_FATAL_PAT = re.compile(
    r"permission denied|not authorized|invalid api key|authentication|"
    r"credit|quota exceeded|model not found|no such session|is_error\":true", re.I)
_TRANSIENT_PAT = re.compile(r"429|too many requests|50[0234]|timeout|overloaded|econn|rate.?limit", re.I)
def _semantic_fail(jd):
    """读 out 尾部判定: 明确语义错误(鉴权/配额/参数)不重试; 瞬态或未知可重试"""
    if jd.get("agent") == "deepseek-harness":
        return not (read_run(jd.get("out_file", ""))["diagnostic"] or diagnostic())["retryable"]
    try:
        f = jd.get("out_file", "")
        with open(f, encoding="utf-8", errors="replace") as fh:
            try:
                fh.seek(max(0, os.path.getsize(f) - 100000))
            except Exception:
                pass
            tail = fh.read()
        if _TRANSIENT_PAT.search(tail):
            return False
        return bool(_FATAL_PAT.search(tail))
    except Exception:
        return False

def poll_jobs():
    """running 且 pid 已死 -> 依据 rc 文件转 exited/stopped"""
    resume = []
    pend_topic = []
    to_notify = []
    with _jl:
        j = jobs_load(); changed = False
        for jd in j.values():
            # W2-12: 补建失败的话题舱
            # W5-2: 只收集, 建卡挪锁外 — root_topic 内部要取 _jl, 锁内调用会同线程重入死锁
            if jd.get("topic_pending") and not jd.get("root_mid"):
                pend_topic.append((jd.get("chat_id", ""), jd["jid"], dict(jd)))
            # W1-5: switching孤儿兜底(进程没起来/reap前ctl重启)
            if jd.get("state") == "switching":
                if time.time() - jd.get("sw_ts", jd.get("started", 0)) > 120:
                    jd.update(state="exited", exit="?", ended=time.time())
                    jd.pop("model_pending", None); changed = True
                continue
            # The unique round rc file is authoritative even if Windows still reports the PID alive.
            if jd.get("state") in ("running", "stopping") and (
                    (jd.get("rc_file") and os.path.exists(jd["rc_file"])) or not alive(jd["pid"])):
                rc = jd.get("rc_file", "")
                rc_txt = ""
                if rc and os.path.exists(rc):
                    try: rc_txt = open(rc).read().strip()
                    except Exception: rc_txt = ""
                if not rc_txt:
                    # reap线程可能已死(如ctl重启); pid死亡超90s仍无rc -> 判lost兜底
                    jd.setdefault("first_dead_see", time.time())
                    if time.time() - jd["first_dead_see"] > 90:
                        stopping = jd.get("state") == "stopping"
                        jd["state"] = "stopped" if stopping else "exited"; jd["exit"] = "lost"
                        if stopping and jd.get("goal"):
                            jd["goal"]["active"] = False
                        jd["ended"] = time.time(); changed = True
                        log(f"job {jd['jid']} lost(rc missing)")
                        if jd.get("chat_id") and not jd.get("notified"):
                            jd["notified"] = True
                            to_notify.append(dict(jd))   # W6-6: 发卡在锁外做
                    else:
                        jd.setdefault("first_dead_see", time.time())
                        changed = True
                    continue
                code = rc_txt
                stopping = jd.get("state") == "stopping"
                jd["state"] = "stopped" if stopping else "exited"
                jd["exit"] = code
                if stopping:
                    jd.pop("retry_at", None)
                    if jd.get("goal"):
                        jd["goal"]["active"] = False
                jd["ended"] = time.time(); changed = True
                log(f"job {jd['jid']} exited rc={code}")
                # 瞬态失败自动重试(等60s, 最多2次): 非goal、非0码、排除语义失败
                if not stopping and code not in ("0", "?") and not (jd.get("goal") or {}).get("active") \
                        and jd.get("retry_cnt", 0) < 2 and jd.get("sess") \
                        and not _semantic_fail(jd):
                    jd["retry_cnt"] = jd.get("retry_cnt", 0) + 1
                    jd["retry_at"] = time.time() + min(300, 60 * 2 ** (jd["retry_cnt"] - 1))
                    changed = True
                    continue
                jd.setdefault("retry_cnt", 0)
                if not jd.get("notified") and jd.get("chat_id") \
                        and not (jd.get("goal") or {}).get("active"):
                    jd["notified"] = True
                    to_notify.append(dict(jd))   # W6-6: 发卡在锁外做
        if changed:
            jobs_save(j)
            resume = [dict(v) for v in j.values()
                      if v.get("state") == "exited" and (v.get("goal") or {}).get("active")]
    # W6-6: 完成卡通知在锁外发(notified 标记已随上面 jobs_save 落盘, 不会重复通知)
    for _jd in to_notify:
        try:
            notify_done(_jd)
        except Exception as e:
            log(f"notify-err {e}")
    # W5-2: 补建话题舱在锁外做(root_topic 内部自取 _jl); 失败保留标记由下轮poll重试
    for _chat, _jid, _jd in pend_topic:
        try:
            root_topic(_chat, _jid, _jd)
            with _jl:
                jj = jobs_load()
                if jj.get(_jid, {}).get("root_mid"):
                    jj[_jid].pop("topic_pending", None); jobs_save(jj)
        except Exception as e:
            log(f"topic-retry-err {_jid} {e}")
    for jd in resume:
        try:
            goal_tick(jd)
        except Exception as e:
            log(f"goal-err {jd.get('jid')} {e}")
    for jid, jd in jobs_load().items():
        if jd.get("root_update_pending") and jd.get("root_mid"):
            if time.monotonic() - _root_retry.get(jid, -60) >= 30:
                _root_retry[jid] = time.monotonic()
                try:
                    refresh_root_card(jid)
                except Exception as e:
                    log(f"root-title-retry-err {jid}: {e}")
    # 单轮运行超时保护
    mx = int(conf().get("max_run_min", 45))
    if mx > 0:
        for jd in jobs_load().values():
            if jd.get("state") == "running" and time.time() - jd.get("started", 0) > mx * 60:
                try:
                    _kill_group(jd["pid"])
                    with _jl:
                        jj = jobs_load()
                        k = jj.get(jd["jid"])
                        if k and k.get("state") == "running":
                            k["state"] = "stopping"
                            k["timeout_killed"] = True
                            jobs_save(jj)
                    log(f"timeout-kill {jd['jid']} >{mx}min")
                except Exception as e:
                    log(f"timeout-err {e}")
    # 到期重试
    now2 = time.time()
    for jd in jobs_load().values():
        if jd.get("state") == "exited" and jd.get("retry_at") and now2 >= jd["retry_at"]:
            jid = jd["jid"]
            rc = jd.get("retry_cnt", 0)
            try:
                Q.put(("__retry__", jid))
                jd.pop("retry_at", None)
                with _jl:
                    jj = jobs_load()
                    if jid in jj: jj[jid].pop("retry_at", None); jobs_save(jj)
                log(f"retry-enqueue {jid} #{rc}")
            except Exception as e:
                log(f"retry-err {e}")

def auto_watch_tick():
    if int(conf().get("auto_watch", 1)) != 1:
        return
    now = time.time()
    try:
        rows = []
        settings = watch_settings().read()
        stats = {"at": now, "candidates": 0, "managed": 0, "ignored": 0,
                 "inactive": 0, "too_small": 0, "missing": 0, "created": 0, "failed": 0}
        for kind, s8, proj, ttl, mt, dup in _scan_active():
            stats['candidates'] += 1
            if dup:
                stats['managed'] += 1
                continue
            if settings.get(s8, {}).get('ignored'):
                stats['ignored'] += 1
                continue
            if now - mt > 600:
                stats['inactive'] += 1
                continue
            try:
                f, uuid = find_rollout(s8)
                if not f:
                    stats['missing'] += 1
                    continue
                if settings.get(uuid, {}).get('ignored'):
                    stats['ignored'] += 1
                    continue
                threshold = 'dsh_auto_watch_min_kb' if harness.is_harness(f) else 'auto_watch_min_kb'
                if os.path.getsize(f) < int(conf().get(threshold, 50)) * 1024:
                    stats['too_small'] += 1
                    continue
            except Exception:
                stats['missing'] += 1
                continue
            rows.append((kind, s8))
    except Exception as e:
        log(f"autowatch-scan-err {e}")
        return
    for kind, s8 in rows[: int(conf().get("auto_watch_max", 3))]:
        try:
            ok, info = watch_session(app()["chat_id"], s8, sender="auto-watch", quiet=True)
            if ok:
                stats['created'] += 1
                log(f"auto-watch 入舱 {s8}")
            elif info != "dup":
                stats['failed'] += 1
                log(f"auto-watch-failed {s8} reason={info}")
        except Exception as e:
            stats['failed'] += 1
            log(f"auto-watch-err {s8} {e}")
    STATUS['auto_watch_scan'] = stats

def archive_stale_topics():
    """结束超24h的任务: 根卡定格为📦归档态(去按钮), jobs标archived"""
    now = time.time()
    with _jl:
        due = [dict(v) for v in jobs_load().values()
               if (v.get("state") in ("exited", "stopped") and not v.get("archived")
                   and v.get("root_mid")
                   and now - v.get("ended", v.get("started", now)) > 86400)]
    done = []
    for jd in due:
        # W6-6: 建卡与PATCH(带重试, 每任务最多2×10s)全在锁外, 锁内只挑候选与回标
        nm = jd.get("title") or str(jd.get("prompt", ""))[:20]
        card = {"schema": "2.0", "config": CARD_CONFIG,
                "header": {"template": "grey",
                           "title": {"tag": "plain_text",
                                     "content": f"📦 {jd['jid']} · {nm} (已归档)"}},
                "body": {"elements": [{"tag": "markdown",
                          "content": "_任务结束超 24 小时,话题留档于此。需要再驱动:主群发 `#go %s 指令` 会自动复活。_" % jd["jid"]}]}}
        o = api("PATCH", f"/im/v1/messages/{jd['root_mid']}",
                {"content": json.dumps(card, ensure_ascii=False)})
        if o and o.get("code") == 0:      # W2-12: API失败不标archived, 下轮重试
            done.append(jd["jid"])
        else:
            log(f"archive-fail {jd['jid']}")
    if done:
        with _jl:
            j = jobs_load(); ch = False
            for _jid in done:
                if _jid in j and not j[_jid].get("archived"):
                    j[_jid]["archived"] = True; ch = True
            if ch:
                jobs_save(j)

_JID_SEQF = f"{STATED}/.jid-seq"

def _jid_next(prefix, j):
    """W1-6: 前缀级单调水位线, 清理过的编号永不复用"""
    seqf = f"{STATED}/.jid-seq.{prefix}"
    try:
        n = int(open(seqf).read().strip() or "0") + 1
    except Exception:
        n = 1
    while f"{prefix}{n}" in j:
        n += 1
    try:
        with open(seqf + ".tmp", "w") as f:
            f.write(str(n))
        os.replace(seqf + ".tmp", seqf)
    except Exception:
        pass
    return f"{prefix}{n}"

def new_jid():
    """W1-6: 单调水位线分配(防旧卡引用路由到新任务)"""
    return _jid_next("j", jobs_load())

def known_models():
    """模型名单只来自 codex 本地配置: config.toml 默认 + model_catalog_json + ctl.conf 白名单"""
    ms = set()
    try:
        txt = open(f"{HOME}/.codex/config.toml", encoding="utf-8").read()
        m = re.search(r'^model\s*=\s*"([^"]+)"', txt, re.M)
        if m: ms.add(m.group(1))
        mc = re.search(r'^model_catalog_json\s*=\s*"([^"]+)"', txt, re.M)
        if mc:
            cat = os.path.join(HOME, ".codex", mc.group(1))
            if os.path.exists(cat):
                for x in json.load(open(cat, encoding="utf-8")).get("models", []):
                    sid = x.get("slug") or x.get("id")
                    if sid: ms.add(sid)
    except Exception as e:
        log(f"catalog-err {e}")
    ms |= {x.strip() for x in conf()["models"].split(",") if x.strip()}
    return ms

_PROJ = {"ts": 0.0, "list": []}
def projects():
    """项目注册表: codex local_thread_catalog 的 distinct cwd(按最近更新) + work_roots 下扫一层.
    返回 [(短名, 全路径, 最近使用epoch或0)] 按最近使用降序, 每项限白名单内."""
    now = time.time()
    if now - _PROJ["ts"] < 300 and _PROJ["list"]:
        return [p for p in _PROJ["list"] if allowed_cwd(p[1])]
    c = conf()
    roots = [os.path.realpath(os.path.expanduser(r)) for r in c["work_roots"].split(",") if r.strip()]
    def in_roots(d):
        return any(d == r or d.startswith(r + os.sep) for r in roots)
    seen = {}
    # 主源: codex catalog
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{HOME}/.codex/sqlite/codex-dev.db?mode=ro", uri=True, timeout=1)
        for d, ts in con.execute(
                "SELECT cwd, MAX(source_updated_at) FROM local_thread_catalog "
                "WHERE cwd != '' GROUP BY cwd ORDER BY 2 DESC"):
            d = os.path.realpath(d or "")
            if d and in_roots(d) and d not in seen:
                seen[d] = float(ts or 0)
        con.close()
    except Exception as e:
        log(f"proj-catalog-err {e}")
    # 辅源: work_roots 下扫一层(git仓或子目录)
    for r in roots:
        try:
            for name in sorted(os.listdir(r)):
                d = os.path.join(r, name)
                if os.path.isdir(d) and not name.startswith((".", "$")) \
                        and (os.path.isdir(os.path.join(d, ".git")) or os.path.isdir(os.path.join(d, "backend"))):
                    if allowed_cwd(d):
                        seen.setdefault(os.path.realpath(d), 0)
        except OSError:
            pass
    def short(d):
        base = os.path.basename(d.rstrip("/"))
        return base
    # 短名去重: 撞名则加父目录前缀
    used = {}
    out = []
    for d, ts in sorted(seen.items(), key=lambda kv: -kv[1]):
        sn = short(d)
        if sn in used:
            sn = os.path.basename(os.path.dirname(d)) + "/" + sn
        used[sn] = d
        out.append((sn, d, ts))
    _PROJ["ts"] = now; _PROJ["list"] = out
    return out

def allowed_cwd(path):
    if not path:
        return False
    candidate = os.path.realpath(os.path.expanduser(path))
    roots = [os.path.realpath(os.path.expanduser(r.strip()))
             for r in str(conf()["work_roots"]).split(",") if r.strip()]
    return os.path.isdir(candidate) and any(
        candidate == r or candidate.startswith(r + os.sep) for r in roots)


def require_cwd(path):
    if not allowed_cwd(path):
        raise ValueError("工作目录不存在或不在 work_roots 白名单内")
    return os.path.realpath(os.path.expanduser(path))


def resolve_target(first):
    """把 #new 首词解析为目录: 真实路径 / 项目短名(唯一命中) / None(不是目录)"""
    if not first:
        return None
    # 绝对/相对路径直判
    cand = os.path.realpath(os.path.expanduser(first))
    if allowed_cwd(cand):
        return cand, "path"
    if os.path.isabs(first) or first.startswith("~") or first.startswith("./") or "/" in first or "\\" in first:
        return None
    # 项目名匹配(忽略大小写; 短名或尾段)
    pl = projects()
    fl = first.lower()
    hits = [x for x in pl if x[0].lower() == fl or os.path.basename(x[1]).lower() == fl]
    if len(hits) == 1:
        return hits[0][1], "proj"
    if len(hits) > 1:
        return hits, "ambiguous"
    pref = [x for x in pl if x[0].lower().startswith(fl) or os.path.basename(x[1]).lower().startswith(fl)]
    if len(pref) == 1:
        return pref[0][1], "proj"
    return None

STOPICF = f"{STATED}/sess-topics.json"
def watch_settings():
    return JsonMap(Path(STOPICF).with_name('session-settings.json'))


def update_watch_settings(sid, **fields):
    def change(data):
        data.setdefault(sid, {}).update(fields)
    watch_settings().update(change)


def source_agent(path):
    if harness.is_harness(path): return 'deepseek-harness'
    if '/.claude/' in path: return 'claude'
    if '/.qoder-cn/' in path: return 'qoder'
    if '/.workbuddy/' in path or '/.codebuddy/' in path: return 'wcodebuddy'
    return 'desktop'


def watch_card_parts(sid, settings, detail=True):
    agent = settings.get('agent', 'desktop')
    icon, color = agent_style(agent)
    title = settings.get('title') or sid[:8]
    mode = {'full': '完整', 'result': '仅结果', 'errors': '异常提醒'}.get(settings.get('mode', 'full'), '完整')
    elements = [{'tag': 'markdown', 'content': f"👁 只读观察 · {agent}\n任务：{clean_md(title)}\n"
                f"播报：{'已暂停' if settings.get('paused') else mode}\n`{sid}`\n暂停或解除观察不会停止原会话。"}]
    controls = [btn('恢复播报' if settings.get('paused') else '暂停播报', 'observe_setting',
                    {'sid': sid, 'op': 'resume' if settings.get('paused') else 'pause'}),
                btn('修改备注', 'observe_title', {'sid': sid})]
    if detail:
        controls.insert(0, btn('观察详情', 'watch_detail', {'sess8': sid}))
    elements += btn_row(*controls)
    return format_title('topic', conf(), icon=icon, title=title, jid=sid[:8],
                        project=settings.get('project', ''), agent=agent), elements, color


def refresh_observed_titles():
    settings = watch_settings().read()
    topics = _st_load()
    for sid, value in settings.items():
        if not value.get('root_update_pending') or sid not in topics:
            continue
        if time.time() < value.get('root_retry_at', 0):
            continue
        title, elements, color = watch_card_parts(sid, value)
        card = {'schema': '2.0', 'config': CARD_CONFIG,
                'header': {'template': color, 'title': {'tag': 'plain_text', 'content': title}},
                'body': {'elements': elements}}
        result = api('PATCH', '/im/v1/messages/' + topics[sid], {'content': json.dumps(card, ensure_ascii=False)})
        def acknowledge(data):
            current = data.get(sid, {})
            # A newer user change must survive an older PATCH response.
            if current == value:
                current.update(root_update_pending=not result or result.get('code') != 0,
                               root_retry_at=time.time() + 30)
        watch_settings().update(acknowledge)


def resolve_observation(target):
    jobs = jobs_load()
    if re.fullmatch(r'[cwqd]?j\d+', target) and target in jobs:
        return 'job:' + target
    if not re.fullmatch(r'[0-9a-f-]{8,36}', target):
        raise ValueError('会话标识无效')
    identities = set(_st_load()) | {key for key in watch_settings().read() if not key.startswith('job:')}
    matches = [key for key in identities if key.startswith(target)]
    if len(matches) != 1:
        raise ValueError('未找到唯一观察会话，请查看 #watch 并使用完整 ID')
    return matches[0]


def cmd_observe(sender, chat_id, args):
    if not args:
        return reply(chat_id, '用法：#observe <会话ID或任务号> status|pause|resume|title 标题|mode full|result|errors')
    try:
        sid = resolve_observation(args[0])
    except ValueError as error:
        return reply(chat_id, str(error), template='orange')
    op = args[1] if len(args) > 1 else 'status'
    if op in ('full', 'result', 'errors'):
        args = [args[0], 'mode', op]
        op = 'mode'
    if op == 'title':
        if len(args) < 3:
            return reply(chat_id, '请提供备注标题')
        if sid.startswith('job:'):
            return cmd_title(sender, chat_id, [sid[4:], ' '.join(args[2:])])
        update_watch_settings(sid, title=normalize_title(' '.join(args[2:]), conf()), root_update_pending=True)
    elif op in ('pause', 'resume'):
        update_watch_settings(sid, paused=op == 'pause', root_update_pending=True)
    elif op == 'mode':
        if len(args) != 3 or args[2] not in ('full', 'result', 'errors'):
            return reply(chat_id, '模式仅支持 full（完整）、result（仅结果）、errors（异常提醒）')
        update_watch_settings(sid, mode=args[2], root_update_pending=True)
    elif op != 'status':
        return reply(chat_id, '操作仅支持 status、title、pause、resume、mode')
    if not sid.startswith('job:'):
        refresh_observed_titles()
    value = watch_settings().read().get(sid, {})
    audit(sender, '#observe', op)
    reply(chat_id, f"`{sid.removeprefix('job:')}` · {'已暂停' if value.get('paused') else '播报开启'} · 模式 {value.get('mode', 'full')}\n"
          f"备注：{clean_md(value.get('title') or '未设置')}\n"
          '暂停只影响进度播报，不停止任务；暂停期间新事件不补播，已排队消息恢复后继续投递。' +
          ('\n根卡待自动同步' if value.get('root_update_pending') and not sid.startswith('job:') else ''))


def _st_load():
    d = JsonMap(STOPICF).read()
    fixed = {}
    changed = False
    import re as _re
    for k, v in d.items():
        m = _re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", k)
        nk = m.group(1) if m else k
        if nk != k:
            changed = True
        fixed[nk] = v
    if changed:
        try:
            _st_save(fixed)
        except Exception:
            pass
    return fixed
def _st_save(d):
    private_json(STOPICF, d)

def find_rollout(sess8):
    if not re.fullmatch(r"[0-9a-f-]{8,36}", sess8):
        return "", ""
    import glob as _g
    home = os.environ.get('HOME') or HOME
    R = f"{home}/.codex/sessions"
    fs = (_g.glob(f"{R}/*/rollout-*{sess8}*.jsonl")
          + _g.glob(f"{R}/*/*/rollout-*{sess8}*.jsonl")
          + _g.glob(f"{R}/*/*/*/rollout-*{sess8}*.jsonl")
          + _g.glob(f"{home}/.workbuddy/projects/*/*{sess8}*.jsonl")
          + _g.glob(f"{home}/.claude/projects/*/*{sess8}*.jsonl")
          + _g.glob(f"{home}/.qoder-cn/projects/*/*{sess8}*.jsonl")
          + _g.glob(f"{home}/.codebuddy/projects/*/*{sess8}*.jsonl"))
    fs = [os.path.normpath(path) for path in fs]
    hits = [p for p in harness.files() if harness.session_id(p).startswith(sess8)]
    if hits:
        return (hits[0], harness.session_id(hits[0])) if len(hits) == 1 and not fs else ("", "")
    if not fs:
        return "", ""
    import re as _re
    matches = {}
    for f in fs:
        b = os.path.basename(f)
        m = _re.search(r"rollout-\S*?-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", b) \
            or _re.match(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl", b)
        if m and m.group(1).startswith(sess8):
            sid = m.group(1)
            if sid not in matches or os.path.getmtime(f) > os.path.getmtime(matches[sid]):
                matches[sid] = f
    if len(matches) != 1:
        return "", ""
    sid, f = next(iter(matches.items()))
    return f, sid

def _codex_proj(path):
    """codex rollout 的项目名: session_meta 首行的 cwd 末段(读坏/无 cwd 都回 '?')"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            o = json.loads(f.readline())
        cwd = (o.get("payload") or {}).get("cwd") or o.get("cwd") or ""
        return os.path.basename(str(cwd).rstrip("/")) or "?"
    except Exception:
        return "?"

def _rollout_task(path):
    if harness.is_harness(path):
        return harness.metadata(path)[1]
    if (".workbuddy/" in path or ".claude/projects/" in path
            or ".codebuddy/" in path or ".qoder-cn/projects/" in path):
        # wb/claude 白拿 ai-title; Qoder 不写该记录, 兜底取首条用户消息
        first = ""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                head = f.read(4194304)
            for ln in reversed(head.splitlines()):
                if '"ai-title"' in ln:
                    t = (json.loads(ln).get("aiTitle") or "").strip()
                    if t:
                        return (t[:20] + "…") if len(t) > 20 else t
            for ln in head.splitlines()[:400]:
                if '"type":"user"' not in ln.replace(" ", ""):
                    continue
                o = json.loads(ln)
                if o.get("isSidechain"):
                    continue
                ct = (o.get("message") or {}).get("content")
                txt = (ct if isinstance(ct, str) else
                       " ".join(x.get("text", "") for x in ct if isinstance(x, dict))
                       if isinstance(ct, list) else "").strip()
                if txt.startswith(("<", "{")):
                    continue    # 本地命令回显(/model)与 JSON 载荷不是用户意图, 往下找
                if txt:
                    first = txt
                    break
        except Exception:
            pass
        return (first[:20] + "…") if len(first) > 20 else first
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(2097152)
        for ln in head.splitlines():
            if '"role":"user"' not in ln and '"role": "user"' not in ln:
                continue
            o = json.loads(ln)
            pp = o.get("payload") or {}
            c = pp.get("content")
            txt = " ".join(x.get("text", "") for x in c if isinstance(x, dict)) if isinstance(c, list) else str(pp.get("message", ""))
            if txt.strip() and "environment_context" not in txt and "codex_internal_context" not in txt:
                t = txt.strip().splitlines()[0]
                return (t[:20] + "…") if len(t) > 20 else t
    except Exception:
        pass
    return ""

def _scan_active():
    """四源近2h活跃会话 -> [(kind, sess8, proj, task, mtime, 是否已纳管)]"""
    import glob as _g
    now = time.time()
    jobs = jobs_load()
    managed_sess = set()
    for jd in jobs.values():
        m = re.search(r"([0-9a-f-]{30,})", str(jd.get("sess") or ""))
        if m:
            managed_sess.add(m.group(1))
        managed_sess.add(str(jd.get("jid")))
    try:
        stop = {}
        with open(f"{STATED}/sess-topics.json", encoding="utf-8") as f:
            stop = json.load(f)
        managed_sess |= set(stop)
    except Exception:
        pass
    out = []
    # codex rollouts (深路径, 取mtime新前6)
    fs = []
    for pat in ("*/rollout-*.jsonl", "*/*/rollout-*.jsonl", "*/*/*/rollout-*.jsonl"):
        fs += _g.glob(f"{HOME}/.codex/sessions/" + pat)
    fs = [x for x in fs if now - os.path.getmtime(x) < 7200]
    fs.sort(key=os.path.getmtime, reverse=True)
    seen = set()
    for f in fs:
        b = os.path.basename(f)
        m = re.search(r"rollout-\S*?-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", b)
        if not m:
            continue
        s8 = m.group(1)
        if s8 in seen:
            continue
        seen.add(s8)
        out.append(("🖥", s8, _codex_proj(f), _rollout_task(f),
                    os.path.getmtime(f), s8 in managed_sess or _in_jobs(b)))
    # claude + qoder + wb/codebuddy
    for src, ic in ((f"{HOME}/.claude/projects", "🤖"),
                    (f"{HOME}/.qoder-cn/projects", "🧩"),
                    (f"{HOME}/.workbuddy/projects", "🦞"),
                    (f"{HOME}/.codebuddy/projects", "🦞")):
        fs = [x for x in _g.glob(src + "/*/*.jsonl") if now - os.path.getmtime(x) < 7200]
        fs.sort(key=os.path.getmtime, reverse=True)
        for f in fs:
            s8 = os.path.basename(f)[:-6]
            if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', s8):
                continue
            proj = os.path.basename(os.path.dirname(f)).replace("-", "/")
            proj = os.path.basename(proj.rstrip("/")) or "?"
            ttl = _rollout_task(f)     # ai-title 优先, Qoder 无该记录则兜底首条用户消息
            dup = s8 in managed_sess or _rollout_mapped(s8)
            out.append((ic, s8, proj, ttl, os.path.getmtime(f), dup))
    for f in sorted(harness.files(), key=os.path.getmtime, reverse=True):
        if now - os.path.getmtime(f) < 7200:
            sid = harness.session_id(f)
            proj, title = harness.metadata(f)
            out.append(("🐋", sid, proj, title, os.path.getmtime(f), sid in managed_sess or _rollout_mapped(sid)))
    return out

def _in_jobs(basename):
    try:
        j = jobs_load()
        return any(basename[:40] in str(v.get("out_file", "")) or basename in str(v) for v in j.values())
    except Exception:
        return False

def _rollout_mapped(sess8):
    try:
        j = jobs_load()
        return any(sess8 in str(v.get("sess", "")) for v in j.values())
    except Exception:
        return False

def cmd_scan(sender, chat_id, args):
    rows = [r for r in _scan_active() if not r[5]]
    if not rows:
        return reply(chat_id, "📥 近2小时没有未纳管的活跃会话(都已入舱或无活动)", template="grey")
    els = []
    bs = []
    for _i, (ic, s8, proj, ttl, mt, _) in enumerate(rows[:8]):
        ago = int((time.time() - mt) / 60)
        lab = f"{s8[:8]}·{ttl[:8] or proj[:8]}·{ago}m前"
        bs.append(btn("📥 " + lab[:22], "watch_sess", {"sess8": s8, "label": ttl or proj},
                      "primary" if _i == 0 else "default"))  # W3-15: 每卡primary≤1
        els.append({"tag": "markdown", "content": f"{ic} `{s8}` · {clean_md((ttl or proj)[:28])} · {ago}分钟前活跃"})
    els += btn_row(*bs)
    els.append({"tag": "markdown", "content": "_📥=建话题舱并收纳后续播报;忽略即无影响_", "text_size": "notation"})
    reply_card(chat_id, "📡 近2h活跃未纳管会话", "wathet", els)

_WATCH_LOCK = threading.RLock()


def create_topic(key, chat_id, title, elements, anchor_title, anchor_text, tag=None):
    """Persist both idempotency tokens and acknowledged steps before proceeding."""
    store = JsonMap(Path(STATED) / 'topic-builds.json' if key.startswith('job:')
                    else Path(STOPICF).with_name('topic-builds.json'))
    with store.locked():
        builds = store.read()
        build = builds.setdefault(key, {'root_token': uuid.uuid4().hex,
                                        'anchor_token': uuid.uuid4().hex,
                                        'chat_id': chat_id, 'created_at': time.time(), 'attempts': 0})
        if build['chat_id'] != chat_id:
            return '', '会话已在另一聊天建舱，请在原聊天管理'
        if build.get('phase') == 'ready':
            return build['root_mid'], build['thread_id']
        if time.time() < build.get('next_at', 0):
            return '', '等待建舱重试'
        private_json(store.path, builds)  # Reserve stable identity before any POST.
        if not build.get('root_mid'):
            mid = action_card(chat_id, title, elements, tag=tag, delivery_id=build['root_token'])
            if not mid:
                topic_build_failed(store, builds, build, '建卡失败')
                return '', '建卡失败'
            build.update(root_mid=mid, phase='root_created')
            private_json(store.path, builds)
        mid = build['root_mid']
        anchor = {'schema': '2.0', 'config': CARD_CONFIG,
                  'header': {'template': 'grey', 'title': {'tag': 'plain_text', 'content': anchor_title}},
                  'body': {'elements': [{'tag': 'markdown', 'content': anchor_text}]}}
        result = api('POST', f'/im/v1/messages/{mid}/reply',
                     {'content': json.dumps(anchor, ensure_ascii=False), 'msg_type': 'interactive',
                      'reply_in_thread': True, 'uuid': build['anchor_token']})
        data = (result or {}).get('data') or {}
        if (result or {}).get('code') != 0 or not (data.get('message_id') or data.get('thread_id')):
            topic_build_failed(store, builds, build, '话题回复失败', (result or {}).get('code'))
            return '', '话题回复失败，已保存根卡，稍后重试'
        build.update(phase='ready', thread_id=data.get('thread_id') or mid, next_at=0)
        build.pop('error', None)
        private_json(store.path, builds)
        return mid, build['thread_id']


def topic_build_failed(store, builds, build, reason, code=None):
    build['attempts'] += 1
    build.update(error={'reason': reason, 'code': code},
                 next_at=time.time() + min(300, 5 * 2 ** min(build['attempts'] - 1, 6)))
    private_json(store.path, builds)
    STATUS['topic_build_failed_at'] = time.time()


def watch_session(chat_id, sess8, sender="auto", quiet=False):
    with _WATCH_LOCK:
        return _watch_session(chat_id, sess8, sender, quiet)


def _watch_session(chat_id, sess8, sender="auto", quiet=False):
    """纳管一个外部会话: 建根卡+话题, sess-topics登记uuid->root_mid。返回(jid槽,mid)或(None,原因)"""
    f, uuid = find_rollout(sess8)
    if not f:
        return None, f"找不到唯一会话 {sess8}*；请 #scan 查看并使用完整 ID"
    st = _st_load()
    if uuid in st:
        if sender != 'auto-watch':
            update_watch_settings(uuid, ignored=False)
        entry = next(({'kind': 'job', 'jid': jid} for jid, job in jobs_load().items()
                      if job.get('root_mid') == st[uuid]), {'kind': 'sess', 'sid8': uuid})
        register_route(CMIDF, st[uuid], entry)
        return None, "dup"
    if sender == 'auto-watch' and watch_settings().read().get(uuid, {}).get('ignored'):
        return None, 'ignored'
    title = watch_settings().read().get(uuid, {}).get('title') or _rollout_task(f) or sess8[:8]
    project = harness.metadata(f)[0] if harness.is_harness(f) else _codex_proj(f) if source_agent(f) == 'desktop' else os.path.basename(os.path.dirname(f))
    metadata = {**watch_settings().read().get(uuid, {}), 'title': title,
                'project': project, 'agent': source_agent(f)}
    root_title, els, color = watch_card_parts(uuid, metadata)
    existing = next((binding for binding in desktop_bindings().read().values()
                     if binding.get('sid') == uuid and binding.get('chat_id') == chat_id
                     and binding.get('root_mid')), None)
    mid = tid = ''
    if existing:
        found = api('GET', f"/im/v1/messages/{existing['root_mid']}") or {}
        items = ((found.get('data') or {}).get('items') or [])
        if found.get('code') == 0 and items and not items[0].get('deleted'):
            mid, tid = existing['root_mid'], existing.get('thread_id') or existing['root_mid']
    if not mid:
        mid, tid = create_topic('watch:' + uuid, chat_id, root_title, els,
                                f'🧵 {sess8[:8]} 播报舱', '本话题收纳该会话全部进度播报。', tag=color)
    if not mid:
        return None, tid
    JsonMap(STOPICF).put(uuid, mid)
    register_route(CMIDF, mid, {'kind': 'sess', 'sid8': uuid})
    update_watch_settings(uuid, ignored=False, title=normalize_title(title, conf()),
                          agent=source_agent(f), project=project, last_active=os.path.getmtime(f))
    audit(sender, f"watch {sess8}", f"topic mid={mid[:16]}")
    if not quiet:
        reply(chat_id, f"🧵 已纳管 {sess8}「{title[:18]}」,新播报进舱;解除: `#unwatch {sess8}`", template="green")
    return mid, tid

def cmd_watch(sender, chat_id, args):
    if not args or args[0] == 'list':
        st = _st_load()
        settings = watch_settings().read()
        ids = sorted(set(st) | {sid for sid, value in settings.items() if value.get('ignored') and not sid.startswith('job:')})
        try:
            page = max(1, int(args[1])) if len(args) > 1 else 1
        except ValueError:
            return reply(chat_id, '用法：#watch list [页码]')
        pages = max(1, (len(ids) + 5) // 6)
        page = min(page, pages)
        elements = []
        for sid in ids[(page - 1) * 6:page * 6]:
            value = settings.get(sid, {})
            status = '主动忽略自动纳管' if value.get('ignored') else '已暂停播报' if value.get('paused') else '已纳管'
            elements.append({'tag': 'markdown', 'content': f"**{clean_md(value.get('title') or sid[:8])}** · {status}\n`{sid}` · 模式 {value.get('mode', 'full')}"})
            if sid in st:
                elements += btn_row(btn('观察详情', 'watch_detail', {'sess8': sid}))
            else:
                elements += btn_row(btn('恢复纳管', 'watch_sess', {'sess8': sid}))
        if not ids:
            elements.append({'tag': 'markdown', 'content': '暂无纳管或主动忽略的会话。'})
        navigation = []
        if page > 1: navigation.append(btn('上一页', 'watch_list', {'page': page - 1}))
        if page < pages: navigation.append(btn('下一页', 'watch_list', {'page': page + 1}))
        elements += btn_row(*navigation)
        elements.append({'tag': 'markdown', 'content': f'第 {page}/{pages} 页 · 共 {len(ids)} 项', 'text_size': 'notation'})
        return reply_card(chat_id, '观察与忽略清单', 'grey', elements)
    sess8 = args[0].replace("sess_", "")
    ok, info = watch_session(chat_id, sess8, sender=sender)
    if not ok:
        reply(chat_id, '已在纳管清单，主动忽略已解除' if info == 'dup' else f"❌ {info}", template='grey' if info == 'dup' else 'red')

def cmd_unwatch(sender, chat_id, args):
    with _WATCH_LOCK:
        return _cmd_unwatch(sender, chat_id, args)


def _cmd_unwatch(sender, chat_id, args):
    # W5-4: 空参不再等于全清(startswith("")恒真会静默删光纳管); 全清须显式 all
    if not args:
        return reply(chat_id, "用法: `#unwatch <sess8>` 解除单个 · `#unwatch all` 全部解除\n"
                              "当前纳管清单: `#watch`", template="orange")
    st = _st_load()
    arg = args[0].replace("sess_", "")
    hit = list(st) if arg == "all" else [k for k in st if k.startswith(arg)]
    if not hit:
        return reply(chat_id, f"未纳管 {arg}*", template="orange")
    if arg != "all" and len(hit) != 1:
        return reply(chat_id, "会话短 ID 存在歧义，请使用完整 ID 解除观察", template="orange")
    for k in hit:
        update_watch_settings(k, ignored=True)
    def remove(data):
        for key in hit:
            data.pop(key, None)
    JsonMap(STOPICF).update(remove)
    audit(sender, f"#unwatch {arg}", f"removed={len(hit)}")
    reply(chat_id, f"✅ 已解除纳管 {len(hit)} 个会话；已加入自动纳管忽略清单，手动 #watch 可恢复(播报回主群)", template="green")

def cmd_projects(sender, chat_id, args):
    pl = projects()[:12]
    if not pl:
        return reply(chat_id, "(项目注册表为空, 检查 catalog / work_roots)", template="red")
    lines = []
    for i, (sn, d, ts) in enumerate(pl, 1):
        ago = f"{int((time.time()-ts)/3600)}h前" if ts and time.time()-ts < 86400*3 else ("—" if not ts else ">3d")
        mark = " ⭐" if i == 1 else ""
        lines.append(f"{i}. **{sn}**{mark} · {ago} · `{d.replace(HOME,'~')}`")
    reply_card(chat_id, "📂 项目注册表(⭐=默认最近)", "turquoise", [
        {"tag": "markdown", "content": "\n".join(lines)},
        btn("🚀 选项目开任务", "wizard", {"w": "new"}, "primary")])

def cmd_new(sender, chat_id, args):
    c = conf()
    if not args or args[0] in ("?", "选择", "list"):
        return cmd_projects(sender, chat_id, [])
    cwd = None; how = ""; rest = args
    tgt = resolve_target(args[0])
    if not tgt and (os.path.isabs(args[0]) or args[0].startswith(("/", "~", ".")) or "/" in args[0] or "\\" in args[0]):
        return reply(chat_id, "❌ 指定目录不存在或不在 work_roots 白名单内", template="red")
    if isinstance(tgt, tuple) and len(tgt) == 2 and isinstance(tgt[0], list):   # ambiguous
        names = [x[0] for x in tgt[0]]
        return reply(chat_id, f"❓ 项目名 `{args[0]}` 有多个候选: {', '.join(names)}\n请用更完整名称,或 `#projects` 查看", template="red")
    if tgt:
        cwd, how = tgt; rest = args[1:]
    elif args[0] in known_models():
        pass  # 首词就是模型 -> 落到默认项目
    if cwd is None and bool(c.get("fallback_recent", 1)):
        pl = projects()
        if pl:
            cwd, how = pl[0][1], f"默认最近({pl[0][0]})"
    if cwd is None:
        return reply(chat_id, "用法: `#new [项目|目录] [模型] 提示词…`\n不带项目时用最近活跃项目; `#projects` 查看列表", template="red")
    model = c["default_model"]
    if rest and rest[0] in known_models():   # 仅当命中codex模型清单才算模型, 否则全为提示词
        model = rest[0]; rest = rest[1:]
    title, perm, mode, keep = parse_new_tokens(rest)
    prompt = " ".join(keep).strip()
    if not prompt:
        return reply(chat_id, "❌ 缺少提示词", template="red")
    j = jobs_load()
    if sum(1 for v in j.values() if v.get("state") == "running") >= int(c["max_jobs"]):
        return reply(chat_id, f"❌ 并发任务已达上限 {c['max_jobs']}", template="red")
    jid = new_jid()
    rcf = new_round_rc(jid)
    outf = f"{STATED}/job-{jid}.out"
    sandbox = c["sandbox"] if perm == "safe" else "danger-full-access"
    if mode == "plan":
        sandbox = "read-only"   # 沙箱级强制只读,模型想动手也动不了
        prompt = ("【计划模式】你只做分析与规划,本回合禁止修改任何文件、禁止 git 写操作。"
                  "请输出结构化方案:目标理解/现状盘点/方案对比(含推荐)/执行阶段拆分/风险与回滚。"
                  "结尾列出等待指令的执行选项。\n\n任务:" + prompt)
    cmd = [c["codex_bin"], "exec", "--skip-git-repo-check",
           "-s", sandbox, "--json", "-C", cwd]
    if model: cmd += ["-c", f"model={model}"]
    cmd += ["--", prompt]
    try:
        env = dict(os.environ, CODEX_SESS_DIR="")
        with open(outf, "wb") as of:
            p = subprocess.Popen(cmd, cwd=require_cwd(cwd), stdout=of,
                                 stderr=subprocess.STDOUT, env=env, **process_flags())
    except Exception as e:
        return reply(chat_id, f"❌ 启动失败: {e}", template="red")
    # rc 回收脚本: 等退出写 rc 文件
    threading.Thread(target=_reap, args=(p, rcf), daemon=True).start()
    sess = ""  # 稍后从 --json 输出/rollout 懒解析
    with _jl:
        j = jobs_load()
        j[jid] = {"jid": jid, "pid": p.pid, "state": "running", "cwd": cwd,
                  "model": model or "(默认)", "prompt": prompt[:120],
                  "title": title, "perm": perm, "mode": mode, "chat_id": chat_id,
                  "rc_file": rcf, "out_file": outf, "started": time.time(),
                  "sender": sender}
        jobs_save(j)
    audit(sender, f"#new {shlex.join(args)}", f"started pid={p.pid} jid={jid}")
    proj_tag = how if how.startswith("默认") else ("显式路径" if how == "path" else "指定项目")
    mode_tag = {"plan": " 📋计划(只读)", "goal": " 🎯目标模式"}.get(mode, "")
    root_topic(chat_id, jid, {**j[jid], "proj_tag": proj_tag + mode_tag})
    if mode == "goal":
        goal_start(chat_id, jid, prompt, sender)

def cmd_cnew(sender, chat_id, args):
    """claude code 任务: #cnew [项目] [模型] 标题=名 提示词… (模式=plan 只读规划 / 模式=goal 装目标环)"""
    c = conf()
    if not args or args[0] in ("?", "选择", "list"):
        return cmd_projects(sender, chat_id, [])
    cwd = None; how = ""; rest = args
    tgt = resolve_target(args[0])
    if not tgt and (os.path.isabs(args[0]) or args[0].startswith(("/", "~", ".")) or "/" in args[0] or "\\" in args[0]):
        return reply(chat_id, "❌ 指定目录不存在或不在 work_roots 白名单内", template="red")
    if isinstance(tgt, tuple) and len(tgt) == 2 and isinstance(tgt[0], list):
        names = [x[0] for x in tgt[0]]
        return reply(chat_id, f"❓ 项目 `{args[0]}` 多候选: {', '.join(names)}", template="red")
    if tgt:
        cwd, how = tgt; rest = args[1:]
    if cwd is None and bool(c.get("fallback_recent", 1)):
        pl = projects()
        if pl:
            cwd, how = pl[0][1], f"默认最近({pl[0][0]})"
    if cwd is None:
        return reply(chat_id, "用法: `#cnew [项目] [模型] 标题=名 提示词…`", template="red")
    model = ""
    CL_MODELS = {"sonnet", "opus", "haiku"}
    if rest and rest[0] in CL_MODELS:
        model = rest[0]; rest = rest[1:]
    title, perm, mode, keep = parse_new_tokens(rest)
    prompt = " ".join(keep).strip()
    if not prompt:
        return reply(chat_id, "❌ 缺少提示词", template="red")
    j = jobs_load()
    if sum(1 for v in j.values() if v.get("state") == "running") >= int(c["max_jobs"]):
        return reply(chat_id, f"❌ 并发任务已达上限 {c['max_jobs']}", template="red")
    import uuid as _uu
    sid = str(_uu.uuid4())
    jid = _jid_next("cj", j)
    rcf = new_round_rc(jid)
    outf = f"{STATED}/job-{jid}.out"
    pm = "plan" if mode == "plan" else ("acceptEdits" if perm == "safe" else "bypassPermissions")
    cmd = [c.get("claude_bin", CLAUDE), "-p", prompt, "--session-id", sid,
           "--permission-mode", pm, "--output-format", "json"]
    if model: cmd += ["--model", model]
    try:
        with open(outf, "wb") as of:
            p_ = subprocess.Popen(cmd, cwd=require_cwd(cwd), stdout=of,
                                  stderr=subprocess.STDOUT, **process_flags())
    except Exception as e:
        return reply(chat_id, f"❌ 启动失败: {e}", template="red")
    threading.Thread(target=_reap, args=(p_, rcf), daemon=True).start()
    with _jl:
        j = jobs_load()
        j[jid] = {"jid": jid, "agent": "claude", "pid": p_.pid, "state": "running",
                  "cwd": cwd, "model": model or "(默认)", "prompt": prompt[:120],
                  "title": title, "perm": perm, "mode": mode, "perm_mode": pm, "sess": sid,
                  "chat_id": chat_id, "rc_file": rcf, "out_file": outf,
                  "started": time.time(), "sender": sender}
        jobs_save(j)
    audit(sender, f"#cnew {shlex.join(args)}", f"started pid={p_.pid} jid={jid}")
    proj_tag = how if how.startswith("默认") else ("显式路径" if how == "path" else "指定项目")
    mode_tag = {"plan": " 📋计划(只读)", "goal": " 🎯目标模式"}.get(mode, "")
    root_topic(chat_id, jid, {**j[jid], "proj_tag": proj_tag + mode_tag})
    if mode == "goal":
        goal_start(chat_id, jid, prompt, sender)

def cmd_qnew(sender, chat_id, args):
    """Qoder CLI 任务: #qnew [项目] 标题=名 提示词… (模式=plan 只读规划 / 模式=goal 装目标环)
    模型不在这里给: Qoder 的可用模型要登录后 `qoderclicn --list-models` 才知, 用 #model 换。"""
    if not args or args[0] in ("?", "选择", "list"):
        return cmd_projects(sender, chat_id, [])
    c = conf()
    qb = c.get("qoder_bin", QODER)
    if not os.path.exists(qb):
        return reply(chat_id, "❌ 未找到 Qoder CLI,安装: `npm i -g @qodercn-ai/qoderclicn`", template="red")
    cwd = None; how = ""; rest = args
    tgt = resolve_target(args[0])
    if not tgt and (os.path.isabs(args[0]) or args[0].startswith(("/", "~", ".")) or "/" in args[0] or "\\" in args[0]):
        return reply(chat_id, "❌ 指定目录不存在或不在 work_roots 白名单内", template="red")
    if isinstance(tgt, tuple) and len(tgt) == 2 and isinstance(tgt[0], list):
        names = [x[0] for x in tgt[0]]
        return reply(chat_id, f"❓ 项目 `{args[0]}` 多候选: {', '.join(names)}", template="red")
    if tgt:
        cwd, how = tgt; rest = args[1:]
    if cwd is None and bool(c.get("fallback_recent", 1)):
        pl = projects()
        if pl:
            cwd, how = pl[0][1], f"默认最近({pl[0][0]})"
    if cwd is None:
        return reply(chat_id, "用法: `#qnew [项目] 标题=名 提示词…`", template="red")
    title, perm, mode, keep = parse_new_tokens(rest)
    prompt = " ".join(keep).strip()
    if not prompt:
        return reply(chat_id, "❌ 缺少提示词", template="red")
    j = jobs_load()
    if sum(1 for v in j.values() if v.get("state") == "running") >= int(c["max_jobs"]):
        return reply(chat_id, f"❌ 并发任务已达上限 {c['max_jobs']}", template="red")
    import uuid as _uu
    sid = str(_uu.uuid4())
    jid = _jid_next("qj", j)
    rcf = new_round_rc(jid)
    outf = f"{STATED}/job-{jid}.out"
    pm = "plan" if mode == "plan" else ("accept_edits" if perm == "safe" else "bypass_permissions")
    cmd = [qb, "-p", prompt, "--session-id", sid, "--permission-mode", pm, "-o", "json"]
    try:
        with open(outf, "wb") as of:
            p_ = subprocess.Popen(cmd, cwd=require_cwd(cwd), stdout=of,
                                  stderr=subprocess.STDOUT, **process_flags())
    except Exception as e:
        return reply(chat_id, f"❌ 启动失败: {e}", template="red")
    threading.Thread(target=_reap, args=(p_, rcf), daemon=True).start()
    with _jl:
        j = jobs_load()
        j[jid] = {"jid": jid, "agent": "qoder", "pid": p_.pid, "state": "running",
                  "cwd": cwd, "model": "(默认)", "prompt": prompt[:120],
                  "title": title, "perm": perm, "mode": mode, "perm_mode": pm, "sess": sid,
                  "chat_id": chat_id, "rc_file": rcf, "out_file": outf,
                  "started": time.time(), "sender": sender}
        jobs_save(j)
    audit(sender, f"#qnew {shlex.join(args)}", f"started pid={p_.pid} jid={jid}")
    proj_tag = how if how.startswith("默认") else ("显式路径" if how == "path" else "指定项目")
    mode_tag = {"plan": " 📋计划(只读)", "goal": " 🎯目标模式"}.get(mode, "")
    root_topic(chat_id, jid, {**j[jid], "proj_tag": proj_tag + mode_tag})
    if mode == "goal":
        goal_start(chat_id, jid, prompt, sender)

def _start_dsh(sender, chat_id, cwd, prompt, title, perm, mode, session=""):
    cwd = require_cwd(cwd)
    c = conf(); j = jobs_load()
    if sum(v.get("state") in ("running", "stopping", "switching") for v in j.values()) >= int(c["max_jobs"]):
        return reply(chat_id, "❌ 并发任务已达上限", template="red")
    if session and any(v.get("sess") == session or (v.get("state") in ("running", "stopping", "switching") and v.get("requested_sess") == session) for v in j.values()):
        return reply(chat_id, "该会话已有遥控任务，请用 #jobs 找到 dj 任务号，再用 #go 续跑", template="orange")
    jid = _jid_next("dj", j)
    rcf = new_round_rc(jid); outf = f"{STATED}/job-{jid}.out"
    try:
        with open(outf, "wb") as of:
            os.chmod(outf, 0o600)
            process = dsh.spawn(c.get("dsh_bin", DSH), cwd, prompt, of,
                                session=session, perm=perm, mode=mode, node_bin=c.get("dsh_node_bin", ""), config_patch=c.get("dsh_config_patch", ""))
    except (OSError, ValueError):
        return reply(chat_id, "❌ Harness 启动失败，请检查 dsh_bin、目录及本机 headless 配置", template="red")
    threading.Thread(target=_reap, args=(process, rcf), daemon=True).start()
    jd = {"jid":jid, "agent":"deepseek-harness", "pid":process.pid, "state":"running",
          "cwd":cwd, "model":"(headless profile)", "prompt":prompt[:120], "title":title,
          "perm":perm, "mode":mode, "sess":"", "requested_sess":session, "chat_id":chat_id, "rc_file":rcf,
          "out_file":outf, "started":time.time(), "sender":sender}
    try:
        with _jl:
            j=jobs_load(); j[jid]=jd; jobs_save(j)
    except OSError:
        _kill_group(process.pid)
        return reply(chat_id, "❌ 任务状态无法持久化，已停止刚启动的进程，避免失去管理", template="red")
    audit(sender, "#dnew" if not session else "#dresume", f"started jid={jid}")
    root_topic(chat_id, jid, jd)
    if mode == "goal": goal_start(chat_id, jid, prompt, sender)


def cmd_dnew(sender, chat_id, args):
    """Explicit project required; no ambiguous fallback to the latest workspace."""
    if len(args) < 2:
        return reply(chat_id, "用法: #dnew <项目或白名单目录> [标题=名] [权限=safe|full] 指令", template="orange")
    target = resolve_target(args[0])
    if not target or isinstance(target[0], list):
        return reply(chat_id, "❌ 项目不存在、不在白名单内或存在多候选，请指定完整目录", template="red")
    title, perm, mode, keep = parse_new_tokens(args[1:])
    prompt = " ".join(keep).strip()
    if not prompt: return reply(chat_id, "❌ 缺少提示词", template="red")
    return _start_dsh(sender, chat_id, target[0], prompt, title, perm, mode)


def cmd_dresume(sender, chat_id, args):
    """Explicit transfer of an idle, persisted Harness session to Tower control."""
    if len(args) < 2:
        return reply(chat_id, "用法: #dresume <会话前8位> [权限=safe|full] 指令；请先关闭桌面端该会话", template="orange")
    path, _ = find_rollout(args[0].removeprefix("session-"))
    if not path or not harness.is_harness(path):
        return reply(chat_id, "❌ 找不到唯一的 Harness v4 会话", template="red")
    try:
        header = harness.header(path)
        if header.get("isSeeded") or header.get("parentSession") or header.get("origin") == "subagent":
            raise ValueError("子会话/派生会话不可直接接管")
        cwd = require_cwd(header.get("cwd", ""))
        session = header.get("id")
        if not dsh.valid_session(session): raise ValueError("原始 session ID 无效")
        dsh.assert_available(path)
    except (OSError, ValueError) as e:
        return reply(chat_id, f"❌ 不能接管: {e}", template="red")
    title, perm, mode, keep = parse_new_tokens(args[1:])
    prompt = " ".join(keep).strip()
    if not prompt: return reply(chat_id, "❌ 缺少提示词", template="red")
    return _start_dsh(sender, chat_id, cwd, prompt, title or harness.metadata(path)[1], perm, mode, session)


def cmd_wnew(sender, chat_id, args):
    """WorkBuddy/CodeBuddy CLI 任务: #wnew [项目] [模型] 标题=名 提示词… (模式=plan / 权限=full)
    注: WB 的 acceptEdits 不放行 Bash, 需跑命令请带 权限=full(即 -y)"""
    c = conf()
    if not os.path.exists(c.get("wcb_bin", WCB)):
        return reply(chat_id, "❌ 未找到 codebuddy CLI(app.asar.unpacked/cli/bin/codebuddy)", template="red")
    if not args or args[0] in ("?", "选择", "list"):
        return cmd_projects(sender, chat_id, [])
    cwd = None; how = ""; rest = args
    tgt = resolve_target(args[0])
    if not tgt and (os.path.isabs(args[0]) or args[0].startswith(("/", "~", ".")) or "/" in args[0] or "\\" in args[0]):
        return reply(chat_id, "❌ 指定目录不存在或不在 work_roots 白名单内", template="red")
    if isinstance(tgt, tuple) and len(tgt) == 2 and isinstance(tgt[0], list):
        names = [x[0] for x in tgt[0]]
        return reply(chat_id, f"❓ 项目 `{args[0]}` 多候选: {', '.join(names)}", template="red")
    if tgt:
        cwd, how = tgt; rest = args[1:]
    if cwd is None and bool(c.get("fallback_recent", 1)):
        pl = projects()
        if pl:
            cwd, how = pl[0][1], f"默认最近({pl[0][0]})"
    if cwd is None:
        return reply(chat_id, "用法: `#wnew [项目] [模型] 标题=名 提示词…`", template="red")
    model = ""
    if rest and rest[0] in WCB_MODELS:
        model = rest[0]; rest = rest[1:]
    title, perm, mode, keep = parse_new_tokens(rest)
    prompt = " ".join(keep).strip()
    if not prompt:
        return reply(chat_id, "❌ 缺少提示词", template="red")
    j = jobs_load()
    if sum(1 for v in j.values() if v.get("state") == "running") >= int(c["max_jobs"]):
        return reply(chat_id, f"❌ 并发任务已达上限 {c['max_jobs']}", template="red")
    import uuid as _uu
    sid = str(_uu.uuid4())
    jid = _jid_next("wj", j)
    rcf = new_round_rc(jid)
    outf = f"{STATED}/job-{jid}.out"
    cmd = [c.get("wcb_bin", WCB), "-p", prompt, "--session-id", sid,
           "--output-format", "json"]
    if mode == "plan":
        cmd += ["--permission-mode", "plan"]
    elif perm == "safe":
        cmd += ["--permission-mode", "acceptEdits"]
    else:
        cmd += ["-y"]   # 默认full(单机自用)
    if model: cmd += ["--model", model]
    try:
        with open(outf, "wb") as of:
            p_ = subprocess.Popen(cmd, cwd=require_cwd(cwd), stdout=of,
                                  stderr=subprocess.STDOUT, **process_flags())
    except Exception as e:
        return reply(chat_id, f"❌ 启动失败: {e}", template="red")
    threading.Thread(target=_reap, args=(p_, rcf), daemon=True).start()
    with _jl:
        j = jobs_load()
        j[jid] = {"jid": jid, "agent": "wcodebuddy", "pid": p_.pid, "state": "running",
                  "cwd": cwd, "model": model or "(auto)", "prompt": prompt[:120],
                  "title": title, "perm": perm, "mode": mode,
                  "perm_mode": ("plan" if mode == "plan" else "acceptEdits" if perm == "safe" else "bypass"),
                  "sess": sid, "chat_id": chat_id, "rc_file": rcf, "out_file": outf,
                  "started": time.time(), "sender": sender}
        jobs_save(j)
    audit(sender, f"#wnew {shlex.join(args)}", f"started pid={p_.pid} jid={jid}")
    proj_tag = how if how.startswith("默认") else ("显式路径" if how == "path" else "指定项目")
    mode_tag = {"plan": " 📋计划(只读)", "goal": " 🎯目标模式"}.get(mode, "")
    if perm == "safe" and not mode:
        mode_tag += " 🔒safe(Bash需授权)"
    root_topic(chat_id, jid, {**j[jid], "proj_tag": proj_tag + mode_tag})
    if mode == "goal":
        goal_start(chat_id, jid, prompt, sender)

_THREAD_IDX = {"mt": 0.0, "d": {}}
def thread_index():
    """root_mid -> jid 反查(话题路由用); thread_id(omt_) 命中也在表内"""
    try:
        mt = os.path.getmtime(JOBSF)
        if mt != _THREAD_IDX["mt"] or not _THREAD_IDX["d"]:
            _THREAD_IDX["mt"] = mt
            j = jobs_load()
            d = {}
            for k, v in j.items():
                if v.get("root_mid"):
                    d[v["root_mid"]] = k
                if v.get("thread_id"):
                    d[v["thread_id"]] = k
            _THREAD_IDX["d"] = d
    except Exception:
        pass
    return _THREAD_IDX["d"]

def thread_to_jid(thread_id):
    return thread_index().get(thread_id, "")

def root_card_parts(jid, jd):
    """Create and rename use the same card, preserving persistent controls."""
    nm = jd.get("title") or str(jd.get("prompt", ""))[:30]
    els = [
        {"tag": "markdown",
         "content": (f"· 📂 `{os.path.basename(jd.get('cwd',''))}` ({jd.get('proj_tag','')})\n"
                     f"· 模型: {jd.get('model','默认')} · PID {jd.get('pid')}\n"
                     f"**指令**：{clean_md(str(jd.get('prompt',''))[:100])}\n\n"
                     f"<font color='blue'>在本话题内直接发文字 = 给 {jid} 下达指令</font>")},
        {"tag": "hr"}]
    run = read_run(jd.get("out_file", "")) if jd.get("agent") == "deepseek-harness" else None
    status = task_panel.job_status(jd,run)
    els.insert(1,{"tag":"markdown","content":f"**{status['icon']} {status['label']}** · 可遥控" +
                (f"\n{status['hint']}" if status['hint'] else "")})
    els += btn_row(btn("🔎 状态", "job_detail", {"jid": jid}),
                   btn(BTN_STOP, "pick_job", {"jid": jid, "kind": "stop"}, "danger") if status["can_stop"] else
                   btn(BTN_GO, "pick_job", {"jid": jid, "kind": "go"}) if status["can_go"] else
                   btn(BTN_LIST, "wizard", {"w":"jobs"}))
    if jd.get("agent") != "deepseek-harness":
        els += btn_row(btn(BTN_MODEL, "pick_job", {"jid": jid, "kind": "model"}),
                       btn(BTN_LIST, "wizard", {"w": "jobs"}))
    else:
        els += btn_row(btn(BTN_LIST, "wizard", {"w": "jobs"}))
    els += btn_row(btn("🏷 修改标题", "pick_job", {"jid": jid, "kind": "title"}),
                   btn("➕ 同项目新任务", "new_in_proj", {"cwd": jd.get("cwd", ""),
                                                      "fam": {"wcodebuddy": "wcb", "deepseek-harness": "dsh"}.get(jd.get("agent"), jd.get("agent") or "codex")}))
    _ic, _hc = agent_style(jd.get("agent"))
    title = format_title("topic", conf(), icon=_ic, jid=jid, title=nm,
                         project=os.path.basename(jd.get("cwd", "")), agent=jd.get("agent") or "codex")
    return title, els, _hc


_root_retry = {}

def refresh_root_card(jid):
    jd = jobs_load().get(jid) or {}
    mid = jd.get("root_mid")
    if not mid:
        return False
    title, els, color = root_card_parts(jid, jd)
    card = {"schema": "2.0", "config": CARD_CONFIG,
            "header": {"template": color, "title": {"tag": "plain_text", "content": title}},
            "body": {"elements": els}}
    result = api("PATCH", f"/im/v1/messages/{mid}",
                 {"content": json.dumps(card, ensure_ascii=False)})
    if not result or result.get("code") != 0:
        return False
    with _jl:
        j = jobs_load()
        if jid in j and j[jid].get("title") == jd.get("title"):
            j[jid].pop("root_update_pending", None)
            jobs_save(j)
    return True


def root_topic(chat_id, jid, jd):
    """任务话题根卡: 花名册+入口按钮; 后续该任务播报全部收进此话题"""
    title, els, color = root_card_parts(jid, jd)
    mid, tid = create_topic('job:' + jid, chat_id, title, els, f'🧵 {jid} 任务舱',
                            '本话题收纳该任务的**全部进度与回复**。\n直接在此发文字 = 下达指令。', tag=color)
    if not mid:
        # W2-12: 建舱失败打标, poll周期补建(防一次API抖动让任务永久散落主群)
        with _jl:
            j = jobs_load()
            if jid in j:
                j[jid]["topic_pending"] = True; jobs_save(j)
        return
    cmid_register(mid, jid)
    with _jl:
        j = jobs_load()
        if jid in j:
            j[jid]["root_mid"] = mid
            j[jid]["thread_id"] = tid
            j[jid].pop('topic_pending', None)
            jobs_save(j)
        _THREAD_IDX["d"][mid] = jid
        if tid:
            _THREAD_IDX["d"][tid] = jid
    log(f"root-topic {jid} mid={mid[:18]} tid={tid[:18]}")

def new_round_rc(jid):
    # A delayed reaper from the previous run must never overwrite this round's exit code.
    return f"{STATED}/.rc.{jid}.{uuid.uuid4().hex}"


def reset_run_flags(jd):
    for key in ("first_dead_see", "timeout_killed", "exit", "retry_at"):
        jd.pop(key, None)


def job_artifacts(jd):
    import glob
    paths = [jd.get("out_file", ""), jd.get("rc_file", "")]
    jid = jd.get("jid", "")
    if re.fullmatch(r"[cwqd]?j\d+", jid):
        paths.extend(glob.glob(f"{STATED}/.rc.{jid}.*"))
    return set(p for p in paths if p)


def _reap(p, rcf):
    code = p.wait()
    try:
        with open(rcf + ".tmp", "w") as f:
            f.write(str(code))
        os.replace(rcf + ".tmp", rcf)   # W2-8: 原子落盘, poll不会读到truncate窗口的空串
    except Exception: pass

def _kill_group(pgid, grace=8):
    if IS_WINDOWS:
        stop_tree(pgid)
        return
    for sig, wait_s in ((signal.SIGINT, grace), (signal.SIGTERM, 4), (signal.SIGKILL, 2)):
        try: os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError): return
        end = time.time() + wait_s
        while time.time() < end:
            try: os.kill(pgid, 0); time.sleep(0.4)
            except ProcessLookupError: return
        log(f"killpg {pgid} still alive after {sig}, escalate")

GOAL_SENTINEL = "GOAL_DONE"
def goal_tick(jd):
    """任务退出后被调用: 判定目标是否达成/继续续跑/熔断。返回 True=已处理(需重启任务)"""
    g = jd.get("goal") or {}
    if not g or g.get("paused"):
        return False
    jid = jd["jid"]
    rounds = int(g.get("rounds", 0)) + 1
    final = ""
    try: final = final_text(jd, 3000) or ""
    except Exception: pass
    outf = jd.get("out_file", "")
    size = os.path.getsize(outf) if outf and os.path.exists(outf) else 0
    stale = (size == g.get("prev_size", -1)) and rounds > 1
    g["rounds"] = rounds; g["prev_size"] = size
    lines = [line.strip() for line in final.splitlines() if line.strip()]
    done = str(jd.get("exit")) == "0" and bool(lines) and lines[-1] == GOAL_SENTINEL
    limit = rounds >= int(g.get("max", 8))
    ec = str(jd.get("exit"))
    crash = ec not in ("0", "?", "None")   # rc文件竞态未写时勿误判为崩溃
    if done or limit or stale or crash:
        g["active"] = False
        with _jl:
            j = jobs_load()
            if jid in j: j[jid]["goal"] = g; jobs_save(j)
        if done:
            tsend(jd, f"🎯 目标达成(第{rounds}轮):{clean_md(g['text'])[:80]}\n\n> {clean_md(final[-500:])}",
                  f"✅ {jid} · 目标完成", "green")
        else:
            why = ("⚠️ 连续两轮零产出,熔断(疑似卡死)" if stale else
                   f"⚠️ 达到轮次上限({g.get('max',8)}),暂停等待指令" if limit else
                   f"⚠️ 第{rounds}轮异常退出(rc={ec})")
            tsend(jd, f"🎯 {clean_md(g['text'])[:80]}\n{why}\n\n继续:`#goal {jid} 继续` 或话题内直接回复",
                  f"⏸ {jid} · 目标暂停", "orange")
        return False
    # 续跑下一轮
    sess = jd.get("sess") or find_sess(jid)
    if not sess:
        tsend(jd, f"⚠️ {jid} 无法解析 session,目标环暂停", f"⏸ {jid} · 目标暂停", "orange")
        return False
    rcf = new_round_rc(jid)
    nxt = (f"继续推进目标:{g['text']}\n"
           f"若目标已完全达成,最终回复最后一行必须输出哨兵标记 {GOAL_SENTINEL};否则报告进展与缺口并直接动手补完。"
           f"(目标模式第{rounds+1}轮,上限{g.get('max',8)}轮,禁止以计划陈述结尾)")
    try:
        with open(jd["out_file"], "ab") as of:
            pr = spawn_resume(jd, sess, nxt, of)
    except Exception as e:
        tsend(jd, f"❌ 目标续跑启动失败:{e}", f"⏸ {jid} · 目标暂停", "red")
        return False
    threading.Thread(target=_reap, args=(pr, rcf), daemon=True).start()
    with _jl:
        j = jobs_load()
        jd2 = j.get(jid) or jd
        jd2.update(pid=pr.pid, state="running", sess=sess, started=time.time(),
                   rc_file=rcf, notified=False)
        reset_run_flags(jd2)
        jd2["goal"] = g; jd2.pop("exit", None)
        j[jid] = jd2; jobs_save(j)
    log(f"goal-round {jid} r{rounds+1} pid={pr.pid}")
    return True

def goal_start(chat_id, jid, text, sender, maxr=8):
    with _jl:
        j = jobs_load()
        if jid in j:
            j[jid]["goal"] = {"text": text[:200], "rounds": 0, "max": maxr,
                              "active": True, "prev_size": -1}
            jobs_save(j)
    audit(sender, f"#goal {jid} {text[:60]}", "armed")
    jd = jobs_load().get(jid) or {}
    tsend(jd, f"🎯 目标已装载:{clean_md(text[:120])}\n· 每轮结束自动续跑直到输出 {GOAL_SENTINEL} 或达 {maxr} 轮\n· 熔断:连续两轮零产出自动暂停问你\n· 中断:`#stop {jid}`(会同时解除目标环)",
          f"🎯 {jid} · 目标模式", "wathet")

def cmd_goal(sender, chat_id, args):
    if not args or args[0] == "stop":
        jid = args[1] if len(args) > 1 else ""
        cleared = False
        with _jl:
            j = jobs_load()
            jd = j.get(jid)
            if jd and jd.get("goal"):
                jd["goal"] = {}; jobs_save(j); cleared = True
        if cleared:                   # W6-6: 回执出锁再发(锁内发网络会冻住poll/心跳)
            return reply(chat_id, f"🎯 {jid} 目标环已解除", template="green")
        return reply(chat_id, "用法: `#goal <jid> 目标描述 [轮数=8]` / `#goal stop <jid>`")
    jid = args[0]
    toks = []; maxr = 8
    for tk in args[1:]:
        if tk.startswith("轮数=") or tk.startswith("rounds="):
            maxr = int(re.sub(r"\D", "", tk) or 8)
        else:
            toks.append(tk)
    text = " ".join(toks).strip()
    jd = jobs_load().get(jid)
    if not jd:
        return reply(chat_id, f"❌ 无任务 {jid}", template="red")
    if not text:
        return reply(chat_id, "用法: `#goal <jid> 目标描述`", template="red")
    goal_start(chat_id, jid, text, sender, maxr)
    if jd.get("state") != "running":
        # 空闲任务 -> 立即以目标指令起跑
        Q.put(("goal-kick", sender, chat_id, jid))

def cmd_stop(sender, chat_id, args):
    if not args: return reply(chat_id, "用法: `#stop <jid|all>`")
    with _jl:
        j = jobs_load()
        ids = [k for k, v in j.items() if v.get("state") in ("running", "exited")] if args[0] == "all" else [args[0]]
        done = []
        kills = []
        for jid in ids:
            jd = j.get(jid)
            if not jd: continue
            if jd.get("state") == "exited":
                jd.pop("retry_at", None)
                jd["state"] = "stopped"
                if jd.get("goal"):
                    jd["goal"]["active"] = False
                done.append(f"{jid}: 已停止(自动续跑已取消)")
                continue
            if jd.get("state") != "running":
                done.append(f"{jid}: 已是 {jd.get('state')}"); continue
            jd["state"] = "stopping"          # W2-7: 先落状态, kill在锁外做
            if jd.get("pid"): kills.append(jd["pid"])
            was_goal = (jd.get("goal") or {}).get("active")
            if was_goal:
                jd["goal"] = {}
                done.append(f"{jid}: 中断中(目标环已解除,#goal 可重装)")
            else:
                done.append(f"{jid}: 中断中(SIGINT)")
        jobs_save(j)
    for pid in kills:
        try: _kill_group(pid)                 # Popen start_new_session 后 pid==pgid
        except Exception as e: log(f"stop-kill-err {e}")
    with _jl:
        j = jobs_load()
    audit(sender, "#stop " + " ".join(args), "; ".join(done))
    reply(chat_id, "⏹ " + "\n".join(done) if done else "❌ 未找到任务", template="orange")

def cmd_clean(sender, chat_id, args):
    """清理已结束任务: 删jobs条目+out/rc文件; 保留天数可带参(默认3天)"""
    keep_days = 3
    if args and re.match(r"^\d+$", args[0]):
        keep_days = int(args[0])
    cutoff = time.time() - keep_days * 86400
    removed = []
    with _jl:
        j = jobs_load()
        gone = [k for k, v in j.items()
                if v.get("state") in ("exited", "stopped") and v.get("ended", v.get("started", 0)) < cutoff]
        for k in gone:
            removed.append(j.pop(k))
        jobs_save(j)
    for jd in removed:
        for f in job_artifacts(jd):
            try:
                os.remove(f)
            except OSError:
                pass
    cmid_prune(set(jobs_load().keys()))   # W1-6: 清理任务同时摘其卡片路由
    audit(sender, f"#clean {keep_days}d", f"removed={gone}")
    reply(chat_id, f"🧹 已清理 {len(gone)} 个结束超{keep_days}天的任务: {', '.join(gone) if gone else '(无)'}",
          template="green")

def cmd_forget(sender, chat_id, args):
    """#forget <jid> 强制移除任意任务(运行中会先kill)"""
    if not args: return reply(chat_id, "用法: `#forget <jid>`")
    jid = args[0]
    if jid not in jobs_load():        # W6-6: 存在性预检在锁外, 缺任务的红卡不占 _jl
        return reply(chat_id, f"❌ 无任务 {jid}", template="red")
    with _jl:
        j = jobs_load()
        jd = j.get(jid)
        if not jd:
            return                    # 兜竞态: 宁可不回, 也绝不持锁发网络
        # W6-6: 锁内只置stopping+取pid, kill(最长14s)与摘除挪锁外
        _pid = jd["pid"] if jd.get("state") == "running" and jd.get("pid") else None
        if _pid:
            jd["state"] = "stopping"; jobs_save(j)
    if _pid:
        try: _kill_group(_pid)
        except Exception as e: log(f"forget-kill-err {jid} {e}")
    with _jl:
        j = jobs_load()
        if j.pop(jid, None) is not None:
            jobs_save(j)
    for f in job_artifacts(jd):
        try:
            if f: os.remove(f)
        except Exception:
            pass
    cmid_prune(set(jobs_load().keys()))
    audit(sender, f"#forget {jid}", "removed")
    reply(chat_id, f"🗑 {jid} 已移除(话题历史保留在飞书)", template="green")

def cmd_autowatch(sender, chat_id, args):
    """#autowatch on|off|status — 自动收编外部活跃会话开关(写ctl.conf,热生效)"""
    import subprocess as _sp
    cf = f"{CFGD}/ctl.conf"
    sub = (args[0].lower() if args else "status")
    if sub in ("on", "off"):
        val = "1" if sub == "on" else "0"
        try:
            lines = open(cf, encoding="utf-8").read().splitlines()
            hit = False
            for i, ln in enumerate(lines):
                if ln.strip().startswith("auto_watch="):
                    lines[i] = f"auto_watch={val}"; hit = True
            if not hit:
                lines.append(f"auto_watch={val}")
            tmp = cf + ".tmp"
            open(tmp, "w", encoding="utf-8").write("\n".join(lines) + "\n")
            os.replace(tmp, cf)
        except Exception as e:
            return reply(chat_id, f"❌ 写配置失败: {e}", template="red")
        audit(sender, f"#autowatch {sub}", "conf updated")
        return reply(chat_id, f"🤖 自动收编已{'开启' if sub == 'on' else '关闭'}；门槛以 #autowatch status 为准", template="green")
    cur = conf().get("auto_watch", 1)
    ignored = sum(bool(v.get('ignored')) for v in watch_settings().read().values())
    stats = STATUS.get('auto_watch_scan', {})
    return reply(chat_id, f"🤖 自动收编: {'🟢 开' if str(cur) == '1' else '⚪ 关'}\n"
                 f"2分钟一查 · 近10分钟活跃 · 普通会话 ≥{conf()['auto_watch_min_kb']}KB · Harness 压缩文件 ≥{conf()['dsh_auto_watch_min_kb']}KB\n"
                 f"主动忽略 {ignored} 项 · 最近扫描统计 {stats or '尚无记录'}")

def _model_fail(jid):
    """W1-5: resume失败时回滚switching死态"""
    with _jl:
        j = jobs_load(); jd = j.get(jid)
        if jd and jd.get("state") == "switching":
            jd.update(state="exited", exit="?", ended=time.time())
            jd.pop("model_pending", None); jobs_save(j)

def cmd_model(sender, chat_id, args):
    if len(args) < 2: return reply(chat_id, "用法: `#model <jid> <模型>`  (用 #models 查可用)")
    jid, model = args[0], args[1]
    jd0 = jobs_load().get(jid) or {}
    if not jd0:                       # W6-6: 缺任务在锁外回执, 不占 _jl 发网络
        return reply(chat_id, f"❌ 无任务 {jid}", template="red")
    if jd0.get("agent") == "deepseek-harness":
        return reply(chat_id, "Harness headless 不提供模型参数；请在本机 headless profile 配置模型，任务未被中断", template="orange")
    if jd0.get("agent") == "wcodebuddy":
        if model not in WCB_MODELS:
            return reply(chat_id, "❌ wcb 模型限: auto/hy4-preview/glm-5.3/kimi-k2.7… (#help 查) ", template="red")
        return _model_wcb(sender, chat_id, jid, model, jd0)
    if jd0.get("agent") == "claude":
        if model not in ("sonnet", "opus", "haiku"):
            return reply(chat_id, "❌ claude 任务模型限 sonnet/opus/haiku", template="red")
        return _model_claude(sender, chat_id, jid, model, jd0)
    if jd0.get("agent") == "qoder":
        # Qoder 可用模型要登录后 `qoderclicn --list-models`, 故不做白名单校验
        if not model.strip():
            return reply(chat_id, "❌ 需要模型名(`qoderclicn --list-models` 查可用)", template="red")
        return _model_claude(sender, chat_id, jid, model, jd0)
    with _jl:
        j = jobs_load(); jd = j.get(jid)
        if not jd: return               # 兜竞态(cmd_model已在锁外预检), 绝不持锁发网络
        # W6-6: 锁内只取快照; kill(最长14s)与find_sess(glob+读大文件)一律挪锁外
        _pid = jd["pid"] if jd.get("state") == "running" and jd.get("pid") else None
        sess = jd.get("sess")
    if not sess:
        sess = find_sess(jid)
        if not sess:
            return reply(chat_id, f"❌ {jid} 尚未解析到 session_id(任务太新?), 稍后再试", template="red")
    if _pid:
        try: _kill_group(_pid)
        except Exception as e: log(f"model-kill-err {jid} {e}")
    with _jl:
        j = jobs_load(); jd = j.get(jid) or jd
        jd["state"] = "switching"; jd["model_pending"] = model; jd["sw_ts"] = time.time()
        jobs_save(j)
    audit(sender, f"#model {jid} {model}", f"sess={sess[:8]} resume")
    time.sleep(1.5)
    rcf = new_round_rc(jid)
    outf = f"{STATED}/job-{jid}.out"
    cmd = build_resume_cmd("codex", sess, "继续执行。刚才你被用户中断并切换了模型,请从中断处继续任务。",
                           perm=jd.get("perm", "full"), model=model)
    try:
        with open(outf, "ab") as of:
            p = subprocess.Popen(cmd, cwd=require_cwd(jd["cwd"]), stdout=of, stderr=subprocess.STDOUT,
                                 **process_flags())
    except Exception as e:
        with _jl:
            j = jobs_load(); jd2 = j.get(jid)
            if jd2:
                jd2.update(state="exited", exit="?", ended=time.time())
                jd2.pop("model_pending", None); jobs_save(j)
        return reply(chat_id, f"❌ resume 失败: {e}\n(任务 {jid} 已停止,可 #new 重开)", template="red")
    threading.Thread(target=_reap, args=(p, rcf), daemon=True).start()
    with _jl:
        j = jobs_load(); jd = j.get(jid)
        jd.update(pid=p.pid, state="running", model=model, sess=sess,
                  rc_file=rcf, started=time.time(), notified=False)
        reset_run_flags(jd)
        jd.pop("model_pending", None); jobs_save(j)
    audit(sender, f"#model {jid} {model}", f"resumed pid={p.pid}")
    reply(chat_id, f"🔁 **{jid}** 已切换模型 → `{model}`\nsession 延续({sess[:8]}…),新 PID {p.pid}", template="green")

def _model_claude(sender, chat_id, jid, model, jd):
    """claude/qoder任务换模型: kill后 resume --model 续跑(会话完整保留)"""
    with _jl:
        j = jobs_load(); jd = j.get(jid)
        if not jd: return               # 兜竞态(cmd_model已在锁外预检), 绝不持锁发网络
        # W6-6: 锁内只取pid并落switching标记, kill(最长14s)挪锁外
        _pid = jd["pid"] if jd.get("state") == "running" and jd.get("pid") else None
        jd["state"] = "switching"; jd["sw_ts"] = time.time(); jobs_save(j)
    if _pid:
        try: _kill_group(_pid)
        except Exception as e: log(f"model-kill-err {jid} {e}")
    rcf = new_round_rc(jid)
    outf = jd["out_file"]
    ag = jd.get("agent") or "claude"
    cmd = build_resume_cmd(ag, jd["sess"], "继续执行。刚才用户切换了模型,请从中断处继续任务。",
                           perm=jd.get("perm", "full"), model=model)
    try:
        with open(outf, "ab") as of:
            p_ = subprocess.Popen(cmd, cwd=require_cwd(jd["cwd"]), stdout=of,
                                  stderr=subprocess.STDOUT, **process_flags())
    except Exception as e:
        _model_fail(jid)
        return reply(chat_id, f"❌ resume 失败: {e}\n(任务 {jid} 已停止,可 #new 重开)", template="red")
    threading.Thread(target=_reap, args=(p_, rcf), daemon=True).start()
    with _jl:
        j = jobs_load(); jd = j.get(jid)
        jd.update(pid=p_.pid, state="running", model=model, rc_file=rcf,
                  started=time.time(), notified=False)
        reset_run_flags(jd)
        jobs_save(j)
    audit(sender, f"#model {jid} {model}", f"{ag} resumed pid={p_.pid}")
    reply(chat_id, f"🔁 **{jid}** {AGENT_STYLE.get(ag, ('🤖',))[0]} 模型 → `{model}`\n"
                   f"session {jd['sess'][:8]}… 延续", template="green")

def _model_wcb(sender, chat_id, jid, model, jd):
    """wcb任务换模型: kill后 -r resume --model 续跑"""
    with _jl:
        j = jobs_load(); jd = j.get(jid)
        if not jd: return               # 兜竞态(cmd_model已在锁外预检), 绝不持锁发网络
        # W6-6: 锁内只取pid并落switching标记, kill(最长14s)挪锁外
        _pid = jd["pid"] if jd.get("state") == "running" and jd.get("pid") else None
        jd["state"] = "switching"; jd["sw_ts"] = time.time(); jobs_save(j)
    if _pid:
        try: _kill_group(_pid)
        except Exception as e: log(f"model-kill-err {jid} {e}")
    rcf = new_round_rc(jid)
    outf = jd["out_file"]
    cmd = build_resume_cmd("wcodebuddy", jd["sess"], "继续执行。刚才用户切换了模型,请从中断处继续任务。",
                           perm=jd.get("perm", "full"), model=model)
    try:
        with open(outf, "ab") as of:
            p_ = subprocess.Popen(cmd, cwd=require_cwd(jd["cwd"]), stdout=of,
                                  stderr=subprocess.STDOUT, **process_flags())
    except Exception as e:
        _model_fail(jid)
        return reply(chat_id, f"❌ resume 失败: {e}\n(任务 {jid} 已停止,可 #new 重开)", template="red")
    threading.Thread(target=_reap, args=(p_, rcf), daemon=True).start()
    with _jl:
        j = jobs_load(); jd = j.get(jid)
        jd.update(pid=p_.pid, state="running", model=model, rc_file=rcf,
                  started=time.time(), notified=False)
        reset_run_flags(jd)
        jobs_save(j)
    audit(sender, f"#model {jid} {model}", f"wcb resumed pid={p_.pid}")
    reply(chat_id, f"🔁 **{jid}** WorkBuddy 模型 → `{model}`\nsession {jd['sess'][:8]}… 延续", template="green")

def find_sess(jid):
    """从 --json 输出文件找 session_id(新任务首行或 rollout 引用)"""
    jd = jobs_load().get(jid) or {}
    outf = jd.get("out_file", "")
    if not outf or not os.path.exists(outf): return ""
    try:
        txt = open(outf, encoding="utf-8", errors="replace").read(4000000)
        if jd.get("agent") == "deepseek-harness":
            for line in txt.splitlines():
                try:
                    event = json.loads(line)
                    if event.get("type") == "session" and dsh.valid_session(event.get("sessionId")):
                        return event["sessionId"]
                except (ValueError, AttributeError):
                    pass
            return ""
        m = re.search(r'"(?:session_id|thread_id)"\s*:\s*"([0-9a-f-]{20,})"', txt)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""

def claude_meta(jd):
    """claude/qoder/wcodebuddy任务: 从 out 文件的 result JSON 提取 cost/turns/duration"""
    try:
        if jd.get("agent") == "wcodebuddy":
            d = json.load(open(jd.get("out_file", ""), encoding="utf-8", errors="replace"))
            rs = [x for x in d if isinstance(x, dict) and x.get("type") == "result"] if isinstance(d, list) else []
            o = rs[-1] if rs else {}
            if not o:
                return ""
        else:
            txt = ""
            for ln in open(jd.get("out_file", ""), encoding="utf-8", errors="replace"):
                if '"type":"result"' in ln.replace(" ", "") or '"result"' in ln:
                    txt = ln
            if not txt:
                return ""
            o = json.loads(txt)
        cost = o.get("total_cost_usd") or 0
        parts = []
        if cost:
            parts.append(f"💰 ${float(cost):.3f}")
        if o.get("num_turns"):
            parts.append(f"{o['num_turns']} turns")
        d = (o.get("duration_ms") or 0) / 1000
        if d:
            parts.append(f"{int(d // 60)}分{int(d % 60)}秒")
        if o.get("is_error"):
            parts.append("⚠️ is_error")
        return " · ".join(parts)
    except Exception:
        return ""

def notify_done(jd):
    jid = jd["jid"]
    ok = str(jd.get("exit")) == "0"
    preferences = watch_settings().read()
    session = str(jd.get('sess') or '').removeprefix('session-')
    policy = {**preferences.get(session, {}), **preferences.get('job:' + jd['jid'], {})}
    if policy.get('paused') or policy.get('mode') == 'errors' and ok:
        return
    dur = int(jd.get("ended", 0) - jd.get("started", 0))
    nm = jd.get("title") or str(jd.get("prompt", ""))[:30]
    ft = final_text(jd) if ok else ""
    err = ""
    if not ok:
        try:
            txt = open(jd.get("out_file", ""), encoding="utf-8", errors="replace").read()[-200000:]
            em = re.findall(r'"(?:error|message)"\s*:\s*"([^"]{5,200})"', txt)
            bad = [x for x in em if any(k in x.lower() for k in
                   ("error", "failed", "429", "500", "503", "exceeded", "timeout", "denied"))]
            err = bad[-1] if bad else (em[-1] if em else "")
        except Exception:
            pass
    body = f"· ⏱ 用时 {dur//60}分{dur%60}秒 · 退出码 {jd.get('exit')}"
    if jd.get("timeout_killed"):
        body += " · ⏰ 超时被中断(超 max_run_min)"
    if jd.get("agent") in ("claude", "wcodebuddy", "qoder"):
        cm = claude_meta(jd)
        if cm:
            body += f"\n· {cm}"
    if err:
        body += f"\n\n**❌ 报错:** {clean_md(err)[:300]}"
    elif ft:
        body += f"\n\n**最终回复:**\n{clean_md(ft)}"
    elif not ok:
        body += "\n\n_(无错误详情,见 out 文件)_"
    else:
        body += "\n\n_(无正文输出)_"
    _e = (err or "").lower()
    hint = ""
    if ok:
        btns = btn_row(btn(BTN_GO, "pick_job", {"jid": jid, "kind": "go"}),
                       btn(BTN_LIST, "wizard", {"w": "jobs"}))
    elif "429" in _e or "too many" in _e or "overload" in _e:
        btns = btn_row(btn(BTN_RETRY, "pick_job", {"jid": jid, "kind": "go"}, "primary"),
                       btn("⏭ 先看别的", "wizard", {"w": "jobs"}))
        hint = "\n\n<font color='grey'>429=网关限流,稍等再点重试更稳(系统也会自动重试)</font>"
    elif any(k in _e for k in ("denied", "permission", "not authorized", "invalid api key", "quota")):
        btns = btn_row(btn(BTN_MODEL, "pick_job", {"jid": jid, "kind": "model"}),
                       btn(BTN_FORGET, "job_forget", {"jid": jid}, "danger"))
        hint = "\n\n<font color='grey'>鉴权/权限/配额类错误,重试无效——换模型或修Key</font>"
    elif jd.get("timeout_killed"):
        btns = btn_row(btn(BTN_GO, "pick_job", {"jid": jid, "kind": "go"}, "primary"),
                       btn(BTN_LIST, "wizard", {"w": "jobs"}))
        hint = "\n\n<font color='grey'>这轮太大被超时保护掐了;继续会接着干,或拆成两个小任务更稳</font>"
    else:
        btns = btn_row(btn(BTN_GO, "pick_job", {"jid": jid, "kind": "go"}),
                       btn(BTN_FORGET, "job_forget", {"jid": jid}, "danger"))
    if not ok and jd.get("agent") == "deepseek-harness":
        issue = read_run(jd.get("out_file", ""))["diagnostic"] or diagnostic()
        hint = f"\n\n**{issue['label']}** · {issue['action']}"
        btns = btn_row(btn(BTN_GO if jd.get("sess") else BTN_LIST,
                           "pick_job" if jd.get("sess") else "wizard",
                           {"jid":jid,"kind":"go"} if jd.get("sess") else {"w":"jobs"}),
                       btn(BTN_FORGET, "job_forget", {"jid":jid}, "danger"))
    body += hint        # W6-1: 提示必须先并入 body, els 定型后再 += 就没人能看见
    els = [{"tag": "markdown", "content": body}] + btns
    r = None
    root = jd.get("root_mid", "")
    if root:
        card={"schema":"2.0","config":CARD_CONFIG,
              "header":{"template":"green" if ok else "red","title":{"tag":"plain_text",
                        "content":f"{'✅' if ok else '⚠️'} {jid} 已结束 · {nm}"}},
              "body":{"elements":els}}
        r = api("POST", f"/im/v1/messages/{root}/reply",
                {"content": json.dumps(card, ensure_ascii=False),
                 "msg_type": "interactive", "reply_in_thread": True})
    if r and r.get("code") == 0:
        cmid_register((((r.get("data") or {}).get("message_id")) or ""), jid)
    else:
        mid = reply_card(jd["chat_id"], f"{'✅' if ok else '⚠️'} {jid} 已结束 · {nm}",
                         "green" if ok else "red", els)
        cmid_register(mid if isinstance(mid, str) else "", jid)

def cmd_jobs(sender, chat_id, args):
    if not args:
        return cmd_board(sender, chat_id, [])
    j = jobs_load()
    if args and args[0] != "all":
        jd = j.get(args[0])
        if not jd:
            return reply(chat_id, f"❌ 无任务 {args[0]}", template="red")
        st = {"running": "🟢 运行中", "stopping": "🟠 中断中",
              "switching": "🔁 切换中"}.get(jd.get("state"), "⚪ 已结束")
        dur = int((jd.get("ended") or time.time()) - jd.get("started", 0))
        ft = final_text(jd, 1600)
        els = [{"tag": "markdown", "content":
                f"**{jd.get('jid')}** {st} · {jd.get('title') or '(自动命名)'}\n"
                f"· 📂 `{os.path.basename(jd.get('cwd',''))}` · 模型 {jd.get('model','?')}\n"
                f"· ⏱ 已运行/共 {dur//60}分{dur%60}秒"
                + (f" · 退出码 {jd.get('exit')}" if jd.get('state')=='exited' else "")}]
        if jd.get("prompt"):
            els.append({"tag": "markdown", "content": f"**指令**：{clean_md(str(jd['prompt'])[:200])}"})
        if ft:
            els.append({"tag": "hr"})
            if len(ft) > 320:   # W3: 首屏直出320字, 全文折叠, 按钮不留三屏外
                els.append({"tag": "markdown", "content": f"**最新回复**：\n{clean_md(ft[:320])}…"})
                els.append(panel("📄 展开完整回复", clean_md(ft[320:])))
            else:
                els.append({"tag": "markdown", "content": f"**最新回复**：\n{clean_md(ft)}"})
        elif jd.get("state") == "running":
            els.append({"tag": "markdown", "content": "_任务在跑,尚无完整回复段落_"})
        bs = []
        if jd.get("state") == "running":
            bs.append(btn(BTN_STOP, "pick_job", {"jid": jd["jid"], "kind": "stop"}, "danger"))
        if jd.get("sess") or jd.get("state") == "exited":
            bs.append(btn(BTN_GO, "pick_job", {"jid": jd["jid"], "kind": "go"}))
        if bs:
            els += btn_row(*bs)
        return reply_card(chat_id, f"🔎 {jd['jid']} · {jd.get('title') or os.path.basename(jd.get('cwd',''))}",
                          "blue", els)
    if not j: return reply(chat_id, "(暂无任务)")
    lines = []
    for jd in sorted(j.values(), key=lambda x: x.get("started", 0), reverse=True)[:10]:
        st = jd.get("state")
        icon = {"running": "🟢", "stopping": "🟠", "switching": "🔁"}.get(st, "⚪")
        tail = ""
        if (jd.get("goal") or {}).get("active"):
            tail += " 🎯"
        elif (jd.get("goal") or {}).get("rounds") and str(jd.get("exit")) != "0":
            tail += " ⏸"
        if jd.get("retry_cnt") and jd.get("state") == "exited":
            tail += f" 🔄{jd['retry_cnt']}"
        dur = ""
        if jd.get("started"):
            m = int((time.time() - jd["started"]) // 60)
            dur = f"{m//60}h{m%60:02d}m" if m >= 60 else f"{m}m"
        nm = jd.get("title") or str(jd.get("prompt",""))[:40]
        lines.append(f"{icon} **{jd['jid']}** {nm} · `{jd.get('model','?')}` · {os.path.basename(jd.get('cwd',''))} · {dur}{tail}")
    run = [(k, v) for k, v in sorted(j.items(), key=lambda x: -x[1].get("started", 0))
           if v.get("state") == "running"]
    els = [{"tag": "markdown", "content": "\n".join(lines)}]
    if run:
        bs = [btn(f"🔎 {k}", "job_detail", {"jid": k}) for k, v in run[:4]]
        bs.append(btn("▶️ 指令…", "wizard", {"w": "go"}))
        bs.append(btn("⏹ 中断(选任务)", "pick_job_stop_list", {"x": 1}, "danger"))
        els += btn_row(*bs)
    reply_card(chat_id, f"🎛 任务 ({len(run)} 运行中)", "turquoise", els)

def final_text(jd, limit=520):
    """从 job 输出文件取最后回复; claude任务=--output-format json 的 result 字段"""
    f = jd.get("out_file", "")
    txt = ""
    if jd.get("agent") == "deepseek-harness":
        try:
            with open(f, encoding="utf-8") as lines:
                for line in lines:
                    try:
                        event = json.loads(line)
                        if event.get("type") in ("run_start", "session"): txt = ""
                        if event.get("type") == "final": txt = str(event.get("text", ""))
                    except (ValueError, AttributeError): pass
        except OSError: return ""
        return txt if len(txt) <= limit else txt[:limit] + "…"
    if jd.get("agent") == "wcodebuddy":
        try:
            d = json.load(open(f, encoding="utf-8", errors="replace"))
            rs = [x for x in d if isinstance(x, dict) and x.get("type") == "result"] if isinstance(d, list) else []
            txt = str(rs[-1].get("result") or "") if rs else ""
        except Exception:
            try:
                import re as _r2
                mm = _r2.findall(r'"result"\s*:\s*"((?:[^"\\]|\\\\.)*)"', open(f, encoding="utf-8", errors="replace").read())
                txt = mm[-1].replace('\\\\n', '\\n') if mm else ""
            except Exception:
                return ""
        return txt if len(txt) <= limit else txt[:limit] + "…"
    if jd.get("agent") in ("claude", "qoder"):
        try:
            for ln in open(f, encoding="utf-8", errors="replace"):
                ln = ln.strip()
                if ln.startswith("{") and '"result"' in ln:
                    try:
                        o = json.loads(ln)
                        if o.get("result"):
                            txt = str(o["result"])
                    except Exception:
                        pass
        except Exception:
            return ""
        return txt if len(txt) <= limit else txt[:limit] + "…"
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln or ln[0] != "{":
                    continue
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                if o.get("type") == "item.completed":
                    it = o.get("item") or {}
                    if it.get("type") == "agent_message" and it.get("text"):
                        txt = it["text"]
    except Exception:
        return ""
    if not txt:
        return ""
    return txt if len(txt) <= limit else txt[:limit] + "…"

def cmd_models(sender, chat_id, args):
    """模型清单只来自 Codex 自身配置: config.toml 默认模型 + model_catalog_json。绝不调用外部API。"""
    lines = []
    try:
        txt = open(f"{HOME}/.codex/config.toml", encoding="utf-8").read()
        m = re.search(r'^model\s*=\s*"([^"]+)"', txt, re.M)
        default = m.group(1) if m else "?"
        lines.append(f"当前默认: **{default}**")
        cat = ""
        mc = re.search(r'^model_catalog_json\s*=\s*"([^"]+)"', txt, re.M)
        if mc:
            cat = os.path.join(HOME, ".codex", mc.group(1))
        if cat and os.path.exists(cat):
            ms = json.load(open(cat, encoding="utf-8")).get("models", [])
            ids = [x.get("slug") or x.get("id") or str(x) for x in ms]
            lines.append("\n可用清单(config model_catalog):")
            lines += [f"· `{'✅ ' if i == default else ''}{i}`" for i in ids]
        else:
            lines.append("(未找到 model_catalog_json, 仅显示默认)")
    except Exception as e:
        lines.append(f"(读 codex 配置失败: {e})")
    wl = [x.strip() for x in conf()["models"].split(",") if x.strip()]
    if wl:
        lines.append("\nctl 补充白名单(ctl.conf):")
        lines += [f"· `{x}`" for x in wl]
    reply(chat_id, "\n".join(lines), title="🧭 codex 模型")

HELP_ELS = None
def cmd_help(sender, chat_id, args):
    if args and args[0] in ("all", "more", "全"):
        return cmd_help_all(sender, chat_id, args)
    t = ("| 命令 | 做什么 |\n| --- | --- |\n"
         "| `#dnew <项目> 指令` / `#dresume <会话前8位> 指令` | 🐋 新建 / 显式接管 |\n"
         "| `#new [项目] [模型] 标题=名 提示词` | 🚀 启动(可省项目;默认🔓full) |\n"
         "| `#go <任务号> 指令` | ▶️ 下达下一步 |\n"
         "| `#sessions` / `#status` / `#leave` | 🖥 可接管任务 / 当前控制 / 退出 |\n"
         "| `#attach <标题或别名>` | 🖥 进入原 Codex / DSH 会话 |\n"
         "| `#jobs [任务号]` / `#board` | 📋 统一面板 · 单任务详情 |\n"
         "| `#retention` | 清理预览；确认后才清理 |\n"
         "| `#observe <会话ID> pause/resume/title/mode` | 观察备注与播报设置 |\n"
         "| `#broadcast <任务号> full/result/errors` · `#queue` | 播报模式 / 命令队列 |\n"
         "| `#stop <任务号 或 all>` | ⏹ 中断 |\n"
         "| `#model <任务号> <模型>` | 🔁 换模型续跑 |\n"
         "| `#goal <任务号> 目标` | 🎯 目标模式(自动续跑) |\n"
         "| `#scan` / `#watch <会话前8位>` | 含 🐋 DeepSeek Harness，只读纳管 |\n"
         "| `#health` / `#deliveries` | 连接、磁盘、投递诊断 |\n"
         "| `#retry-delivery <完整ID>` | 将失败消息排队补发到原话题 |")
    els = ([{"tag": "markdown", "content": "**👇 点按钮,或直接发左列命令**"},
            {"tag": "markdown", "content": t},
            {"tag": "hr"},
            {"tag": "markdown", "content": ("**💬 引用玩法**\n"
             "<font color='blue'>引用任务卡/完成卡直接回复</font> = 给该任务下指令\n"
             "<font color='grey'>话题舱内发文字同样直达 · 🖥桌面问题用 #questions 回答</font>")},
            {"tag": "hr"}]
           + btn_row(btn("🚀 新建任务", "wizard", {"w": "new"}, "primary"),
                     btn("▶️ 下达指令", "wizard", {"w": "go"}))
           + btn_row(btn("📥 纳管扫描", "scan_now", {"x": 1}),
                     btn("⏹ 中断(向导)", "wizard", {"w": "stop"}, "danger"))
           + [{"tag": "markdown",
               "content": "_例:`#new sample-app 标题=登录修复 修下登录跳转` · 完整命令:`#help all`_",
               "text_size": "notation"}])
    reply_card(chat_id, "🎛 飞书控制台", "turquoise", els)

def cmd_help_all(sender, chat_id, args):
    t1 = ("| 启动任务 | 说明 |\n| --- | --- |\n"
          "| `#new [项目] [模型] 标题=名 提示词` | 🚀 Codex |\n"
          "| `#cnew [项目] [模型] 标题=名 提示词` | 🤖 Claude(sonnet/opus/haiku) |\n"
          "| `#wnew [项目] [模型] 标题=名 提示词` | 🦞 WorkBuddy(glm/kimi/deepseek/hy3) |\n"
          "| `#dnew <项目> 指令` / `#dresume <会话前8位> 指令` | 🐋 新建 / 显式接管 |\n"
          "| `#qnew [项目] 标题=名 提示词` | 🧩 Qoder CLI(模型用 `#model` 换) |\n"
          "| 令牌 | `模式=plan` 只读规划 · `模式=goal` 装目标环 · `权限=safe` 降级沙箱 |\n")
    t2 = ("| 遥控 | 说明 |\n| --- | --- |\n"
          "| `#sessions` · `#status` · `#leave` | 🖥 可接管任务 / 当前控制 / 退出 |\n"
          "| `#attach <标题或别名>` | 🖥 进入原 Codex / DSH 会话 |\n"
          "| `#go <jid> 指令` | ▶️ 续跑(引用任务卡=免打命令) |\n"
          "| `#goal <jid> 目标 [轮数=8]` | 🎯 自动续跑至 GOAL_DONE/熔断 |\n"
          "| `#model <jid> <模型>` | 🔁 换模型延续上下文 |\n"
          "| `#stop <jid 或 all>` · `#title <jid> 名` | ⏹ 中断(解除目标环) · 🏷 改名 |\n"
          "| `#jobs [jid]` | 📋 列表 / 详情+最新回复 |\n"
          "| `#cancel` | 🚫 放弃进行中的向导(不发出任何指令) |\n")
    t3 = ("| 纳管 | 说明 |\n| --- | --- |\n"
          "| `#scan` | 📥 列近2h活跃未纳管会话,一键入舱 |\n"
          "| `#watch <sess8>` · `#unwatch <sess8 或 all>` | 🖥 纳管/解除桌面会话 |\n"
          "| `#projects` · `#models` | 📂 项目表 · 模型表 |\n"
          "| `#clean [天]` · `#forget <jid>` | 🧹 清理结束任务 / 🗑 强制移除 |\n")
    # W3: 启动段直出, 遥控/纳管折叠(440px下三表全展要滚3屏)
    els = [{"tag": "markdown", "content": "**🧰 启动**\n" + t1},
           {"tag": "hr"},
           panel("🎮 遥控命令", t2),
           panel("📡 纳管与清理", t3),
           {"tag": "hr"}]
    els += btn_row(btn("🚀 新建任务", "wizard", {"w": "new"}, "primary"),
                   btn("📥 纳管扫描", "scan_now", {"x": 1}))
    els.append({"tag": "markdown",
                "content": "_任务号 jN=🚀 cjN=🤖 wjN=🦞 qjN=🧩 djN=🐋;舱内发文字直达该任务_",
                "text_size": "notation"})
    reply_card(chat_id, "🎛 控制台 · 全部命令", "turquoise", els)

def cmd_board(sender, chat_id, args):
    kind = args[0] if args and args[0] in ("all","control","watch","errors") else "all"
    try: page = int(args[1]) if len(args)>1 else 1
    except ValueError: page=1
    jobs=jobs_load()
    statuses={jid:task_panel.job_status(job,read_run(job.get("out_file", ""))
              if job.get("agent")=="deepseek-harness" else None) for jid,job in jobs.items()}
    try: active=_scan_active()
    except (OSError,ValueError): active=[]
    entries,page,pages,total=task_panel.rows(jobs,_st_load(),active,statuses,kind,page, preferences=watch_settings().read())
    health = "🟢 已连接" if STATUS.get("ws_connected") else "🟠 未连接/尚未确认"
    els=[{"tag":"markdown","content":f"飞书连接：{health} · 连接状态不等同于任务状态\n**可遥控**与**只读观察**分开展示；观察不会自动变成控制。"}]
    els+=btn_row(*[btn(label,"board",{"filter":key,"page":1},"primary" if kind==key else "default")
                   for key,label in (("all","全部"),("control","可遥控"),("watch","只读观察"),("errors","异常"))])
    if not entries:els.append({"tag":"markdown","content":"暂无符合条件的任务。可新建任务，或扫描并观察已有会话。"})
    for entry in entries:
        status=entry['status'];ident=entry['id'];is_job=entry['kind']=='control'
        els.append({"tag":"hr"})
        els.append({"tag":"markdown","content":f"**{status['icon']} {clean_md(entry['title'][:48])}**\n`{ident}` · {'可遥控' if is_job else '只读观察'} · {status['label']}"+ (' · 🎯目标模式' if entry.get('goal') else '')+
                    ("\n"+status['hint'] if status['hint'] else "")})
        if is_job:
            buttons=[btn("查看详情","job_detail",{"jid":ident})]
            if status['can_stop']:buttons.append(btn(BTN_STOP,"pick_job",{"jid":ident,"kind":"stop"},"danger"))
            elif status['can_go']:buttons.append(btn(BTN_GO,"pick_job",{"jid":ident,"kind":"go"}))
            else:buttons.append(btn("同项目新建","new_in_proj",{"cwd":jobs[ident].get('cwd',''),
                            "fam":{"wcodebuddy":"wcb","deepseek-harness":"dsh"}.get(entry['agent'],entry['agent'])}))
        else:buttons=[btn("观察详情","watch_detail",{"sess8":ident}),btn("解除观察","unwatch_sess",{"sess8":ident})]
        els+=btn_row(*buttons)
    els.append({"tag":"hr"})
    navigation=[btn("刷新","board",{"filter":kind,"page":page})]
    if page>1:navigation.append(btn("上一页","board",{"filter":kind,"page":page-1}))
    if page<pages:navigation.append(btn("下一页","board",{"filter":kind,"page":page+1}))
    els+=btn_row(*navigation)
    els+=btn_row(btn("新建任务","wizard",{"w":"new"}),btn("扫描会话","scan_now",{}))
    els.append({"tag":"markdown","content":f"第 {page}/{pages} 页 · 共 {total} 项 · 手动刷新获取最新状态","text_size":"notation"})
    reply_card(chat_id,"📋 统一任务面板","turquoise",els)


def cmd_watch_detail(sender, chat_id, args):
    sid=args[0] if args else ''
    try:
        sid = resolve_observation(sid)
    except ValueError as error:
        return reply(chat_id, str(error))
    path,_=find_rollout(sid)
    value = watch_settings().read().get(sid, {})
    title=value.get('title') or (_rollout_task(path) if path else '历史观察会话')
    _, elements, _ = watch_card_parts(sid, {**value, 'title': title}, detail=False)
    elements += btn_row(btn('完整播报', 'observe_setting', {'sid': sid, 'op': 'full'}),
                        btn('仅结果', 'observe_setting', {'sid': sid, 'op': 'result'}),
                        btn('异常提醒', 'observe_setting', {'sid': sid, 'op': 'errors'}))
    elements.append({'tag': 'markdown', 'content': f'备注标题：`#observe {sid[:8]} title 新标题`\n仅结果模式按来源结束信号结算；无结束信号时只显示标注为未确认结束的静默摘要。'})
    if path and harness.is_harness(path):
        elements.append({"tag":"markdown","content":f"需要写控制时，先关闭桌面端该会话，再显式发送：\n`#dresume {sid} 指令`\n系统保留跨进程写锁，不会强抢会话。"})
    elements+=btn_row(btn("返回任务面板","board",{"filter":"watch","page":1}))
    reply_card(chat_id,"👁 观察详情","grey",elements)


_retention_previews = {}

def cmd_retention(sender, chat_id, args):
    now=time.time()
    for key,item in list(_retention_previews.items()):
        if now-item['plan']['created_at']>600:_retention_previews.pop(key,None)
    key=(sender,chat_id)
    box=Outbox(f"{STATED}/delivery.sqlite")
    if args and args[0]=='apply':
        item=_retention_previews.get(key)
        if len(args)!=2 or not item or args[1]!=item['token']:
            return reply(chat_id,"请先 #retention 预览；确认码仅对当前用户和会话有效",template="orange")
        try:
            with _jl:
                jobs=jobs_load()
                result=retention.apply(item['plan'],STATED,jobs,box,conf(),now)
                for jid in result['jobs']:
                    jobs[jid]['output_expired_at']=now
                    jobs[jid].pop('prompt',None)
                jobs_save(jobs)
        except (OSError,ValueError):
            _retention_previews.pop(key,None)
            return reply(chat_id,"候选数据已变化、预览过期或文件不可访问；请重新预览，不会按旧列表强制清理",template="orange")
        _retention_previews.pop(key,None)
        audit(sender,'#retention','applied')
        return reply(chat_id,f"已清理 {result['files_removed']} 个任务文件（{result['bytes_removed']} 字节），过期失败正文 {result['failed_payloads_expired']} 条，跳过 {result['skipped']} 项。会话、任务身份、去重记录与待投递消息均保留。")
    if args:return reply(chat_id,"用法：#retention 预览；#retention apply <确认码> 执行")
    plan=retention.preview(STATED,jobs_load(),box,conf(),now)
    token=uuid.uuid4().hex[:12]
    _retention_previews[key]={'plan':plan,'token':token}
    if len(_retention_previews)>100:_retention_previews.pop(next(iter(_retention_previews)))
    size=sum(entry['size'] for entry in plan['files'])
    reply(chat_id,f"**清理预览（尚未删除）**\n已结束任务输出：保留 {plan['policy']['job_days']} 天；候选 {len(plan['files'])} 个文件，{size} 字节。\n失败投递正文：保留 {plan['policy']['failed_days']} 天；候选 {len(plan['failures'])} 条。\n不动运行/待重试任务、源会话、凭据、待投递消息和去重身份。\n确认执行：`#retention apply {token}`（10 分钟有效；数据变化需重预览；每批最多250项）。")


_status_cards={}

def refresh_status_cards():
    now=time.time()
    jobs=jobs_load()
    try: gap=max(10,int(conf().get('status_card_gap',30)))
    except (ValueError,TypeError): gap=30
    for jid,job in jobs.items():
        if not job.get('root_mid') or job.get('archived'):continue
        if job.get('state') not in ('running','stopping','switching') and now-job.get('ended',0)>600:continue
        run=read_run(job.get('out_file','')) if job.get('agent')=='deepseek-harness' else None
        signature=task_panel.status_signature(job,run)
        last=_status_cards.get(jid,{'at':0,'signature':''})
        if signature==last['signature'] or now-last['at']<gap:continue
        _status_cards[jid]={'at':now,'signature':last['signature']}
        if refresh_root_card(jid):_status_cards[jid]['signature']=signature
    for jid in set(_status_cards)-set(jobs):_status_cards.pop(jid,None)


def cmd_health(sender, chat_id, args):
    health_tick(STATED, LOGD)
    try:
        status = json.loads(Path(f"{STATED}/health.json").read_text())
    except (OSError, ValueError):
        return reply(chat_id, "健康快照不可读，请检查磁盘/权限；不能据此判定健康")
    delivery = Outbox(f"{STATED}/delivery.sqlite").summary()
    counts = delivery['counts']
    stats = status.get('auto_watch_scan', {})
    now = time.time()
    progress = '\n'.join(f"{label}: {'运行' if status.get(name + '_alive') else '未确认'} · "
                         + (f"进度 {int(max(0, now-status[name + '_progress_at']))}秒前" if status.get(name + '_progress_at') else '暂无进度')
                         for name, label in (('ctl', '控制循环'), ('sync', '来源扫描'), ('sender', '投递线程')))
    status['delivery_recent_failed'] = delivery['recent_failed']
    issues = health_issues(status)
    reply(chat_id, f"WebSocket: {'已连接' if status.get('ws_connected') else '未连接/尚未确认'}\n"
          f"磁盘可用: {status.get('disk_free_bytes', 0)/1024**3:.1f} GiB\n"
          f"{progress}\n来源: 候选 {status.get('sync_candidates', 0)} / 活跃 {status.get('sync_active_sources', 0)}\n"
          f"命令: 等待 {status.get('command_queue_depth', 0)} / 最早等待 {status.get('command_oldest_wait_seconds', 0)}秒 / "
          f"处理中 {int(max(0, now-status['command_worker_busy_since'])) if status.get('command_worker_busy_since') else 0}秒\n"
          f"投递: {counts} · 历史失败 {counts.get('failed', 0)} / 最近5分钟失败 {delivery['recent_failed']}\n"
          f"未暂停来源最早积压: {int(status.get('delivery_active_oldest_seconds', 0))}秒\n"
          f"纳管统计: {stats or '尚未扫描'}\n当前故障: {'；'.join(issues) if issues else '无'}")


def cmd_deliveries(sender, chat_id, args):
    box = Outbox(f"{STATED}/delivery.sqlite")
    summary = box.summary()
    lines = [f"📨 投递状态: {summary['counts']}"]
    for row in summary['failed']:
        error = json.loads(row['error'] or '{}')
        lines.append(f"`{row['id']}` · 尝试 {row['attempts']} 次 · HTTP {error.get('http_status', '-')} · code {error.get('code', '-')}" )
    lines.append("补发: `#retry-delivery <完整ID>`（仅失败消息，仍发到原话题）")
    reply(chat_id, "\n".join(lines))


def cmd_retry_delivery(sender, chat_id, args):
    if len(args) != 1 or not re.fullmatch(r"[0-9a-f]{32}", args[0]):
        return reply(chat_id, "用法: `#retry-delivery <完整ID>`")
    ok = Outbox(f"{STATED}/delivery.sqlite").retry(args[0])
    reply(chat_id, "已排队补发（不等于已送达）" if ok else "未找到可补发消息；已发送或已清理正文的消息不会重发")


# Only advertised request identities live in memory; never retain answer text.
_NATIVE_ADVERTISED = {}


def native_context(chat_id, sid):
    from tower_native_input import NativeInputError
    path, full = find_rollout(sid)
    watched = _st_load()
    if not full or full not in watched:
        raise NativeInputError('请先纳管一个明确的会话。')
    build = JsonMap(Path(STOPICF).with_name('topic-builds.json')).read().get('watch:' + full, {})
    if not build:
        # Legacy watches have only a root ID. Verify its chat from Feishu rather
        # than treating the caller's chat as authority or recreating the topic.
        response = api('GET', f"/im/v1/messages/{watched[full]}") or {}
        items = (response.get('data') or {}).get('items') or []
        message = next((m for m in items if m.get('message_id') == watched[full]
                        and not m.get('deleted')), {}) if response.get('code') == 0 else {}
        build = {'chat_id': message.get('chat_id'), 'root_mid': message.get('message_id')}
    if build.get('root_mid') != watched[full]:
        raise NativeInputError('无法核验纳管根消息，请检查会话登记。')
    if build.get('chat_id') != chat_id or not build.get('root_mid'):
        raise NativeInputError('请在该会话已纳管的原聊天中回答。')
    route = getattr(_REPLY_CONTEXT, 'route', {}) or {}
    root = route.get('root_id')
    if root and root != build['root_mid']:
        raise NativeInputError('当前话题与目标会话不一致。')
    _REPLY_CONTEXT.route = {'chat_id': chat_id, 'message_id': build['root_mid'], 'root_id': build['root_mid']}
    engine = source_agent(path)
    native_sid = harness.header(path).get('id', '') if engine == 'deepseek-harness' else full
    if not native_sid:
        raise NativeInputError('无法识别原生会话。')
    return full, engine, native_sid


def native_reply(sender, chat_id, sid, text=None, token=None):
    from tower_native_input import NativeInput, NativeInputError
    try:
        full, engine, native_sid = native_context(chat_id, sid)
        key = (sender, chat_id, full)
        with NativeInput(engine, native_sid) as native:
            if text is not None:
                token = token or _NATIVE_ADVERTISED.get(key)
                if token:
                    result = native.answer(token, text)
                    status = result['status']
                    messages = {'answered': '✅ 已确认答案送回原会话。',
                                'queued': '答案已加入原会话队列，尚未确认 Agent 已读取。',
                                'expired': '问题已失效或已在原会话回答，请重新 #questions 查看。',
                                'unconfirmed': '已提交，但未取得原会话确认。请先在原会话检查，避免重复回答。',
                                'unavailable': '原生回答通道当前不可用，请在原会话回答。',
                                'unsupported': '此类问题暂不支持原生回填，请在原会话回答。'}
                    if status in ('answered', 'queued', 'expired', 'unconfirmed'):
                        _NATIVE_ADVERTISED.pop(key, None)
                    return reply(chat_id, messages.get(status, '回答状态未知，请在原会话检查。'))
            pending = native.pending()
            if not pending:
                _NATIVE_ADVERTISED.pop(key, None)
                return reply(chat_id, '原会话当前没有可回答的原生问题。')
            request = pending[0]
            # Bound in-memory identities; restart requires re-advertising.
            if len(_NATIVE_ADVERTISED) >= 1024:
                _NATIVE_ADVERTISED.clear()
            lines = ['请在本话题回答；单题可发选项编号或文字，多题逐行填写 1=答案、2=答案。']
            if text is not None:
                lines.insert(0, '尚未绑定待回答问题，刚才的文字未发送，请确认以下问题后重新回答。')
            for i, question in enumerate(request['questions'], 1):
                lines.append(f"**{i}. {clean_md(question['question'])}**")
                for j, option in enumerate(question.get('options', []), 1):
                    lines.append(f"{j}) {clean_md(option['label'])} {clean_md(option.get('description', ''))}")
            els = [{'tag': 'markdown', 'content': '\n'.join(lines)}]
            if len(request['questions']) == 1:
                options = request['questions'][0].get('options', [])
                for start in range(0, min(len(options), 20), 5):
                    els.append({'tag': 'action', 'actions': [btn(o['label'][:40], 'native_answer',
                        { 'sid': full, 'token': request['token'], 'answer': str(j + 1)})
                        for j, o in enumerate(options[start:start + 5], start)]})
            els.append({'tag': 'markdown', 'content': f"显式回答：`#answer {full} {request['token']} 答案`"})
            mid = reply_card(chat_id, '原会话等待回答', 'orange', els)
            if mid:
                _NATIVE_ADVERTISED[key] = request['token']
            return mid
    except NativeInputError as error:
        return reply(chat_id, str(error), template='orange')
    except Exception:
        # Native transport errors may contain launch credentials; never echo/log them.
        return reply(chat_id, '原会话回答通道不可用，请确认桌面会话仍在运行；dsh 需已连接的 Host。', template='orange')


def cmd_questions(sender, chat_id, args):
    sid = args[0] if args else ''
    if not sid:
        route = getattr(_REPLY_CONTEXT, 'route', {}) or {}
        hit = cmid_lookup(route.get('root_id') or route.get('message_id', ''))
        sid = hit.get('sid8', '') if hit and hit.get('kind') == 'sess' else ''
    return native_reply(sender, chat_id, sid)


def cmd_answer(sender, chat_id, args):
    if len(args) < 3:
        return reply(chat_id, '用法：#answer <会话> <问题令牌> 答案；也可在 #questions 展示问题后直接在话题回答。')
    return native_reply(sender, chat_id, args[0], ' '.join(args[2:]), args[1])


CMDS = {"questions": cmd_questions, "answer": cmd_answer, "board": cmd_board, "retention": cmd_retention,"dnew": cmd_dnew, "dresume": cmd_dresume,"health": cmd_health,"deliveries": cmd_deliveries, "retry-delivery": cmd_retry_delivery, "new": cmd_new, "stop": cmd_stop, "model": cmd_model, "go": cmd_go, "say": cmd_go, "attach": cmd_attach, "leave": cmd_leave, "status": cmd_status, "sessions": cmd_sessions,
        "title": cmd_title, "jobs": cmd_jobs, "models": cmd_models,
        "projects": cmd_projects, "watch": cmd_watch, "unwatch": cmd_unwatch, "goal": cmd_goal, "cnew": cmd_cnew, "wnew": cmd_wnew, "qnew": cmd_qnew,
        "clean": cmd_clean, "forget": cmd_forget, "autowatch": cmd_autowatch, "wizard": lambda s, c, a: wiz_entry(s, c, (a or ["new"])[0]),
        "help": cmd_help, "scan": cmd_scan}

# ---------- 飞书长连接 + 命令队列 ----------
Q = queue.Queue()
_INBOUND_LOCK = threading.Lock()


def command_inbox():
    return CommandInbox(Path(STATED) / 'command-inbox.sqlite')


def enqueue_inbound(item, source, event_id='', reply_route=None, image_key=''):
    with _INBOUND_LOCK:
        if Q.qsize() >= int(conf().get('command_queue_max', 100)):
            STATUS['command_queue_full_at'] = time.time()
            return 'full'
        ident = command_inbox().claim(source, event_id) if event_id else ''
        if event_id and not ident:
            return 'duplicate'
        if ident or reply_route or image_key:
            item = (*item, {'command_id': ident, 'queued_at': time.time(),
                           'reply_route': reply_route or {}, 'image_key': image_key,
                           'image_message_id': event_id if image_key else ''})
        Q.put(item)
        STATUS['command_queue_depth'] = Q.qsize()
        return 'queued'


def cmd_queue(sender, chat_id, args):
    summary = command_inbox().summary()
    reply(chat_id, f"命令状态：{summary['counts']}\n待处理队列 {Q.qsize()} 项 · 最早等待 {summary['oldest_wait_seconds']} 秒\n"
                   'handled 表示命令已处理，不代表模型任务完成。重启中断的命令不会自动重放；请检查任务后再发新指令。')


CMDS['queue'] = cmd_queue
CMDS['observe'] = cmd_observe
CMDS['broadcast'] = cmd_observe

# ---------- 卡片按钮交互(向导状态机) ----------
SESSW = f"{STATED}/ctl-wizard.json"
_wiz = {}     # sender -> {"step": proj|model|prompt, "cwd":, "model":, "ts":, "mode":"new|go|model", "jid":}
def wiz_get(sender):
    w = _wiz.get(sender)
    if w and time.time() - w.get("ts", 0) > 300:
        _wiz.pop(sender, None)
        return None
    return w
def wiz_set(sender, chat_id=None, **kw):
    w = _wiz.get(sender) or {}
    w.update(kw); w["ts"] = time.time()
    if chat_id: w["chat"] = chat_id
    _wiz[sender] = w
def wiz_pop(sender):
    return _wiz.pop(sender, None)

def btn(text, action, value, btn_type="default"):
    return {"tag": "button", "text": {"tag": "plain_text", "content": text},
            "type": btn_type, "behaviors": [
                {"type": "callback", "value": dict(value, act=action)}]}

def mark_card(mid, title, desc, icon="✅"):
    """被点击的卡片 -> 定格为已处理(去掉按钮, 只留结果一行)"""
    if not mid:
        return
    if any(jd.get("root_mid") == mid for jd in jobs_load().values()):
        return  # 话题根卡是持久入口，不能定格为一次性回执
    card = {"schema": "2.0", "config": CARD_CONFIG,
            "header": {"template": "grey",
                       "title": {"tag": "plain_text", "content": f"{icon} {title}"}},
            "body": {"elements": [{"tag": "markdown",
                                   "content": f"{desc}\n已处理 · {time.strftime('%H:%M:%S')} · 后续看新卡片",
                                   "text_size": "notation"}]}}
    api("PATCH", f"/im/v1/messages/{mid}",
        {"content": json.dumps(card, ensure_ascii=False)})

def action_card(chat_id, title, els, tag=None, delivery_id=None):
    card = {"msg_type": "interactive", "card": {"schema": "2.0",
        "config": CARD_CONFIG,
        "header": {"template": tag or "turquoise", "title": {"tag": "plain_text", "content": title}},
        "body": {"elements": els}}}
    o = api("POST", "/im/v1/messages?receive_id_type=chat_id",
            {"receive_id": chat_id, "msg_type": "interactive",
             "content": json.dumps(card["card"], ensure_ascii=False),
             **({'uuid': delivery_id} if delivery_id else {})})
    return ((o or {}).get("data") or {}).get("message_id", "")

def btn_row(*btns):
    """双列一行(column_set 实测支持); 奇数个最后一列留空"""
    bs = list(btns)
    if len(bs) % 2:
        bs.append(None)
    cols = []
    for i in range(0, len(bs), 2):
        pair = bs[i:i+2]
        for b in pair:
            cols.append({"tag": "column", "width": "weighted", "weight": 1,
                         "horizontal_align": "center",
                         "elements": [b] if b else [{"tag": "markdown", "content": " "}]}
                        )
    return [{"tag": "column_set", "horizontal_spacing": "medium",
             "columns": cols[i:i+2]} for i in range(0, len(cols), 2)]

def handle_ui(sender, chat_id, value, card_mid=""):
    # W5-3: 与文字命令侧(worker)同语义 — 白名单为空=拒绝一切, 卡片按钮不是后门
    allow = [x.strip() for x in conf()["allow_ou"].split(",") if x.strip()]
    if not allow or sender not in allow:
        audit(sender, f"[ui]{value.get('act')}", "DENY(not in allow_ou)")
        return
    if not rate_ok(sender):
        return reply(chat_id, "⛔ 操作过于频繁,稍后再试", template="red")
    act = value.get("act")
    if act == "native_answer":
        native_reply(sender, chat_id, str(value.get("sid", "")), str(value.get("answer", "")), str(value.get("token", "")))
    elif act == "board":
        cmd_board(sender,chat_id,[str(value.get('filter','all')),str(value.get('page',1))])
    elif act == "watch_detail":
        cmd_watch_detail(sender,chat_id,[str(value.get('sess8',''))])
    elif act == 'watch_list':
        cmd_watch(sender, chat_id, ['list', str(value.get('page', 1))])
    elif act == 'observe_setting':
        cmd_observe(sender, chat_id, [str(value.get('sid', '')), str(value.get('op', 'status'))])
    elif act == 'observe_title':
        try:
            sid = resolve_observation(str(value.get('sid', '')))
        except ValueError as error:
            return reply(chat_id, str(error))
        mid = action_card(chat_id, '修改观察备注', [{'tag': 'markdown', 'content': '下一条消息作为备注标题；5分钟有效，#cancel 取消。'}])
        wiz_set(sender, chat_id, step='watch_title', sid=sid, wcard=mid)
    elif act == "unwatch_sess":
        sid=str(value.get('sess8',''))
        if re.fullmatch(r'[0-9a-f-]{8,36}',sid):
            cmd_unwatch(sender,chat_id,[sid])
        else:
            reply(chat_id, "会话标识无效", template="orange")
    elif act == "scan_now":
        mark_card(card_mid, "纳管扫描", "📡 列表见新卡片")
        cmd_scan(sender, chat_id, [])
    elif act == "watch_sess":
        ok, info = watch_session(chat_id, value.get('sess8', ''), sender=sender)
        if ok or info == 'dup':
            mark_card(card_mid, '已纳管', f"🧵 {value.get('sess8')} 已建立播报路由")
        else:
            reply(chat_id, f'❌ 纳管未完成：{info}；请稍后点击原按钮重试', template='orange')
    elif act == "fam_proj":
        mark_card(card_mid, "选引擎", f"🧰 {value.get('fam')} · 下一步选项目")
        ui_fam_projects(sender, chat_id, value.get("fam", "codex"))
    elif act == "fam_help":
        reply_card(chat_id, "📖 五个编码引擎", "turquoise", [{"tag": "markdown", "content":
            "**🐋 DeepSeek Harness** — #dnew 新建 / #dresume 显式接管；#go 续跑 / #stop 停止\n"
            "**🚀 Codex** — 主力。模型全(网关清单),目标环/计划模式最成熟\n"
            "**🤖 Claude Code** — 原生 Anthropic 工具链,sonnet/opus/haiku\n"
            "**🦞 WorkBuddy** — CodeBuddy CLI,glm/kimi/deepseek/hy3,自带办公插件\n"
            "**🧩 Qoder CLI** — Qoder 自家 CLI(`#qnew`),模型名用 `qoderclicn --list-models` 查\n\n"
            "四家都有: 话题舱 · `#go`续跑 · `#stop` · `#goal` · `#model` · 💰成本卡"}])
    elif act == "pick_proj_fam":
        fam, cwd = value.get("fam", "codex"), value.get("cwd", "")
        mark_card(card_mid, "已选项目", f"📂 `{os.path.basename(cwd)}`")
        ui_prompt_fam(sender, chat_id, fam, cwd)
    elif act == "new_in_proj":
        cwd = value.get("cwd", "")
        mark_card(card_mid, "新建任务", f"📂 {os.path.basename(cwd)} · 等待指令")
        ui_prompt_fam(sender, chat_id, value.get("fam", "codex"), cwd)
    elif act == "pick_proj":
        cwd = value.get("cwd", "")
        mark_card(card_mid, "已选项目", f"📂 `{os.path.basename(cwd)}`")
        ui_models(sender, chat_id, cwd, mode=value.get("mode", "new"))
    elif act == "pick_model":
        cwd, model = value.get("cwd", ""), value.get("model", "")
        mark_card(card_mid, "已选模型", f"🧭 {model or '默认(gpt-6-astra)'} → `{os.path.basename(cwd)}`")
        ui_prompt(sender, chat_id, cwd, model)
    elif act == "pick_job":
        jid = value.get("jid", "")
        kind = value.get("kind", "go")
        mark_card(card_mid, f"{jid} · {'指令' if kind=='go' else kind}",
                  {"go": f"▶️ 等待给 **{jid}** 的指令(下条消息即指令)",
                   "stop": f"⏹ 正在中断 **{jid}**…",
                   "model": f"🔁 等待 **{jid}** 的新模型名(下条消息即模型)"}.get(kind, jid),
                  icon={"go": "▶️", "stop": "⏹", "model": "🔁"}.get(kind, "✅"))
        if kind == "go":
            ui_running_prompt(sender, chat_id, jid)
        elif kind == "stop":
            cmd_stop(sender, chat_id, [jid])
        elif kind == "title":
            wiz_set(sender, chat_id, step="job", act="title", jid=jid)
            reply(chat_id, "🏷 请回复新标题（支持空格），5分钟内有效；取消：`#cancel`")
        elif kind == "model":
            wiz_set(sender, chat_id, step="job", act="model", jid=jid)
            reply(chat_id, f"🧭 请回复新模型名(如 `{'` `'.join(sorted(known_models())[:2])}`),5分钟内有效")
    elif act == "job_detail":
        cmd_jobs(sender, chat_id, [value.get("jid", "")])   # 查询类不定格原列表
    elif act == "job_forget":
        mark_card(card_mid, f"{value.get('jid')} 已放弃", "🗑 任务记录已移除(话题历史保留)", icon="🗑")
        cmd_forget(sender, chat_id, [value.get("jid", "")])
    elif act == "pick_job_stop_list":
        mark_card(card_mid, "中断", "⏹ 选择要中断的任务 ↓", icon="⏹")
        ui_running_jobs(sender, chat_id, act="stop")
    elif act == "wizard":
        w = value.get("w", "new")
        if w == "jobs":
            cmd_jobs(sender, chat_id, [])                    # 查询类不定格
        else:
            mark_card(card_mid, "向导", {"new": "🚀 选择项目开任务", "go": "▶️ 选择目标任务",
                                         "model": "🔁 选择要换模型的任务",
                                         "stop": "⏹ 选择要中断的任务"}.get(w, w))
            wiz_entry(sender, chat_id, w)

def wiz_entry(sender, chat_id, w):
    if w == "new":
        ui_agent_family(sender, chat_id)
    elif w == "go":
        ui_running_jobs(sender, chat_id, act="go")
    elif w == "model":
        ui_running_jobs(sender, chat_id, act="model")
    elif w == "stop":
        cmd_stop_all_picker(sender, chat_id)

def ui_running_prompt(sender, chat_id, jid):
    mid = action_card(chat_id, f"✍️ 给 {jid} 下达指令", [{"tag": "markdown",
        "content": "下一条消息即作为 `#go` 指令发出(5分钟有效)"}])
    wiz_set(sender, chat_id, step="job", act="go", jid=jid, wcard=mid)

def ui_projects(sender, chat_id, mode="new", carry=None):
    """选项目按钮矩阵"""
    pl = projects()[:12]
    bs = [btn(("⭐ " if i == 0 else "") + sn, "pick_proj",
               {"mode": mode, "cwd": d, "carry": carry},
               "primary" if i == 0 else "default")
          for i, (sn, d, ts) in enumerate(pl)]
    els = ([{"tag": "markdown", "content": "点项目名开始（⭐=最近活跃）："}]
           + btn_row(*bs)
           + [{"tag": "markdown", "content": "_或直接文字: `#new <项目名> 提示词`_",
               "text_size": "notation"}])
    title = {"new": "📂 选择项目 → 启动任务", "go": "📂 选择项目",
             "model": "📂 选择项目"}.get(mode, "📂 选择项目")
    action_card(chat_id, title, els)
    wiz_set(sender, chat_id, step="proj", mode=mode, carry=carry)

def ui_agent_family(sender, chat_id):
    els = ([{"tag": "markdown", "content": "用哪个编码 Agent 干这活？"}]
           + btn_row(btn("🚀 Codex", "fam_proj", {"fam": "codex"}, "primary"),
                     btn("🤖 Claude Code", "fam_proj", {"fam": "claude"}))
           + btn_row(btn("🦞 WorkBuddy", "fam_proj", {"fam": "wcb"}),
                     btn("🧩 Qoder CLI", "fam_proj", {"fam": "qoder"}))
           + btn_row(btn("🐋 DeepSeek Harness", "fam_proj", {"fam": "dsh"}),
                     btn("📖 差别说明", "fam_help", {"x": 1}))
           + [{"tag": "markdown",
               "content": "_默认🔓full权限;跑偏 `#stop`。🚀模型全 🤖原生Claude链 🦞腾讯系+办公插件 🧩Qoder自家CLI_",
               "text_size": "notation"}])
    action_card(chat_id, "🧰 新建任务 · 选引擎", els)

def ui_fam_projects(sender, chat_id, fam):
    pl = projects()[:12]
    ic = FAM_IC.get(fam, "🧰")
    bs = [btn(("⭐ " if i == 0 else "") + sn, "pick_proj_fam",
               {"fam": fam, "cwd": d},
               "primary" if i == 0 else "default")
          for i, (sn, d, ts) in enumerate(pl)]
    els = ([{"tag": "markdown", "content": f"{ic} 选工作目录（⭐=最近活跃）："}]
           + btn_row(*bs)
           + [{"tag": "markdown",
               "content": f"_或文字直发: `#{FAM_CMD.get(fam, 'new')} <项目> 提示词`_",
               "text_size": "notation"}])
    action_card(chat_id, f"{ic} 选择项目", els)
    wiz_set(sender, chat_id, step="proj", mode=fam)

def ui_prompt_fam(sender, chat_id, fam, cwd):
    ic = FAM_IC.get(fam, "🧰")
    mid = action_card(chat_id, f"{ic} 发送任务指令", [{"tag": "markdown",
        "content": (f"下一条消息即作为指令发出：\n· 📂 `{os.path.basename(cwd)}`\n"
                    f"· 引擎: {ic} {fam}\n· 权限: {conf().get('default_perm', 'full')}(默认)\n\n_5 分钟内有效,发 `#cancel` 放弃_")}])
    wiz_set(sender, chat_id, step="prompt_fam", fam=fam, cwd=cwd, wcard=mid)

def ui_models(sender, chat_id, cwd, mode="new"):
    kn = sorted(known_models())
    bs = [btn("(默认)", "pick_model", {"cwd": cwd, "model": ""}, "primary")]
    bs += [btn(x, "pick_model", {"cwd": cwd, "model": x}) for x in kn[:5]]
    action_card(chat_id, "🧭 选择模型(可跳过)",
                  btn_row(*bs) + [{"tag": "markdown", "content": "_点「默认」或直接等下一步_", "text_size": "notation"}])
    wiz_set(sender, chat_id, step="model", cwd=cwd)

def ui_prompt(sender, chat_id, cwd, model):
    mid = action_card(chat_id, "✍️ 请发送任务指令", [{"tag": "markdown",
        "content": f"下一条消息即作为指令发出：\n· 📂 `{os.path.basename(cwd)}`\n· 模型: {model or '默认'}\n\n_5 分钟内有效,发 `#cancel` 放弃_"}])
    wiz_set(sender, chat_id, step="prompt", cwd=cwd, model=model, wcard=mid)

def cmd_stop_all_picker(sender, chat_id):
    ui_running_jobs(sender, chat_id, act="stop")

def ui_running_jobs(sender, chat_id, act="go"):
    j = jobs_load()
    run = [(k, v) for k, v in j.items() if v.get("state") == "running"]
    if not run:
        return reply(chat_id, f"没有运行中的 🚀 任务(选 {'指令目标' if act=='go' else '要换模型的'} 任务)", template="orange")
    bs = [btn(f"{k}·{(v.get('title') or v.get('prompt',''))[:10]}", "pick_job",
              {"jid": k, "kind": act}) for k, v in run[:8]]
    action_card(chat_id, "🚀 选择任务", btn_row(*bs))
    wiz_set(sender, chat_id, step="job", act=act)

def on_card_action(data):
    """卡片按钮回调 -> 命令等价入队(多路防御取值: SDK为dict薄封装)"""
    value, sender = {}, ""
    try:
        ev = getattr(data, "event", None)
        if ev is None:
            ev = data
        def g(o, k, d=None):
            if isinstance(o, dict):
                return o.get(k, d)
            return getattr(o, k, d)
        action = g(ev, "action") or {}
        value = g(action, "value") or {}
        act = g(value, "act", "") or ""
        op = g(ev, "operator") or {}
        sender = g(op, "open_id") or g(ev, "open_id") or ""
        ctx = g(ev, "context") or g(action, "context") or {}
        chat_id = g(ctx, "open_chat_id") or g(ev, "open_chat_id") or ""
        msg_id = g(ctx, "open_message_id") or g(ev, "open_message_id") or ""
        if not (chat_id and sender):
            log("card-act received")
        log(f"card-act {act} context={bool(chat_id and sender)}")
        if not (chat_id and sender):
            return
        header = g(data, 'header') or {}
        event_id = g(header, 'event_id') or g(ev, 'token') or ''
        queued = enqueue_inbound(("__ui__", sender, chat_id, value, msg_id), 'card', event_id)
        if queued != 'queued':
            return {'toast': {'type': 'info' if queued == 'duplicate' else 'warn',
                              'content': '该操作已接收' if queued == 'duplicate' else '队列已满，请稍后重试'}}
    except Exception as e:
        log(f"card-act-err type={type(e).__name__}")
    _vt = value if isinstance(value, dict) else {}
    _allow = [x.strip() for x in conf()["allow_ou"].split(",") if x.strip()]
    if not _allow or sender not in _allow:
        return {"toast": {"type": "warn", "content": "⛔ 未授权: open_id 不在 allow_ou"}}
    _danger = _vt.get("act") in ("job_forget", "pick_job_stop_list") or (
        _vt.get("act") == "pick_job" and _vt.get("kind") == "stop")
    _toast = {"type": "warn" if _danger else "info",
              "content": f"已排队，前方 {max(0, Q.qsize() - 1)} 项" if Q.qsize() > 1 else "收到,处理中…" if not _danger else "收到,正在执行…"}
    return {"toast": _toast}

def _plain(o):
    if isinstance(o, dict):
        return {k: _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(x) for x in o]
    return str(o)[:60]

def inbound_text(body, message_type):
    """Extract user-authored text from plain or rich Feishu messages once."""
    if not isinstance(body, dict):
        return ''
    if message_type != 'post':
        text = body.get('text')
        return text.strip() if isinstance(text, str) else ''
    post = body
    if not isinstance(post.get('content'), list):
        post = next((body[language] for language in ('zh_cn', 'en_us', 'ja_jp')
                     if isinstance(body.get(language), dict)
                     and isinstance(body[language].get('content'), list)), {})
    lines = [post['title']] if isinstance(post.get('title'), str) and post['title'].strip() else []
    for paragraph in post.get('content', []):
        if not isinstance(paragraph, list):
            continue
        lines.append(''.join(node['text'] for node in paragraph
                             if isinstance(node, dict) and node.get('tag') in ('text', 'a', 'code_block')
                             and isinstance(node.get('text'), str)))
    return '\n'.join(lines).strip()


def on_message(data):
    try:
        ev = data.event
        sender_id = ev.sender.sender_id.open_id or "?"
        msg = ev.message
        chat_id = msg.chat_id
        chat_type = msg.chat_type
        parent_id = getattr(msg, "parent_id", "") or ""
        thread_id = getattr(msg, "thread_id", "") or ""
        root_id = getattr(msg, 'root_id', '') or ''
        message_id = getattr(msg, 'message_id', '') or ''
        message_type = getattr(msg, 'message_type', '') or ''
        try:
            body = json.loads(msg.content)
            text = inbound_text(body, message_type)
            image_key = (body.get('image_key') or '') if message_type == 'image' else ''
        except Exception:
            text = ""
            image_key = ''
        log(f"recv {chat_type} type={message_type} chars={len(text)} parent={bool(parent_id)}")
        if not text and not image_key:
            return
        route = {'chat_id': chat_id, 'message_id': message_id or root_id or parent_id,
                 'root_id': root_id} if thread_id or root_id else {}
        queued = enqueue_inbound((sender_id, chat_id, text, parent_id, thread_id),
                                 'message', message_id, reply_route=route,
                                 image_key=image_key)
        if queued == 'full':
            _REPLY_CONTEXT.route = route
            try:
                reply(chat_id, '命令队列已满，本条未执行，请稍后重新发送', template='orange')
            finally:
                _REPLY_CONTEXT.route = {}
    except Exception as e:
        try:
            log(f"evt-err type={type(e).__name__}")
        except Exception:
            log(f"evt-err {e}")

_rate = {}
def rate_ok(sender):
    now = time.time(); win = float(conf()["rate_win"])
    lst = [t for t in _rate.get(sender, []) if now - t < win]
    if len(lst) >= int(conf()["rate_n"]):
        return False
    lst.append(now); _rate[sender] = lst
    return True

def worker():
    while True:
        item = Q.get()
        metadata = item[-1] if len(item) == 6 and isinstance(item[-1], dict) else {}
        ident = metadata.get('command_id')
        _REPLY_CONTEXT.route = metadata.get('reply_route') or {}
        outcome = 'handled'
        STATUS['command_worker_busy_since'] = time.time()
        try:
            if ident:
                command_inbox().mark(ident, 'processing')
            if isinstance(item, tuple) and item[0] == "__retry__":
                jid_ = item[1]
                jd = jobs_load().get(jid_) or {}
                if jd.get("state") != "exited":
                    continue
                if (jd.get("goal") or {}).get("active"):
                    # W2-9: 目标环任务的重试改走goal-kick, 续跑命令带哨兵提示, 环不静默死亡
                    Q.put(("goal-kick", "ctl-retry", jd.get("chat_id", ""), jid_))
                    continue
                try:
                    cmd_go("ctl-retry", jd.get("chat_id", ""), [jid_, "刚才失败(瞬态错误自动重试),请继续执行未完成任务"])
                    tsend(jd, f"🔄 自动重试第{jd.get('retry_cnt',1)}次已发起", f"🔄 {jid_} · 重试", "orange")
                except Exception as e:
                    log(f"retry-run-err {e}")
                continue
            if isinstance(item, tuple) and item[0] == "goal-kick":
                jid_ = item[3]
                jd = jobs_load().get(jid_) or {}
                g = jd.get("goal") or {}
                sess = jd.get("sess") or find_sess(jid_)
                if g.get("active") and sess and jd.get("state") != "running":
                    c = conf()
                    rcf = new_round_rc(jid_)
                    nxt = f"开始推进目标:{g['text']}\n若已完全达成,最终回复最后一行输出 {GOAL_SENTINEL};否则直接动手实施,禁止以计划陈述结尾。(目标模式第1轮)"
                    try:
                        with open(jd["out_file"], "ab") as of:
                            pr = spawn_resume(jd, sess, nxt, of)
                        threading.Thread(target=_reap, args=(pr, rcf), daemon=True).start()
                        with _jl:
                            jj = jobs_load()
                            jj[jid_].update(pid=pr.pid, state="running", sess=sess,
                                            started=time.time(), rc_file=rcf, notified=True)
                            reset_run_flags(jj[jid_])
                            if not jj[jid_].get("goal", {}).get("rounds"):
                                jj[jid_]["goal"]["rounds"] = 1   # W2-9: 重试kick不抹已有轮次
                            jobs_save(jj)
                        log(f"goal-kick {jid_} pid={pr.pid}")
                    except Exception as e:
                        log(f"goal-kick-err {e}")
                continue
            if isinstance(item, tuple) and item[0] == "__ui__":
                _, sender_id, chat_id, value, card_mid = item[:5]
                try:
                    handle_ui(sender_id, chat_id, value, card_mid)
                except Exception as e:
                    outcome = "failed"
                    log(f"ui-err {e}")
                continue
            sender_id, chat_id, text, parent_id, thread_id = (*item, "")[:5]
            root_id = (_REPLY_CONTEXT.route or {}).get('root_id', '')
            thread_id = thread_id or root_id
            if thread_id and not root_id:
                # Some Feishu events carry only thread_id; recover watched roots.
                builds = JsonMap(Path(STOPICF).with_name('topic-builds.json')).read()
                root_id = next((b.get('root_mid', '') for key, b in builds.items()
                                if key.startswith('watch:') and b.get('chat_id') == chat_id
                                and thread_id in (b.get('thread_id'), b.get('root_mid'))), '')
                if root_id:
                    _REPLY_CONTEXT.route = {**(_REPLY_CONTEXT.route or {}), 'chat_id': chat_id,
                                            'root_id': root_id,
                                            'message_id': (_REPLY_CONTEXT.route or {}).get('message_id') or root_id}
            if thread_id and not _REPLY_CONTEXT.route:
                root_mid = next((v.get('root_mid') for v in jobs_load().values()
                                 if thread_id in (v.get('thread_id'), v.get('root_mid'))), '')
                _REPLY_CONTEXT.route = {'chat_id': chat_id, 'message_id': root_mid or parent_id}
            log(f"worker input chars={len(text)}")
            try:
                allow = [x.strip() for x in conf()["allow_ou"].split(",") if x.strip()]
                if not allow or sender_id not in allow:
                    audit(sender_id, text, "DENY(not in allow_ou)")
                    reply(chat_id, f"⛔ 未授权。你的 open_id 是 `{sender_id}`，群 chat_id 是 `{chat_id}`。\n把 open_id 填入 ctl.conf 的 allow_ou= 后即可放行。"
                          if not allow else "⛔ 无权执行控制命令", template="red")
                    continue
                if not rate_ok(sender_id):
                    audit(sender_id, text, "DENY(rate)")
                    reply(chat_id, "⛔ 命令过于频繁,稍后再试", template="red"); continue
                if metadata.get('image_key'):
                    bound = (desktop_thread_binding(sender_id, chat_id, thread_id, root_id)
                             if thread_id else desktop_binding(sender_id, chat_id))
                    if bound:
                        desktop_send(sender_id, chat_id, text or '请查看这张截图。', bound,
                                     str(uuid.uuid5(uuid.NAMESPACE_URL, ident)) if ident else '',
                                     image=(metadata.get('image_message_id'), metadata['image_key']))
                    else:
                        reply(chat_id, '请先在控制群主聊天进入 Codex 任务，再在主聊天或该任务的控制话题发送图片。', template='orange')
                    continue
                if text.strip() in ('退出控制', '退出任务'):
                    cmd_leave(sender_id, chat_id, [])
                    continue
                target_title = (attach_title(text) or dsh_attach_title(text) or desktop_shortcut(text)) if not text.startswith('#') else ''
                if target_title:
                    cmd_attach(sender_id, chat_id, [target_title])
                    continue
                # 向导: prompt 步的下一条普通消息 = 指令
                w = wiz_get(sender_id)
                if w and w.get("chat") and w.get("chat") != chat_id:
                    w = None
                # W6-5: 取消判定必须早于下面三个"下一条消息即指令"分支, 否则裸「取消」会被当提示词起任务;
                #        且无挂起向导时不再吞掉它(话题舱里对 agent 说"取消"要能照常送达)
                if text.strip() == "#cancel" or (w and text.strip() == "取消"):
                    wiz_pop(sender_id)
                    reply(chat_id, "🚫 向导已取消,这条消息未发出" if w
                                   else "没有进行中的向导可取消", template="grey")
                    continue
                if w and w.get('step') == 'watch_title' and not text.startswith('#'):
                    wiz_pop(sender_id)
                    cmd_observe(sender_id, chat_id, [w['sid'], 'title', text])
                    continue
                if w and w.get("step") == "prompt_fam" and not text.startswith("#"):
                    fam, cwd = w.get("fam"), w.get("cwd")
                    wiz_pop(sender_id)
                    fn = {"codex": cmd_new, "claude": cmd_cnew, "wcb": cmd_wnew,
                          "qoder": cmd_qnew, "dsh": cmd_dnew}.get(fam, cmd_new)
                    fn(sender_id, chat_id, [cwd] + [text])
                    continue
                if w and w.get("step") == "prompt" and not text.startswith("#"):
                    cwd, model = w.get("cwd"), w.get("model", "")
                    wiz_pop(sender_id)
                    # 复用 cmd_new 的目录解析: 直接把 cwd 塞回args首位路径形式
                    cmd_new(sender_id, chat_id, [cwd] + ([model] if model else []) + [text])
                    continue
                if w and w.get("step") == "job" and not text.startswith("#"):
                    act = w.get("act", "go"); jid = w.get("jid")
                    wiz_pop(sender_id)
                    mark_card(w.get("wcard", ""), {"go": "指令已发出 → " + jid, "title": "标题已接收"}.get(act, "模型已选定"),
                              clean_md(text[:80]))
                    if act == "go" and jid:
                        cmd_go(sender_id, chat_id, [jid, text])
                    elif act == "title" and jid:
                        cmd_title(sender_id, chat_id, [jid, text])
                    elif act == "model" and jid:
                        cmd_model(sender_id, chat_id, [jid, text])
                    continue
                # 话题内 # 命令: 自动补当前任务号(#goal/#stop/#model/#title)
                if thread_id and text.strip().startswith("#"):
                    jid_t = thread_to_jid(thread_id) or thread_to_jid(root_id)
                    toks = text.split()
                    head = toks[0].lower()
                    if jid_t and head in ("#goal", "#stop", "#model", "#title") \
                            and (len(toks) < 2 or not re.match(r"^[cwqd]?j\d+$", toks[1])):
                        if head == "#goal" and len(toks) > 1 and toks[1] == "stop":
                            text = f"#goal stop {jid_t} " + " ".join(toks[2:])
                        else:
                            text = f"{head} {jid_t} " + " ".join(toks[1:])
                        log("thread-cmd expanded")
                # 话题内普通文字 = 对该任务下指令
                if thread_id and not text.startswith("#"):
                    bound = desktop_thread_binding(sender_id, chat_id, thread_id, root_id)
                    if bound:
                        desktop_send(sender_id, chat_id, text, bound,
                                     str(uuid.uuid5(uuid.NAMESPACE_URL, metadata['command_id']))
                                     if metadata.get('command_id') else '')
                        continue
                    jid_t = thread_to_jid(thread_id) or thread_to_jid(root_id)
                    if jid_t:
                        audit(sender_id, f"[thread->{jid_t}] {text[:80]}", "auto-go")
                        cmd_go(sender_id, chat_id, [jid_t, text])
                        continue
                    if root_id or parent_id:
                        hit = cmid_lookup(root_id or parent_id)
                        if hit and hit.get('kind') == 'sess':
                            native_reply(sender_id, chat_id, hit.get('sid8', ''), text)
                            continue
                if text.startswith("#"):
                    parts = text[1:].split(maxsplit=3) if text.split()[0].lower() == '#answer' else text[1:].split()
                    if not parts: continue
                    fn = CMDS.get(parts[0].lower())
                    if fn:
                        fn(sender_id, chat_id, parts[1:])
                    else:
                        reply(chat_id, f"❓ 未知命令 `#{parts[0]}`,试试 `#help`", template="orange")
                elif parent_id:
                    hit = cmid_lookup(parent_id)
                    if not hit:
                        bound = desktop_binding(sender_id, chat_id)
                        if bound:
                            desktop_send(sender_id, chat_id, text, bound,
                                         str(uuid.uuid5(uuid.NAMESPACE_URL, metadata['command_id']))
                                         if metadata.get('command_id') else '')
                    elif hit.get("kind") == "job":
                        jid = hit["jid"]
                        audit(sender_id, f"[quote->{jid}] {text[:80]}", "routed-go")
                        cmd_go(sender_id, chat_id, [jid, text])
                    else:
                        native_reply(sender_id, chat_id, hit.get('sid8', ''), text)
                elif not thread_id:
                    bound = desktop_binding(sender_id, chat_id)
                    if bound:
                        desktop_send(sender_id, chat_id, text, bound,
                                     str(uuid.uuid5(uuid.NAMESPACE_URL, metadata['command_id']))
                                     if metadata.get('command_id') else '')
                # 未绑定的普通非#消息保持静默
            except Exception as e:
                outcome = "failed"
                log(f"worker-err {e}")
                audit(sender_id, text, f"ERROR {e}")
        except Exception as exc:
            outcome = 'failed'
            log(f'worker-item-failed type={type(exc).__name__}')
        finally:
            _REPLY_CONTEXT.route = {}
            STATUS['command_worker_busy_since'] = 0
            STATUS['command_worker_progress_at'] = time.time()
            STATUS['command_queue_depth'] = Q.qsize() if hasattr(Q, 'qsize') else 0
            if ident:
                try:
                    command_inbox().mark(ident, outcome)
                except Exception:
                    STATUS['command_state_write_failed_at'] = time.time()
                    log('command-state-write-failed')

def main_loop():
    import lark_oapi as l
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    try:
        STATUS['root_routes_repaired'] = repair_root_routes()
    except (OSError, ValueError):
        STATUS['route_write_failed_at'] = time.time()
        log('root-route-repair-failed')
    interrupted = command_inbox().recover()
    STATUS["interrupted_commands"] = interrupted
    a = app()
    c = conf()
    if not c["allow_ou"]:
        log("WARN: allow_ou 未配置 -> 启动但不执行任何命令(仅记录 open_id 供配置)")
    handler = (l.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message)
               .register_p2_card_action_trigger(on_card_action).build())
    cli = l.ws.Client(a["app_id"], a["app_secret"], event_handler=handler,
                      auto_reconnect=True)
    if cli is None:
        log("FATAL: lark-oapi 无 ws 模块, 需升级 SDK"); sys.exit(2)
    ws_thread = threading.Thread(target=cli.start, name="tower-ws", daemon=True)
    worker_thread = threading.Thread(target=worker, name="tower-worker", daemon=True)
    ws_thread.start()
    worker_thread.start()
    log("ctl: ws thread started")
    last_hb = last_poll = 0.0
    while True:
        STATUS.update(ctl_alive=True, ctl_progress_at=time.time(),
                      command_worker_alive=worker_thread.is_alive(), ws_thread_alive=ws_thread.is_alive())
        if not ws_thread.is_alive() or not worker_thread.is_alive():
            raise RuntimeError("关键线程退出: ws/worker")
        now = time.monotonic()
        if now - last_hb > 5:
            STATUS['command_queue_depth'] = Q.qsize()
            STATUS['command_oldest_wait_seconds'] = command_inbox().summary()['oldest_wait_seconds']
            try:
                with open(HB, "w") as f:
                    f.write(str(time.time()))
            except OSError:
                STATUS["heartbeat_write_failed_at"] = time.time()
            health_tick(STATED, LOGD)
            last_hb = now
        if now - last_poll > 3:
            poll_jobs(); refresh_sess(); refresh_status_cards(); refresh_observed_titles()
            try:
                poll_desktop_bindings()
            except (OSError, ValueError) as error:
                log(f'desktop-poll-err {type(error).__name__}')
            last_poll = now
        if now - (main_loop.__dict__.get("_arch_ts") or 0) > 600:
            main_loop._arch_ts = now
            try:
                archive_stale_topics()
            except Exception as e:
                log(f"archive-err {e}")
        if now - (main_loop.__dict__.get("_aw_ts") or 0) > 120:
            main_loop._aw_ts = now
            try:
                auto_watch_tick()
            except Exception as e:
                log(f"autowatch-err {e}")
        time.sleep(1)

def refresh_sess():
    """给 running 但还没解析到 sess 的任务补 sess"""
    with _jl:
        due = [jd["jid"] for jd in jobs_load().values()
               if jd.get("state") == "running" and not jd.get("sess")]
    hit = {}
    for jid in due:
        s = find_sess(jid)        # W6-6: glob+读大文件挪锁外
        if s:
            hit[jid] = s
    if hit:
        with _jl:
            j = jobs_load(); ch = False
            for jid, s in hit.items():
                if j.get(jid) and not j[jid].get("sess"):
                    j[jid]["sess"] = s; ch = True
            if ch:
                jobs_save(j)

def selftest():
    c = conf()
    print("conf OK:", {k: c[k] for k in ("max_jobs", "sandbox")})
    a = app()
    if (not a.get("app_id") or not a.get("app_secret") or
            a["app_id"] == 'cli_xxxxxxxxxxxxx' or a["app_secret"].startswith('REPLAC')):
        raise RuntimeError("缺少飞书 app_id/app_secret，自检失败")
    import importlib.util
    missing = [name for name in ("lark_oapi", "requests") if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError("缺少依赖: " + ", ".join(missing))
    print("app OK: True")
    print("播报目标群已配置:", bool(a.get("chat_id")))
    print("codex OK:", os.path.exists(c["codex_bin"]))
    print("qoder OK:", os.path.exists(c.get("qoder_bin", QODER)))
    # W6-7: 只读水位线, 绝不调 new_jid() — 自检曾每次烧掉一个编号(install.sh 每升级必跑)
    seq = {}
    print("dsh OK:", os.path.exists(c.get("dsh_bin", DSH)))
    for p in ("j", "cj", "wj", "qj", "dj"):
        try:
            seq[p] = int(open(f"{STATED}/.jid-seq.{p}").read().strip() or "0")
        except Exception:
            seq[p] = 0
    print("jid 水位线:", seq)
    print("allow_ou 已配置:", bool(c["allow_ou"]))

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest(); sys.exit(0)
    try:
        main_loop()
    except KeyboardInterrupt:
        pass
