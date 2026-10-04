# Windows 本机支持

使用 Python 3.9+ 和支持 `app-server --listen stdio://` 的 Codex。默认配置是 `%USERPROFILE%\.codex\config.toml`，默认运行目录是 `%LOCALAPPDATA%\bark-task-notify`。明确传入当前桌面应用捆绑的 `codex.exe` 路径；自动发现仅接受唯一候选。

```powershell
python <skill>\scripts\manage.py enable --config <config.toml> --runtime <runtime> --codex <codex.exe> --key-file <private-key-file>
python <skill>\scripts\manage.py status --config <config.toml> --runtime <runtime> --codex <codex.exe>
python <skill>\scripts\manage_questions.py enable --hooks <hooks.json> --runtime <runtime> --codex <codex.exe>
python <skill>\scripts\manage_questions.py status --hooks <hooks.json> --runtime <runtime> --codex <codex.exe>
```

命令中的路径应为绝对路径。管理器沿用首行 JSON 兼容 `notify` 数组的契约；其他位置或格式需要先准备可还原的配置调整，并确认 TOML 含义保持一致。回调首参数须为本机 `.exe` 或 `.com` 可执行文件；Python、PowerShell 等脚本应作为对应解释器的参数，避免批处理文件隐式经过 `cmd.exe`。

私有文件通过 `CreateFileW` 句柄检查当前用户所有者 SID、磁盘普通文件、单一硬链接和无重解析属性。DACL 必须非空且只允许当前用户；仅继承私有父目录当前用户权限的文件也可接受。祖先目录通过不共享删除的句柄固定，拒绝 junction、UNC、设备路径和备用数据流。新文件和目录在创建时获得当前用户专用 ACL。配置替换保留原 DACL，并检查原内容与文件身份；替换前刷新文件内容。Windows 普通目录句柄不支持 POSIX 目录 `fsync`，因此不宣称断电后的目录持久性。

发送器用同一字节范围的 `LockFileEx` 共享锁，启停管理器用排他锁，均有期限。每个元数据或 HTTP 子进程先进入带 `KILL_ON_JOB_CLOSE` 的 Job Object，再通过一字节 bootstrap 门开始执行；分配失败时关闭子进程。Job 关闭会清理已退出主进程留下的后代。工作进程保持原有 10 秒工作预算和清理余量，管道读取用 `PeekNamedPipe` 轮询截止时间。后台工作进程与通知回调分离；原回调用直接 argv 调用，继承标准流并返回其退出码。

提问 hook 使用 `commandWindows` 和 PowerShell `-EncodedCommand`。编码内容通过单引号转义后的独立参数调用 Python，标准输入仍传给 hook。管理器不会写入信任记录；必须用当前 Codex 的原生 `/hooks` 审阅和信任。

`tests/test_windows_native.py` 使用临时目录、合成密钥和模拟网络结果，覆盖私有及继承 ACL、空/宽 ACL 拒绝、硬链接/junction/数据流拒绝、锁竞争、子进程后代清理、管道截止时间、回调转发、固定网络结果解析、管理器配置恢复和 PowerShell 参数/标准输入。运行此测试不访问 Bark。

这些测试只能证明代码行为。真实结束回调、原生 hook 信任与自动执行、Bark API 接收、手机显示和铃声应分别记录验收；本文件不宣称已经验证这些路径。

## 本机部署验收（2026-10-03）

本机 Windows、Python 3.13.15、桌面捆绑 Codex 0.159.0-alpha.12.1 已完成安装。15 项 Windows 测试和独立 QA、代码及安全审查通过。三进程重复事件仅调用一次模拟发送；unknown 不重试；停用等待正在发送的共享锁释放。两轮完整启停恢复原 TOML 字节，后续编辑在恢复冲突时保留。

真实只读 app-server 查询成功，聊天 source=vscode、threadSource=user，名称可用。完成通知管理器状态为 enabled/active；提问提醒 configured/active。通过 Codex 原生 Hook 审阅信任后，新 app-server 的 hooks/list 确认 PostToolUse enabled=true、trustStatus=trusted，无配置错误。原有 notify 回调已保留；密钥通过私有本地文件引用。

Bark HTTPS 连通性已验证。尚未手动发送真实通知；真实桌面自动回调、Bark API 接收、手机显示及铃声需在已加载新配置的聊天中验收。POSIX 测试未在这台 Windows 电脑运行。

## Codex 更新后的重新绑定（2026-10-04）

桌面应用更新后，旧 Codex 可执行文件路径失效，完成回调也发生变化。本机已重新绑定 Codex 0.160.0，保留原有主机标识、密钥引用及去重数据库。完成通知为 enabled/active；提问提醒为 configured/active。重新审阅后，原生 hooks/list 确认提问 Hook enabled=true、trustStatus=trusted，无配置错误。

15 项 Windows 隔离测试再次全部通过。以上只证明代码、配置和 Hook 信任状态；真实自动推送、Bark API 接收、手机显示和铃声仍待验收。

当前管理器尚未提供通用安装向导或升级后自动修复入口。Codex 路径变更时需要重新绑定本机配置；分发源码时应使用前面的管理器命令，并展开当前机器的实际路径。
