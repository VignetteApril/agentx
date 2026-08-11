# AgentX

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

## Quick start

Move into a project and start AgentX:

```bash
cd ~/projects/hermes
agentx
```

The first run creates a project-specific tmux session with one window per configured profile. Every agent starts in the same project directory:

```text
0  codex-1
1  codex-2
2  agy
```

Running `agentx` again from the same directory reconnects to that session instead of starting duplicate agents.

### Switch agents

Press `Ctrl+Space` to open the switch menu for the next agent. You can choose:

- **Yes** — capture a Context Pack, inject it into the target agent, and switch.
- **No** — switch windows without transferring context.
- **Cancel** — stay with the current agent.

You can also use `Ctrl+G`, release it, and then press a number to choose a specific agent.

```text
Ctrl+Space       Open the context-transfer switch menu
Ctrl+G 1         Choose agent 1
Ctrl+G 2         Choose agent 2
Ctrl+G 3         Choose agent 3
Ctrl+G h         Capture a Context Pack
Ctrl+G H         Show the current Context Pack
Ctrl+G c         Open the Context Dashboard
Ctrl+G a         Open the Agent Dashboard
Ctrl+G r         Refresh the Workspace Snapshot
Ctrl+G R         Recover the current agent after its pane exits
Ctrl+G Ctrl+D    Intentionally send Ctrl+D (EOF) to the agent
Ctrl+G ?         Show keyboard help
Ctrl+G d         Detach and leave all agents running
```

A bare `Ctrl+D` is blocked to prevent an accidental agent exit. If an agent exits normally, its pane stays open with a recovery prompt: press `Enter` to resume the latest conversation, or `Ctrl+C` to stop. `Ctrl+G R` is available as a fallback if the outer recovery process also exits and the pane becomes dead.

### Transfer context from the command line

You can explicitly capture, inject, and switch with:

```bash
agentx transfer codex-2
agentx transfer agy
```

AgentX captures the current Git state, diff, and recent terminal output locally. It writes a Context Pack, sends its path to the target agent, and switches only after injection succeeds. It does not ask the source agent to write a handoff and does not wait for a generated summary.

The target agent is instructed to treat the actual working tree as the source of truth and verify it with `git status` and `git diff`.

> All agents in a project share the same working tree. Avoid asking two agents to edit the same files at the same time unless you are comfortable resolving overlapping changes.

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

# Restart or recover one agent
agentx new codex-1
agentx new codex-1 --resume
agentx recover codex-1

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

AgentX keeps the native agent interface and adds a compact control layer through tmux.

- The status bar shows the project, active agent, branch, change count, and latest context age.
- The active agent tab uses a cyan background; inactive agent tabs use gray.
- Each agent window has a thin configurable frame with its agent and project name.
- Dashboards and help open in lightweight popups.
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

The frame uses input-disabled one-cell helper panes on the left, right, and bottom. Clicking one returns focus to the main agent pane, while context capture, recovery, and logs continue to target the correct pane.

Customize the frame:

```bash
agentx config set-frame-color '#22d3ee'
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
