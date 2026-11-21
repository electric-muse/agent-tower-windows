# Codex 桌面会话重新进入故障（2026-11-16）

- 用户从飞书向“示例学习助手升级”发送文本后，原会话后台完成且飞书绑定清空，但 Codex 客户端重新打开该聊天显示“ChatGPT 遇到了问题”；重启客户端可暂时恢复。
- 受控复现“页面联调一”后，客户端本地 Sentry 面包屑记录 `TypeError: Cannot read properties of undefined (reading 'length')`，首帧为桌面资源 `907538.0572f09d40.js:119:40124`。
- 已安装客户端该位置的解析函数读取文本输入项的 `text_elements.length`；塔台发送的文本输入项此前缺少 `text_elements`，客户端原生发送路径会提供该数组。
- 桥接层现对文本输入提供 `text_elements: []`，回归测试锁定此结构。修复后需重启当前已进入错误页的 Codex 客户端，再做飞书发送、退出和桌面重新进入的现场验证。
