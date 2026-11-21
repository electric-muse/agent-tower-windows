#!/bin/bash
# agent-tower 一键安装/更新: 建venv、生成配置模板、注册launchd服务
# 幂等: 已有配置不覆盖; 可重复执行(升级后同步plist)
set -euo pipefail
HQ="$(cd "$(dirname "$0")/.." && pwd)"
TAG="com.$(id -un).agent-tower"
PLIST="$HOME/Library/LaunchAgents/$TAG.plist"

echo "== agent-tower @ $HQ"

# 1. venv / 依赖同步（已有 venv 也检查缺失依赖）
command -v uv >/dev/null || { echo "需要 uv: brew install uv"; exit 1; }
if [ ! -x "$HQ/.venv/bin/python" ]; then
  uv venv "$HQ/.venv" --python 3.12 2>/dev/null || uv venv "$HQ/.venv"
fi
uv pip install -p "$HQ/.venv/bin/python" -r "$HQ/requirements.txt"
echo "venv OK"

# 2. 配置模板(不覆盖已有)
for f in ctl.conf stream.conf; do
  [ -f "$HQ/config/$f" ] || { cp "$HQ/config/$f.example" "$HQ/config/$f"; echo "生成 config/$f (请按需编辑)"; }
done
[ -f "$HQ/config/feishu-app.json" ] || echo "!! 缺 config/feishu-app.json: 复制 feishu-app.example.json 并填入飞书应用凭据"
mkdir -p "$HQ/logs" "$HQ/state"

# 3. 自检
"$HQ/.venv/bin/python" "$HQ/scripts/tower.py" --selftest

# 4. launchd 注册(旧标签自动迁移)
"$HQ/.venv/bin/python" "$HQ/scripts/render_launchd.py" "$PLIST"
for suffix in agent-tower agent-hq-ctl agent-hq-sync codex-feishu-ctl codex-feishu-sync; do
  old="com.$(id -un).$suffix"
  launchctl bootout "gui/$(id -u)/$old" 2>/dev/null || true
done
launchctl bootout "gui/$(id -u)/$TAG" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$TAG" || true
sleep 3
launchctl print "gui/$(id -u)/$TAG" | grep -E "state|pid" | head -2
echo "== 完成: 服务 $TAG 已注册(开机自启). 群内发 #help 验证"
