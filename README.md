# Agent Tower Windows

[English README](README.en.md)

> **平台状态：** 在 macOS 原版基础上增强了 Windows 支持。Windows 已适配 Codex、DSH 任务控制、指定桌面会话接管和飞书图文交互，macOS 继续使用 `launchd`。Windows 安装及验证范围见下文。

本项目基于 [wowofighting/agent-tower](https://gitee.com/wowofighting/agent-tower)（Apache-2.0）修改，重点是 Windows 支持与 DSH 桌面接管。上游项目只支持 macOS；上游在 GitHub 的镜像为 [zouxuyang/agent-tower](https://github.com/zouxuyang/agent-tower)，改动说明见 [NOTICE](NOTICE)。

本项目同时发布在两个地方，内容一致：GitHub（[electric-muse/agent-tower-windows](https://github.com/electric-muse/agent-tower-windows)）与 Gitee（[june-zheng/agent-tower-windows](https://gitee.com/june-zheng/agent-tower-windows)）。

把飞书变成 Codex / Claude Code / WorkBuddy / Qoder CLI / DeepSeek Harness 的**统一指挥塔台**：从手机发指令起任务、盯进度、收结果。

单进程双循环：`tower.py` 一个守护同时跑「遥控」与「播报」两条循环线，launchd 只注册一个服务。

可选独立看门狗：`scripts/tower_watchdog.py` 每分钟检查健康快照和ctl/sync/sender进度，超过300秒且连续两次确认才重启原服务，重启冷却600秒。探测间隔超过180秒视为可能主机睡眠，唤醒后重新确认；单次接口失败或断线不触发重启。只依赖原进程内线程无法覆盖整个进程不推进的情况，因此看门狗由独立launchd任务运行。它不重投失败记录，也不改动来源游标。

安装看门狗属于额外服务注册，需明确授权后执行；仅提交模板不会启用。生成方式：`.venv/bin/python scripts/render_launchd.py <目标plist路径> --watchdog`，再使用`launchctl bootstrap gui/<uid> <目标plist路径>`注册。非恢复检查可运行`.venv/bin/python scripts/tower_watchdog.py --label gui/<uid>/com.<user>.agent-tower`；仅模板中的`--recover`允许自动重启。

## 快速开始（Windows）

```powershell
# GitHub
git clone https://github.com/electric-muse/agent-tower-windows.git
# 或 Gitee（国内更快）
git clone https://gitee.com/june-zheng/agent-tower-windows.git

cd agent-tower-windows

# 1. 飞书应用: open.feishu.cn 建企业自建应用, 拿 app_id/app_secret;
#    开通 im:message / im:chat / im:message.group_msg 和长连接事件订阅, 把机器人拉进群
# 2. 本机装 Python 3.14、uv、Node.js 和 Codex CLI, 完成 Codex 登录
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-windows.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-windows.ps1 -Start
```

启动后在飞书群发 `#help`，按回复把 `open_id` 填进 `config/ctl.conf`、`chat_id` 填进 `config/feishu-app.json`，再重启塔台。完整步骤、桌面接管和安全边界见 [Windows + 飞书手机端](#windows--飞书手机端)。

## 快速开始（macOS）

```bash
# GitHub
git clone https://github.com/electric-muse/agent-tower-windows.git ~/agent-tower-windows
# 或 Gitee
# git clone https://gitee.com/june-zheng/agent-tower-windows.git ~/agent-tower-windows

cd ~/agent-tower-windows

# 1. 飞书应用: open.feishu.cn 建企业自建应用, 拿 app_id/app_secret;
#    开通权限 im:message / im:chat / im:message.group_msg, 事件订阅选"长连接", 把机器人拉进群
cp config/feishu-app.example.json config/feishu-app.json   # 填入凭据(chat_id可先留空)

# 2. 一键安装(建venv + 生成配置 + 注册launchd自启服务)
./scripts/install.sh

# 3. 群内发 #help, 然后从日志取自己的 open_id 填入 config/ctl.conf 的 allow_ou= 再跑一次 install.sh
tail logs/ctl.log
```

## Windows + 飞书手机端

Windows 上可运行 Codex、DeepSeek Harness (dsh) 遥控任务和飞书消息播报。
飞书客户端就是手机控制端，无需另装小程序。
Windows 登录自启需单独注册；手动启动时 PowerShell 窗口需保持运行。
macOS 使用 launchd；接管已有 Harness 会话的 CLI 锁转移方式未移植到 Windows，
桌面 DSH 可通过下述 Host 方式控制。
Windows 启动脚本使用虚拟环境的依赖和同版本的基础 Python 运行守护进程，避免仓库目录被标为
低完整性级别时，dsh 无法写入本机 headless profile。不要直接用 `.venv\Scripts\python.exe`
启动塔台。

1. 安装 Python 3.14、uv、Node.js 和 Codex CLI，并在本机完成 Codex 登录及 dsh headless 配置。
2. 在 PowerShell 执行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-windows.ps1`。
   脚本在 `.dsh-runtime` 安装已验证的 dsh `0.1.7-rc.2`，不改动全局 dsh；随后生成本机配置。
   默认只允许当前仓库的父目录中的项目，并设为 `safe` 权限、关闭自动纳管。
   如需控制其他工作目录，在 `config/ctl.conf` 的 `work_roots` 中增加绝对路径。
3. 在飞书开放平台创建企业自建应用，开启机器人、`im:message`、`im:chat` 和
   `im:message.group_msg`（获取群组中所有消息，敏感权限）及长连接事件订阅/卡片回调；
   将机器人加入目标群。仅开通 `im:message.group_at_msg` 时，群内普通消息不会推送给机器人。
   将真实 `app_id`、`app_secret` 填入 `config/feishu-app.json`，`chat_id` 暂留空；保留该文件在本机。
4. 执行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-windows.ps1 -Start`。
   在飞书群发 `#help`，从机器人的未授权回复读取自己的 `open_id` 和群 `chat_id`。
   将 `open_id` 填入 `config/ctl.conf` 的 `allow_ou`，`chat_id` 填入
   `config/feishu-app.json` 并重启塔台。在群里点“新建任务”，选 Codex 或 DeepSeek Harness，
   再选项目；下一条普通消息就是自然语言任务。
   任务话题内直接回复可继续指令。也可用 `#new <项目名> 任务内容`、
   `#dnew <项目名> 任务内容`、`#jobs` 和 `#stop <任务号>` 验证。

可选 Windows 登录自启：先运行 `powershell -NoProfile -ExecutionPolicy Bypass -File
scripts/register-windows-autostart.ps1 -WhatIf` 预检；单独授权后去掉 `-WhatIf` 才会注册
`AgentTower` 计划任务。任务仅在当前用户登录时启动，使用 `scripts/run-windows.ps1`
和基础 Python；脚本拒绝覆盖同名任务。注册不会立即启动第二个塔台进程。

### 从手机接管已有 Codex 桌面任务

在 `config/ctl.conf` 的 `attach_sessions` 中加入要接管的完整会话 UUID，或确保该会话的
工作目录位于 `work_roots`。电脑端须运行 Codex；目标会话尚未加载时，塔台会通过
本机会话链接自动打开并限时等待。飞书授权用户在 `feishu-app.json`
指定的控制群里发送
`#attach <桌面任务完整标题>`，也可直接说「我要控制 Codex 里<标题>的任务」。
可在 `desktop_aliases` 中把短名称映射到 `attach_sessions` 里的会话 UUID，
例如 `desktop_aliases={"学习":"<会话UUID>"}`；随后可发 `#attach 学习` 或「进入学习」。
「进入<完整标题>」也支持唯一匹配的显式授权会话；不会模糊匹配或自动选中重名任务。
`#sessions` 只列出已授权的桌面会话，`#status` 显示当前用户在控制群的绑定及待回传状态。
塔台只绑定唯一命中的本机桌面任务；重名时可用完整 UUID。绑定后，在群主聊天或新建的
任务话题直接发普通中文，消息进入原 Codex 会话，最终回复回到发话的位置。发送
`退出控制` 或 `#leave` 解除绑定。原会话运行中时不插入消息，等当前轮结束后再发。
绑定后也可在控制群主聊天或该任务的专用控制话题直接发单张截图；塔台将飞书图片
作为原 Codex 会话的图片输入，支持 PNG/JPEG/GIF，单张上限 10 MB。图片保存在本机
`state/inbound-images/` 运行态目录以供该会话读取；未绑定或发在其他话题的图片不会
送入 Codex。飞书应用需具备图片资源读取权限。
若上一条指令仍在等待回复，普通 `#leave` 会提示等待；明确放弃该次回传才使用 `#leave force`。
`#go jN` 只续跑塔台新建任务，不能进入已有桌面任务。
普通文本和飞书富文本中的文字使用相同控制入口；例如复制为代码样式的 `#leave`
也能识别，富文本同时携带 `content` 和 `content_v2` 时仅解析一次。

### 从手机接管已有 DSH 桌面任务

保持 DSH 桌面应用运行。在 `config/ctl.conf` 的 `dsh_attach_sessions` 中逐条指定
获准控制的完整 `session-UUID`，可在 `desktop_aliases` 中配置同样的固定别名。
DSH Desktop 可运行 `scripts/install-dsh-host-export.ps1 -WhatIf` 预检；授权安装后去掉
`-WhatIf`，脚本备份并向本机 Desktop profile 追加独立插件条目。插件由 DSH Host 自己
生成当前 loopback 认证地址，原子写入 `config/dsh-host.json`，DSH 重启后自动更新。
安装不会重启 DSH 或改变已有任务。插件源码为 `scripts/dsh-host-export.mjs`，仅使用
Node.js 内置模块；它不读取凭据库，也不输出地址。卸载时移除 profile 中相应插入条目即可。
也可手动填写 Host 提供的地址，结构参考 `config/dsh-host.example.json`，重启后需更新。
真实文件已忽略；地址包含启动令牌，不要提交或发到群中。仅支持经过认证的本机 HTTP Host。

在控制群发送 `#attach DSH <完整标题>`、`#attach <session-UUID>`，或配置好别名后说
「进入<别名>」。绑定后可发中文、截图，`#status` 查看状态，`#leave` 退出。
`#sessions` 同时列出获准的 Codex 和 DSH 会话。DSH 忙碌或等待原生问题时拒发新指令。
此方式通过 DSH 现有 Host 的 `session/prompt` 发到原会话，按持久请求 ID 核对回复；
退出只解除飞书绑定，DSH 桌面端继续使用原会话，不启动 CLI 续跑进程或改动会话锁。

Windows 离线验证：`.venv\Scripts\python.exe scripts\test_platform.py`、
`.venv\Scripts\python.exe scripts\test_windows_dsh.py`，以及
`$env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python.exe scripts\test_wave5.py`。
完整 `run_tests.py` 套件包含 macOS 专用断言，需在 macOS 上运行。

`config/stream.conf` 的 `watched_only=1` 只播报手动 `#watch <会话ID>` 已纳管的会话
和塔台创建的任务；其它会话不再向控制群主聊天播报。`auto_watch=0` 只关闭自动建话题，
单独使用仍会播报其它活跃会话。手动纳管只发送之后的新进度，停机期间的历史消息不会自动补发。

Codex 回复中使用 `![说明](图片路径)` 引用 PNG/JPEG/GIF 时，塔台会把工作目录内、
不超过 10 MB 的本机图片作为独立图片消息送到同一飞书话题；每条回复最多 4 张。
图片上传需要飞书应用开通 `im:resource:upload` 并发布生效。目录外、远程链接、
找不到的文件和其它格式只保留文字路径，不上传。图片投递进入 `#deliveries` 队列，
失败时可按投递 ID 使用 `#retry-delivery`，不会补发以前未纳管的历史图片。
手机飞书的群主聊天只展示话题回复预览；要查看图片，请点根卡下方的话题行进入完整话题。

飞书账号本身没有 `app_id` 和 `app_secret`。用该账号登录[飞书开放平台](https://open.feishu.cn/)，
进入开发者后台创建“企业自建应用”；在应用的“凭证与基础信息”中找 App ID、App Secret。
在“应用能力”开启机器人，在“权限管理”添加上面的消息/群权限，在“事件订阅”选择长连接并订阅
`im.message.receive_v1`，同时开启卡片回调。创建版本并发布到所在企业后，把机器人加入手机上的目标群。
`im:message.group_msg` 是接收话题内普通文字所必需的敏感权限，可能需要企业管理员审批。
企业若限制自建应用，需管理员审批。不要把 App Secret 发到聊天里，只填本机配置文件。

## 仓库结构

```
scripts/
  tower.py           唯一入口: 主线程=遥控循环, daemon线程=播报循环
  tower_ctl.py       遥控: #new/#cnew/#wnew/#qnew 起任务, #go 续跑, #goal 目标环, 话题舱交互
  tower_sync.py      播报: 监听 codex/claude/qoder/workbuddy 会话 jsonl → 流式卡片进飞书话题
  feishu-notify.sh   Codex 生命周期钩子(可选, 配合 ~/.codex/hooks.json)
  install.sh         一键安装/更新(幂等, macOS)
  setup-windows.ps1  Windows 引导(建 venv + 生成配置 + 装 dsh 运行时)
  run-windows.ps1    Windows 守护进程启动脚本
  register-windows-autostart.ps1  可选: 注册登录自启计划任务
  tower_platform.py  平台差异(路径/进程/服务注册)
  tower_desktop_control.py  Codex 桌面会话接管
  tower_dsh_control.py      DSH 桌面会话接管
  tower_dsh.py             DSH headless 适配
  dsh-host-export.mjs      DSH Host 连接导出插件(可选)
config/
  *.example          配置模板(入库) — 安装时复制为同名实例文件
  ctl.conf           本机实例(不入库): 白名单/权限/CLI路径
  stream.conf        本机实例(不入库): 播报调参
  feishu-app.json    本机实例(不入库): 飞书应用凭据 ★密钥
launchd/
  agent-tower.plist.example   服务模板(__HOME__/__USER__占位)
state/  logs/        运行态(不入库)
```

> 设计约定：**入库的都是模板与代码，含个人信息/密钥的实例文件一律不入库**，换机器 clone 后跑 install.sh 重新生成。

## 命令速查（飞书群内）

| 命令 | 作用 |
|---|---|
| `#new <目录> 提示词` | 起 🚀Codex 任务 |
| `#cnew <目录> 提示词` | 起 🤖Claude 任务 |
| `#wnew <目录> 提示词` | 起 🦞WorkBuddy 任务 |
| `#qnew <目录> 提示词` | 起 🧩Qoder CLI 任务（模型用 `#model` 换） |
| `#go <jid> 指令` | 续跑追加指令（话题舱内可直接发文字） |
| `#attach <标题、别名或UUID>` / `#leave` | 进入 / 退出已有 Codex 桌面会话 |
| `#sessions` / `#status` | 已授权桌面会话 / 当前用户绑定与待回传状态 |
| `#goal <jid> 目标` | 目标环模式（自动续跑直到完成/熔断） |
| `#stop` `#jobs` `#model` `#title` | 中断 / 列表 / 换模型 / 改名 |
| `#scan` `#watch` `#unwatch` `#clean` | 纳管游离会话 / 清理 |
| `#help` / `#help all` | 常用 / 全量命令 |
| `权限=safe` 令牌 | 单任务降级沙箱（默认 full） |

## 运维

`#unwatch` 会保存主动忽略状态，自动扫描不会重新建舱；手动 `#watch` 可恢复。
`#scan` 展示完整会话 UUID；短 ID 有歧义时需使用完整 ID，不会随意选中会话。
`#autowatch status` 显示真实配置门槛及最近扫描统计；Harness 压缩文件门槛可独立配置 `dsh_auto_watch_min_kb`（默认50）。

纳管成功后立即启用后续播报；已有主会话进度卡保留为历史，新增进度进入话题。
建舱失败不会将扫描按钮标记为成功，原按钮可重试。用户在话题发送命令时，控制台反馈也回复在该话题。
已知提问工具的问题和选项会单独提醒。纳管话题内发送 `#questions` 查看当前原生问题，然后点选项或直接发送答案；也可用 `#answer <会话ID> <问题令牌> 答案`。多题逐行填写 `1=答案`、`2=答案`，多选题用逗号分隔编号。

原生回填支持仍在运行的本机 Codex 桌面会话，以及已连接 Host 的 dsh 会话。dsh 需在守护进程环境中设置 `TOWER_DSH_URL` 为现有 Host 的 loopback 认证地址；该地址含启动令牌，勿写入仓库或消息。没有 Host 的 dsh 终端会话、其它引擎仍需在原会话回答。`#watch` 不授予启动、续跑或中断原会话的权限。

首次直接发文字但尚未展示问题时，只展示问题并要求重新回答；进程重启后同样需要重新绑定。旧问题令牌失效时不发送答案。只有原生答案记录确认一致才提示成功；dsh 的后台继续状态可能仅确认排队。原生交互的验证边界见 [测试说明](docs/testing/README.md)。
实时双向交互需在使用者自己的授权环境中验证。

`#queue` 查看命令排队、处理中和中断数量。相同飞书消息/卡片事件重复送达不会重复执行。
重启时未完成命令标为 interrupted，不自动重放；先查看任务状态再发新指令。
身份库不保存提示词；旧事件缺少事件 ID 时仅走原内存队列，无法提供跨重启防重保证。

```bash
launchctl kickstart -k gui/$(id -u)/com.$(id -un).agent-tower   # 改码后重启
./scripts/install.sh                                             # 升级(git pull后重跑)
tail -f logs/ctl.log logs/sync.log                               # 双循环日志
.venv/bin/python scripts/tower.py --selftest                     # 自检
```

## 依赖

- macOS + launchd、Python ≥3.12、lark-oapi / requests / setproctitle（install.sh 自动装）
- 被管 Agent CLI：`codex`（exec/resume）、`claude`（-p/--resume）、WorkBuddy 内置 codebuddy CLI、
  `qoderclicn`（`npm i -g @qodercn-ai/qoderclicn`）。Qoder 的登录态本机实测可直接复用（凭据在
  `~/.qoder-cn/.auth/user`），先自查一次再决定是否登录：`qoderclicn -p hi -o json` 返回
  `is_error:false` 即可用；若提示 `Not logged in` 才需 `qoderclicn login` 一次
- 别在 Qoder 会话里直接起 `qoderclicn`：会带上 `QODER_AGENT_SDK_ENTRYPOINT`，CLI 报
  `sdk_invalid_args` 而退出。塔台由 launchd 拉起，环境干净，不受影响
- 只读纳管不需要 CLI 可执行文件：Qoder 会话只要在 `~/.qoder-cn/projects/` 有转录就能播报

## 自定义任务/话题标题

适用于塔台创建的四类任务（`jN` / `cjN` / `wjN` / `qjN`）：

```text
#new <白名单内项目目录> 标题="登录页 回归测试" 检查登录页
#qnew <白名单内项目目录> 标题=接口排障 定位接口错误
#title qj1 新的任务标题（支持空格）
```

- 创建向导的下一条消息也可携带 `标题=...`、`权限=safe`、`模式=plan`；带空格的标题用引号包裹。
- 在已关联话题内发 `#title 新标题` 会自动补当前任务号，也可点击根卡的 **🏷 修改标题** 后回复新标题。
- 改名会保存任务标题并 PATCH **原有话题根卡**；后续播报使用新标题。PATCH 失败会明确提示“待自动同步”，轮询会重试（失败后约 30 秒退避），不会为改名再建一个话题。
- “话题标题”在本项目中特指机器人任务根卡标题；不修改群名称，也不承诺修改客户端自行生成的话题摘要。已有历史播报不批量改写。
- 外部纳管/桌面会话仍是只读播报，`#title` 不修改其原生会话名称。

`config/ctl.conf` 可配置以下字段，默认保持既有标题顺序：

```ini
title_max_len=24
topic_title_template={icon} {jid} · {title}
stream_title_template={icon} {title} · {jid}
```

可用变量：`{icon}`、`{jid}`、`{title}`、`{project}`、`{agent}`。`agent` 是引擎标识，
如 `codex`、`claude`、`wcodebuddy`、`qoder`；外部 Codex 会话使用 `desktop`。
标题长度配置限制在 8–80 字符，默认 24；非法模板回退默认，不阻断播报。
配置热加载作用于新根卡、改名更新的根卡和后续播报，不主动批量修改已有卡片。
根卡模板同时用于遥控任务和外部只读观察；观察备注不修改原生会话名称。

## 离线回归与升级边界

```bash
.venv/bin/python scripts/run_tests.py
```

测试器复制代码和配置模板到临时目录，替换 `HOME`/`TMPDIR`，启用 `SYNC_DRY=1`，
并阻止测试 Python 进程连接网络。不会复制本机飞书凭据或运行态。
测试包括统一入口与循环调度、目录白名单、鉴权/频控、五引擎标题路由、卡片改名、停止/重试、
轮次退出码隔离及原有九组回归。执行后自动清理沙箱。

安装器需要 `uv`，每次运行都会按 `requirements.txt` 检查/安装直接依赖；现有配置不覆盖。
launchd 配置根据实际 checkout 路径生成，不再要求仓库必须位于 `~/agent-tower`。
`--selftest` 校验本地必要配置/依赖，失败返回非零；它**不等于飞书在线鉴权或真实群回调通过**。
CLI 是按使用场景可选的，自检列出的 CLI 不存在并不阻止只读纳管启动。

代码修改不会热替换已运行的守护进程。确认升级并重启后，再在指定测试群验证真实消息、按钮回调和原有话题根卡更新。
走查与已知验证边界见 `docs/testing/README.md`。

## DeepSeek Harness 纳管与读写控制（2026-10-02）

- 自动发现 `~/.dsh/sessions/*/*/session.v4.jsonl.zstd`，从 v4 会话头识别项目，
  从可选的 session cache 读取标题；不读取 Harness 凭据文件。
- 使用 `#scan` 查看近期会话、`#watch <会话ID前8位>` 收进飞书话题，图标为 🐋。
  沿用 `auto_watch`、活跃窗口和大小门槛；压缩会话的大小门槛按**压缩后文件大小**计算。
  较小会话可手动 `#watch`，无需降低所有来源的自动纳管门槛。
- 只播报新增助手正文、回合边界、工具名称；不播报 reasoning、原始 stream、工具参数/结果。
  首次接入跳过历史，重启后按持久化解压游标继续；分批解压，避免大文件长期阻塞扫描。
- 普通 `#watch`/自动纳管以观察为主，可按上述条件回答原生问题；使用下方显式控制命令才授予 Tower 对该任务的控制权。
  旧版 `session.jsonl.zstd`、seeded/子会话不纳管。`#unwatch` 只解除观察话题路由，
  不关闭来源播报，也不停止已经通过 `#dnew`/`#dresume` 创建的遥控任务。
- Harness 根卡和后续播报均支持既有标题模板，`{agent}` 为 `deepseek-harness`，
  `{jid}` 为观察会话短 ID 或遥控任务号 `djN`。观察标题取首次发现/纳管时快照；
  `#title djN 新标题` 更新遥控根卡并保留操作按钮。
- Python 3.14+ 使用标准库 zstd；较旧 Python 使用 requirements 中的 `backports.zstd`。

### 遥控命令

| 命令 | 行为 |
|---|---|
| `#dnew <项目或白名单目录> [标题=名] [权限=safe\|full] 指令` | 通过本机 `dsh headless` 新建，任务号 `djN` |
| `#dresume <会话前8位> [权限=safe\|full] 指令` | 显式接管已关闭/未被占用的 v4 会话；不强抢桌面会话 |
| `#go djN 指令` / 遥控话题内直接回复 | 续跑同一原始 session，保留上下文 |
| `#stop djN` / `#jobs djN` / `#title djN 名称` | 停止 Tower 自己的进程组、查看结果、修改标题 |

- 从“新建任务 → 🐋 DeepSeek Harness”也可选择项目、输入指令；同项目新建保持 Harness 引擎。
- 使用本机 `dsh_bin`（可配置，默认从 PATH 或 `~/.npm-global/bin/dsh` 查找）。启动始终校验
  `work_roots`、授权用户、限流与并发上限；任务文本走 stdin，避免命令行选项注入与进程参数泄露。
- 权限沿用 `default_perm`（现有默认 full），可显式 `权限=safe` 切换 workspace-write；
  `模式=plan` 传 read-only，`模式=goal` 使用现有有界目标循环。以上通过 `DSH_PERMISSION_MODE`
  交给 Harness 官方沙箱执行；不要自行修改 headless profile 绕过沙箱设置。
- headless 没有远程审批 UI；safe/plan 下需额外审批的操作可能失败/等待，Tower 超时保护仍生效。
  不会为了通过审批自动提升权限。
- 接管预检只读取现有 `session.lock` 并尝试非阻塞锁；Harness 自身跨进程写锁是最终裁决。
  **不删除锁，不并发写桌面会话，不手改历史会话日志。** CLI 确认 session 后才建立遥控路由；
  未成功接管的请求不会抢走原观察会话的播报。
- 本机验证的 CLI 是 `0.1.7-rc.2`。`--session-id` 只接受已有会话，不用于新建指定 ID。
  不支持的 preset/派生会话仍由 CLI 拒绝。`#model djN` 明确拒绝且不打断任务，模型由本机
  headless profile 决定，不伪造不存在的 CLI 模型参数。
- launchd 不继承交互 shell 的 PATH；适配器会发现 Homebrew Node.js（含 node@24），
  也可配置 `dsh_node_bin` 指向自定义 Node.js。缺失显式配置会拒绝启动。
- 进程输出仅保留 session ID、最终助手正文、安全阶段状态和固定词表失败分类；原始 reasoning、工具参数和 stderr 不落盘。
  CLI 失败详情因此不会原样回传；认证/profile 问题需在本机诊断。

## 投递恢复与运行诊断

| 命令 | 含义 |
|---|---|
| `#health` | WebSocket、扫描/投递线程进度、命令等待、纳管统计、当前故障和历史失败 |
| `#deliveries` | 持久化队列计数、最近失败消息 ID、HTTP/业务错误码 |
| `#retry-delivery <完整32位ID>` | 仅重排失败消息，保留原话题和幂等 ID；不会重发已确认成功记录 |

应用模式的**助手消息卡**先写入 `state/delivery.sqlite` 再投递。网络错误、429、5xx 等
采用退避，单轮最多尝试 4 次；不可重试错误立即保留为 failed，供人工检查、补发。
成功后清除队列中的卡片正文，保留身份用于去重；失败不会再写入“已发送”集合。
队列写入失败会回退来源游标，避免磁盘满时读过消息却没有持久化待发送记录。
工具段 PATCH、话题建卡和旧 webhook 不属于这个持久化消息队列。

队列与新状态文件为 0600；启动时 state/logs 目录为 0700。SDK 新日志按完整行脱敏并轮转，
HTTP 诊断只留机器错误码/请求 ID；默认不保存通知原始载荷。
必要时可临时设置 `TOWER_DEBUG_PAYLOAD=1`，但该文件包含敏感内容，调试后应自行清理。
**已有历史日志/原始载荷不会被自动改写或删除**；不要将它们上传到仓库或工单。

`--doctor` 不再仅靠线程心跳判定健康；缺少/过期健康快照、WS 未确认连接、低磁盘或失败投递
均返回非零。WS 状态来自 SDK 连接/断开日志证据，不等同于端到端成功送达；本次进程曾发生的
状态写入错误会保留告警。队列只恢复启用本功能后持久化的消息，不能恢复历史已经丢失的正文。
同一幂等 ID 用于降低不确定响应造成的重复风险，不承诺跨服务端幂等窗口的严格 exactly-once。


## 统一面板、失败分类与保留策略（2026-10-03）

- `#jobs` 或 `#board` 打开统一面板；`#board control|watch|errors [页码]` 过滤遥控、观察或异常任务。
  每页 6 项，支持刷新和翻页；按钮依据任务能力显示，观察任务不会获得写控制权限。
- Harness 根卡展示初始化、执行工具、等待输入/本机审批、退避、完成、停止和失败。
  `status_card_gap=30` 限制状态 PATCH 频率（最小 10 秒），状态不变不反复推送。
- Harness 仅网络、限流、上游暂时错误自动重试，退避 60/120 秒，最多两次。
  认证、配额、占用、权限、会话不兼容、未知错误均不自动重试。stderr 只在内存分类，
  卡片显示固定恢复建议，不回传供应方原文，也不自动提升权限。
- 可选 `dsh_config_patch=/absolute/path/to/cordis.patch.yml` 复用已有本机配置覆盖，
  不复制密钥；留空仍使用 headless 默认值。headless 和 desktop 的模型配置可能不同。
  该路径只允许本机配置，不开放聊天参数；改变供应方或计费入口前自行确认。
- `#retention` 仅预览；`#retention apply <确认码>` 才删除预览中的过期数据。
  默认任务输出保留 30 天、失败投递正文保留 14 天，分别由 `retention_job_days` /
  `retention_failed_days` 配置。确认绑定用户和聊天，10 分钟有效，候选变化必须重新预览。
  每批每类最多 250 项；文件修改时间也必须超期。运行/待重试/活跃目标任务、源会话、
  凭据、pending 投递和去重身份不删除。失败正文清空后保留 expired 墓碑，不能再补发。
- 大型 Harness zstd 会话保留增量解码器，每 tick 最多解压 2 MiB、处理 256 行，
  完整行才提交游标。重启从已知 frame 边界恢复，不扫描压缩 magic；未知/异常游标重新建立
  只读基线。单个巨大 frame 的首次恢复仍可能需要跨多个 tick 解码，不能保证随机访问。

真实模型调用验收需显式执行 `scripts/verify_harness_live.py --execute`；它创建独立工作目录、
会话和存储，只写固定测试文件，默认清理；`--profile-patch` 可指定已有本机配置，
`--keep` 仅供后续验收，使用后须手动删除该次返回的隔离目录。
具体结果及浏览器验证限制见 [测试说明](docs/testing/README.md)。

### 观察管理与播报模式

- `#watch` / `#watch list [页码]`：分页查看纳管及主动忽略记录。
- `#observe <完整UUID或唯一短ID> status|title 新备注|pause|resume`：管理只读观察。暂停不停止原会话；暂停期间的新消息不补播，原有持久待投递消息恢复后继续。
- `#observe <会话ID或任务号> mode full|result|errors`，或 `#broadcast <会话ID或任务号> result`：选择完整、仅结果或异常提醒。默认完整。
- 仅结果模式按来源原生结束信号发送最后助手正文；缺少结束信号时，静默60秒后显示“静默摘要（未确认结束）”，不能据此认为任务完成。异常模式仅发送固定异常提醒。
- 观察根卡支持详情、暂停/恢复、模式与修改备注；备注更新原根卡，失败保留待同步状态。观察不会获得遥控权限。
- 长助手正文按段落分卡保留全文，每片独立恢复；`#queue` 查看命令等待。重启中断的入站命令不自动重放。

运行诊断将历史失败数量与当前故障分开：最近5分钟投递/状态异常、超过300秒的未暂停来源积压或命令等待会告警；控制、来源扫描与投递线程默认120秒无进度告警。旧失败记录保留供 `#deliveries` 定位，不会永久拉低 `doctor` 状态。线程退出会交由守护重启；进度超时只告警，避免自动重放命令。观察根卡同步失败每30秒退避，周期轮询重试；较旧PATCH响应不会清除新备注的待同步状态。

旧 webhook 模式的助手消息也通过持久投递队列，遵循暂停与三种模式，并完整分卡；每片成功后保存本地确认，不创建回复路由。webhook不支持服务端幂等 token，响应不确定时重试仍可能重复，应用模式使用原有幂等投递。分卡长度在首次事件落盘时冻结，热改 max_len 不会改变已开始投递事件的分片身份。新用户回合不能证明旧回合完成，没有原生结束信号的旧正文只作为“未确认结束”摘要。

## 许可

Apache-2.0，见 [LICENSE](LICENSE)。本项目基于 [wowofighting/agent-tower](https://gitee.com/wowofighting/agent-tower) 修改，改动清单见 [NOTICE](NOTICE)。
