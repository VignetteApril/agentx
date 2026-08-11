#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from agentxlib.context import mark_context_pack_transferred, save_context_pack, save_workspace
from agentxlib.handoff import (clean_terminal_output, context_pack_injection_prompt,
                               format_context_pack)
from agentxlib.projects import ensure_project, now_iso, project_dir, project_id, read_json, update_project
from agentxlib.theme import current_theme
from agentxlib.ui import render_dashboard, render_keys, render_statusline
from agentxlib.workspace import MAX_DIFF_SIZE, collect_workspace, parse_git_status

APP_NAME = "agentx"
VERSION = "0.5.0"
HOME = Path.home()
STATE_DIR = Path(os.environ.get("AGENTX_HOME", HOME / ".agentx")).expanduser()
CONFIG_PATH = STATE_DIR / "config.json"
PROFILES_DIR = STATE_DIR / "profiles"
PROJECTS_DIR = STATE_DIR / "projects"

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "profiles": [],
    "settings": {
        "prefix": "C-g",
        "auto_resume": False,
        "default_shell": os.environ.get("SHELL", "/bin/zsh"),
        "max_diff_size": MAX_DIFF_SIZE,
        "recent_output_lines": 300,
        "frame_enabled": True,
        "frame_color": "colour51",
        "frame_background": "default",
    },
}


class AgentXError(RuntimeError):
    pass


@dataclass
class Profile:
    name: str
    kind: str
    command: str
    created_at: int

    @property
    def dir(self) -> Path:
        return PROFILES_DIR / self.name

    @property
    def codex_home(self) -> Path:
        return self.dir / "codex-home"


def ensure_state() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)


def load_config() -> dict[str, Any]:
    ensure_state()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AgentXError(f"Invalid config: {CONFIG_PATH}: {exc}") from exc
    data.setdefault("version", 1)
    data.setdefault("profiles", [])
    data.setdefault("settings", {})
    data["settings"].setdefault("prefix", "C-g")
    data["settings"].setdefault("auto_resume", False)
    data["settings"].setdefault("default_shell", os.environ.get("SHELL", "/bin/zsh"))
    data["settings"].setdefault("max_diff_size", MAX_DIFF_SIZE)
    data["settings"].setdefault("recent_output_lines", 300)
    data["settings"].setdefault("frame_enabled", True)
    data["settings"].setdefault("frame_color", "colour51")
    data["settings"].setdefault("frame_background", "default")
    return data


def save_config(config: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def profiles(config: dict[str, Any]) -> list[Profile]:
    out: list[Profile] = []
    for raw in config.get("profiles", []):
        out.append(Profile(
            name=str(raw["name"]),
            kind=str(raw["kind"]),
            command=str(raw.get("command") or default_command(str(raw["kind"]))),
            created_at=int(raw.get("created_at", 0)),
        ))
    return out


def find_profile(config: dict[str, Any], name: str) -> Profile:
    for p in profiles(config):
        if p.name == name:
            return p
    raise AgentXError(f"Unknown profile: {name}")


def default_command(kind: str) -> str:
    if kind == "codex":
        return "codex"
    if kind == "agy":
        return "agy"
    raise AgentXError(f"Unsupported agent kind: {kind}")


def valid_profile_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name))


def valid_tmux_color(value: str) -> bool:
    named = {"default", "black", "red", "green", "yellow", "blue", "magenta",
             "cyan", "white"}
    indexed = r"colour(?:[0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])"
    return value in named or bool(re.fullmatch(indexed, value)) or bool(
        re.fullmatch(r"#[0-9A-Fa-f]{6}", value)
    )


def project_path(value: str | None = None) -> Path:
    path = Path(value or os.getcwd()).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        raise AgentXError(f"Project directory does not exist: {path}")
    return path


def session_name(path: Path) -> str:
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", path.name).strip("-") or "project"
    return f"agentx-{stem[:28]}-{digest}"


def require_command(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise AgentXError(f"Required command not found: {name}")
    return found


def run(args: list[str], *, check: bool = True, capture: bool = False,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        env=env,
    )


def tmux(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    require_command("tmux")
    return run(["tmux", *args], check=check, capture=capture)


def tmux_session_exists(name: str) -> bool:
    return tmux("has-session", "-t", name, check=False, capture=True).returncode == 0


def shell_join_command(parts: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(p)) for p in parts)


def agent_command(profile: Profile, resume: bool = False) -> str:
    if profile.kind == "codex":
        parts = ["env", f"CODEX_HOME={profile.codex_home}", profile.command]
        if resume:
            parts += ["resume", "--last"]
        return shell_join_command(parts)
    if profile.kind == "agy":
        parts = [profile.command]
        if resume:
            parts += ["--continue"]
        return shell_join_command(parts)
    raise AgentXError(f"Unsupported kind: {profile.kind}")


def launch_command(profile: Profile, resume: bool = False) -> str:
    """Run an Agent with an in-pane recovery prompt around normal exits."""
    first_command = agent_command(profile, resume=resume)
    resume_command = agent_command(profile, resume=True)
    return (
        f"{first_command}; agentx_exit_status=$?; "
        "while :; do "
        "printf '\\nAgent exited (status %s). Press Enter to resume, or Ctrl+C to stop.\\n' "
        '"$agentx_exit_status"; '
        "if ! IFS= read -r agentx_recovery_input; then exit \"$agentx_exit_status\"; fi; "
        f"{resume_command}; agentx_exit_status=$?; "
        "done"
    )


FRAME_PANE_COMMAND = "while :; do sleep 86400; done"


def pane_roles(target: str) -> dict[str, str]:
    """Return AgentX pane roles for a tmux window, keyed by role."""
    proc = tmux(
        "list-panes", "-t", target, "-F", "#{pane_id}\t#{pane_active}\t#{@agentx_role}",
        check=False, capture=True,
    )
    if not hasattr(proc, "stdout") or proc.returncode != 0:
        return {}
    rows: list[tuple[str, str, str]] = []
    for line in proc.stdout.splitlines():
        pane_id, separator, remainder = line.partition("\t")
        active, separator2, role = remainder.partition("\t")
        if separator and separator2 and pane_id:
            rows.append((pane_id, active, role))
    roles = {role: pane_id for pane_id, _, role in rows if role}
    if "main" not in roles and rows:
        # Existing AgentX windows predate pane roles and contain one real Agent
        # pane. Prefer the active non-decoration pane during an in-place upgrade.
        candidates = [row for row in rows if not row[2]]
        main = next((row for row in candidates if row[1] == "1"), candidates[0] if candidates else rows[0])
        roles["main"] = main[0]
        tmux("set-option", "-p", "-t", main[0], "@agentx_role", "main",
             check=False, capture=True)
    return roles


def agent_pane_target(target: str) -> str:
    """Resolve a window target to its real Agent pane, excluding frame panes."""
    return pane_roles(target).get("main", target)


def configure_window_frame(target: str, enabled: bool) -> None:
    """Create or remove the helper panes that make a four-sided Agent frame."""
    roles = pane_roles(target)
    main = roles.get("main")
    if not main:
        return
    decoration_roles = ("left", "right", "bottom")
    if not enabled:
        for role in decoration_roles:
            if role in roles:
                tmux("kill-pane", "-t", roles[role], check=False, capture=True)
        tmux("set-hook", "-wu", "-t", target, "after-select-pane", check=False, capture=True)
        tmux("select-pane", "-e", "-t", main, check=False, capture=True)
        return

    split_specs = {
        "left": ("-h", "-b"),
        "right": ("-h",),
        "bottom": ("-v",),
    }
    for role in decoration_roles:
        if role in roles:
            continue
        proc = tmux(
            "split-window", "-d", *split_specs[role], "-l", "1", "-t", main,
            "-P", "-F", "#{pane_id}", FRAME_PANE_COMMAND,
            check=False, capture=True,
        )
        pane_id = proc.stdout.strip() if hasattr(proc, "stdout") and proc.returncode == 0 else ""
        if not pane_id:
            continue
        roles[role] = pane_id
        tmux("set-option", "-p", "-t", pane_id, "@agentx_role", role,
             check=False, capture=True)
        tmux("select-pane", "-d", "-t", pane_id, check=False, capture=True)

    tmux("set-option", "-w", "-t", target, "@agentx_main_pane", main,
         check=False, capture=True)
    focus_hook = f'if-shell -F "#{{!=:#{{pane_id}},{main}}}" "select-pane -t {main}"'
    tmux("set-hook", "-w", "-t", target, "after-select-pane", focus_hook,
         check=False, capture=True)
    tmux("select-pane", "-e", "-t", main, check=False, capture=True)
    tmux("select-pane", "-t", main, check=False, capture=True)


def configure_status_spacing(session: str) -> None:
    """Vertically centre the AgentX bar between terminal-coloured rows."""
    default_format = tmux(
        "show-options", "-gqv", "status-format[0]", check=False, capture=True,
    )
    template = default_format.stdout.rstrip("\n") if hasattr(default_format, "stdout") else ""
    if not template:
        # Older tmux versions without status-format arrays retain the compact
        # single-line status bar instead of receiving a broken second row.
        tmux("set-option", "-t", session, "status", "on")
        return
    tmux("set-option", "-t", session, "status", "3")
    tmux("set-option", "-t", session, "status-format[0]", "#[bg=default]")
    tmux("set-option", "-t", session, "status-format[1]", template)
    tmux("set-option", "-t", session, "status-format[2]", "#[bg=default]")


def configure_session(session: str, project: Path, config: dict[str, Any]) -> None:
    settings = config.get("settings", {})
    prefix = str(settings.get("prefix") or "C-g")
    opts = [
        ("base-index", "1"),
        ("pane-base-index", "1"),
        ("renumber-windows", "on"),
        ("remain-on-exit", "on"),
        ("remain-on-exit-format", "Agent exited (status #{pane_dead_status}). Press Ctrl+G R to resume this Agent."),
        ("status", "on"),
        ("status-interval", "5"),
        ("status-left-length", "80"),
        ("status-right-length", "100"),
        ("status-justify", "absolute-centre"),
        ("status-style", "bg=colour233,fg=colour250"),
        ("message-style", "bg=colour17,fg=colour51,bold"),
        ("mode-style", "bg=colour54,fg=colour231,bold"),
        ("window-status-style", "fg=colour244,bg=colour233"),
        ("window-status-current-style", "fg=colour16,bg=colour51,bold"),
        ("window-status-format", " #I  #W "),
        ("window-status-current-format", " #[fg=colour16]◆ #I  #W "),
        ("window-status-separator", "#[fg=colour238]│"),
        ("status-left", f"#[bg=default]  #[bg=colour51,fg=colour16,bold] AGENTX #[bg=colour233,fg=colour51] ⟫ {project.name} #[default]"),
        ("set-titles", "on"),
        ("set-titles-string", f"AGENTX // {project.name} // #W"),
        ("status-position", "bottom"),
        ("mouse", "on"),
        ("popup-style", "bg=colour234,fg=colour252"),
        ("popup-border-style", "fg=colour51,bg=colour234"),
        ("menu-style", "bg=colour234,fg=colour252"),
        ("menu-selected-style", "bg=colour54,fg=colour231,bold"),
    ]
    for key, value in opts:
        tmux("set-option", "-t", session, key, value)

    frame_enabled = bool(settings.get("frame_enabled", True))
    frame_color = str(settings.get("frame_color") or "colour51")
    frame_background = str(settings.get("frame_background") or "default")
    if not valid_tmux_color(frame_color):
        frame_color = "colour51"
    if not valid_tmux_color(frame_background):
        frame_background = "default"
    frame_title = (
        f"#[align=left,bg={frame_background},fg={frame_color},bold]  #W "
        f"#[fg=colour238,nobold]· #[fg=colour245]{project.name} #[fg={frame_color}] "
    )
    frame_format = f"#{{?#{{==:#{{@agentx_role}},main}},{frame_title},}}"
    # Cover legacy zero-based windows and new one-based Agent slots without
    # depending on a live-window query during session configuration.
    for idx in range(10):
        target_window = f"{session}:{idx}"
        tmux("set-window-option", "-t", target_window, "pane-border-status",
             "top" if frame_enabled else "off", check=False, capture=True)
        if frame_enabled:
            tmux("set-window-option", "-t", target_window, "pane-border-format",
                 frame_format, check=False, capture=True)
            tmux("set-window-option", "-t", target_window, "pane-border-style",
                 f"fg={frame_color},bg={frame_background}", check=False, capture=True)
            tmux("set-window-option", "-t", target_window, "pane-active-border-style",
                 f"fg={frame_color},bg={frame_background}", check=False, capture=True)
            tmux("set-window-option", "-t", target_window, "pane-border-lines",
                 "single", check=False, capture=True)

    windows = tmux("list-windows", "-t", session, "-F", "#{window_index}",
                   check=False, capture=True)
    if hasattr(windows, "stdout") and windows.returncode == 0:
        for index in windows.stdout.splitlines():
            if index:
                configure_window_frame(f"{session}:{index}", frame_enabled)

    tmux("set-option", "-t", session, "@agentx_project", str(project))
    tmux("set-option", "-t", session, "@agentx_version", VERSION)
    tmux("set-option", "-t", session, "prefix", prefix)
    # Ctrl+D is interpreted as EOF by terminal applications.  Intercept it in
    # tmux's root table so a stray keypress can never terminate the Agent (or
    # the recovery shell behind it).  Deliberate EOF remains available as the
    # two-key prefix sequence, which sends Ctrl+D straight to the pane.
    tmux("bind-key", "-T", "root", "C-d", "if-shell", "-F",
         "#{m/r:^agentx-,#{session_name}}",
         "display-message 'Ctrl+D blocked to protect this Agent. Press Ctrl+G Ctrl+D to send EOF intentionally.'",
         "send-keys C-d")
    agentx_session = "#{m/r:^agentx-,#{session_name}}"

    def bind_prefix(key: str, action: str, fallback: str = "") -> None:
        tmux("bind-key", "-T", "prefix", key, "if-shell", "-F",
             agentx_session, action, fallback)

    bind_prefix("C-d", "send-keys C-d")
    bind_prefix("d", "detach-client", "detach-client")
    bind_prefix("s", "choose-tree", "choose-tree")
    bind_prefix("q", "display-panes", "display-panes")
    # Use the module path rather than sys.argv[0]: configure_session is also
    # called by installers/tests/hot-update helpers where argv[0] may be `-c`.
    executable = str(Path(__file__).resolve())
    configured_profiles = profiles(config)

    # Key tables are global to a tmux server. Keep every binding project-agnostic
    # and resolve the project from the session where the key was pressed.
    prompt_switch_base = shell_join_command([executable, "prompt-switch"])
    prompt_runtime_args = (
        '--session "#{session_name}" --current "#{window_name}" --client "#{client_name}"'
    )
    for idx in range(1, 10):
        if idx <= len(configured_profiles):
            target_profile = configured_profiles[idx - 1]
            prompt_command = (
                f"{prompt_switch_base} {prompt_runtime_args} --target "
                f"{shlex.quote(target_profile.name)}"
            )
            agentx_action = f"run-shell {shlex.quote(prompt_command)}"
        else:
            agentx_action = f"display-message 'No Agent configured in slot {idx}.'"
        bind_prefix(str(idx), agentx_action, f"select-window -t :{idx}")
        tmux("bind-key", "-T", "root", f"M-{idx}", "send-keys", f"M-{idx}")

    prompt_switch_command = f"{prompt_switch_base} {prompt_runtime_args}"
    next_agent_action = f"run-shell {shlex.quote(prompt_switch_command)}"
    tmux("bind-key", "-T", "root", "C-Space", "if-shell", "-F",
         "#{m/r:^agentx-,#{session_name}}", next_agent_action, "send-keys C-Space")
    runtime_session_arg = '--session "#{session_name}"'
    handoff_command = f'{shell_join_command([executable, "context", "capture", "--wait"])} {runtime_session_arg}'
    context_command = f'{shell_join_command([executable, "context", "show", "--wait"])} {runtime_session_arg}'
    dashboard_command = f'{shell_join_command([executable, "dashboard", "--wait"])} {runtime_session_arg}'
    keys_command = shell_join_command([executable, "keys", "--wait"])
    refresh_command = f'{shell_join_command([executable, "context", "refresh", "--wait"])} {runtime_session_arg}'
    recover_command = f'{shell_join_command([executable, "recover", "#W"])} {runtime_session_arg}'
    statusline_command = shell_join_command([executable, "statusline", "--project", str(project)])
    tmux("set-option", "-t", session, "status-right",
         f"#{{?client_prefix,#[bg=colour54,fg=colour231,bold] PREFIX #[default] ,}}"
         f"#[fg=colour141]#({statusline_command}) #[fg=colour238]│ #[fg=colour51]%H:%M "
         f"#[bg=default]  ")
    configure_status_spacing(session)
    bind_prefix("h", shell_join_command(["display-popup", "-E", handoff_command]))
    bind_prefix("H", shell_join_command(
        ["display-popup", "-w", "85%", "-h", "80%", "-E", context_command]))
    dashboard_popup = shell_join_command(
        ["display-popup", "-w", "90%", "-h", "80%", "-E", dashboard_command])
    bind_prefix("c", dashboard_popup, "new-window")
    bind_prefix("a", dashboard_popup)
    bind_prefix("r", shell_join_command(
        ["display-popup", "-w", "75%", "-h", "35%", "-E", refresh_command]),
        "refresh-client")
    bind_prefix("R", shell_join_command(["run-shell", recover_command]))
    bind_prefix("?", shell_join_command(
        ["display-popup", "-w", "70%", "-h", "75%", "-E", keys_command]),
        "list-keys")


def create_session(project: Path, config: dict[str, Any], *, resume: bool = False) -> str:
    ps = profiles(config)
    if not ps:
        raise AgentXError(
            "No agent profiles configured.\n"
            "Add them first:\n"
            "  agentx account add codex codex-1\n"
            "  agentx account add codex codex-2\n"
            "  agentx account add agy agy"
        )

    name = session_name(project)
    if tmux_session_exists(name):
        # Reapply options and key bindings so an existing v0.1 tmux session can
        # pick up AgentX upgrades without killing its running agent processes.
        configure_session(name, project, config)
        return name

    first = ps[0]
    first.dir.mkdir(parents=True, exist_ok=True)
    if first.kind == "codex":
        first.codex_home.mkdir(parents=True, exist_ok=True)

    tmux("new-session", "-d", "-s", name, "-n", first.name, "-c", str(project),
         launch_command(first, resume=resume))
    configure_session(name, project, config)

    for p in ps[1:]:
        p.dir.mkdir(parents=True, exist_ok=True)
        if p.kind == "codex":
            p.codex_home.mkdir(parents=True, exist_ok=True)
        tmux("new-window", "-t", name, "-n", p.name, "-c", str(project),
             launch_command(p, resume=resume))

    # Apply per-window frame options after every Agent window exists.
    configure_session(name, project, config)
    tmux("select-window", "-t", f"{name}:1")
    update_project(STATE_DIR, project, active_agent=first.name, last_active_agent=first.name)
    return name


def attach_session(session: str, window: str | None = None) -> None:
    target = f"{session}:{window}" if window else session
    if os.environ.get("TMUX"):
        tmux("switch-client", "-t", target)
    else:
        os.execvp("tmux", ["tmux", "attach-session", "-t", target])


def project_for_session(session: str) -> str:
    proc = tmux("show-option", "-t", session, "-v", "@agentx_project", check=False, capture=True)
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else "?"


def runtime_project(project: str | None = None, session: str | None = None) -> Path:
    """Resolve a project explicitly or from a live AgentX tmux session."""
    if not session:
        return project_path(project)
    if not session.startswith("agentx-") or not tmux_session_exists(session):
        raise AgentXError(f"Not a live AgentX session: {session}")
    stored = project_for_session(session)
    if stored == "?":
        raise AgentXError(f"AgentX project metadata is missing for session: {session}")
    resolved = project_path(stored)
    if session_name(resolved) != session:
        raise AgentXError(f"AgentX session/project metadata mismatch: {session}")
    if project is not None and project_path(project) != resolved:
        raise AgentXError(f"Requested project does not match AgentX session: {session}")
    return resolved


def list_agentx_sessions() -> list[tuple[str, str]]:
    proc = tmux("list-sessions", "-F", "#{session_name}", check=False, capture=True)
    if proc.returncode != 0:
        return []
    return [(name, project_for_session(name)) for name in proc.stdout.splitlines() if name.startswith("agentx-")]


def current_window_states(session: str) -> list[dict[str, str]]:
    proc = tmux("list-windows", "-t", session, "-F",
                "#{window_index}\t#{window_name}\t#{window_active}\t#{pane_dead}\t#{pane_current_command}",
                capture=True)
    rows = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 5:
            rows.append(dict(index=parts[0], name=parts[1], active=parts[2], dead=parts[3], command=parts[4]))
    return rows


def active_agent(session: str) -> str | None:
    for row in current_window_states(session):
        if row["active"] == "1":
            return row["name"]
    return None


def capture_output(session: str, agent: str, lines: int = 300) -> str:
    target = agent_pane_target(f"{session}:{agent}")
    proc = tmux("capture-pane", "-p", "-t", target, "-S", str(-abs(lines)),
                check=False, capture=True)
    if proc.returncode != 0:
        raise AgentXError(f"Agent window does not exist: {agent}")
    return proc.stdout


def send_text(target: str, text: str) -> None:
    target = agent_pane_target(target)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(text)
            temporary = handle.name
        proc = tmux("load-buffer", temporary, check=False, capture=True)
        if proc.returncode != 0:
            raise AgentXError(f"Could not load tmux prompt buffer: {proc.stderr.strip()}")
        proc = tmux("paste-buffer", "-d", "-t", target, check=False, capture=True)
        if proc.returncode != 0:
            raise AgentXError(f"Agent window does not exist: {target}")
        tmux("send-keys", "-t", target, "Enter")
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def respawn_profile(session: str, profile: Profile, project: Path, *, resume: bool) -> None:
    proc = tmux("list-windows", "-t", session, "-F", "#{window_index}\t#{window_name}", capture=True)
    target_index = None
    for line in proc.stdout.splitlines():
        idx, _, name = line.partition("\t")
        if name == profile.name:
            target_index = idx
            break
    command = launch_command(profile, resume=resume)
    if target_index is None:
        tmux("new-window", "-t", session, "-n", profile.name, "-c", str(project), command)
    else:
        target = agent_pane_target(f"{session}:{target_index}")
        tmux("respawn-pane", "-k", "-t", target, "-c", str(project), command)


def cmd_open(args: argparse.Namespace) -> None:
    config = load_config()
    project = project_path(args.project)
    auto_resume = bool(config.get("settings", {}).get("auto_resume", False))
    session = create_session(project, config, resume=args.resume or auto_resume)
    selected = args.profile or active_agent(session)
    if selected:
        if args.profile:
            find_profile(config, selected)
        update_project(STATE_DIR, project, active_agent=selected, last_active_agent=selected)
    attach_session(session, window=args.profile)


def cmd_use(args: argparse.Namespace) -> None:
    config = load_config()
    find_profile(config, args.profile)
    project = project_path(args.project)
    session = create_session(project, config, resume=False)
    previous = active_agent(session)
    update_project(STATE_DIR, project, previous_agent=previous, active_agent=args.profile,
                   last_active_agent=args.profile, last_switch_at=now_iso())
    attach_session(session, window=args.profile)


def collect_and_save(project: Path, agent: str | None, config: dict[str, Any],
                     recent_output: str | None = None) -> tuple[Any, str, str, Path]:
    limit = int(config.get("settings", {}).get("max_diff_size", MAX_DIFF_SIZE))
    snapshot, diff, staged_diff = collect_workspace(project, agent, max_diff_size=max(0, limit))
    directory = save_workspace(STATE_DIR, project, snapshot, diff, staged_diff, recent_output)
    return snapshot, diff, staged_diff, directory


def capture_context_pack(project: Path, source_agent: str | None = None,
                         target_agent: str | None = None) -> Path:
    config = load_config()
    session = session_name(project)
    if not tmux_session_exists(session):
        raise AgentXError(f"No live session for: {project}")
    source = source_agent or active_agent(session)
    if not source:
        raise AgentXError("Could not determine the active agent window.")
    find_profile(config, source)
    if target_agent is not None:
        find_profile(config, target_agent)
    lines = int(config.get("settings", {}).get("recent_output_lines", 300))
    warnings: list[str] = []
    try:
        raw_output = capture_output(session, source, max(0, lines))
    except AgentXError as exc:
        raw_output = ""
        warnings.append(f"Terminal output unavailable: {exc}")
    recent, output_truncated = clean_terminal_output(raw_output, max_lines=max(0, lines))

    print(f"AgentX Context Pack\n\nSource:\n{source}\n\nTarget:\n{target_agent or '(none)'}\n")
    print("[1/2] Collecting local context...")
    limit = int(config.get("settings", {}).get("max_diff_size", MAX_DIFF_SIZE))
    snapshot, diff, staged_diff = collect_workspace(project, source, max_diff_size=max(0, limit))
    print(f"✓ {len(snapshot.status)} changed files")
    created_at = now_iso()
    directory = project_dir(STATE_DIR, project)
    markdown = format_context_pack(
        snapshot, source_agent=source, target_agent=target_agent, created_at=created_at,
        directory=directory, recent_output=recent, output_truncated=output_truncated,
        warnings=warnings,
    )
    print("\n[2/2] Saving Context Pack...")
    directory = save_context_pack(
        STATE_DIR, project, source, target_agent, created_at, markdown, snapshot, diff,
        staged_diff, recent, warnings,
    )
    print(f"✓ {directory / 'context-pack.md'}\n\nReady.")
    return directory


def cmd_handoff(args: argparse.Namespace) -> None:
    """Compatibility alias for `agentx context capture`."""
    project = runtime_project(args.project, getattr(args, "session", None))
    capture_context_pack(project, getattr(args, "profile", None))
    wait_for_close(args)


def wait_for_close(args: argparse.Namespace) -> None:
    if not getattr(args, "wait", False):
        return
    print("\nPress q or Enter to close...", end="", flush=True)
    try:
        if sys.stdin.isatty():
            import termios
            import tty
            descriptor = sys.stdin.fileno()
            previous = termios.tcgetattr(descriptor)
            try:
                tty.setcbreak(descriptor)
                sys.stdin.read(1)
            finally:
                termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)
        else:
            sys.stdin.readline()
    except (EOFError, OSError):
        pass
    print()


def cmd_context(args: argparse.Namespace) -> None:
    project = runtime_project(args.project, getattr(args, "session", None))
    if args.action == "capture":
        capture_context_pack(project, getattr(args, "source", None))
        wait_for_close(args)
        return
    directory, metadata = ensure_project(STATE_DIR, project)
    live_session = session_name(project)
    if shutil.which("tmux") and tmux_session_exists(live_session):
        live_agent = active_agent(live_session)
        if live_agent and live_agent != metadata.get("active_agent"):
            metadata = update_project(STATE_DIR, project, active_agent=live_agent,
                                      last_active_agent=live_agent)
    if args.action == "path":
        print(directory)
        return
    if args.action == "show":
        context_file = directory / "context-pack.md"
        if not context_file.exists():
            context_file = directory / "handoff.md"
        if not context_file.exists():
            print(f"No Context Pack exists for: {project}")
            wait_for_close(args)
            return
        sys.stdout.write(context_file.read_text(encoding="utf-8"))
        wait_for_close(args)
        return
    if args.action == "refresh":
        config = load_config()
        agent = metadata.get("active_agent")
        snapshot, _, _, _ = collect_and_save(project, agent, config)
        print(f"Workspace snapshot refreshed: {directory / 'workspace.json'}")
        if snapshot.diff_truncated:
            print(f"Diff omitted because it is {snapshot.diff_size} bytes (limit exceeded).")
        wait_for_close(args)
        return

    workspace = read_json(directory / "workspace.json", {}) or {}
    context = read_json(directory / "context.json", {}) or {}
    transfer = context.get("transfer") or context.get("handoff", {})
    print(f"Project: {metadata.get('name', project.name)}")
    git_state = workspace.get("git") or {}
    print(f"Branch: {git_state.get('branch') or '(not a Git repository)'}")
    print(f"\nActive Agent:\n{metadata.get('active_agent') or '(none)'}")
    print("\nLast Context Transfer:")
    if transfer:
        destination = transfer.get("to_agent")
        route = transfer.get("from_agent") or "(unknown)"
        if destination:
            route += f" -> {destination}"
        print(f"{route}\n{transfer.get('created_at')}")
    else:
        print("(none)")
    print("\nChanges:")
    status = git_state.get("status", [])
    print("\n".join(status) if status else "(clean or unavailable)")
    handoff_path = directory / "handoff.md"
    if not context.get("transfer") and handoff_path.exists():
        text = handoff_path.read_text(encoding="utf-8")
        goal = re.search(r"^## Current Goal\s*\n+(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
        next_steps = re.search(r"^## Next Steps\s*\n+(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
        print(f"\nHandoff:\n{goal.group(1).strip() if goal else '(not specified)'}")
        print(f"\nNext:\n{next_steps.group(1).strip() if next_steps else '(not specified)'}")
    wait_for_close(args)


def cmd_dashboard(args: argparse.Namespace) -> None:
    project = runtime_project(args.project, getattr(args, "session", None))
    config = load_config()
    directory, metadata = ensure_project(STATE_DIR, project)
    session = session_name(project)
    windows: list[dict[str, str]] = []
    agent = metadata.get("active_agent")
    if shutil.which("tmux") and tmux_session_exists(session):
        windows = current_window_states(session)
        agent = active_agent(session) or agent
        if agent != metadata.get("active_agent"):
            metadata = update_project(STATE_DIR, project, active_agent=agent, last_active_agent=agent)
    if not args.no_refresh:
        collect_and_save(project, agent, config)
    workspace = read_json(directory / "workspace.json", {}) or {}
    context = read_json(directory / "context.json", {}) or {}
    try:
        markdown = (directory / "handoff.md").read_text(encoding="utf-8")
    except OSError:
        markdown = ""
    sys.stdout.write(render_dashboard(metadata, workspace, context, markdown, windows, current_theme()))
    wait_for_close(args)


def cmd_keys(args: argparse.Namespace) -> None:
    sys.stdout.write(render_keys(current_theme()))
    wait_for_close(args)


def cmd_statusline(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    directory = project_dir(STATE_DIR, project)
    workspace = read_json(directory / "workspace.json", {}) or {}
    context = read_json(directory / "context.json", {}) or {}
    print(render_statusline(workspace, context))


def cmd_transfer(args: argparse.Namespace) -> None:
    config = load_config()
    target_profile = find_profile(config, args.profile)
    project = project_path(args.project)
    session = create_session(project, config, resume=False)
    current = active_agent(session)
    if current == target_profile.name:
        raise AgentXError(f"Already using agent: {target_profile.name}")
    target_row = next(
        (row for row in current_window_states(session) if row["name"] == target_profile.name), None
    )
    if target_row is None:
        raise AgentXError(f"Agent window does not exist: {target_profile.name}")
    if target_row["dead"] == "1":
        raise AgentXError(
            f"Target Agent has exited: {target_profile.name}. Recover it before transferring."
        )
    print(f"Current:\n{current or '(unknown)'}\n\nTarget:\n{target_profile.name}\n")
    directory = capture_context_pack(project, current, target_profile.name)
    target = f"{session}:{target_profile.name}"
    print("\nInjecting Context Pack...")
    send_text(target, context_pack_injection_prompt(str(directory)))
    switched_at = now_iso()
    mark_context_pack_transferred(directory, switched_at)
    print("✓\n\nPreparing switch...")
    if not args.defer_switch:
        tmux("select-window", "-t", target)
    print("✓")
    update_project(STATE_DIR, project, previous_agent=current, active_agent=target_profile.name,
                   last_active_agent=target_profile.name, last_switch_at=switched_at,
                   last_transfer_at=switched_at)
    print(f"\nActive:\n{target_profile.name}")
    if args.defer_switch:
        # A display-popup restores its original window when it closes. Schedule
        # the selection just after popup teardown so it cannot undo the switch.
        select_command = shell_join_command(["tmux", "select-window", "-t", target])
        tmux("run-shell", "-b", f"sleep 0.2; {select_command}")
    else:
        attach_session(session, window=target_profile.name)


def cmd_switch(args: argparse.Namespace) -> None:
    """Compatibility alias for `agentx transfer`."""
    cmd_transfer(args)


def cmd_prompt_switch(args: argparse.Namespace) -> None:
    """Show the context-transfer menu for the Agent after the current one."""
    config = load_config()
    configured_profiles = profiles(config)
    if len(configured_profiles) < 2:
        tmux("display-message", "No other Agent is configured.")
        return
    requested_target = getattr(args, "target", None)
    if requested_target:
        target_profile = find_profile(config, requested_target)
    else:
        current_index = next(
            (idx for idx, profile in enumerate(configured_profiles) if profile.name == args.current),
            -1,
        )
        target_profile = configured_profiles[(current_index + 1) % len(configured_profiles)]
    if target_profile.name == args.current:
        message_args = ["display-message", f"Already using Agent: {target_profile.name}"]
        if args.client:
            message_args[1:1] = ["-c", args.client]
        tmux(*message_args)
        return
    project = runtime_project(getattr(args, "project", None), args.session)
    executable = str(Path(__file__).resolve())
    switch_command = shell_join_command([
        executable, "transfer", target_profile.name, "--project", str(project), "--defer-switch",
    ])
    target = f"{args.session}:{target_profile.name}"
    menu_args = [
        "display-menu", "-T", f"Switch to {target_profile.name}: transfer Context Pack?",
        "Yes — capture + transfer", "y",
        f"display-popup -w 80% -h 70% -E {shlex.quote(switch_command)}",
        "No — switch only", "n", f"select-window -t {shlex.quote(target)}",
        "Cancel", "q", "",
    ]
    if args.client:
        menu_args[1:1] = ["-c", args.client]
    tmux(*menu_args)


def login_profile(profile: Profile) -> None:
    require_command(profile.command)
    profile.dir.mkdir(parents=True, exist_ok=True)
    if profile.kind == "codex":
        profile.codex_home.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["CODEX_HOME"] = str(profile.codex_home)
        print(f"\nLogging in Codex profile '{profile.name}'")
        print(f"CODEX_HOME={profile.codex_home}\n")
        proc = subprocess.run([profile.command, "login"], env=env)
        if proc.returncode != 0:
            raise AgentXError(f"Codex login exited with status {proc.returncode}")
        return
    if profile.kind == "agy":
        print(
            f"\nStarting '{profile.command}' for AGY profile '{profile.name}'.\n"
            "Complete sign-in if requested, then exit AGY to return to agentx.\n"
            "This first version assumes one AGY login on the Mac and uses AGY's own auth persistence.\n"
        )
        proc = subprocess.run([profile.command])
        if proc.returncode != 0:
            raise AgentXError(f"AGY exited with status {proc.returncode}")
        return
    raise AgentXError(f"Unsupported kind: {profile.kind}")


def cmd_account_add(args: argparse.Namespace) -> None:
    config = load_config()
    if not valid_profile_name(args.name):
        raise AgentXError("Profile name must start with a letter/number and use only letters, numbers, '.', '_' or '-'.")
    if any(p.name == args.name for p in profiles(config)):
        raise AgentXError(f"Profile already exists: {args.name}")
    command = args.command or default_command(args.kind)
    require_command(command)
    config["profiles"].append({"name": args.name, "kind": args.kind, "command": command, "created_at": int(time.time())})
    save_config(config)
    p = find_profile(config, args.name)
    p.dir.mkdir(parents=True, exist_ok=True)
    if p.kind == "codex":
        p.codex_home.mkdir(parents=True, exist_ok=True)
    print(f"Added {p.kind} profile: {p.name}")
    print(f"Profile directory: {p.dir}")
    if not args.no_login:
        login_profile(p)


def cmd_account_login(args: argparse.Namespace) -> None:
    login_profile(find_profile(load_config(), args.name))


def cmd_account_remove(args: argparse.Namespace) -> None:
    config = load_config()
    p = find_profile(config, args.name)
    if not args.yes:
        answer = input(f"Remove profile '{p.name}' from agentx config? This will NOT delete {p.dir}. [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return
    config["profiles"] = [raw for raw in config["profiles"] if raw.get("name") != p.name]
    save_config(config)
    print(f"Removed profile from config: {p.name}")
    print(f"Authentication/profile files were kept at: {p.dir}")


def cmd_accounts(args: argparse.Namespace) -> None:
    ps = profiles(load_config())
    if not ps:
        print("No profiles configured.")
        return
    print(f"{'#':<3} {'NAME':<20} {'TYPE':<8} {'COMMAND':<16} PATH")
    for idx, p in enumerate(ps, 1):
        print(f"{idx:<3} {p.name:<20} {p.kind:<8} {p.command:<16} {p.dir}")


def cmd_status(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    name = session_name(project)
    if not tmux_session_exists(name):
        print(f"No live agentx session for: {project}")
        return
    print(f"Project: {project}\nSession: {name}\n")
    print(f"{'#':<3} {'AGENT':<20} {'ACTIVE':<8} {'STATE':<10} COMMAND")
    for row in current_window_states(name):
        state = "exited" if row["dead"] == "1" else "running"
        active = "*" if row["active"] == "1" else ""
        print(f"{row['index']:<3} {row['name']:<20} {active:<8} {state:<10} {row['command']}")


def cmd_sessions(args: argparse.Namespace) -> None:
    sessions = list_agentx_sessions()
    if not sessions:
        print("No live agentx sessions.")
        return
    print(f"{'SESSION':<48} PROJECT")
    for name, project in sessions:
        print(f"{name:<48} {project}")


def cmd_new(args: argparse.Namespace) -> None:
    config = load_config()
    p = find_profile(config, args.profile)
    project = project_path(args.project)
    session = create_session(project, config, resume=False)
    respawn_profile(session, p, project, resume=args.resume)
    attach_session(session, window=p.name)


def cmd_recover(args: argparse.Namespace) -> None:
    """Resume an exited Agent without replacing a healthy process."""
    config = load_config()
    project = runtime_project(args.project, getattr(args, "session", None))
    session = session_name(project)
    if not tmux_session_exists(session):
        raise AgentXError(f"No live session for: {project}")
    profile_name = args.profile or active_agent(session)
    if not profile_name:
        raise AgentXError("Could not determine the active Agent to recover.")
    profile = find_profile(config, profile_name)
    row = next((item for item in current_window_states(session) if item["name"] == profile.name), None)
    if row is None:
        raise AgentXError(f"Agent window does not exist: {profile.name}")
    if row["dead"] != "1":
        tmux("display-message", "-t", f"{session}:{profile.name}",
             f"{profile.name} is still running; nothing was restarted.")
        return
    respawn_profile(session, profile, project, resume=True)
    tmux("display-message", "-t", f"{session}:{profile.name}",
         f"Resumed {profile.name} after it exited.")


def cmd_detach(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    name = session_name(project)
    if not tmux_session_exists(name):
        print(f"No live session for: {project}")
        return
    proc = tmux("detach-client", "-s", name, check=False, capture=True)
    print("Detached. Agent processes are still running in tmux." if proc.returncode == 0 else "No attached client, but the session is still running.")


def cmd_kill(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    name = session_name(project)
    if not tmux_session_exists(name):
        print(f"No live session for: {project}")
        return
    if not args.yes:
        answer = input(f"Kill ALL agent processes for project '{project.name}'? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return
    tmux("kill-session", "-t", name)
    print(f"Killed agentx session: {name}")


def cmd_logs(args: argparse.Namespace) -> None:
    config = load_config()
    p = find_profile(config, args.profile)
    project = project_path(args.project)
    name = session_name(project)
    if not tmux_session_exists(name):
        raise AgentXError(f"No live session for: {project}")
    target = agent_pane_target(f"{name}:{p.name}")
    proc = tmux("capture-pane", "-p", "-t", target, "-S", str(-abs(args.lines)), capture=True)
    sys.stdout.write(proc.stdout)


def cmd_doctor(args: argparse.Namespace) -> None:
    checks: list[tuple[str, bool, str]] = []
    tmux_path = shutil.which("tmux")
    checks.append(("tmux", bool(tmux_path), tmux_path or "not found"))
    ps = profiles(load_config())
    checks.append(("profiles", bool(ps), f"{len(ps)} configured" if ps else "none configured"))
    for p in ps:
        command_path = shutil.which(p.command)
        checks.append((f"{p.name} ({p.kind})", bool(command_path), command_path or f"{p.command} not found"))
    checks.append(("config", CONFIG_PATH.exists(), str(CONFIG_PATH)))
    for label, ok, detail in checks:
        print(f"[{'OK' if ok else '!!'}] {label:<24} {detail}")
    if not tmux_path:
        print("\nInstall tmux on macOS with:\n  brew install tmux")


def cmd_config(args: argparse.Namespace) -> None:
    config = load_config()
    if args.action == "path":
        print(CONFIG_PATH)
    elif args.action == "show":
        print(json.dumps(config, indent=2, ensure_ascii=False))
    elif args.action == "set-auto-resume":
        value = args.value.lower()
        if value not in {"on", "off"}:
            raise AgentXError("Value must be 'on' or 'off'.")
        config["settings"]["auto_resume"] = value == "on"
        save_config(config)
        print(f"auto_resume = {value}")
    elif args.action == "set-frame":
        value = args.value.lower()
        if value not in {"on", "off"}:
            raise AgentXError("Value must be 'on' or 'off'.")
        config["settings"]["frame_enabled"] = value == "on"
        save_config(config)
        print(f"frame_enabled = {value}")
        reconfigure_live_sessions(config)
    elif args.action in {"set-frame-color", "set-frame-background"}:
        value = args.value
        if not valid_tmux_color(value):
            raise AgentXError(
                "Color must be default, a basic tmux color, colour0..colour255, or #RRGGBB."
            )
        key = "frame_color" if args.action == "set-frame-color" else "frame_background"
        config["settings"][key] = value
        save_config(config)
        print(f"{key} = {value}")
        reconfigure_live_sessions(config)


def reconfigure_live_sessions(config: dict[str, Any]) -> int:
    if not shutil.which("tmux"):
        return 0
    updated = 0
    for session, project in list_agentx_sessions():
        if project == "?":
            continue
        configure_session(session, Path(project), config)
        updated += 1
    if updated:
        print(f"Updated {updated} live AgentX session(s).")
    return updated


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Multi-agent CLI session manager for Codex and AGY.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  agentx account add codex codex-1
  agentx account add codex codex-2
  agentx account add agy agy

  cd ~/projects/my-project
  agentx
  agentx context refresh
  agentx dashboard
  agentx context capture
  agentx transfer codex-2

Inside agentx:
  Ctrl+Space   ask about context, then switch to the next agent
  Ctrl+G 1     switch to agent 1
  Ctrl+G 2     switch to agent 2
  Ctrl+G 3     switch to agent 3
  Ctrl+G h     capture a local Context Pack
  Ctrl+G H     show the current Context Pack
  Ctrl+G c     show context status
  Ctrl+G a     show agent dashboard
  Ctrl+G r     refresh workspace snapshot
  Ctrl+G R     resume the current Agent if it exited
  Ctrl+G C-d   send Ctrl+D (EOF) intentionally
  Ctrl+G ?     show key reference
  Ctrl+G d     detach and keep all agents running
""",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="subcommand")

    p = sub.add_parser("open", help="Open/attach the agent workspace for a project.")
    p.add_argument("--project", "-p")
    p.add_argument("--profile")
    p.add_argument("--resume", action="store_true")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("use", help="Open a project directly on one agent.")
    p.add_argument("profile")
    p.add_argument("--project", "-p")
    p.set_defaults(func=cmd_use)

    p = sub.add_parser("transfer", help="Capture a Context Pack, inject it, and switch Agent.")
    p.add_argument("profile")
    p.add_argument("--project", "-p")
    p.add_argument("--defer-switch", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_transfer)

    p = sub.add_parser("switch", help="Compatibility alias for `agentx transfer`.")
    p.add_argument("profile")
    p.add_argument("--project", "-p")
    p.add_argument("--defer-switch", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_switch)

    p = sub.add_parser("prompt-switch", help=argparse.SUPPRESS)
    p.add_argument("--project")
    p.add_argument("--session", required=True)
    p.add_argument("--current", required=True)
    p.add_argument("--client", default="")
    p.add_argument("--target")
    p.set_defaults(func=cmd_prompt_switch)

    p = sub.add_parser("handoff", help="Compatibility alias for `agentx context capture`.")
    p.add_argument("profile", nargs="?")
    p.add_argument("--project", "-p")
    p.add_argument("--session", help=argparse.SUPPRESS)
    p.add_argument("--wait", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_handoff)

    p = sub.add_parser("context", help="Capture, show, or refresh shared project context.")
    p.add_argument("action", nargs="?", default="status",
                   choices=["status", "path", "show", "refresh", "capture"])
    p.add_argument("--project", "-p")
    p.add_argument("--session", help=argparse.SUPPRESS)
    p.add_argument("--source")
    p.add_argument("--wait", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("dashboard", help="Show the AgentX project dashboard.")
    p.add_argument("--project", "-p")
    p.add_argument("--session", help=argparse.SUPPRESS)
    p.add_argument("--no-refresh", action="store_true", help="Use the saved snapshot without running Git.")
    p.add_argument("--wait", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("keys", help="Show AgentX tmux key bindings.")
    p.add_argument("--wait", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_keys)

    p = sub.add_parser("statusline", help="Render the compact tmux status summary.")
    p.add_argument("--project", "-p")
    p.set_defaults(func=cmd_statusline)

    account = sub.add_parser("account", help="Manage agent login profiles.")
    account_sub = account.add_subparsers(dest="account_command", required=True)
    p = account_sub.add_parser("add", help="Add an agent profile and log in.")
    p.add_argument("kind", choices=["codex", "agy"])
    p.add_argument("name")
    p.add_argument("--command")
    p.add_argument("--no-login", action="store_true")
    p.set_defaults(func=cmd_account_add)
    p = account_sub.add_parser("login", help="Run login/setup for an existing profile.")
    p.add_argument("name")
    p.set_defaults(func=cmd_account_login)
    p = account_sub.add_parser("remove", help="Remove a profile from config.")
    p.add_argument("name")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_account_remove)

    p = sub.add_parser("accounts", help="List configured agent profiles.")
    p.set_defaults(func=cmd_accounts)
    p = sub.add_parser("status", help="Show live agent state for a project.")
    p.add_argument("--project", "-p")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("sessions", help="List all live agentx project sessions.")
    p.set_defaults(func=cmd_sessions)
    p = sub.add_parser("new", help="Restart one agent window.")
    p.add_argument("profile")
    p.add_argument("--project", "-p")
    p.add_argument("--resume", action="store_true")
    p.set_defaults(func=cmd_new)
    p = sub.add_parser("recover", help="Resume an Agent window only if it has exited.")
    p.add_argument("profile", nargs="?")
    p.add_argument("--project", "-p")
    p.add_argument("--session", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_recover)
    p = sub.add_parser("detach", help="Detach clients but keep agents running.")
    p.add_argument("--project", "-p")
    p.set_defaults(func=cmd_detach)
    p = sub.add_parser("kill", help="Kill all agents for a project.")
    p.add_argument("--project", "-p")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_kill)
    p = sub.add_parser("logs", help="Print recent terminal output for an agent.")
    p.add_argument("profile")
    p.add_argument("--project", "-p")
    p.add_argument("--lines", type=int, default=200)
    p.set_defaults(func=cmd_logs)
    p = sub.add_parser("doctor", help="Check local dependencies and configured agents.")
    p.set_defaults(func=cmd_doctor)

    config = sub.add_parser("config", help="Show or change agentx settings.")
    config_sub = config.add_subparsers(dest="action", required=True)
    config_sub.add_parser("show").set_defaults(func=cmd_config)
    config_sub.add_parser("path").set_defaults(func=cmd_config)
    p = config_sub.add_parser("set-auto-resume")
    p.add_argument("value")
    p.set_defaults(func=cmd_config)
    p = config_sub.add_parser("set-frame")
    p.add_argument("value")
    p.set_defaults(func=cmd_config)
    p = config_sub.add_parser("set-frame-color")
    p.add_argument("value")
    p.set_defaults(func=cmd_config)
    p = config_sub.add_parser("set-frame-background")
    p.add_argument("value")
    p.set_defaults(func=cmd_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "subcommand", None):
        args = parser.parse_args(["open"])
    try:
        args.func(args)
        return 0
    except AgentXError as exc:
        print(f"agentx: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except subprocess.CalledProcessError as exc:
        print(f"agentx: command failed ({exc.returncode}): {' '.join(exc.cmd)}", file=sys.stderr)
        if exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)
        return exc.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
