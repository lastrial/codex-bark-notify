# codex-bark-notify

把 Codex 的本轮完成和等待回答事件推送到 Bark。通知标题为执行主机名，正文使用聊天的真实名称。

| 事件 | 正文 | 铃声 |
| --- | --- | --- |
| 主聊天一轮回复结束 | `<任务名> 已完成` | `calypso` |
| 异步问题卡片被应用接受 | `<任务名> 等待你回复` | `alarm` |

“已完成”表示本轮回复结束。提问通知不包含问题正文。Skill 提供安装、启停和排查说明；自动通知由 Codex 的 `notify` 和 `PostToolUse` 事件触发。

## 当前状态

- 原 macOS 版本的 48 项自动化测试通过，覆盖发送结果、并发去重、停用、超时和配置保留。加入 Windows 适配后的 macOS 回归仍待实机运行。
- Windows 的 15 项隔离测试通过，覆盖文件权限、锁、进程清理、回调转发、管理器和 PowerShell Hook 命令。
- 已验证本地 Codex 结束回调，以及既有本地 Work 聊天通过工具继续回复时的结束回调。
- 使用已完成事件手动测试发送器，Bark API 接收成功，手机显示的标题和内容正确。
- 提问 Hook 已在隔离 Codex 0.160.0 中验证。真实桌面自动提问推送和 `alarm` / `calypso` 的手机播放仍待验收；新增 Hook 必须先通过 `/hooks` 信任。

详见 [验证记录](docs/validation.md)。

## 环境

当前源码提供 macOS 和 Windows 本机实现，要求 Python 3.9+、已登录且支持 app-server 的 Codex，以及已有 Bark 设备密钥。Windows 使用 Win32 文件权限、共享锁、Job Object 进程清理和有期限的管道读取，详见 [Windows 操作和边界](docs/windows.md)。

默认 Codex 路径为桌面应用捆绑的可执行文件：

```text
/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex
```

不同安装路径可通过 `--codex /absolute/path/to/codex` 指定。Windows 桌面自动回调、手机到达及声音需要单独验收；远程主机和云端 Work 尚未验收。

## 安装 Skill

将本仓库的 `skills/bark-task-notify` 目录复制到 `~/.codex/skills/bark-task-notify`。已有同名安装时先核对版本，避免直接覆盖正在使用的运行文件。

然后让 Codex 使用 `bark-task-notify` skill 安装和管理，或执行下面的命令。

## 启用完成通知

准备一个只含 Bark 设备密钥的 UTF-8 文件，不能带换行。文件必须属于当前用户，POSIX 权限为 `0600`，Windows DACL 仅允许当前用户访问，且不是符号链接或硬链接。密钥不应写入仓库、聊天、命令参数或日志。

```sh
python3 ~/.codex/skills/bark-task-notify/scripts/manage.py enable \
  --key-file /absolute/path/to/private-key-file

python3 ~/.codex/skills/bark-task-notify/scripts/manage.py status
```

配置默认使用 `~/.codex/config.toml`，运行目录为 `~/.local/share/bark-task-notify`。重新启用时可省略已保存的密钥文件引用。

Windows 默认运行目录为 `%LOCALAPPDATA%\bark-task-notify`，使用 `python` 并明确传入当前桌面应用的 `--codex` 绝对路径。

管理器保留已有的 `notify` 回调。目前只接受首行 JSON 兼容的单行 `notify` 数组；其他格式会报告限制，需先检查后调整。

## 启用等待回答通知

完成通知安装后执行：

```sh
python3 ~/.codex/skills/bark-task-notify/scripts/manage_questions.py enable
python3 ~/.codex/skills/bark-task-notify/scripts/manage_questions.py status
```

进入 Codex CLI 的 `/hooks`，审阅并信任对应的 `PostToolUse` 定义。管理器显示 `configured` 只代表配置完成。Codex 会跳过未信任的新增或变更 Hook，详见 [官方说明](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)。

当前仅接入 `request_user_input_async` 返回布尔 `accepted: true` 的问题卡片。同步提问、普通文字问句和权限审批未接入。问题可能在通知到达前已被回答。

已运行的聊天可能保留此前配置；首次验收应使用已加载新配置的聊天，分别核对自动触发、手机显示和声音。

## 停用

```sh
# 只停用提问通知
python3 ~/.codex/skills/bark-task-notify/scripts/manage_questions.py disable

# 停止两类发送，并恢复本功能拥有的 notify 配置
python3 ~/.codex/skills/bark-task-notify/scripts/manage.py disable
```

停用保留去重数据库。发送结果未知时不会自动重发；不要删除历史记录来重试，以免重复通知。`sent` 表示 Bark API 接受请求，手机到达需要另外核对。

## 开发与测试

实现仅使用 Python 标准库。测试使用临时文件及模拟元数据、HTTP，不需要真实密钥，也不会发送 Bark：

```sh
python3 -B -m unittest discover -s tests -p 'test_*.py'
```

Windows 隔离测试使用 `python -B -m unittest discover -s tests -p test_windows_native.py -v`。原有 POSIX 测试依赖 `fcntl`，需在 POSIX 主机运行。

- [设计说明](docs/design.md)
- [验证记录与支持边界](docs/validation.md)
- [Skill 操作说明](skills/bark-task-notify/SKILL.md)

灵感来自 CodexResetAlarm。本仓库保存任务通知功能，额度重置提醒另行维护。
