# 已有 DSH 桌面会话的飞书控制（2026-11-16）

- 已安装桌面 DSH 为 `0.2.2-rc.3`，现有 Host 的 loopback HTTP 入口需认证；已静态核对 `session/prompt`、`session/state`、`session/events` 的请求和回执结构。
- 桥接器仅允许配置中的原会话 ID；新轮提交前核对运行、队列和待回答状态，先保存请求 ID 和游标，再发送 `mode=queue` 的原生图文消息。回复需在后续日志中找到匹配的 `source.rpcId`、助手消息和 `turn/end`，不以 HTTP 成功代替任务完成。
- 同一个飞书绑定可切换 Codex 或 DSH；复用现有群、用户白名单、话题路由、状态命令与退出逻辑。DSH 不继承 Codex 的目录白名单，必须逐条授权。
- 修复 DSH 中文标题缓存读取的 Windows 编码问题；本机已唯一匹配用户指定会话，并配置固定短别名。
- Host 认证地址从本机 `config/dsh-host.json` 读取，忽略规则已添加；不读取 DSH 凭据库、不启动第二个写入进程、不删除会话锁。
- 11 月 17 日复核发现 DSH 模型没有可访问 Host 内存的 cordis 工具，先前要求模型写入配置的操作实际未完成。改由本机小插件在 DSH Host 内生成当前地址，原子写入配置；安装脚本先预检、备份并追加独立条目，DSH 热加载成功，未重启 DSH。
- 真实 Host 认证和指定会话的状态读取通过。由塔台对指定原会话发出一条不调用工具、不改文件的短句，DSH 完成且回复包含预期文字，塔台按 `source.rpcId` 关联回复并发送到飞书，等待状态清零。
- 实测补齐 `session/events` 的 `address.kind=session`；DSH 自动插入的 `time-context` 消息不再被误当作新的用户发话而截断回复。
- 已通过 `test_dsh_control.py` 12 项、`test_dsh_bridge.py` 22 项、`test_desktop_control.py` 29 项、`test_management.py` 40 项、`test_topic_interaction.py` 8 项、Windows DSH 启动测试、`run-windows.ps1 -SelfTest`，以及新增的 `test_dsh_host_export.mjs` 本机发布与字段校验。
- 11 月 17 日用户确认手机收到 DSH 回复，电脑端也能正常使用。复核时塔台仍保留一条空闲 DSH 绑定，等待状态已清零；不能将此快照记作绑定已经退出。解除手机控制仍使用 `#leave`。
