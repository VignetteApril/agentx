<p align="center">
  <img src="assets/agentx-logo.png" alt="AgentX logo" width="180">
</p>

<h1 align="center">AgentX</h1>

<p align="center">
  <a href="https://github.com/VignetteApril/agentx"><img src="https://img.shields.io/badge/version-0.5.0-00d7ff?style=flat-square" alt="Version 0.5.0"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.9%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.9+"></a>
  <a href="https://github.com/tmux/tmux"><img src="https://img.shields.io/badge/tmux-3.2%2B-1BB91F?style=flat-square&amp;logo=tmux&amp;logoColor=white" alt="tmux 3.2+"></a>
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-8b5cf6?style=flat-square" alt="macOS and Linux">
  <img src="https://img.shields.io/badge/context-local--only-6b7280?style=flat-square&amp;logo=shield&amp;logoColor=white" alt="Local-only context">
</p>

AgentX is a friendly tmux-based session manager for working with multiple coding agents in one project.

It keeps each agent alive in its own tmux window, gives every Codex profile an isolated login, and makes it easy to pass reliable project context from one agent to another. You keep the native Codex or AGY interface; AgentX simply adds session management, context transfer, recovery tools, and a compact terminal UI around it.

```text
Your project
    │
    └── AgentX tmux session
          ├── 1  codex-1   (its own Codex login)
          ├── 2  codex-2   (another Codex login)
          └── 3  agy       (AGY's existing login)
```

AgentX is intentionally small. It does not schedule tasks, create Git worktrees, commit code, or upload project context. You remain in control of the work and the agents.

## Highlights

- Keep multiple Codex accounts signed in independently.
- Run Codex and AGY side by side in the same working tree.
- Detach from the terminal without stopping your agents.
- Rejoin the same project session later with one command.
- Switch agents from a menu with `Ctrl+Space`.
- Capture a deterministic Context Pack without asking the source agent to summarize itself.
- Transfer workspace state and recent terminal output to the next agent.
- Recover an agent that exited without restarting healthy agents.
- Run several projects at once with isolated sessions and context files.
- Use a polished tmux status bar, framed agent windows, dashboards, and popups.
- Keep everything local under `~/.agentx`.

## Requirements

AgentX supports macOS and Linux. You will need:

- Python 3
- tmux
- the Codex CLI, already installed
- the AGY CLI if you want to use AGY

On macOS, install tmux with Homebrew:

```bash
brew install tmux
```

You can quickly check the agent commands available on your machine:

```bash
which codex
which agy
```

## Install

Clone the repository and run the installer:

```bash
git clone https://github.com/VignetteApril/agentx.git
cd agentx
./install.sh
```

AgentX installs to `~/.local/bin/agentx`. If that directory is not already on your `PATH`, add it:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

Then check your setup:

```bash
agentx doctor
```

## Add your agents

### Codex profiles

Create the first Codex profile:

```bash
agentx account add codex codex-1
```

AgentX creates a dedicated home directory and starts the normal Codex login flow:

```text
~/.agentx/profiles/codex-1/codex-home
```

Add a second account the same way:

```bash
agentx account add codex codex-2
```

Each Codex profile receives its own `CODEX_HOME`, so the two accounts do not overwrite each other's authentication or settings.

### AGY

Add AGY with:

```bash
agentx account add agy agy
```

AgentX uses AGY's existing authentication on your machine. If the executable has a different name or location, provide it explicitly:

```bash
agentx account add agy agy --command /full/path/to/agy
```

Review all configured profiles at any time:

```bash
agentx accounts
```

### Windows desktop controller (Rust)

The native Windows x64 controller retains the same profile layout and the official
client interface. One `AgentX.exe` starts two independently signed-in clients and
adds an account tab bar for window switching and position tracking. Closing the
bar restores client windows while keeping their processes running.

Download the portable Windows x64 ZIP from [GitHub Releases](https://github.com/VignetteApril/agentx/releases).
The EXE, native windows, tray, and desktop shortcut use the AgentX icon. Run the
included shortcut helper to create a desktop entry.

Build and package with `python scripts/build-windows-desktop.py`.
See [Windows usage and validation](desktop/rust/WINDOWS.md) for setup,
diagnostics, build instructions, and the distinction between fixture tests and
real official-client account validation. The Rust implementation uses native Win32
APIs. The terminal session manager uses its existing entry point.

## Quick start

Move into a project and start AgentX:

```bash
cd ~/projects/hermes
agentx
```

The first run creates a project-specific tmux session with one window per configured profile. Every agent starts in the same project directory, then AgentX opens a full-screen cockpit over the session:

```text
1  codex-1
2  codex-2
3  agy
```

Use the arrow keys or `1`–`9` to choose an agent and press `Enter`. Switching to a different live agent asks whether to capture and transfer a Context Pack first. `Esc` closes the cockpit without switching. Running `agentx` again reconnects to the same session and reopens the cockpit instead of starting duplicate agents.

To bypass the cockpit and open one profile directly, use `agentx use <profile>` or `agentx open --profile <profile>`.

### Switch agents

Press `Ctrl+Space` to open the switch menu for the next agent. You can choose:

- **Yes** — capture a Context Pack, inject it into the target agent, and switch.
- **No** — switch windows without transferring context.
- **Cancel** — stay with the current agent.

You can also use `Ctrl+G`, release it, and then press a number to choose a specific agent.

```text
Ctrl+Space       Open the context-transfer switch menu
Ctrl+G x         Open the AgentX cockpit
Ctrl+G 1         Choose agent 1
Ctrl+G 2         Choose agent 2
Ctrl+G 3         Choose agent 3
Ctrl+G h         Capture a Context Pack
Ctrl+G H         Show the current Context Pack
Ctrl+G c         Open the Context Dashboard
Ctrl+G a         Open the Agent Dashboard
Ctrl+G r         Refresh the Workspace Snapshot
Ctrl+G R         Recover all exited agents in the current project
Ctrl+G Ctrl+D    Intentionally send Ctrl+D (EOF) to the agent
Ctrl+G ?         Show keyboard help
Ctrl+G d         Detach and leave all agents running
```

A bare `Ctrl+D` is blocked to prevent an accidental agent exit. If an agent exits normally, its pane stays open with a recovery prompt: press `Enter` to resume the latest conversation, or `Ctrl+C` to stop. Press `Ctrl+G R` from any window to resume every exited Agent in the current project at once; Agents that are still running are left untouched.

### Transfer context from the command line

You can explicitly capture, inject, and switch with:

```bash
agentx transfer codex-2
agentx transfer agy
```

AgentX captures the current Git state, diff, and recent terminal output locally. It writes a Context Pack, sends its path to the target agent, and switches only after injection succeeds. It does not ask the source agent to write a handoff and does not wait for a generated summary.

The target agent is instructed to treat the actual working tree as the source of truth and verify it with `git status` and `git diff`.

> All agents in a project share the same working tree. Avoid asking two agents to edit the same files at the same time unless you are comfortable resolving overlapping changes.

### Local memory (Obsidian-compatible)

AgentX can keep durable project decisions, architecture notes, and recurring gotchas in a local Markdown vault. It selects up to three relevant notes and includes them in every Context Pack, so the next agent can recover decisions that are not visible in the Git diff.

Create the default local vault once:

```bash
agentx memory init
agentx memory add "API convention" --tag architecture --note "Use JSON error envelopes for all API failures."
agentx memory search "API error convention"
```

The default vault is `~/.agentx/memory`; open that folder with Obsidian if you prefer its graph, editor, and search UI. Project notes created by `agentx memory add` are automatically scoped to the current project. You can point AgentX at an existing Obsidian vault instead:

```bash
agentx config set-memory-path ~/Documents/Obsidian/AgentX
```

For manually-authored notes, use simple frontmatter to associate a note with a project:

```markdown
---
project: /absolute/path/to/project
tags: architecture, decision
---

# Why we use the queue

The queue keeps browser requests independent from model inference latency.
```

## Leave and come back later

Press `Ctrl+G d` to detach. The Codex and AGY processes continue running inside tmux, so it is safe to close the terminal.

When you return:

```bash
cd ~/projects/hermes
agentx
```

You can also detach from another terminal:

```bash
agentx detach
```

tmux survives terminal closure, but it does not survive a full machine restart. After a restart, AgentX creates fresh processes while keeping your profile logins and saved Context Packs.

To ask new processes to resume their latest conversations automatically:

```bash
agentx config set-auto-resume on
```

Turn it off again with:

```bash
agentx config set-auto-resume off
```

Auto-resume is off by default because resume behavior can differ between CLI versions. Codex uses `codex resume --last`; AGY uses `agy --continue` when supported.

## Useful commands

```bash
# Open or reconnect to the current project
agentx
agentx open

# Enter a particular agent window
agentx use codex-1
agentx use codex-2
agentx use agy

# Inspect agents and project sessions
agentx status
agentx sessions
agentx accounts

# Work with saved context
agentx context
agentx context refresh
agentx context path
agentx context show
agentx context capture
agentx context capture --source codex-1

# Capture, inject, and switch
agentx transfer codex-2

# Open terminal UI helpers
agentx dashboard
agentx dashboard --no-refresh
agentx keys

# Read recent terminal output
agentx logs codex-1
agentx logs codex-2 --lines 500

# Restart one agent, or recover one/all exited agents
agentx new codex-1
agentx new codex-1 --resume
agentx recover codex-1
agentx recover --all

# Leave agents running, or stop the whole project session
agentx detach
agentx kill
```

The older `agentx handoff` and `agentx switch` commands remain available as compatibility aliases.

## Multiple projects

Every absolute project path receives a different tmux session and state directory. You can safely run:

```bash
cd ~/projects/hermes
agentx
```

in one terminal and:

```bash
cd ~/projects/another-project
agentx
```

in another. Shortcuts resolve the project from the tmux session where you pressed the key, so a dashboard, recovery action, or context transfer cannot accidentally target the last project you opened.

Opening the same project from two terminals connects both terminals to the same tmux session and the same agent processes. Opening different projects creates separate processes. A profile such as `codex-1` still shares its login and `CODEX_HOME` across projects.

List all live AgentX sessions with:

```bash
agentx sessions
```

## Context Packs

AgentX stores project state under:

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

The project ID is the first 10 characters of a SHA-1 hash of the absolute project path. These files never appear in your project repository.

A Context Pack can include:

- the project path, branch, and current commit;
- changed, staged, and untracked file summaries;
- unstaged and staged diffs;
- recent commits;
- the latest 300 terminal lines, up to 128 KiB;
- transfer source, destination, timestamp, and warnings.

Unstaged and staged diffs are stored separately. If their combined size exceeds the configured limit, AgentX records the size and omission instead of writing the full diff.

AgentX does not recursively inspect your project, read `.env` files, access SSH directories or Keychain, or upload context to a remote service. Even so, diffs and terminal output may contain sensitive information. Keep `~/.agentx` private and avoid syncing or committing it.

The trust order is always:

```text
Actual working tree and Git state
    → Workspace Snapshot
        → recent terminal output and Context Pack notes
```

## Terminal UI

AgentX keeps the native agent interface and adds a dark, keyboard-driven control layer through tmux.

- The full-screen cockpit uses the AgentX cyan and purple palette to select an agent without replacing its native interface.
- The single-line footer shows the project path, active agent, branch, change count, latest context age, and AgentX version.
- The active agent tab uses a cyan background; inactive agent tabs use gray.
- Each agent window has a subtle neutral frame with an accent title for its agent and project name.
- Dashboards and help open in lightweight popups.
- The mouse wheel keeps native agent scrolling. Full-screen agents without mouse reporting receive PageUp/PageDown instead; ordinary shell output uses tmux scrollback.
- Layouts remain readable in narrow terminals and with ANSI colors disabled.

```text
╭─[ AGENTX // CONTROL DECK ]────────────────────────────────────╮
│ PROJECT  hermes                                               │
│ SIGNAL   feature/auth  //  Δ 4                                │
╰───────────────────────────────────────────────────────────────╯

╭─[ AGENT MATRIX ]──────────────────────────────────────────────╮
│ ◆  codex-1            FOCUS     ONLINE                       │
│ ◇  codex-2            STANDBY   ONLINE                       │
│ ◇  agy                STANDBY   ONLINE                       │
╰───────────────────────────────────────────────────────────────╯

╭─[ CONTEXT BUS ]───────────────────────────────────────────────╮
│ ROUTE      codex-1 → codex-2  //  3m ago                     │
│ PACK       context-pack.md                                   │
│ SNAPSHOT   2 modified · 1 staged · 1 untracked               │
╰───────────────────────────────────────────────────────────────╯
```

The Agent pane takes the full width with no side or bottom gutters. The top title remains, and the single-line footer sits directly below the Agent content; context capture, recovery, and logs continue to target the correct pane.

Customize the frame:

```bash
agentx config set-frame-color '#22d3ee'  # title accent
agentx config set-frame-background '#111827'
agentx config set-frame-background default
agentx config set-frame off
agentx config set-frame on
```

Colors may be basic tmux color names, `colour0` through `colour255`, or `#RRGGBB`. The main Codex or AGY background still comes from your terminal profile; AgentX styles only its frame, status bar, popups, and menus.

To render the dashboard without ANSI colors:

```bash
NO_COLOR=1 agentx dashboard
```

## Configuration and local data

Show the current configuration or its location:

```bash
agentx config show
agentx config path
```

The default file is:

```text
~/.agentx/config.json
```

To store all AgentX data somewhere else, set `AGENTX_HOME` before running it:

```bash
export AGENTX_HOME="$HOME/somewhere/agentx"
```

Removing a profile only removes it from `config.json`; AgentX deliberately keeps the authentication directory so an accidental command cannot erase your login:

```bash
agentx account remove codex-1
```

Delete `~/.agentx/profiles/codex-1` manually only when you truly want to remove its saved state.

## Uninstall

From the cloned repository, run:

```bash
./uninstall.sh
```

The uninstaller removes:

```text
~/.local/bin/agentx
~/.local/bin/agentxlib/
```

It leaves `~/.agentx` untouched so your profiles, logins, and saved project context are not accidentally lost.

## Current boundaries

AgentX 0.5 focuses on reliable local handoff between agents:

```text
Workspace Snapshot + recent terminal output
    → deterministic Context Pack
        → context injection
```

Context Packs only contain observable local state. AgentX does not read the private conversation database of Codex or AGY. tmux sessions cannot survive a full machine restart, although saved Context Packs can. AgentX also does not provide task scheduling, concurrent-edit coordination, Git worktrees, automatic commits, or remote context storage.

If that focused workflow fits the way you work, welcome aboard — open a project, run `agentx`, and let each agent pick up where the last one left off.
