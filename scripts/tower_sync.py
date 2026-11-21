#!/usr/bin/env python3
# codex rollout 实时跟踪 -> 飞书 v9 (线性播报流)
# 模式变更: v8 每会话一张终身卡(要往前翻) -> v9 以 assistant 消息为分界线:
#   · LLM消息 = 即时独立消息卡(按时间沉底, 群聊自然流)
#   · 两条消息之间的工具命令 = 合并进一张"段卡", 段内实时PATCH, 下一条消息/静默到期即定格
#   · 所有卡片三段式标题: 项目 · jid(ctl任务号,sess8兜底) · 回合号
# 卡片数受 seg_max/arm_events 门槛保护, 碎片会话不建卡; 投递仍走 Sender 队列合并+全局限速
# 保留: 巨型单行不死锁 | .text_sent去重 | conf mtime缓存 | state低频落盘 | --doctor | webhook兜底(旧逐条模式)
import copy
import platform
from pathlib import Path
import sys, json, os, time, glob, hashlib, re, threading, queue, urllib.request, urllib.error, uuid
import tower_harness as harness
from tower_questions import question_text
from tower_delivery import Outbox
from tower_runtime import safe_log, http_failure, STATUS, private_json, health_issues

from tower_ui import format_title, title_config, sanitize_message_body
from tower_media import checked_image, image_references, codex_session_cwd
from tower_store import register_route, JsonMap

VERSION = 9
HOME = os.path.expanduser("~")
HQ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFGD, STATED, LOGD = f"{HQ}/config", f"{HQ}/state", f"{HQ}/logs"

SESS = os.environ.get("CODEX_SESS_DIR") or f"{HOME}/.codex/sessions"
CSESS = f"{HOME}/.claude/projects"
QSESS = f"{HOME}/.qoder-cn/projects"     # Qoder 转录: <项目slug>/<uuid>.jsonl, 与 claude 同构
WBSESS = f"{HOME}/.workbuddy/projects"
CBSESS = f"{HOME}/.codebuddy/projects"   # WorkBuddy CLI(#wnew任务)独立数据目录
def is_qoder(p):
    return "/.qoder-cn/projects/" in p.replace('\\', '/')
def is_claude(p):
    """claude 形态转录(jsonl schema 与 slug 目录规则一致, 解析器共用): Claude Code + Qoder"""
    return "/.claude/projects/" in p.replace('\\', '/') or is_qoder(p)
def is_wb(p):
    p = p.replace('\\', '/')
    return "/.workbuddy/projects/" in p or "/.codebuddy/projects/" in p
HOOKF = f"{HOME}/.codex/feishu_webhook.txt"
APPF = f"{CFGD}/feishu-app.json"
TOKF = f"{STATED}/.token-cache.json"
BASE = "https://open.feishu.cn/open-apis"
CONF = f"{CFGD}/stream.conf"
GLOSSF = f"{CFGD}/tool-glossary.json"
STATE = os.environ.get("SYNC_STATE") or f"{STATED}/sync-state.json"
JOBSF = f"{STATED}/ctl-jobs.json"
TEXTSENT = f"{STATED}/.text_sent"
LOG = f"{LOGD}/sync.log"
HOSTN = platform.node().split(".")[0]

DEF = {"window_h": 8, "poll": 1.0, "max_len": 1200, "gap": 1.0, "tools": 1,
       "roll_gap": 18, "roll_max": 10, "live_gap": 2.5,
       "chunk": 262144, "big": 8388608,
       "api_gap": 0.3, "arm_events": 2, "save_gap": 10,
       "result_idle_seconds": 60, "watched_only": 0,
       "seg_mode": 1, "seg_max": 25}

# ---------- 配置(mtime缓存) ----------
_CONF = None; _CONF_MT = 0.0
def conf():
    global _CONF, _CONF_MT
    try:
        mt = os.path.getmtime(CONF)
    except OSError:
        mt = 0
    if _CONF is not None and mt == _CONF_MT:
        return _CONF
    c = dict(DEF)
    try:
        for ln in open(CONF, encoding="utf-8"):
            k, _, v = ln.partition("=")
            k, v = k.strip(), v.strip()
            if k in c and v:
                try:
                    c[k] = float(v) if "." in v else int(v)
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    _CONF, _CONF_MT = c, mt
    return c

GLOSS = None
def glossary():
    global GLOSS
    if GLOSS is None:
        try:
            GLOSS = json.load(open(GLOSSF, encoding="utf-8"))
        except Exception:
            GLOSS = {"tools": {}, "cmds": {}}
    return GLOSS

def label(name, desc):
    g = glossary()
    if name in g["tools"]:
        return g["tools"][name]
    first = (desc.strip().split(" ", 1)[0] if desc else "").lower()
    if first in g["cmds"]:
        return g["cmds"][first]
    if desc:
        two = " ".join(desc.strip().split(" ")[:2]).lower()
        if two in g["cmds"]:
            return g["cmds"][two]
    return name

def log(m):
    safe_log(LOG, m)


def hook_url():
    try:
        u = open(HOOKF, encoding="utf-8").read().strip()
        return u if u.startswith("https://open.feishu.cn/") else None
    except Exception:
        return None

# ---------- 应用模式 ----------
_APP = None
def app_cfg():
    global _APP
    if _APP is None:
        try:
            a = json.load(open(APPF, encoding="utf-8"))
            _APP = a if a.get("app_id") and a.get("app_secret") and a.get("chat_id") else {}
        except Exception:
            _APP = {}
    return _APP

def app_mode():
    return bool(app_cfg()) and not DRY

def token():
    a = app_cfg()
    if not a:
        return None
    try:
        c = json.load(open(TOKF, encoding="utf-8"))
        if c.get("tok") and c.get("exp", 0) - 60 > time.time():
            return c["tok"]
    except Exception:
        pass
    req = urllib.request.Request(BASE + "/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": a["app_id"], "app_secret": a["app_secret"]}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            o = json.loads(r.read().decode())
        if o.get("code") == 0 and o.get("tenant_access_token"):
            tok = o["tenant_access_token"]
            try:
                json.dump({"tok": tok, "exp": time.time() + int(o.get("expire") or 7000)},
                          open(TOKF, "w", encoding="utf-8"))
                os.chmod(TOKF, 0o600)
            except Exception:
                pass
            return tok
        log(f"token-fail code={o.get('code')}")
    except Exception as e:
        log(f"token-err {e}")
    return None

_API_ERROR = threading.local()

def api(method, path, body=None):
    body = sanitize_message_body(body)
    _API_ERROR.value = {}
    tok = token()
    if not tok:
        return None
    o = None
    for attempt in (1, 2):
        req = urllib.request.Request(BASE + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"}, method=method)
        req.add_header("Authorization", f"Bearer {tok}")
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                o = json.loads(r.read().decode())
        except Exception as e:
            _API_ERROR.value = http_failure(e)
            log(f"api-err {method} {json.dumps(_API_ERROR.value)}")
            return None
        code = o.get("code")
        if code == 0:
            return o
        if code in (99991661, 99991663) and attempt == 1:
            try:
                os.remove(TOKF)
            except Exception:
                pass
            tok = token()
            if not tok:
                return None
            continue
        _API_ERROR.value = {"kind": "business", "code": code, "retryable": code in (99991400, 99991661, 99991663)}
        log(f"api-fail {method} code={code}")
        return None
    return None

def upload_image(payload):
    """Upload a queued local image and return its Feishu image key."""
    try:
        image = checked_image(payload['path'], payload['root_dir'], payload['sha256'])
        content = Path(image['path']).read_bytes()
        if hashlib.sha256(content).hexdigest() != image['sha256']:
            raise ValueError('image changed during upload')
    except (OSError, ValueError, KeyError):
        _API_ERROR.value = {'kind': 'invalid_image', 'retryable': False}
        return ''
    boundary = uuid.uuid4().hex
    filename = f'image-{image["sha256"][:12]}{Path(image["path"]).suffix.lower()}'
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="image_type"\r\n\r\n'
            f'message\r\n--{boundary}\r\nContent-Disposition: form-data; name="image"; '
            f'filename="{filename}"\r\nContent-Type: {image["mime"]}\r\n\r\n').encode()
    data = head + content + f'\r\n--{boundary}--\r\n'.encode()
    for attempt in (1, 2):
        access_token = token()
        if not access_token:
            _API_ERROR.value = {'kind': 'token', 'retryable': True}
            return ''
        request = urllib.request.Request(BASE + '/im/v1/images', data=data, method='POST',
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}',
                     'Authorization': f'Bearer {access_token}'})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                result = json.loads(error.read())
            except (ValueError, UnicodeError):
                _API_ERROR.value = http_failure(error)
                return ''
        except Exception as error:
            _API_ERROR.value = http_failure(error)
            return ''
        code = result.get('code')
        if code in (99991661, 99991663) and attempt == 1:
            try:
                os.remove(TOKF)
            except OSError:
                pass
            continue
        if code == 0 and (result.get('data') or {}).get('image_key'):
            return result['data']['image_key']
        _API_ERROR.value = {'kind': 'business', 'code': code,
                            'retryable': code in (99991400, 99991661, 99991663)}
        log(f'image-upload-fail code={code}')
        return ''
    return ''

def app_send(card, root_mid="", delivery_id=""):
    a = app_cfg()
    if card.get('kind') == 'local_image':
        image_key = upload_image(card)
        if not image_key:
            return None
        body = {'msg_type': 'image', 'content': json.dumps({'image_key': image_key}),
                **({'uuid': delivery_id} if delivery_id else {})}
        if root_mid:
            o = api('POST', f'/im/v1/messages/{root_mid}/reply',
                    {**body, 'reply_in_thread': True})
        else:
            o = api('POST', '/im/v1/messages?receive_id_type=chat_id',
                    {**body, 'receive_id': a['chat_id']})
        return ((o or {}).get('data') or {}).get('message_id') or None
    if root_mid:
        o = api("POST", f"/im/v1/messages/{root_mid}/reply",
                {"content": json.dumps(card["card"], ensure_ascii=False),
                 "msg_type": "interactive", "reply_in_thread": True, **({"uuid": delivery_id} if delivery_id else {})})
    else:
        o = api("POST", "/im/v1/messages?receive_id_type=chat_id",
                {"receive_id": a["chat_id"], "msg_type": "interactive",
                 "content": json.dumps(card["card"], ensure_ascii=False), **({"uuid": delivery_id} if delivery_id else {})})
    if o:
        mid = ((o.get("data") or {}).get("message_id")) or ""
        if mid:
            return mid
    return None

def app_update(mid, card):
    return bool(api("PATCH", f"/im/v1/messages/{mid}",
                    {"content": json.dumps(card["card"], ensure_ascii=False)}))

def post(payload):
    _API_ERROR.value = {}
    url = hook_url()
    if not url:
        _API_ERROR.value = {'kind': 'webhook_missing', 'retryable': False}
        return False
    if DRY:
        log("dry-post ok")
        return True
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            body = json.loads(r.read(65536))
        code = body.get('code', body.get('StatusCode'))
        ok = code == 0
        if not ok:
            _API_ERROR.value = {'kind': 'business', 'code': code, 'retryable': code in (99991400,)}
        return ok
    except Exception as e:
        _API_ERROR.value = http_failure(e)
        log(f"webhook-fail {json.dumps(_API_ERROR.value)}")
        return False

def deliver(card):
    if DRY:
        log("dry-deliver ok")
        return True
    if app_mode():
        if app_send(card):
            return True
        log("app-send-fail fallback=webhook")
    return post(card)

# ---------- 后台投递(合并+限速) ----------
class Sender:
    def __init__(self):
        self.q = queue.Queue()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.results = {}
        self.delivery_cursor = None
        self.thread = None
    def outbox(self):
        return Outbox(os.path.join(os.path.dirname(STATE), "delivery.sqlite"))
    def enqueue(self, key, op, mid=None, card=None):
        if not DRY and op in ("create", "hook") and key[0] == "msg":
            h = (_MSGTRY.get(key) or [""])[0]
            # Hash+path makes identity stable even if the scan state checkpoint lags.
            stable = ("hook" if op == 'hook' else "msg", key[1], h or str(key[2]))
            try:
                row = self.outbox().put(stable, card, value_root(key), h)
            except Exception:
                _MSGTRY.pop(key, None)
                _MSGPEND.pop(h, None)
                raise
            if row['status'] == 'sent':
                self._put(key, ('created', row['message_id']))
            elif row['status'] in ('failed','expired'):
                _MSGTRY.pop(key, None)
                _MSGPEND.pop(h, None)
            else:
                self.wake.set()
            return
        self.q.put((key, op, mid, card))
        self.wake.set()
    def replay_due(self):
        if DRY:
            return
        box = self.outbox()
        rows = box.due(limit=20, after=self.delivery_cursor)
        if not rows:
            self.delivery_cursor = None
            rows = box.due(limit=20)
        attempts = 0
        for row in rows:
            STATUS['sender_progress_at'] = time.time()
            self.delivery_cursor = (row['created_at'], row['id'])
            identity = json.loads(row['key'])
            source = identity[1]
            if identity[0] != 'desktop_image' and session_policy(source).get('paused'):
                continue  # Pending messages remain durable until explicitly resumed.
            attempts += 1
            cfg = conf()
            gap = max(0.1, float(cfg.get('api_gap', 0.3)))
            if identity[0] == 'hook':
                gap = max(gap, float(cfg.get('gap', 1.0)))
            time.sleep(gap)
            if identity[0] == 'hook':
                # Webhooks have no API idempotency token/message ID. Keep a local
                # receipt, but never register it as a reply route.
                mid = 'hook_' + row['id'] if post(json.loads(row['payload'])) else ''
            else:
                mid = app_send(json.loads(row["payload"]), row["root"], row["id"])
            error = getattr(_API_ERROR, "value", {}) or {"kind": "network", "retryable": True}
            status = box.finish(row["id"], mid or "", error)
            if mid:
                STATUS["last_sent_at"] = time.time()
                if row["hash"]:
                    # Dedup persistence is handled by harvest on the scanner thread.
                    _MSGPEND.pop(row["hash"], None)
                for key, pending in list(_MSGTRY.items()):
                    if key[1] == json.loads(row["key"])[1] and pending[0] == row["hash"]:
                        self._put(key, ("created", mid))
            else:
                if status == 'failed':
                    _MSGPEND.pop(row['hash'], None)
                    for key, pending in list(_MSGTRY.items()):
                        if key[1] == json.loads(row['key'])[1] and pending[0] == row['hash']:
                            _MSGTRY.pop(key, None)
                log(f"delivery-failed id={row['id']} diagnostic={json.dumps(error)}")
            if attempts >= 4:
                break
        if len(rows) < 20 and attempts < 4:
            self.delivery_cursor = None
        # Counts, not card payloads, are exposed in status.
        summary = box.summary()
        STATUS["delivery_counts"] = summary["counts"]
        STATUS['delivery_recent_failed'] = summary['recent_failed']
        preferences = session_preferences()
        active = [row['oldest_at'] for row in summary['pending_sources']
                  if not session_policy(row['source'], preferences).get('paused')]
        STATUS['delivery_active_oldest_seconds'] = max(0, time.time() - min(active)) if active else 0
        if summary["last_sent_at"]: STATUS["last_sent_at"] = summary["last_sent_at"]
    def take_results(self):
        with self.lock:
            r, self.results = self.results, {}
        return r
    def _put(self, key, val):
        with self.lock:
            self.results[key] = val
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        STATUS.update(sender_alive=True, sender_progress_at=time.time())
        self.thread = threading.Thread(target=self._loop, name='tower-sender', daemon=True)
        self.thread.start()
    def _loop(self):
        last_call = 0.0
        while True:
            STATUS['sender_progress_at'] = time.time()
            self.wake.clear()
            try:
                self.replay_due()
            except Exception as e:
                STATUS["delivery_worker_error_at"] = time.time()
                log(f"outbox-error {type(e).__name__}")
            if self.q.empty():
                self.wake.wait(timeout=1.0)
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                continue
            batch = {item[0]: item}
            while True:
                try:
                    item = self.q.get_nowait()
                    batch[item[0]] = item
                except queue.Empty:
                    break
            for key, (_k, op, mid, card) in batch.items():
                STATUS['sender_progress_at'] = time.time()
                policy = session_policy(key[1])
                if policy.get('paused') or (key[0] in ('seg', 'note') and policy['mode'] != 'full'):
                    continue
                gap = float(conf().get("api_gap", 0.3))
                wait = last_call + gap - time.time()
                if wait > 0:
                    time.sleep(wait)
                last_call = time.time()
                root_mid = value_root(key) if op in ("create",) else ""
                if DRY:
                    self._put(key, ("created", "om_dry_" + hashlib.md5(str(key).encode()).hexdigest()[:8])
                              if op == "create" else ("patched",))
                    continue
                try:
                    if op == "create":
                        nm = app_send(card, root_mid)
                        self._put(key, ("created", nm) if nm else ("create-fail",))
                    elif op == "patch":
                        self._put(key, ("patched",) if app_update(mid, card) else ("patch-fail",))
                except Exception as e:
                    log(f"sender-err {e}")
                    self._put(key, ("create-fail",) if op == "create" else ("patch-fail",))

SENDER = Sender()

def queue_local_images(identity, text, root_dir, root_mid=''):
    if not app_mode() or not root_dir:
        return 0
    count = 0
    for index, image in enumerate(image_references(text, root_dir)):
        SENDER.outbox().put((*identity, index), {'kind': 'local_image', **image}, root_mid)
        count += 1
    if count:
        SENDER.wake.set()
    return count
DRY = os.environ.get("SYNC_DRY") == "1"

_STOPIC = {"mt": 0.0, "d": {}}
def sess_topics():
    """sess_uuid -> root_mid: 任意会话的播报话题(含🖥桌面会话, 由 ctl #track 维护)"""
    try:
        f = f"{STATED}/sess-topics.json"
        mt = os.path.getmtime(f)
        if mt != _STOPIC["mt"]:
            data = JsonMap(f).read()
            _STOPIC.update(mt=mt, d=data)
    except FileNotFoundError:
        _STOPIC.update(mt=0.0, d={})
    except Exception:
        pass
    return _STOPIC["d"]


def reconcile_topic(path, state):
    """A newly watched source must stop patching its old main-chat segment."""
    root = value_root(('seg', path, 0))
    changed = state.get('delivery_root', '') != root
    if changed:
        previous = state.get('seg') or {}
        pending = list(previous.get('roll') or []) if not previous.get('mid') else []
        open_new_seg(state)
        state['seg']['roll'] = pending
        state['pending_seg'] = bool(pending)
        state.pop('prev_seg', None)
        state['sleepy'] = False
        state['delivery_root'] = root
    if root and not state.get('armed'):
        state['armed'] = True
        changed = True
    return changed


def session_preferences():
    try:
        return JsonMap(Path(STATE).with_name('session-settings.json')).read()
    except (OSError, ValueError):
        STATUS['session_settings_error_at'] = time.time()
        return {'__unreadable__': True}


def session_policy(path, preferences=None):
    preferences = session_preferences() if preferences is None else preferences
    if preferences.get('__unreadable__'):
        return {'paused': True, 'mode': 'errors', 'unreadable': True}
    sid, jid = sess_uuid_of(path), jid_of(path)
    value = {**preferences.get(sid, {}), **preferences.get('job:' + jid, {})}
    if conf().get('watched_only') and sid not in sess_topics() and jid not in jobs_raw():
        value['paused'] = True
        value['scope_excluded'] = True
    return {**value, 'mode': value.get('mode') if value.get('mode') in ('full', 'result', 'errors') else 'full'}

def value_root(key):
    """create 投递是否进任务话题: jid 为 jN 取 jobs.root_mid; 否则查 sess-topics 映射"""
    try:
        if not isinstance(key, tuple) or len(key) < 2:
            return ""
        p = key[1]
        jid = jid_of(p)
        if jid.startswith("j") or jid.startswith("cj") or jid.startswith("wj") or jid.startswith("qj") or jid.startswith("dj"):
            rm = (jobs_raw().get(jid) or {}).get("root_mid", "")
            if rm:
                return rm
        if is_claude(p) or is_wb(p) or harness.is_harness(p):
            return sess_topics().get(sess_uuid_of(p), "")
        for u in re.findall(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                            os.path.basename(p)):
            hit = sess_topics().get(u, "")
            if hit:
                return hit
        return ""
    except Exception:
        return ""

def clean(t, n):
    t = t.replace("\r", "").strip()
    return t if len(t) <= n else t[:n] + "…"


def split_message(text, limit):
    """Split all content at paragraph/line boundaries; close/reopen code fences."""
    text = text.replace('\r', '').strip()
    limit = max(256, min(6000, int(limit)))
    if len(text) <= limit:
        return [text]
    raw = []
    start = 0
    while start < len(text):
        end = min(len(text), start + limit - 100)
        if end < len(text):
            boundary = text.rfind('\n\n', start, end)
            if boundary <= start:
                boundary = text.rfind('\n', start, end)
            if boundary > start:
                end = boundary + 1
        raw.append(text[start:end])
        start = end
    parts = []
    opened = None
    for body in raw:
        prefix = (opened[0] + opened[1] + '\n') if opened else ''
        for line in body.splitlines():
            match = re.match(r'^\s{0,3}(`{3,}|~{3,})(.*)$', line)
            if not match:
                continue
            fence, language = match.groups()
            if opened and fence[0] == opened[0][0] and len(fence) >= len(opened[0]) and not language.strip():
                opened = None
            elif not opened:
                opened = (fence[:32], language[:32])
        suffix = ('\n' + opened[0]) if opened else ''
        parts.append(prefix + body + suffix)
    return parts

def fence(s, n=400):
    s = clean(s, n).replace("```", "``")
    return "```\n" + s + "\n```" if s else "(无)"

def extract_cmd(s):
    s = (s or "").strip()
    if s.startswith("{"):
        try:
            a = json.loads(s)
            c = a.get("command")
            if isinstance(c, list):
                return " ".join(map(str, c))
            for key in ("command", "cmd", "path", "url", "query"):
                if isinstance(a.get(key), str):
                    return a[key]
            for v in a.values():
                if isinstance(v, str):
                    return v
        except Exception:
            pass
    return s

def panel(title, md_body, expanded=False):
    return {"tag": "collapsible_panel", "expanded": expanded,
            "header": {"title": {"tag": "markdown", "content": title}},
            "elements": [{"tag": "markdown", "content": md_body}]}

CARD_CONFIG = {"wide_screen_mode": True, "enable_forward": True,
               "update_multi": True}   # W3-17: 全卡统一config

def mkcard(title, template, els, note=""):
    els = list(els)
    if note:
        els.append({"tag": "hr"})
        els.append({"tag": "markdown", "content": note, "text_size": "notation"})
    if not els:
        els = [{"tag": "markdown", "content": " "}]
    return {"msg_type": "interactive", "card": {
        "schema": "2.0",
        "config": dict(CARD_CONFIG),
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "body": {"elements": els}}}

def card(title, template, md_content, note=""):
    return mkcard(title, template,
                  [{"tag": "markdown", "content": (md_content or " ").strip() or " "}], note)

def send_spec(spec, cfg=None):
    cfg = cfg or conf()
    if spec.get("gloss"):
        cmd = extract_cmd(spec.get("cmd", ""))
        lb = label(spec.get("name", "?"), cmd)
        md = f"**{lb}** · `{spec.get('name','?')}`\n{fence(cmd, 500)}"
        title = spec.get("title", f"🔧 {lb}")
    else:
        md = spec.get("body", "")
        title = spec.get("title", "codex")
    parts = split_message(md, cfg['max_len'])
    for index, part in enumerate(parts):
        heading = title + (f' · {index + 1}/{len(parts)}' if len(parts) > 1 else '')
        if not deliver(card(heading, spec.get("template", "blue"), part, spec.get("note", ""))):
            return False
    return True

def roll_lines(roll, cfg):
    kinds = len(roll)
    cap = max(int(cfg["roll_max"]) * 4, 20)
    lines = [f"- **{lb}** `{clean(cmd, 100)}`" + (f" ×{c}" if c > 1 else "")
             for lb, cmd, c in roll[:cap]]
    if kinds > cap:
        lines.append(f"- …另 {kinds - cap} 类")
    return "\n".join(lines)

def flush_roll(p, st, host, cfg):
    """webhook 兜底模式: 聚合进度卡"""
    roll = st.get("roll") or []
    if not roll:
        return
    total = sum(r[2] for r in roll)
    tn = st.get("tn", 0)
    proj = st.get("proj", "?")
    ok = deliver(mkcard(title_of(p, st, "🔧" + card_kind(p)), "wathet", [
        {"tag": "markdown", "content": f"✅ 已执行 **{total}** 项 · {len(roll)} 类"},
        panel(f"📋 命令明细（全 {len(roll)} 类）", roll_lines(roll, cfg),
              expanded=len(roll) <= 3)],
        note_of2(p, st, host)))
    if ok:
        st["roll"] = []
        st["roll_ts"] = 0
    else:
        st["roll_ts"] = time.time() - float(cfg["roll_gap"]) + 8

def read_lines(p, st, size, cfg):
    """从 st['off'] 取完整行; 巨型单行超 big 上限则整行跳过(不死锁)。"""
    ch, big = int(cfg["chunk"]), int(cfg["big"])
    off0 = st["off"]
    with open(p, "rb") as f:
        f.seek(off0)
        data = f.read(min(size - off0, ch))
        if not data:
            return None
        nl = data.rfind(b"\n")
        if nl >= 0:
            out = data[: nl + 1]
            st["off"] = off0 + len(out)
            return out
        if size - off0 <= len(data):
            return None  # 行还在写,下轮再看
        scan = off0 + len(data)
        while scan - off0 <= big:
            f.seek(scan)
            w = f.read(ch)
            if not w:
                return None
            j = w.find(b"\n")
            if j >= 0:
                end = scan + j + 1
                if end - off0 <= big:
                    f.seek(off0)
                    blob = f.read(end - off0)
                    st["off"] = end
                    return blob
                log(f"big-skip {os.path.basename(p)[:36]} line={end - off0}B")
                st["off"] = end
                return b""
            scan += len(w)
        log(f"big-skip {os.path.basename(p)[:36]} >{big}B")
        # W2-10: seek到下一行边界再推进off, 不把残半行首字节留给下一条
        f.seek(scan)
        tail = f.read(65536)
        k = tail.find(b"\n")
        st["off"] = scan + (k + 1 if k >= 0 else len(tail))
        return b""

# ---------- jid 反查(ctl-jobs.json, 5s缓存) ----------
_JIDS = {}; _JIDS_MT = 0.0; _JIDS_T = 0.0
_JOBS_RAW = None; _JOBS_MT = 0.0
_JOBS_LOCK = threading.RLock()
def jobs_raw():
    """Publish a coherent cache snapshot; concurrent readers cannot regress it."""
    global _JOBS_RAW, _JOBS_MT
    with _JOBS_LOCK:
        try:
            mt = os.path.getmtime(JOBSF)
            if _JOBS_RAW is None or mt != _JOBS_MT:
                j = json.load(open(JOBSF, encoding="utf-8"))
                if isinstance(j, dict) and os.path.getmtime(JOBSF) == mt:
                    _JOBS_RAW, _JOBS_MT = j, mt
        except (OSError, ValueError):
            pass  # Keep the last valid snapshot through partial writes/removal.
        return _JOBS_RAW or {}

def jid_map():
    global _JIDS, _JIDS_MT, _JIDS_T
    with _JOBS_LOCK:
        now = time.time()
        if now - _JIDS_T < 5:
            return _JIDS
        jobs = jobs_raw()
        if _JOBS_MT != _JIDS_MT or not _JIDS:
            updated = jid_by_sessfile(jobs)
            _JIDS, _JIDS_MT = updated, _JOBS_MT
        _JIDS_T = now  # Only publish the refresh time after its map is ready.
        return _JIDS

def jid_by_sessfile(jobs):
    """rollout 文件名里的 session uuid -> jid: 读每个 job 的 out_file 前 64KB 找 session_id
    claude 任务(jid以cj开头): sess 即 uuid, sync 用 ~/.claude/projects/*/<sess>.jsonl 直接命中"""
    out = {}
    for jid, jd in (jobs or {}).items():
        f = jd.get("out_file", "")
        sid = jd.get("sess", "")
        if not sid and f and os.path.exists(f):
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    head = fh.read(4000000)
                if jd.get("agent") == "deepseek-harness":
                    for line in head.splitlines():
                        try:
                            event = json.loads(line)
                            if event.get("type") == "session": sid = event.get("sessionId", "")
                        except (ValueError, AttributeError): pass
                m = re.search(r'"(?:session_id|thread_id)"\s*:\s*"([0-9a-f-]{20,})"', head)
                if m:
                    sid = m.group(1)
            except Exception:
                pass
        if sid:
            out[sid.removeprefix("session-") if jd.get("agent") == "deepseek-harness" else sid] = jid
    return out

def jid_of(p):
    if is_claude(p) or is_wb(p) or harness.is_harness(p):
        return jid_map().get(sess_uuid_of(p)) or sid8(p)
    jm = jid_map()
    # fork文件名含两个uuid(parent_child): 任一命中即归属同任务
    for u in re.findall(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", os.path.basename(p)):
        jid = jm.get(u)
        if jid:
            return jid
    return sid8(p)

def custom_title(p):
    """ctl 任务的自定义标题(#new 标题= / #title)"""
    jid = jid_of(p)
    if not re.match(r"^[cwqd]?j\d+$", jid):
        return ""
    try:
        return (jobs_raw().get(jid) or {}).get("title", "")
    except Exception:
        return ""

def card_kind(p):
    if harness.is_harness(p):
        return "🐋"
    """jN=🚀(codex遥控) / cjN=🤖(claude遥控) / wjN=🦞(wcb遥控) / qjN=🧩(Qoder遥控) / 其他桌面=🖥/🦞/🧩"""
    jid = jid_of(p)
    if jid.startswith("cj"):
        return "🤖"
    if jid.startswith("wj"):
        return "🦞"
    if jid.startswith("qj"):
        return "🧩"
    if jid.startswith("j"):
        return "🚀"
    if is_wb(p):
        return "🦞"
    return "🧩" if is_qoder(p) else "🖥"

def title_of(p, st, icon="🤖"):
    """S-2: 标题=图标·任务名·jid (项目/回合号挪到脚注)"""
    task = session_policy(p).get('title') or custom_title(p) or st.get("task") or st.get("proj") or "?"
    return format_title("stream", title_config(f"{CFGD}/ctl.conf"), icon=icon,
                        title=task, jid=jid_of(p), project=st.get("proj", ""),
                        agent={"🚀": "codex", "🤖": "claude", "🦞": "wcodebuddy",
                               "🧩": "qoder", "🐋": "deepseek-harness"}.get(card_kind(p), "desktop"))

def note_of2(p, st, host, extra=""):
    return note_of(p, st, host, f"{st.get('proj','?')} · T{st.get('tn',0)}" + (f" · {extra}" if extra else ""))

def note_of(p, st, host, extra=""):
    return f"{host} · sess {sid8(p)} · {time.strftime('%H:%M:%S')}" + (f" · {extra}" if extra else "")

# ---------- 消息卡 / 工具段卡 ----------
_MSGPEND = {}          # h -> True 在途未确认(W1-2: 防重且不提前占坑.text_sent)
_MSGTRY = {}           # key -> [h, text, tries] 供create-fail重试

def send_msg_card(p, st, text, host, cfg, template='blue', suffix=''):
    base = st.pop("_msgh", "")
    limit = cfg['max_len']
    if not DRY and base:
        try:
            limit = SENDER.outbox().message_limit([os.path.realpath(p), base], text, limit)
        except Exception:
            _MSGPEND.pop(base, None)
            raise
    parts = split_message(text, limit)
    if len(parts) > 1:
        _MSGPEND.pop(base, None)  # Completion is tracked per part, never by part 1 alone.
    for index, part in enumerate(parts):
        st["_msgseq"] = st.get("_msgseq", 0) + 1
        key = ("msg", p, st["_msgseq"])
        h = base if len(parts) == 1 else msg_hash(json.dumps([base, 'part', index]))
        _MSGPEND[h] = True
        _MSGTRY[key] = [h, part, 0]
        heading = title_of(p, st, "💬" + card_kind(p))
        heading += suffix
        if len(parts) > 1:
            heading += f' · {index + 1}/{len(parts)}'
        SENDER.enqueue(key, "create" if app_mode() or DRY else "hook",
                       card=card(heading, template, part, note_of2(p, st, host)))
    if app_mode():
        cwd = codex_session_cwd(p, sess_uuid_of(p))
        if cwd:
            queue_local_images(('image', p, base or msg_hash(text)), text, cwd,
                               value_root(('image', p)))


def source_signals(line):
    """Only native lifecycle/error fields, never classify free text as completion."""
    try:
        value = json.loads(line)
    except (ValueError, TypeError):
        return False, False
    if not isinstance(value, dict):
        return False, False
    payload = value.get('payload') if isinstance(value.get('payload'), dict) else {}
    data = value.get('data') if isinstance(value.get('data'), dict) else {}
    message = value.get('message') if isinstance(value.get('message'), dict) else {}
    kind = value.get('type')
    error = (kind in ('error', 'turn/error') or
             kind == 'result' and value.get('is_error') is True or
             kind == 'event_msg' and payload.get('type') in ('error', 'task_failed') or
             kind == 'turn/end' and data.get('status') in ('failed', 'error'))
    final = (kind in ('result', 'turn/end') or
             kind == 'event_msg' and payload.get('type') == 'task_complete' or
             kind == 'response_item' and payload.get('type') == 'message' and payload.get('channel') == 'final' or
             kind == 'assistant' and message.get('stop_reason') == 'end_turn' or
             kind == 'message' and value.get('channel') == 'final')
    return final, error


def emit_result(path, state, host, cfg, preview=False):
    pending = state.get('result_pending')
    if not pending:
        return False
    state['_msgh'] = pending['identity']
    send_msg_card(path, state, pending['text'], host, cfg,
                  suffix=' · 静默摘要（未确认结束）' if preview else ' · 结果')
    state.pop('result_pending', None)
    return True

def seg_body(p, st, host, cfg, done=False):
    seg = st.get("seg", {})
    roll = seg.get("roll") or []
    total = sum(r[2] for r in roll)
    els = [{"tag": "markdown",
            "content": f"🔧 已执行 **{total}** 项 · {len(roll)} 类"
                       + (" · ✅ 本段完成" if done else " · 🔄 进行中…")}]
    els.append(panel(f"📋 命令明细（{len(roll)} 类）", roll_lines(roll, cfg),
                     expanded=len(roll) <= 3))
    gt = f" · 批{seg.get('gen',0)+1}" if seg.get("gen") else ""
    return mkcard(title_of(p, st, "🔧" + card_kind(p)) + f" · 段{seg.get('no',0)}", "wathet", els,
                  note_of2(p, st, host, f"累计批{seg.get('gen',0)+1}{''}"))

def open_new_seg(st):
    st["seg"] = {"no": (st.get("seg") or {}).get("no", 0) + 1,
                 "roll": [], "mid": "", "closed": False, "ts": time.time(),
                 "gen": st.get("gen", 0)}

def collapse_prev(p, st, host):
    """S-1: 开新段前, 把上一张已定格段卡压成一行历史摘要(话题主屏只见活动卡)"""
    prev = st.get("prev_seg")
    if not prev or not prev.get("mid"):
        return
    total = prev.get("total", 0)
    inner = {"schema": "2.0", "config": dict(CARD_CONFIG),
             "header": {"template": "grey",
                        "title": {"tag": "plain_text",
                                  "content": f"▸ 第{prev.get('no',0)}段 · 已完成 {total} 项操作"}},
             "body": {"elements": [{"tag": "markdown", "text_size": "notation",
                 "content": f"{clean(prev.get('peek',''),60)} …(点话题查看历史卡片原文)"}]}}
    SENDER.enqueue(("seg", p, -prev.get("no", 0)), "patch", mid=prev["mid"],
                   card={"msg_type": "interactive", "card": inner})

def clear_lost_roll(st):
    """W1-3: 未建卡的段被边界/轮换丢弃前, roll并入新段继续投递

    返回 True = 确实搬了活进去。调用方此时不能再清 pending_seg:
    清了就没有建卡触发点, 搬运等于没发生(段卡只靠该标记起卡)。
    """
    seg = st.get("seg") or {}
    if seg.get("mid"):
        return False
    lost = [list(r) for r in (seg.get("roll") or [])]
    open_new_seg(st)
    if not lost:
        return False
    st["seg"]["roll"] = lost
    st["pending_seg"] = True
    return True

def close_seg(p, st, host, cfg):
    """段定格: 有卡则PATCH完成态; 无卡直接丢弃(下条工具事件开新段)"""
    seg = st.get("seg")
    if not seg:
        return
    if seg.get("mid") and not seg.get("closed"):
        roll = seg.get("roll") or []
        st["prev_seg"] = {"mid": seg["mid"], "no": seg.get("no", 0),
                          "total": sum(r[2] for r in roll),
                          "peek": roll[0][0] if roll else ""}
        SENDER.enqueue(("seg", p, seg["no"]), "patch", mid=seg["mid"],
                       card=seg_body(p, st, host, cfg, done=True))
        seg["closed"] = True
        seg["roll"] = []      # W7-1: 定格后 roll 再无读取点(摘要已入 prev_seg), 别留在状态里
        st["pending_seg"] = False
    elif not clear_lost_roll(st):
        # W1-3: 未建卡的roll并入新段继续投递, 不静默蒸发
        # W8: 只有没搬走东西时才清标记, 否则搬进的 roll 永远等不到建卡触发
        st["pending_seg"] = False

# ---------- P2: 发送去重 ----------
_SENT = None
_SENT_LINES = 0                          # .text_sent 现有行数, 决定何时压缩
SENT_KEEP = 4000                         # 去重窗口(条)
SENT_MAX = SENT_KEEP * 2                 # W7-2: 越线才重写整表, 不再每卡重写

def sent_set():
    global _SENT, _SENT_LINES
    if _SENT is None:
        _SENT = set()
        try:
            lines = open(TEXTSENT, encoding="utf-8").read().splitlines()
            _SENT_LINES = len(lines)
            for ln in lines[-SENT_KEEP:]:
                _SENT.add(ln.split(" ", 1)[0])
        except FileNotFoundError:
            pass
        except Exception:
            pass
    return _SENT

def msg_hash(t):
    return hashlib.sha1(t.encode("utf-8", "replace")).hexdigest()[:16]


def event_hash(path, cursor, index=0, identity=None):
    """Stable source event identity; equal text in another event is legitimate."""
    if identity is None:
        stat = os.stat(path)
        identity = [stat.st_dev, stat.st_ino]
    return msg_hash(json.dumps(['event-v2', os.path.realpath(path), identity, cursor, index]))

def compact_sent():
    """W7-2: 摊销式压缩(旧实现每张卡都读+重写 11 万字节, 现在是每 4000 张一次)"""
    global _SENT_LINES
    _SENT_LINES += 1
    if _SENT_LINES <= SENT_MAX:
        return
    try:
        keep = open(TEXTSENT, encoding="utf-8").read().splitlines()[-SENT_KEEP:]
        with open(TEXTSENT, "w", encoding="utf-8") as f:
            f.write("\n".join(keep) + "\n")
        _SENT_LINES = len(keep)
    except Exception:
        pass                          # 压缩失败不回退计数, 下一张卡再试一次

def record_sent(h):
    s = sent_set()
    if h in s:
        return False
    s.add(h)
    try:
        with open(TEXTSENT, "a", encoding="utf-8") as f:
            f.write(f"{h} {int(time.time())}\n")
        compact_sent()
    except Exception:
        pass
    return True

def load_state():
    try:
        d = json.load(open(STATE, encoding="utf-8"))
        if isinstance(d, dict) and d.get("v") == VERSION:
            prune_state(d["files"])
            return d["files"]
        log(f"state-version {d.get('v') if isinstance(d,dict) else 'legacy'} -> reset")
    except Exception:
        pass
    return {}

# W7-1: 这些键写进状态后从未被读取(持久化由局部 dirty 驱动); 老状态里仍带着它们,
#       加载时剪一刀即可, 不靠重置版本号(重置会丢 off 游标 -> 整档重放)
DEAD_ST_KEYS = ("hist", "mdone", "msgs_sent", "dirty", "live_mid", "live_ts")

def prune_state(files):
    """返回清掉的项数: 死键 + 已定格段残留的 roll(定格后无读取点)"""
    n = 0
    for st in (files or {}).values():
        if not isinstance(st, dict):
            continue
        for k in DEAD_ST_KEYS:
            if k in st:
                del st[k]
                n += 1
        seg = st.get("seg")
        if isinstance(seg, dict) and seg.get("closed") and seg.get("roll"):
            seg["roll"] = []
            n += 1
    return n

def save_state(s):
    try:
        private_json(STATE, {"v": VERSION, "files": s})
        return True
    except OSError:
        STATUS['state_write_failed_at'] = time.time()
        log('state-write-failed')
        return False


def _user_text(pp):
    c = pp.get("content")
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict)).strip()
    m = pp.get("message")
    return m.strip() if isinstance(m, str) else ""

_JUNK = re.compile(r"^(继续(吧|干|做[啥什么]*)?|完成下?|确认(一下)?|好的?|ok|行|嗯+|是的?|收到|需要我做什么|\\d+|.{1,3})$", re.I)

def clean_task(t):
    """goal objective / 用户消息 -> 简短任务名"""
    t = re.sub(r"<[^>]+>", " ", t)          # 去内部标签
    for ln in t.splitlines():
        ln = ln.strip()
        ln = re.sub(r"^[\-*#>]+\s*", "", ln)      # 去 markdown 前缀
        ln = re.sub(r"\*\*?", "", ln)
        ln = ln.strip(" :：。.、")
        if len(ln) >= 4 and not _JUNK.match(ln):
            return ln[:18] + ("…" if len(ln) > 18 else "")
    return ""

CATALOG_DB = f"{HOME}/.codex/sqlite/codex-dev.db"
_cat_cache = {}
def catalog_title(sess_uuid):
    """Codex 桌面 App 界面显示的标题 = local_thread_catalog.display_title"""
    if sess_uuid in _cat_cache:
        return _cat_cache[sess_uuid]
    t = ""
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{CATALOG_DB}?mode=ro", uri=True, timeout=1)
        row = con.execute(
            "SELECT display_title FROM local_thread_catalog WHERE thread_id = ?",
            (sess_uuid,)).fetchone()
        con.close()
        if row and row[0]:
            t = re.sub(r"\s*\(\d+\)$", "", row[0].strip())  # 去重名后缀 (3)
    except Exception:
        pass
    if len(_cat_cache) < 2000:
        _cat_cache[sess_uuid] = t
    return t

def sess_uuid_of(path):
    if harness.is_harness(path):
        return harness.session_id(path)
    if is_claude(path) or is_wb(path):
        return os.path.basename(path)[:-6]
    m = re.search(r"rollout-\S*?-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                  os.path.basename(path))
    if m:
        return m.group(1)
    m2 = re.search(r"-([0-9a-f-]{36})", os.path.basename(path))
    return m2.group(1) if m2 else ""

def _find_goal(path):
    """分块扫描(上限32MB)找第一个 <objective>"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            buf = ""
            while True:
                chunk = f.read(4 * 1024 * 1024)
                if not chunk:
                    return ""
                buf += chunk
                i = buf.find("<objective>")
                if i >= 0:
                    j = buf.find("</objective>", i)
                    if j >= 0:
                        raw = buf[i + 11:j].replace("\\n", "\n").replace('\\"', '"')
                        return clean_task(raw)
                if len(buf) > 33554432:
                    return ""
    except Exception:
        return ""

def claude_title(path):
    """claude jsonl 里最新的 aiTitle"""
    t = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                if '"ai-title"' in ln:
                    try:
                        t = (json.loads(ln).get("aiTitle") or t)
                    except Exception:
                        pass
    except Exception:
        pass
    return t.strip()

def claude_proj(path):
    # ~/.claude/projects 与 ~/.qoder-cn/projects 同规则: <转写cwd的slug>/<uuid>.jsonl -> 反解项目短名
    try:
        d = os.path.basename(os.path.dirname(path))
        cwd = d.replace("-", "/")
        return os.path.basename(cwd.rstrip("/")) or d
    except Exception:
        return "?"

def wb_meta(path):
    proj, task = "?", ""
    title = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                if '"ai-title"' in ln and not title:
                    try:
                        title = (json.loads(ln).get("aiTitle") or "").strip()
                    except Exception:
                        pass
                elif proj == "?" and '"cwd":' in ln:
                    try:
                        cwd = json.loads(ln).get("cwd") or ""
                        if cwd:
                            proj = os.path.basename(cwd.rstrip("/"))
                    except Exception:
                        pass
                elif not task and '"role": "user"' in ln.replace('"role":"user"', '"role": "user"'):
                    try:
                        o = json.loads(ln)
                        for x in (o.get("content") or []):
                            t = x.get("text", "") if isinstance(x, dict) else ""
                            t = re.sub(r"<system-reminder.*?</system-reminder>", "", t, flags=re.S)
                            uq = re.search(r"<user_query>(.*?)</user_query>", t, flags=re.S)
                            t = (uq.group(1) if uq else t).strip()
                            if t:
                                task = t
                                break
                    except Exception:
                        pass
                if title and proj != "?" and task:
                    break
    except Exception:
        pass
    return proj, clean_task(title or task) or (title or task)[:18]

def meta_of(path, deep=False):
    if harness.is_harness(path):
        return harness.metadata(path)
    """返回 (proj, task): task=goal objective(优先,深扫) > 首条有效用户消息"""
    if is_wb(path):
        return wb_meta(path)
    if is_claude(path):
        task = ""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for ln in f:
                    if '"type":"user"' not in ln.replace(" ", ""):
                        continue
                    try:
                        o = json.loads(ln)
                    except Exception:
                        continue
                    if o.get("isSidechain"):
                        continue
                    c = (o.get("message") or {}).get("content")
                    txt = ""
                    if isinstance(c, str):
                        txt = c.strip()
                    elif isinstance(c, list):
                        for x in c:
                            if isinstance(x, dict) and x.get("type") == "text":
                                txt = (x.get("text") or "").strip(); break
                    if txt and not txt.startswith("<") and "command-name" not in txt:
                        task = txt
                        break
        except Exception:
            pass
        return claude_proj(path), (clean_task(task) if task else "")
    proj = "?"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            first = f.readline()
        o = json.loads(first)
        cwd = (o.get("payload") or {}).get("cwd") or o.get("cwd") or ""
        proj = os.path.basename(cwd.rstrip("/")) if cwd else "?"
    except Exception:
        pass
    t = catalog_title(sess_uuid_of(path))
    if t:
        return proj, clean_task(t) or t[:18]
    if deep:
        g = _find_goal(path)
        if g:
            return proj, g
    task = ""
    first_real = ""
    try:
        head = open(path, encoding="utf-8", errors="replace").read(2097152)
    except Exception:
        return proj, ""
    for ln in head.splitlines():
        if '"role": "user"' not in ln and '"role":"user"' not in ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        pp = o.get("payload") or {}
        if pp.get("role") != "user" and pp.get("type") != "message":
            continue
        txt = _user_text(pp)
        if not txt:
            continue
        if "codex_internal_context" in txt and "<objective>" in txt:
            m = re.search(r"<objective>(.*?)</objective>", txt, re.S)
            if m:
                t = clean_task(m.group(1))
                if t:
                    return proj, t
        if "<environment_context>" in txt:
            continue
        if not first_real:
            c = clean_task(txt)
            if c and not _JUNK.match(txt.strip()):
                first_real = c
    return proj, first_real

def events_of(line, path=""):
    """yield (kind, text); kind: turn|msg|tool"""
    try:
        o = json.loads(line)
    except Exception:
        return
    if harness.is_harness(path):
        yield from harness.events(o)
        return
    if is_claude(path):
        yield from _claude_events(o)
        return
    if is_wb(path):
        yield from _wb_events(o)
        return
    t = o.get("type")
    if t == "turn_context":
        yield ("turn", "")
        return
    p = o.get("payload") or {}
    if t == "event_msg" and p.get("type") == "task_started":
        yield ("turn", "")
        return
    if t != "response_item":
        return
    pt = p.get("type")
    if pt == "message" and p.get("role") == "assistant":
        parts = [x.get("text", "") for x in (p.get("content") or [])
                 if isinstance(x, dict) and x.get("text")]
        if not parts and isinstance(p.get("message"), str):
            parts = [p["message"]]
        txt = "\n".join(parts).strip()
        if txt:
            yield ("msg", txt)
    elif pt == "function_call":
        name = p.get("name") or "?"
        args = p.get("arguments")
        question = question_text(name, args)
        if question:
            yield 'question', question
            return
        desc = ""
        try:
            a = json.loads(args) if isinstance(args, str) else (args or {})
            if isinstance(a, dict):
                cmd = a.get("command")
                if isinstance(cmd, list):
                    desc = " ".join(map(str, cmd))
                elif isinstance(cmd, str):
                    desc = cmd
                elif isinstance(a.get("cmd"), str):
                    desc = a["cmd"]
                else:
                    for v in a.values():
                        if isinstance(v, str):
                            desc = v
                            break
        except Exception:
            desc = str(args or "")
        yield ("tool", f"{name}|{desc}")

def _claude_events(o):
    """claude projects jsonl: user(纯文本输入)=turn / assistant.text=msg / tool_use=tool"""
    if o.get("isSidechain"):
        return
    t = o.get("type")
    if t == "user":
        c = (o.get("message") or {}).get("content")
        if isinstance(c, str) and c.strip():
            yield ("turn", "")
    elif t == "assistant":
        for c in ((o.get("message") or {}).get("content") or []):
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text" and (c.get("text") or "").strip():
                yield ("msg", c["text"].strip())
            elif c.get("type") == "tool_use":
                name = c.get("name") or "?"
                inp = c.get("input") or {}
                question = question_text(name, inp)
                if question:
                    yield 'question', question
                    continue
                desc = ""
                if isinstance(inp, dict):
                    desc = (inp.get("command") or inp.get("prompt") or inp.get("description")
                            or inp.get("file_path") or "")
                    if not desc:
                        for v in inp.values():
                            if isinstance(v, str):
                                desc = v; break
                yield ("tool", f"{name}|{desc}")

def _wb_events(o):
    """workbuddy jsonl: message(user纯文本)=turn / message(assistant output_text)=msg / function_call=tool"""
    t = o.get("type")
    if t == "message":
        role = o.get("role")
        cont = o.get("content")
        if role == "user" and isinstance(cont, list):
            txts = [x.get("text", "") for x in cont
                    if isinstance(x, dict) and x.get("type") in ("input_text", "text")]
            joined = "\n".join(txts)
            # 剔除 system-reminder 包裹后仍有实文本才算用户指令回合
            stripped = re.sub(r"<system-reminder.*?</system-reminder>", "", joined, flags=re.S)
            uq = re.search(r"<user_query>(.*?)</user_query>", stripped, flags=re.S)
            stripped = (uq.group(1) if uq else stripped).strip()
            if stripped:
                yield ("turn", "")
        elif role == "assistant" and isinstance(cont, list):
            for x in cont:
                if isinstance(x, dict) and x.get("type") in ("output_text", "text"):
                    txt = (x.get("text") or "").strip()
                    if txt:
                        yield ("msg", txt)
    elif t == "function_call":
        name = o.get("name") or "?"
        question = question_text(name, o.get('arguments'))
        if question:
            yield 'question', question
            return
        desc = ""
        try:
            a = json.loads(o.get("arguments") or "{}")
            if isinstance(a, dict):
                desc = (a.get("command") or a.get("cmd") or a.get("path")
                        or a.get("file_path") or a.get("pattern") or "")
                if not desc:
                    for v in a.values():
                        if isinstance(v, str):
                            desc = v; break
        except Exception:
            desc = str(o.get("arguments") or "")[:120]
        yield ("tool", f"{name}|{desc}")

def sid8(path):
    if harness.is_harness(path):
        return harness.session_id(path)[:8]
    b = os.path.basename(path)
    if is_claude(path) or is_wb(path):
        return b[:8]
    m = re.search(r"rollout-\S*?-([0-9a-f]{8})-", b)
    return m.group(1) if m else "?"

CMIDF = f"{STATED}/card-mids.json"
_cmid_cache = {"d": {}, "n": 0}
def register_mid(mid, p):
    if not mid or str(mid).startswith(("om_dry", "hook_")):
        return
    d = _cmid_cache["d"]
    jid = jid_of(p)
    entry = ({"kind": "job", "jid": jid}
             if re.match(r"^[cwqd]?j\d+$", jid)
             else {"kind": "sess", "sid8": sid8(p)})
    if d.get(mid) == entry:
        return
    d[mid] = entry
    _cmid_cache["n"] += 1
    try:
        register_route(CMIDF, mid, entry)
    except Exception:
        d.pop(mid, None)  # Allow the next result to retry persistence.
        STATUS['route_write_failed_at'] = time.time()
        log('route-write-failed')

def harvest_results(state):
    for key, val in SENDER.take_results().items():
        if not isinstance(key, tuple):
            continue
        kind, p = key[0], key[1]
        st = state.get(p)
        if st is None:
            continue
        if kind == "seg":
            seg = st.get("seg") or {}
            if seg.get("no") != key[2]:
                continue                      # 已开新段, 旧段结果作废
            if val[0] == "created" and val[1]:
                seg["mid"] = val[1]
                register_mid(val[1], p)
            elif val[0] == "create-fail":
                seg["pending_create"] = True    # 下轮重试建卡
        elif kind == "msg":
            t = _MSGTRY.pop(key, None) if val[0] != "create-fail" else _MSGTRY.get(key)
            if val[0] == "created":
                register_mid(val[1], p)
                if t:
                    record_sent(t[0])          # W1-2: 确认送达才占去重坑
                    _MSGPEND.pop(t[0], None)
            elif val[0] == "create-fail" and t:
                t[2] += 1
                if t[2] <= 3:
                    SENDER.enqueue(key, "create",   # 原卡重投
                                   card=card(title_of(p, st, "💬" + card_kind(p)), "blue",
                                             t[1],
                                             note_of2(p, st, HOSTN)))
                    log(f"msg-retry {key[2]} try={t[2]}")
                else:
                    _MSGTRY.pop(key, None)
                    _MSGPEND.pop(t[0], None)       # 放弃: 释放占位, 下轮重扫可再来
                    # Failed delivery is never inserted into the sent dedup table.
                    log("msg-card-fail-final")
            else:
                log("msg-card-fail")
        elif kind == "note":
            # W8: 心跳卡原先无人认领其结果。失败只记日志: 不重投(每轮poll重投会打穿API),
            #     sleepy 保持置位, 该会话下次活动后再走一轮静默窗口才会再提醒
            if val[0] != "created":
                log(f"note-card-fail {os.path.basename(p)[:32]}")

_GLOB = {"sig": None, "files": [], "ts": 0.0}

def _scan_files(root, csession, qsession, wbsession, cbsession, test_dir):
    """W2-11: 目录签名缓存 — 集合短期不变则复用, 省每轮5次glob×600+项"""
    now = time.time()
    try:
        sig = (os.path.getmtime(root), os.path.getmtime(csession),
               os.path.getmtime(qsession) if os.path.isdir(qsession) else 0,
               os.path.getmtime(wbsession) if os.path.isdir(wbsession) else 0,
               os.path.getmtime(cbsession) if os.path.isdir(cbsession) else 0)
    except Exception:
        sig = None
    if sig and sig == _GLOB["sig"] and now - _GLOB["ts"] < 5 and not test_dir:
        return _GLOB["files"]
    files = glob.glob(f"{root}/*/rollout-*.jsonl") + glob.glob(f"{root}/*/*/*/rollout-*.jsonl")
    if not test_dir:
        files += glob.glob(f"{csession}/*/*.jsonl")
        files += glob.glob(f"{qsession}/*/*.jsonl")
        files += glob.glob(f"{wbsession}/*/*.jsonl")
        files += glob.glob(f"{cbsession}/*/*.jsonl")
        files += harness.files()
    _GLOB.update(sig=sig, files=files, ts=now)
    return files

def scan_dir(test_dir=None):
    state = load_state()
    start = time.time()
    last_hb = last_save = 0.0
    dirty = False
    host = HOSTN
    root = test_dir or SESS
    cfg = conf()
    seg_mode = bool(cfg["seg_mode"])
    if not (app_mode() or DRY):
        seg_mode = False
    if app_mode() and not bool(cfg["seg_mode"]):
        log("seg_mode=0 但 app 模式: 降级为仅消息卡(工具段不播报)")
    log(f"watcher start v9 app={app_mode()} dry={DRY} seg={seg_mode} test={bool(test_dir)}")
    SENDER.start()
    STATUS.update(sync_alive=True, sync_progress_at=time.time(), sender_required=True)
    while True:
        STATUS['sync_progress_at'] = time.time()
        if STATUS['sender_required']:
            STATUS['sender_alive'] = bool(SENDER.thread and SENDER.thread.is_alive())
            if not STATUS['sender_alive']:
                raise RuntimeError('关键线程退出: sender')
        cfg = conf()
        preferences = session_preferences()
        seg_mode = bool(cfg["seg_mode"]) and (app_mode() or DRY)
        now = time.time()
        if now - last_hb > 5:
            try:
                with open(f"{LOGD}/sync.heartbeat", "w") as heartbeat:
                    heartbeat.write(str(now))
                last_hb = now
            except Exception:
                pass
        harvest_results(state)
        try:
            files = _scan_files(root, CSESS, QSESS, WBSESS, CBSESS, test_dir)
        except Exception:
            files = _GLOB["files"]
        recent = set()
        for p in files:
            try:
                if now - os.path.getmtime(p) < cfg["window_h"] * 3600:
                    recent.add(p)
            except Exception:
                pass
        STATUS['sync_candidates'] = len(files)
        STATUS['sync_active_sources'] = len(recent)
        STATUS['sync_budget_seconds'] = max(120, float(cfg['poll']) * 3)
        for p in recent:
            STATUS['sync_progress_at'] = time.time()
            policy = session_policy(p, preferences)
            if policy.get('unreadable'):
                continue  # Preserve source cursors until policy is readable again.
            try:
                size = os.path.getsize(p)
            except Exception:
                continue
            st = state.get(p)
            if st is None:
                try:
                    birth = os.stat(p).st_birthtime
                except Exception:
                    birth = 0
                off = 0 if birth >= start - 5 else size
                pj, tk = meta_of(p)
                state[p] = st = {"off": off, "proj": pj, "task": tk, "tdeep": bool(tk),
                                 "roll": [], "roll_ts": 0,
                                 "lev_ts": time.time(),
                                 "ev": 0, "has_msg": False, "armed": False,
                                 "seg": None}
                if harness.is_harness(p):
                    st["armed"] = True
                reconcile_topic(p, st)
                if off == size:
                    log(f"init-skip {os.path.basename(p)[:40]}")
                continue
            dirty = reconcile_topic(p, st) or dirty
            if harness.is_harness(p):
                pass  # compressed cursor is handled by read_delta, including replacement
            elif size < st["off"]:
                log(f"rewind {os.path.basename(p)[:40]} {size}<{st['off']}")
                # W2-10: 文件被截断/轮转 -> 快进到EOF重同步(丢旧游标不整档重放, tn/roll不致×2)
                st["off"] = size
                st["roll"] = []
                continue
            if size == st["off"] and not harness.is_harness(p):
                continue
            checkpoint = copy.deepcopy(st)
            try:
                consumed = (harness.read_delta(p, st, baseline="dsh_offset" not in st)
                            if harness.is_harness(p) else read_lines(p, st, size, cfg))
                if consumed is None:
                    continue
                dirty = True
                cursor = checkpoint.get('dsh_offset', checkpoint.get('off', 0))
                stat = os.stat(p)
                identity = [stat.st_dev, stat.st_ino]
                for raw_line in consumed.splitlines(keepends=True):
                    line_cursor = cursor
                    cursor += len(raw_line)
                    ln = raw_line.decode('utf-8', 'replace')
                    if not ln.strip():
                        continue
                    final_signal, error_signal = source_signals(ln)
                    result_texts = []
                    result_identity = None
                    for event_index, (kind, text) in enumerate(events_of(ln, p)):
                        st["lev_ts"] = time.time()
                        st["ev"] = st.get("ev", 0) + 1
                        if kind == "turn":
                            if not policy.get('paused') and policy['mode'] == 'result':
                                # A new user turn is not proof the previous turn completed.
                                emit_result(p, st, host, cfg, preview=True)
                            st["tn"] = st.get("tn", 0) + 1
                            if policy.get('paused') or policy['mode'] != 'full':
                                continue
                            if seg_mode:
                                sg = st.get("seg")
                                if sg and not sg.get("closed") and (sg.get("mid") or sg.get("roll")):
                                    close_seg(p, st, host, cfg)   # 回合边界定格
                            if not seg_mode:
                                if app_mode():
                                    st["roll"] = []
                                else:
                                    flush_roll(p, st, host, cfg)
                        elif kind == 'question':
                            if policy.get('paused'):
                                continue
                            st['_msgh'] = event_hash(p, line_cursor, event_index, identity)
                            send_msg_card(p, st, text + '\n\n在纳管话题发送 #questions 查看原生待回答问题；Codex 桌面与已连接 Host 的 dsh 可回填，其它会话请在原会话回答。',
                                          host, cfg, template='orange')
                        elif kind == "tool":
                            if policy.get('paused') or policy['mode'] != 'full':
                                continue
                            if not cfg["tools"]:
                                continue
                            name, _, desc = text.partition("|")
                            cmd = clean(extract_cmd(desc), 110)
                            lb = label(name, cmd)
                            if seg_mode:
                                if not st.get("armed"):
                                    continue
                                if not st.get("seg") or st["seg"].get("closed"):
                                    collapse_prev(p, st, host)   # S-1: 旧段卡收拢成一行
                                    open_new_seg(st)      # 消息/定格后新事件开新段
                                seg = st["seg"]
                                roll = seg.setdefault("roll", [])
                                if roll and roll[-1][:2] == [lb, cmd]:
                                    roll[-1][2] += 1
                                else:
                                    roll.append([lb, cmd, 1])
                                st["pending_seg"] = True
                            else:
                                roll = st.setdefault("roll", [])
                                if roll and roll[-1][:2] == [lb, cmd]:
                                    roll[-1][2] += 1
                                else:
                                    roll.append([lb, cmd, 1])
                                st["roll_ts"] = time.time()
                                if not app_mode() and len(roll) >= int(cfg["roll_max"]):
                                    flush_roll(p, st, host, cfg)
                        else:  # msg
                            h = event_hash(p, line_cursor, event_index, identity)
                            if policy.get('paused') or policy['mode'] == 'errors':
                                st.pop('result_pending', None)
                                continue
                            if policy['mode'] == 'result':
                                # One native assistant message may contain multiple text
                                # blocks. Keep its whole body, excluding tools/reasoning.
                                result_texts.append(text)
                                result_identity = result_identity or h
                                st['result_pending'] = {'text': '\n\n'.join(result_texts),
                                                        'identity': result_identity, 'at': time.time()}
                                continue
                            if h in sent_set():
                                continue
                            if h in _MSGPEND:          # 在途未确认, 防同轮重复
                                continue
                            st["_msgh"] = h
                            st['last_message_event'] = h
                            st["has_msg"] = True
                            if not st.get("armed") and st["ev"] >= int(cfg.get("arm_events", 2)) \
                                    and st.get("has_msg"):
                                st["armed"] = True
                            if st.get("armed"):
                                _MSGPEND[h] = True
                                seg0 = st.get("seg")
                                if seg0 and not seg0.get("closed") and (seg0.get("roll") or seg0.get("mid")):
                                    close_seg(p, st, host, cfg)   # 消息到达 = 段结束
                                send_msg_card(p, st, text, host, cfg)
                    if error_signal and not policy.get('paused'):
                        st.pop('result_pending', None)
                        st['_msgh'] = event_hash(p, line_cursor, -1, identity)
                        send_msg_card(p, st, '⚠️ 会话报告执行异常，请在原会话检查原因；本提醒不代表已自动重试。', host, cfg, template='red')
                    elif final_signal and not policy.get('paused') and policy['mode'] == 'result':
                        emit_result(p, st, host, cfg)
            except Exception as e:
                # Re-read from the last committed cursor; durable identity dedups any
                # messages already queued before a later record failed.
                state[p] = checkpoint
                log(f"read-fail type={type(e).__name__}")
        # task 空缺时深扫一次(goal块可能在文件深处)
        for p, st in state.items():
            policy = session_policy(p, preferences)
            if policy.get('paused') or policy['mode'] != 'result':
                if st.pop('result_pending', None):
                    dirty = True
                continue
            pending = st.get('result_pending')
            if pending and now - pending['at'] >= max(10, float(cfg['result_idle_seconds'])):
                before = copy.deepcopy(st)
                try:
                    dirty = emit_result(p, st, host, cfg, preview=True) or dirty
                except Exception:
                    state[p] = before
                    STATUS['result_write_failed_at'] = time.time()
        for p, st in state.items():
            if not st.get("task") and not st.get("tdeep") and st.get("armed"):
                st["tdeep"] = True
                pj, tk = meta_of(p, deep=True)
                if tk:
                    st["task"] = tk
                    dirty = True
        # ---- M-2: 静默心跳: armed会话10分钟零事件 -> 一张💤卡(恢复活动自动撤防) ----
        for p, st in state.items():
            policy = session_policy(p, preferences)
            if policy.get('paused') or policy['mode'] != 'full':
                continue
            if not (app_mode() or DRY):
                continue  # Webhooks do not support thread notes/PATCH cards.
            if not st.get("armed"):
                continue
            silent = now - st.get("lev_ts", now)
            if silent > 600 and not st.get("sleepy"):
                st["sleepy"] = True
                SENDER.enqueue(("note", p, 0), "create",
                               card=card(title_of(p, st, "💤" + card_kind(p)), "grey",
                                         f"<font color='grey'>已静默 {int(silent//60)} 分钟"
                                         f"(最后动作 {time.strftime('%H:%M', time.localtime(st.get('lev_ts', now)))})"
                                         f" · 可能卡住或在深度思考,可随时对话/下指令</font>",
                                         note_of2(p, st, host)))
                dirty = True
            elif silent < 30 and st.get("sleepy"):
                st["sleepy"] = False   # 复活复位, 下轮静默可再提醒
                dirty = True
        # ---- 投递阶段 ----
        if seg_mode:
            max_seg = int(cfg.get("seg_max", 25))
            for p, st in state.items():
                policy = session_policy(p, preferences)
                if policy.get('paused') or policy['mode'] != 'full':
                    st['pending_seg'] = False
                    continue
                if not st.get("armed"):
                    continue
                seg = st.get("seg")
                if not seg or seg.get("closed"):
                    continue
                if seg.get("no") >= max_seg:
                    # 批次轮换: 有卡先定格(历史不撕), 计数归零开新批
                    lost = [] if seg.get("mid") else [list(r) for r in (seg.get("roll") or [])]
                    if seg.get("mid"):
                        close_seg(p, st, host, cfg)
                    st["gen"] = st.get("gen", 0) + 1
                    open_new_seg(st)
                    st["seg"]["no"] = 1
                    if lost:
                        # W8: 未建卡的roll跟到新批。旧写法先 clear_lost_roll 搬运、
                        #     再被这里的 open_new_seg 覆盖 -> 命令照样蒸发
                        st["seg"]["roll"] = lost
                        st["pending_seg"] = True
                    else:
                        st["pending_seg"] = False
                    dirty = True
                    log(f"seg-rotate {os.path.basename(p)[:32]} gen={st['gen']}")
                    continue
                if not seg.get("mid"):
                    if seg.get("roll") and (st.get("pending_seg") or seg.get("pending_create")):
                        SENDER.enqueue(("seg", p, seg["no"]), "create",
                                       card=seg_body(p, st, host, cfg))
                        seg["pending_create"] = False
                        st["pending_seg"] = False
                        seg["ts"] = time.time()
                        dirty = True
                elif st.get("pending_seg") and \
                        now - seg.get("ts", 0) >= float(cfg["live_gap"]):
                    SENDER.enqueue(("seg", p, seg["no"]), "patch",
                                   mid=seg["mid"], card=seg_body(p, st, host, cfg))
                    seg["ts"] = time.time()
                    st["pending_seg"] = False
                    dirty = True
                # (v10) 空闲不再切段: 命令卡随 消息/回合 边界关闭, 思考期间的下一条命令并进同卡
        elif app_mode():
            pass  # seg_mode=0: 消息卡已在事件循环直发, 无终身卡逻辑
        else:
            for p, st in state.items():
                policy = session_policy(p, preferences)
                if policy.get('paused') or policy['mode'] != 'full':
                    st['roll'] = []
                    continue
                if st.get("roll") and now - st.get("roll_ts", 0) > float(cfg["roll_gap"]):
                    flush_roll(p, st, host, cfg)
        for k in list(state):
            if k not in recent:
                try:
                    if now - os.path.getmtime(k) > 3 * 86400:
                        state.pop(k)
                        dirty = True
                except Exception:
                    state.pop(k)
                    dirty = True
        if dirty and now - last_save >= float(cfg.get("save_gap", 10)):
            dirty = not save_state(state)
            last_save = now
        if test_dir is not None and now - start > float(os.environ.get("SYNC_TEST_SECS", "25")):
            log("test-mode exit")
            save_state(state)
            return
        time.sleep(cfg["poll"])

def doctor():
    try:
        with open(f"{LOGD}/sync.heartbeat") as heartbeat:
            age = time.time() - float(heartbeat.read().strip())
    except Exception:
        print("DOWN: 心跳文件缺失")
        return 1
    if age > 30:
        print(f"STALE: 心跳 {age:.0f}s 前")
        return 1
    print(f"ALIVE: 同步心跳 {age:.1f}s 前（不代表飞书连接正常）")
    try:
        with open(f"{STATED}/health.json", encoding="utf-8") as snapshot:
            status = json.load(snapshot)
    except (OSError, ValueError):
        print("UNKNOWN: 健康快照缺失/损坏")
        return 1
    counts = status.get('delivery_counts', {})
    print(f"DELIVERY: {counts}（failed为历史累计，近期失败另计）")
    issues = health_issues(status)
    for issue in issues:
        print('WARN: ' + issue)
    return 1 if issues else 0

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--send-test" in args:
        print("app-ok" if app_mode() else ("url-ok" if hook_url() else "url-missing"))
        sys.exit(0 if (app_mode() or hook_url()) else 1)
    if "--doctor" in args:
        sys.exit(doctor())
    if "--card" in args:
        try:
            sys.exit(0 if send_spec(json.load(sys.stdin)) else 1)
        except Exception as e:
            log(f"card-fail {e}")
            sys.exit(1)
    td = None
    for i, a in enumerate(args):
        if a == "--test-dir" and i + 1 < len(args):
            td = args[i + 1]
    try:
        scan_dir(td)
    except KeyboardInterrupt:
        pass
