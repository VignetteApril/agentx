# AgentX Windows 桌面外框

Windows x64 的 Rust 原生启动器。它在两个独立的官方 Codex 客户端外增加账号标签栏，保留官方客户端界面。

## 使用

1. 在 Windows 10 2004 或更新版本、或 Windows 11 x64 上安装[官方客户端](https://learn.chatgpt.com/docs/windows/windows-app)。新版应用显示名称为 ChatGPT，应用内包含 Codex。
2. 从 [GitHub Releases](https://github.com/VignetteApril/agentx/releases) 下载并解压完整 Windows x64 ZIP，然后双击 `AgentX.exe`。
3. 第一次运行会为两个账号创建独立配置。分别在官方客户端里完成登录，并确认选择了所需的不同账号。
4. 点击“账号 1 / 账号 2”切换客户端。隐藏窗口的进程继续运行。

双击 `创建桌面快捷方式.cmd` 可以创建带 AgentX 图标的桌面入口。更新已有入口时保留原来的启动参数。图标同时嵌入 EXE，并用于原生窗口和托盘；请保留程序旁的 `AgentX.ico`，供桌面快捷方式使用。

如果没有找到客户端，可以从标签栏的“···”菜单或托盘图标选择“选择官方客户端”，定位安装目录下的 `app\ChatGPT.exe`。启动器会先检查 EXE 架构和客户端的数据目录隔离入口。它不会启动缺少隔离入口的客户端版本。

标签栏会贴近当前客户端窗口；可以拖动到其他位置。菜单中的“贴近当前窗口”恢复自动跟随。切换到其他应用时标签栏会隐藏，点击托盘图标可找回。关闭外框会显示仍在运行的客户端，不会结束客户端任务。再次打开 AgentX 会核对进程身份并复用现有账号实例。

运行时不需要 Python、tmux 或 Rust 开发环境。自动识别 Microsoft Store/MSIX 安装位置使用系统自带的 Windows PowerShell；检测受限时可以手动选择客户端。

## 数据位置

默认沿用项目的 `%USERPROFILE%\.agentx`：

```text
.agentx\
  config.json
  profiles\
    codex-1\
      codex-home\          # 该账号自己的 Codex 登录、配置及会话
      desktop-data\        # 该账号自己的官方桌面应用数据
      desktop-instance.json
      desktop.log
    codex-2\
      ...
```

已有 `config.json` 中的其他 agent、settings 和未知字段会保留；优先使用配置中前两个 Codex profile。没有两个 Codex profile 时会补齐。Windows 不区分大小写，因此大小写冲突、设备保留名、目录链接和目录联接会被拒绝。

也可以通过环境变量 `AGENTX_HOME`，或 `AgentX.exe --home "D:\AgentX Data"` 指定目录。账号目录不会写入发行 ZIP，也不会复制原来 `%USERPROFILE%\.codex` 的登录信息。

## 验证与诊断

- 双击 `检查环境.cmd` 生成 `diagnostics.json`。它检查安装位置和配置，不读取或输出登录令牌。
- 双击 `测试窗口切换.cmd` 生成 `window-test.json`。这个测试只启动两个明确标注的本地测试窗口，不连接 Codex 账号，也不提交请求。
- `build-info.json` 记录构建目标、文件校验值和原生窗口测试是否实际运行。

命令行入口：

```powershell
.\AgentX.exe --app "C:\path\to\app\ChatGPT.exe"
.\AgentX.exe --doctor .\diagnostics.json
.\AgentX.exe --self-test .\window-test.json
.\AgentX.exe --plan-json codex-1
```

Windows 安装包的静态检查已确认官方 `OpenAI.Codex` x64 包 `26.1106.6628.0` 使用 `CODEX_ELECTRON_USER_DATA_PATH` 设置应用数据目录，再处理单实例锁。这个参数属于客户端内部实现，因此启动器会检查每个实际安装版本。

0.5.1 已在 Windows 11 x64、1920×1080、96 DPI 的真实桌面会话中验收到官方登录页：两个官方实例、标签切换、主窗口焦点、标签栏跟随、关闭后保留进程、重开复用进程和单外框入口均通过。验收发现并修复了将隐藏辅助窗口误认为主窗口的问题，并增加了对应的原生窗口测试。

两个账号尚未登录；登录后的会话隔离、真实任务连续性、多显示器与其他缩放比例仍未验证。测试窗口报告的范围仅为本地窗口管理。当前开发包未做 Windows 发布签名。

## 从源码构建

Windows 构建需要 Rust MSVC 工具链、Visual Studio C++ Build Tools 和 Windows SDK。项目根目录运行：

```powershell
python scripts\build-windows-desktop.py
```

脚本执行格式检查、Rust 测试、x64 GUI EXE 编译和本地测试窗口检查，然后在 `.build\windows` 生成便携 ZIP。Python 只用于源码构建和打包。

Windows 图标沿用项目的 `assets/agentx-logo.png`，预生成的 ICO 已随源码提供。需要修改图标时，安装 Pillow 后运行 `python scripts/build-windows-icon.py`。Rust 构建会自动嵌入图标资源。

推送与 Rust 版本一致的 `desktop-v<版本>` 标签后，GitHub Actions 在 Windows 上构建、检查图标、执行原生窗口测试，通过后自动将 ZIP 和 SHA-256 文件发布到 Release。

macOS 也可以用 Rust 的 `x86_64-pc-windows-msvc` 目标、`rust-lld` 和 xwin 或 POSIX 布局的 MSVC sysroot 交叉构建：

```bash
rustup target add x86_64-pc-windows-msvc
python3 scripts/build-windows-desktop.py \
  --linker /path/to/rust-lld --sysroot /path/to/xwin
```

共享核心与 Windows 目标检查可以独立运行：

```bash
cargo test --manifest-path desktop/rust/Cargo.toml --locked
cargo clippy --manifest-path desktop/rust/Cargo.toml --all-targets -- -D warnings
cargo check --manifest-path desktop/rust/Cargo.toml --target x86_64-pc-windows-msvc --all-targets
```

Rust 的配置、隔离路径和启动计划与项目的现有配置兼容。
