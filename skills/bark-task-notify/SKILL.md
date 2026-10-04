---
name: bark-task-notify
description: 管理 Codex 每轮回复结束和异步问题卡片出现时的 Bark 推送，支持安装、状态检查、停用及排查。使用执行主机名和真实聊天名称，不用于 Codex 用量或额度提醒。
---

# Bark 任务通知

用随附的 `scripts/manage.py` 管理通知。自动推送由 Codex 的 `notify` 结束事件触发；安装后无需每轮调用 skill，也不要在最终回复前手动发送。

等待回复提醒由 `scripts/manage_questions.py` 单独管理，使用异步问题工具的成功事件。

## 行为与范围

- 每个符合条件的主聊天轮次最多发起一次推送。标题是 `socket.gethostname()`，正文是元数据中的聊天名称原文加 ` 已完成`，铃声为 `calypso`。
- “已完成”表示这一轮回复结束。子代理、未知来源、读取不到名称的事件跳过。
- 支持当前 Mac 上经过验证的本地 Codex 桌面通知入口，以及既有本地 Work 聊天通过工具继续回复的路径。Windows 提供本机管理器和发送器，要求 Python 3.9+、NTFS 私有权限以及支持 app-server 的 Codex。Windows 代码测试不等同于桌面回调或手机验收。Work 的本地与云端执行路径须分别验证，不能仅凭使用本机工具就宣称支持。
- 保留已有的 `notify` 回调并原样传递事件。只接受首行 JSON 兼容的单行 `notify` 数组；遇到其他配置格式先报告限制，不自动重写整份配置。

## 操作

先定位本 skill 的绝对路径。命令中的路径均须展开为当前主机的真实路径；不要复制另一台主机的路径。

```text
python3 <skill>/scripts/manage.py status --config <config.toml> --runtime <runtime>
python3 <skill>/scripts/manage.py enable --config <config.toml> --runtime <runtime> --codex <codex-executable> --key-file <private-key-file>
python3 <skill>/scripts/manage.py disable --config <config.toml> --runtime <runtime>
python3 <skill>/scripts/manage.py test --config <config.toml> --runtime <runtime> --codex <codex-executable> --thread-id <completed-thread-id> --turn-id <completed-turn-id>
```

通常配置位于 `~/.codex/config.toml`，POSIX 运行目录使用 `~/.local/share/bark-task-notify`，Windows 使用 `%LOCALAPPDATA%\bark-task-notify`。Windows 命令使用 `python`；始终展开参数为真实绝对路径。桌面环境优先使用该应用捆绑的 Codex 可执行文件，以减少版本差异。Windows 默认只在 `%LOCALAPPDATA%\OpenAI\Codex\bin` 下发现唯一安全候选时选择 `codex.exe`；存在多个版本时明确传入 `--codex`。

用户要求启用自动 Bark 通知时，可在已授权范围内完成安装和验证。`test` 会真实推送：只对用户授权测试、且已确认结束的事件使用它；不要编造事件 ID。先检查状态，避免重复测试已处理事件。

密钥通过已有私有文件提供，配置仅保存文件路径。密钥文件须为当前用户所有的普通文件、无符号链接或硬链接；POSIX 权限为 `0600`，Windows DACL 仅允许当前用户访问，可继承私有父目录的等效权限。Windows 拒绝空 DACL、其他主体的允许 ACE、重解析路径、UNC、设备路径及备用数据流。内容为无换行的 UTF-8 密钥。不要在聊天、命令参数或日志中输出密钥。没有可用文件时，让用户在本机私下配置，避免要求把密钥粘贴到聊天。

启用后检查状态和原回调保留情况。已运行的聊天可能仍使用此前加载的配置；用用户授权的新一轮聊天验证实际回调。不要为了加载配置自行重启桌面应用。

若检测到旧观察器包装，应先确认其配置归属，再用观察器自己的管理器停用后启用本功能；安装失败则恢复原观察器。保留旧转发脚本，供仍使用旧配置的会话执行原回调。

## 结果和排查

- `sent`：Bark API 接受请求，手机是否显示仍需用户核对。
- `rejected`：服务明确拒绝。
- `unknown`：可能已送达，无法确定；不会自动重发。
- `skipped`：来源、名称、激活状态或其他检查不满足发送条件。
- `processing`：已取得该事件的发送资格；遗留记录超时后转为 `unknown`。

所有已有记录均不自动重试。停用或重新启用保留去重数据库；不要通过删除记录或改变 ID 重试，否则可能重复推送。当前版本没有手动重试命令。

停用会先关闭发送，再恢复本功能拥有的配置项。若配置被其他工具修改，保留它并报告冲突。状态、日志仅检查事件 ID、时间和固定结果码，不采集原始回调、回复正文或标题。

交付时区分代码测试、真实结束回调、Bark API 接收与手机显示四种证据，并明确实际验证过的 Codex / Work 执行路径。

## 等待回复提醒

用户要求提问提醒时，在已经安装的完成通知运行目录上使用：

```text
python3 <skill>/scripts/manage_questions.py enable --hooks <hooks.json> --runtime <runtime> --codex <codex-executable>
python3 <skill>/scripts/manage_questions.py status --hooks <hooks.json> --runtime <runtime> --codex <codex-executable>
python3 <skill>/scripts/manage_questions.py disable --hooks <hooks.json> --runtime <runtime> --codex <codex-executable>
```

默认 hook 文件是 `~/.codex/hooks.json`。管理器只增删自己拥有的定义，保留其他 hooks。若有同层 inline hooks 或其他配置冲突，先检查现状，不覆盖整个配置。

Windows hook 提供 `commandWindows`，通过 PowerShell 编码命令和单引号参数调用 Python，保留标准输入。必须另外确认实际 Codex 版本的执行器支持此定义；命令隔离测试和管理器的 `configured` 状态不能证明原生 Hook 已受信任。

- 接收 `PostToolUse` 的 `request_user_input_async` 成功结果，要求 `accepted` 为布尔 `true`。
- 标题仍为主机名，正文为聊天名称原文加 ` 等待你回复`，铃声为 `alarm`。完成通知使用 `calypso`。
- 每个提问调用单独去重；同一轮后续新提问可以再次提醒，完成通知的状态保持独立。
- 子代理 hook 的 session ID 可能是父聊天。存在 `agent_id` 或 `agent_type` 字段即跳过，并要求 transcript 文件名末尾 UUID 与 session ID 一致；只检查路径，不读取对话内容。
- 提问被应用接受后可能很快得到回答，因此该事件不能证明推送到达手机时问题仍未作答。
- 当前范围为异步问题卡片。同步 `request_user_input`、普通文字问句、权限审批和云端 Work 的提问入口尚未支持。

新增或修改的 hook 需要通过 Codex 原生 `/hooks` 流程检查和信任；配置成功不代表已经获得信任或开始自动推送。完成可审查的安装和测试后再处理这一步，沿用用户已有授权。不要直接改写信任记录或启用全局信任绕过。[官方说明](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)

单独停用提问通知会关闭其激活状态并移除拥有的 hook；停用完成通知也会阻止提问发送。两种操作均保留去重数据。真实验收需在已加载新配置且 hook 获得信任的聊天中提出一张问题卡片，确认自动收到 `alarm` 提醒；不要用人工调用 hook 的结果冒充自动触发。
