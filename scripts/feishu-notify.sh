#!/bin/bash
# codex 生命周期事件 -> 飞书群卡片推送 (消息流/工具调用由 tower_sync.py 守护负责)
# 原则: fail-open, 永不阻塞 codex, 一切异常 exit 0

CONF="$HOME/.codex/feishu_webhook.txt"
# W6-8: HQ 曾硬编码 ~/agent-hq(已删), 日志/去重缓存/同步脚本三路径集体失效 -> 自我定位
HQ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
[ -n "$HQ" ] && [ -d "$HQ/scripts" ] || exit 0
LOG="$HQ/logs/feishu-notify.log"
SENT="$HQ/state/.sent_cache"
SYNC="$HQ/scripts/tower_sync.py"
PY="$HQ/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
{ [ -n "$PY" ] && [ -f "$SYNC" ]; } || exit 0

input="$(cat 2>/dev/null || true)"
[ -z "$input" ] && exit 0
# 原始载荷捕获 (最近一次, 供调试; 单文件覆写)
if [ "${TOWER_DEBUG_PAYLOAD:-0}" = "1" ]; then
  (umask 077; touch "$HQ/logs/last-input.json"; chmod 600 "$HQ/logs/last-input.json"; printf '%s\n' "$input" > "$HQ/logs/last-input.json") 2>/dev/null
fi

url="$(head -n1 "$CONF" 2>/dev/null | tr -d '[:space:]')"
case "$url" in
  https://open.feishu.cn/*) ;;
  *) exit 0 ;;
esac

norm="$(printf '%s' "$input" | jq -c '
  if has("agent-turn-complete") then
    (.["agent-turn-complete"] | {ev:"turn-complete", tid:(."turn-id"), cwd:.cwd, msg:."last-assistant-message"})
  elif ((.["notify-type"]? // .type? // null) == "agent-turn-complete") then
    {ev:"turn-complete", tid:(."turn-id"), cwd:.cwd, msg:."last-assistant-message"}
  elif (.hook_event_name? == "Stop") then
    {ev:"turn-complete", tid:.turn_id, cwd:.cwd, msg:.last_assistant_message}
  elif has("last-assistant-message") and (.hook_event_name? == null) then
    {ev:"turn-complete", tid:(."turn-id"), cwd:.cwd, msg:."last-assistant-message"}
  elif (.hook_event_name? == "SessionStart") then
    {ev:"session-start", tid:.session_id, cwd:.cwd, src:(.source // .trigger)}
  elif (.hook_event_name? == "UserPromptSubmit") then
    {ev:"prompt", tid:.turn_id, cwd:.cwd, prompt:.prompt}
  elif (.hook_event_name? == "PermissionRequest") then
    {ev:"approval", tid:.turn_id, cwd:.cwd, tool:(.tool_name // .toolName), tinput:((.tool_input // .toolInput // "") | tostring)}
  elif (.hook_event_name? == "SubagentStop") then
    {ev:"subagent", tid:(.agent_id // .turn_id), cwd:.cwd, src:.agent_type}
  else
    {ev:"unknown"}
  end' 2>/dev/null)"

[ -z "$norm" ] && exit 0
ev="$(printf '%s' "$norm" | jq -r '.ev // "unknown"')"
[ "$ev" = "unknown" ] && exit 0

cwd="$(printf '%s' "$norm" | jq -r '.cwd // empty')"
tid="$(printf '%s' "$norm" | jq -r '.tid // empty')"
proj="?"
[ -n "$cwd" ] && proj="$(basename "$cwd" 2>/dev/null)"
host="$(hostname -s 2>/dev/null)"
now_h="$(date '+%H:%M:%S')"
note="$host · $now_h · $(printf '%s' "$tid" | cut -c1-8)"

# ---- 去重 ----
key="${ev}:${tid}:${cwd}"
hash="$(printf '%s' "$key" | shasum 2>/dev/null | cut -c1-16)"
now="$(date +%s)"
if [ -n "$hash" ] && [ -f "$SENT" ]; then
  last="$(grep "^$hash " "$SENT" 2>/dev/null | tail -n1 | cut -d' ' -f2)"
  last="${last:-0}"
  if [ $((now - last)) -lt 90 ]; then exit 0; fi
fi
{ echo "$hash $now" >> "$SENT" 2>/dev/null; awk -v n="$now" '$2 > n-600' "$SENT" > "$SENT.tmp" 2>/dev/null && mv "$SENT.tmp" "$SENT"; } 2>/dev/null

trunc() { printf '%s' "$1" | sed 's/```/``/g' | head -c "$2"; }

spec=""
case "$ev" in
  turn-complete)
    msg="$(printf '%s' "$norm" | jq -r '.msg // ""')"
    hb="$HQ/logs/sync.heartbeat"
    if [ -f "$hb" ] && [ $(( $(date +%s) - $(stat -f %m "$hb" 2>/dev/null || echo 0) )) -lt 90 ]; then
      spec="$(jq -nc --arg p "$proj" --arg n "$note" \
        '{title:("✅ codex 回合完成 · " + $p), template:"green", body:"流式同步在线, 本回合内容已逐条推送", note:$n}')"
    else
      spec="$(jq -nc --arg p "$proj" --arg n "$note" --arg b "$msg" \
        '{title:("✅ codex 回合完成 · " + $p), template:"green", body:$b, note:$n}')"
    fi ;;
  session-start)
    src="$(printf '%s' "$norm" | jq -r '.src // "startup"')"
    spec="$(jq -nc --arg p "$proj" --arg n "$note" --arg s "$src" \
      '{title:("🚀 codex 任务开始 · " + $p), template:"blue", body:("来源: " + $s), note:$n}')" ;;
  prompt)
    p="$(printf '%s' "$norm" | jq -r '.prompt // ""')"
    spec="$(jq -nc --arg p "$proj" --arg n "$note" --arg b "$(trunc "$p" 500)" \
      '{title:("📥 codex 新指令 · " + $p), template:"blue", body:$b, note:$n}')" ;;
  approval)
    tool="$(printf '%s' "$norm" | jq -r '.tool // "?"')"
    tin="$(printf '%s' "$norm" | jq -r '.tinput // ""')"
    spec="$(jq -nc --arg p "$proj" --arg n "$note" --arg t "$tool" --arg c "$tin" \
      '{title:("🔔 等待审批 · " + $p), template:"orange", gloss:1, name:$t, cmd:$c, note:$n}')" ;;
  subagent)
    at="$(printf '%s' "$norm" | jq -r '.src // "agent"')"
    spec="$(jq -nc --arg p "$proj" --arg n "$note" --arg a "$at" \
      '{title:("🧩 codex 子代理结束 · " + $p), template:"grey", body:$a, note:$n}')" ;;
  *) exit 0 ;;
esac

[ -z "$spec" ] && exit 0
out="$(printf '%s' "$spec" | "$PY" "$SYNC" --card 2>&1)"; rc=$?
echo "$(date '+%F %T') ev=$ev card-rc=$rc $out" >> "$LOG" 2>/dev/null

sz="$(wc -c < "$LOG" 2>/dev/null || echo 0)"
[ "${sz:-0}" -gt 200000 ] && tail -n 200 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"

exit 0
