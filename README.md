# agentx

`agentx` 是一个面向 macOS / Linux 的多智能体 CLI Session Manager。

它的目标不是自动拆任务，而是让你在**同一个项目目录**里长期保持多个编码智能体会话，并随时切换：

```text
同一个 Project
    │
    └── agentx / tmux session
          ├── 1 → Codex #1（独立登录态）
          ├── 2 → Codex #2（独立登录态）
          └── 3 → AGY（使用 AGY 自己的登录态）
```

v0.5 提供 Shared Context Pack、科技感 tmux TUI 和多项目并发隔离。它仍然不做任务调度、并发开发、Git Worktree、自动提交或远端上传。

## 核心特性

- 两个或更多 Codex 账号独立保存登录状态
- 一个 AGY / Antigravity CLI 账号
- 所有 Agent 默认进入同一个 working directory
- tmux 保持 Agent 真实交互进程
- 退出 Terminal / detach 后 Agent 继续运行
- 回到项目再次执行 `agentx` 自动接回原 session
- `Ctrl+G 1 / 2 / 3` 快速切换 Agent
- 可单独重启某个 Agent，不影响其他 Agent
- 支持多个项目同时拥有自己的 agentx session
- 从 Git 客观采集 Workspace Snapshot（非 Git 项目也支持）
- 本地即时采集 Git 状态、diff 和最近终端输出生成 Context Pack
- `agentx transfer` 完成采集、注入和切换，不等待源 Agent 回复
- Context Pack 历史持久保存在 `~/.agentx/projects/`，不污染项目仓库
- 彩色动态状态栏、Context Dashboard 和快捷键帮助 Popup
- 支持窄终端降级及 `NO_COLOR` 无颜色模式

---

## 1. 安装依赖

需要：

- Python 3
- tmux
- 你已经安装好的 `codex`
- 你已经安装好的 `agy`

macOS 安装 tmux：

```bash
brew install tmux
```

检查 Codex / AGY：

```bash
which codex
which agy
```

---

## 2. 安装 agentx

解压后：

```bash
cd agentx
chmod +x install.sh
./install.sh
```

默认安装到：

```text
~/.local/bin/agentx
```

如果 `~/.local/bin` 不在 PATH：

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

检查：

```bash
agentx doctor
```

---

## 3. 配置两个 Codex Plus 账号

### Codex #1

```bash
agentx account add codex codex-1
```

agentx 会创建：

```text
~/.agentx/profiles/codex-1/codex-home
```

然后实际执行类似：

```bash
CODEX_HOME=~/.agentx/profiles/codex-1/codex-home codex login
```

用第一个 ChatGPT / Codex 账号完成登录。

### Codex #2

```bash
agentx account add codex codex-2
```

它会使用另一个完全独立目录：

```text
~/.agentx/profiles/codex-2/codex-home
```

用第二个账号登录。

因此两个 Codex 不会共用同一份 `CODEX_HOME`。

---

## 4. 配置 AGY

```bash
agentx account add agy agy
```

agentx 会启动 `agy`。如果 AGY 要求登录，完成登录后退出 AGY 即可。

第一版假设你只有一个 AGY 账号，所以直接复用 AGY 自己在 Mac 上保存的认证状态。

如果你的 AGY 可执行文件不叫 `agy`：

```bash
agentx account add agy agy --command /完整路径/agy
```

查看账号：

```bash
agentx accounts
```

理想结果：

```text
#   NAME                 TYPE     COMMAND          PATH
1   codex-1              codex    codex            ~/.agentx/profiles/codex-1
2   codex-2              codex    codex            ~/.agentx/profiles/codex-2
3   agy                  agy      agy              ~/.agentx/profiles/agy
```

---

## 5. 日常使用

进入项目：

```bash
cd ~/projects/hermes
agentx
```

第一次会创建一个项目专属 tmux session，并创建三个 window：

```text
1: codex-1
2: codex-2
3: agy
```

三个 Agent 的 working directory 都是：

```text
~/projects/hermes
```

### 切换 Agent

Agent 窗口可以用 `Ctrl+Space` 切换到下一个 Agent，不需要先按 tmux prefix。切换前会主动
询问是否传输上下文：

- 选择 `Yes`：本地生成 Context Pack，向目标 Agent 注入后切换。
- 选择 `No`：只切换窗口，不生成或注入上下文。
- 选择 `Cancel`：取消切换。

原有的 `Ctrl+G + 数字` 仍保留兼容；其他管理快捷键继续使用 `Ctrl+G` prefix。

```text
Ctrl+Space → 询问是否传输上下文，然后切换到下一个 Agent
Ctrl+G 1   → Codex #1
Ctrl+G 2   → Codex #2
Ctrl+G 3   → AGY
Ctrl+G h   → 采集 Context Pack
Ctrl+G H   → 显示当前 Context Pack
Ctrl+G c   → 显示 Context 状态
Ctrl+G a   → 显示 Agent Dashboard
Ctrl+G r   → 刷新 Workspace Snapshot
Ctrl+G R   → 当前 Agent 误退出后原地恢复并尝试续接会话
Ctrl+G Ctrl+D → 有意向当前 Agent 发送 Ctrl+D (EOF)
Ctrl+G ?   → 显示快捷键帮助
```

单独按 `Ctrl+D` 会被 tmux 层拦截，只显示保护提示，不会再发送给 Agent，因此也不会让 pane
进入退出链路。如果确实需要发送 EOF，先按 `Ctrl+G`、松开，再按 `Ctrl+D`。

如果 Agent 因其他原因退出，AgentX 会留在当前 pane 并显示恢复提示。直接按 `Enter`
即可尝试续接最近会话；确认不再恢复时按 `Ctrl+C`。通常不需要执行 tmux 命令。
只有外层守护也异常退出、pane 已显示为 dead 时，才需要使用 `Ctrl+G R` 兜底恢复。

也可以在命令行直接要求带上下文切换：

```bash
agentx transfer codex-2
agentx transfer agy
```

它会直接采集工作区快照和最近终端输出，写入 Context Pack，向目标 Agent 注入本地文件路径后切换。全程不向源 Agent 发送总结请求，也不轮询它的输出。目标 Agent 以实际 Working Tree 为最高优先级，自行核对 `git status` 和 `git diff`。

因此你可以：

```text
Codex #1 修改 authentication
        ↓
Ctrl+G 2
        ↓
Codex #2 读取刚才修改后的同一批文件继续开发
        ↓
Ctrl+G 3
        ↓
AGY Review / 修改同一 working tree
```

这正是 agentx 第一版的核心工作流。

> 注意：因为三个 Agent 操作的是同一个 working tree，不建议同时让两个 Agent 并发修改相同文件。

---

## 6. 退出但保持 Agent 活着

在 agentx 里面：

```text
Ctrl+G d
```

这叫 detach。

Codex / AGY 进程不会被杀掉。

你甚至可以关闭 Terminal。

之后回来：

```bash
cd ~/projects/hermes
agentx
```

会重新接回原来的 tmux session，而不是重新启动三个 Agent。

也可以从另一个终端：

```bash
agentx detach
```

---

## 7. 常用命令

```bash
# 当前项目打开 / 接回
agentx
agentx open

# 直接进入某个 Agent
agentx use codex-1
agentx use codex-2
agentx use agy

# 查看当前项目的 Agent 状态
agentx status

# 查看所有项目 session
agentx sessions

# 只刷新客观 Workspace Snapshot，不调用 Agent
agentx context refresh

# 查看 Context 摘要、目录或完整 Context Pack
agentx context
agentx context path
agentx context show

# 打开项目 Dashboard / 查看快捷键
agentx dashboard
agentx dashboard --no-refresh
agentx keys

# 本地采集 Context Pack
agentx context capture
agentx context capture --source codex-1

# Capture → Context Injection → Switch
agentx transfer codex-2

# 旧命令仍作为兼容别名
agentx handoff
agentx switch codex-2

# 看某 Agent 最近终端输出
agentx logs codex-1
agentx logs codex-2 --lines 500

# 单独重启一个 Agent
agentx new codex-1

# 尝试恢复该 Agent 最近对话
agentx new codex-1 --resume

# 仅在 pane 已退出时恢复（不会重启正在运行的 Agent）
agentx recover codex-1

# 退出前台，但保持 Agent 运行
agentx detach

# 真正结束当前项目全部 Agent
agentx kill
```

---

## 8. Mac 重启后的区别

### 登录状态

Codex 的 profile 目录还在：

```text
~/.agentx/profiles/codex-1/codex-home
~/.agentx/profiles/codex-2/codex-home
```

所以重启 agentx 不会主动覆盖两个账号的状态。

AGY 继续使用 AGY 自身的认证持久化。

### 正在运行的会话

tmux 能跨 Terminal 关闭保持进程，但不能跨整台 Mac 重启保持进程。

Mac 重启后进入项目：

```bash
cd ~/projects/hermes
agentx
```

会创建新的 Agent 进程。

如果想让新进程自动尝试恢复上一次对话：

```bash
agentx config set-auto-resume on
```

关闭：

```bash
agentx config set-auto-resume off
```

默认关闭，因为不同版本 CLI 的 resume 行为可能有差异。

Codex 的 `--resume` 模式使用：

```text
codex resume --last
```

AGY 第一版在 `--resume` 时尝试：

```text
agy --continue
```

如果你的 AGY 版本不支持这个参数，正常使用 `agentx new agy` 即可。

---

## 9. 多项目

每个绝对项目路径都会生成不同 tmux session。

比如：

```bash
cd ~/projects/hermes
agentx
```

和：

```bash
cd ~/projects/ai-doc-analysis
agentx
```

两个项目会分别运行自己的 Agent 进程并保存自己的 Context Pack。快捷键会根据当前
Terminal 所连接的 tmux session 解析项目，因此后打开的项目不会覆盖先打开项目的
Dashboard、恢复或 transfer 目标。

同一个项目如果从两个 Terminal 打开，则两个 Terminal 会连接同一个 tmux session，
共同操作同一组 Agent 进程。Codex 登录态仍然按 profile 共享：例如两个项目中的
`codex-1` 是两个独立进程，但使用同一个 `CODEX_HOME` 和账号。

查看：

```bash
agentx sessions
```

---

## 10. 配置位置

```bash
agentx config path
```

默认：

```text
~/.agentx/config.json
```

每个项目的共享状态使用绝对路径的 SHA1 前 10 位作为 ID：

```text
~/.agentx/projects/<project_id>/
├── project.json
├── context.json
├── context-pack.md
├── workspace.json
├── workspace.diff
├── workspace-staged.diff
├── recent-output.txt
└── history/
```

`workspace.json` 只保存摘要。未暂存与已暂存 diff 分开保存；两者合计默认超过 200 KB 时不会落盘完整内容，`workspace.json` 会记录 `diff_truncated` 和原始字节数。AgentX 不递归扫描项目文件、不读取 `.env` 内容、SSH 目录或 Keychain，也不会把 Context 上传到远端。

Context 的可信优先级始终是：实际 Working Tree 与当前 Git 状态，其次是 Workspace Snapshot，最后是最近终端输出。

查看配置：

```bash
agentx config show
```

你也可以把整个 agentx 状态目录放到别处：

```bash
export AGENTX_HOME="$HOME/somewhere/agentx"
```

---

## 11. 删除账号配置

```bash
agentx account remove codex-1
```

为了避免误删登录状态，这只会从 `config.json` 移除 profile，**不会自动删除**：

```text
~/.agentx/profiles/codex-1
```

需要彻底删除时再手动处理。

---

## 12. 卸载

```bash
./uninstall.sh
```

只删除程序文件：

```text
~/.local/bin/agentx
~/.local/bin/agentxlib/
```

不会删除 `~/.agentx`，防止你的登录态被误删。

---

## 13. TUI

AgentX 保留 Codex/AGY 的原生交互界面，只使用 tmux 状态栏和 popup 提供辅助信息。状态栏显示当前项目、Agent、Git 分支、变更数量及最近 Context Transfer 时间；它读取保存过的 snapshot，不会每 5 秒运行 Git。

AgentX 底栏内容上下各保留一行终端底色 padding，使内容垂直居中，也避免它与 Agent 原生状态行贴在一起。Agent 标签组相对终端绝对水平居中，底栏左右同样保留终端底色 padding。

每个 Agent 窗口由等宽细边框完整围住，Agent 名称和项目嵌在上边框中。左右和底部使用禁止输入的 1 单元装饰 pane，因此误点边框会自动回到 Agent，Context Pack、恢复和输出捕获也始终指向中央 Agent pane。默认继承终端底色，不会形成第二条状态栏；颜色也可配置：

```bash
agentx config set-frame-color '#22d3ee'
agentx config set-frame-background '#111827'
agentx config set-frame-background default   # 恢复透明/终端底色
agentx config set-frame off   # 关闭
agentx config set-frame on    # 开启
```

支持 tmux 基础色名、`colour0` 到 `colour255` 以及 `#RRGGBB`。Codex/AGY 正文区域的底色仍由终端模拟器的 profile/theme 控制；AgentX 负责顶部边框、底部状态栏、popup 和菜单。

```text
╭─[ AGENTX // CONTROL DECK ]────────────────────────────────────╮
│ PROJECT hermes                                                │
│ SIGNAL feature/auth // Δ 4                                    │
╰───────────────────────────────────────────────────────────────╯

╭─[ AGENT MATRIX ]──────────────────────────────────────────────╮
│ ◆ codex-1          FOCUS     ONLINE                           │
│ ◇ codex-2          STANDBY   ONLINE                           │
│ ◇ agy              STANDBY   ONLINE                           │
╰───────────────────────────────────────────────────────────────╯

╭─[ CONTEXT BUS ]───────────────────────────────────────────────╮
│ ROUTE codex-1 → codex-2 // 3m ago                             │
│ PACK context-pack.md                                          │
│ SNAPSHOT 2 modified · 1 staged · 1 untracked                  │
╰───────────────────────────────────────────────────────────────╯
```

Dashboard 默认刷新 Workspace Snapshot。仅查看已保存状态时：

```bash
agentx dashboard --no-refresh
```

禁用 ANSI 颜色：

```bash
NO_COLOR=1 agentx dashboard
```

---

## v0.5.0 的设计边界与限制

这一版只解决 Agent A 如何可靠交给 Agent B：

```text
Workspace Snapshot + Recent Terminal Output
+
Deterministic Context Pack
+
Context Injection
```

已知限制：Context Pack 只包含可观测的工作区状态和最近 300 行终端输出（最大 128 KiB），不读取 Codex/AGY 的私有会话数据库。tmux session 本身不能跨机器重启，Context 文件可以。`git diff` 和终端输出可能包含敏感内容，因此这些文件仅保存在本机 `~/.agentx`，用户应避免把该目录同步或提交到远端。
