# 飞书截图进入原 Codex 会话（2026-11-16）

- 原实现只读取飞书消息体中的 `text`，纯图片消息只有 `image_key`，因此在入队前被丢弃。
- 已用控制群已有的用户图片消息验证资源读取 API，返回 JPEG，约 181 KB；本次没有把图片内容写入日志或仓库。
- 新路径在授权用户已绑定原桌面会话时，按飞书消息 ID 与图片键下载图片；限制 10 MB 和 PNG/JPEG/GIF，保存在私有运行态目录，向同一 Codex 会话发送带 `text_elements: []` 的文字项和 `localImage` 项。
- 未绑定、错误话题及不支持的图片不会进入 Codex；下载失败会在控制群给出明确回复。离线测试覆盖事件入队、格式限制、图片输入、绑定路由与错误话题拒发。
- 补送用户此前发到控制群、当时被旧代码忽略的一张截图后，原 Codex 会话的最新用户消息包含 `text` 与 `localImage`；Codex 正确识别出测试截图中的题目内容，并完成回复。
- 塔台清除待回传状态，控制群出现该任务的回复卡；用户确认手机能看到识图回复。退出控制后绑定为 0，用户再次切出并点回原 Codex 任务，确认电脑端可直接打开。
- 适用离线验证：`test_desktop_control.py` 29 项、`test_management.py` 40 项、`test_media.py` 5 项、`test_topic_interaction.py` 8 项及 Windows `-SelfTest` 通过。较宽的 `test_delivery_harness.py` 仍有既有 Windows 文件替换、权限位和健康文案编码断言失败。
- 实际飞书纯图片入站事件在本次部署后尚未由手机重新发送；接收层使用 SDK 的 `message_type=image` 与 `image_key` 结构，已用离线事件测试覆盖。图片下载及 Codex 图文输入、回传则已使用真实飞书图片验证。
