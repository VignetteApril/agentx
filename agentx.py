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
from agentxlib.cockpit import CockpitAgent, run_cockpit
from agentxlib.handoff import (clean_terminal_output, context_pack_injection_prompt,
                               format_context_pack)
from agentxlib.memory import (add_memory, context_memory, initialise_memory, memory_root,
                               read_memory_notes, search_memory, shorten_note)
from agentxlib.projects import ensure_project, now_iso, project_dir, project_id, read_json, update_project
from agentxlib.theme import AGENTX_PALETTE, current_theme
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
        "memory_path": None,
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
    data["settings"].setdefault("memory_path", None)
    return data


def save_config(config: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def configured_memory_root(config: dict[str, Any]) -> Path:
    value = config.get("settings", {}).get("memory_path")
    return memory_root(str(value) if value else None, STATE_DIR / "memory")


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


def tmux_format_literal(value: str) -> str:
    """Escape user-controlled text embedded in a tmux format string."""
    return value.replace("#", "##")


def display_project_path(project: Path) -> str:
    """Return a compact project path for terminal chrome."""
    try:
        relative = project.relative_to(HOME)
    except ValueError:
        return str(project)
    return "~" if not relative.parts else f"~/{relative}"


def cockpit_shell_command() -> str:
    """Build the project-agnostic command expanded by the invoking tmux client."""
    executable = str(Path(__file__).resolve())
    base = shell_join_command([executable, "cockpit"])
    return (
        f'{base} --session "#{{session_name}}" --current "#{{window_name}}" '
        f'--client "#{{client_name}}"'
    )


def cockpit_popup_args() -> list[str]:
    """Arguments for the tmux 3.2-compatible full-client cockpit popup."""
    return ["display-popup", "-w", "100%", "-h", "100%", "-E", cockpit_shell_command()]


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
    set_running = 'tmux set-option -p -t "$TMUX_PANE" @agentx_state running 2>/dev/null || :'
    set_exited = 'tmux set-option -p -t "$TMUX_PANE" @agentx_state exited 2>/dev/null || :'
    return (
        f"{set_running}; {first_command}; agentx_exit_status=$?; {set_exited}; "
        "while :; do "
        "printf '\\nAgent exited (status %s). Press Enter to resume, or Ctrl+C to stop.\\n' "
        '"$agentx_exit_status"; '
        "if ! IFS= read -r agentx_recovery_input; then exit \"$agentx_exit_status\"; fi; "
        f"{set_running}; {resume_command}; agentx_exit_status=$?; {set_exited}; "
        "done"
    )


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


def configure_window_frame(target: str, enabled: bool, background: str = "default") -> None:
    """Keep a compact top title without empty decoration gutters."""
    roles = pane_roles(target)
    main = roles.get("main")
    if not main:
        return
    decoration_roles = ("left", "right", "bottom")
    if not enabled:
        for role in decoration_roles:
            if role in roles:
                tmux("kill-pane", "-t", roles[role], check=False, capture=True)
        for option in ("window-style", "window-active-style"):
            tmux("set-window-option", "-u", "-t", target, option,
                 check=False, capture=True)
            tmux("set-option", "-pu", "-t", main, option,
                 check=False, capture=True)
        tmux("set-hook", "-wu", "-t", target, "window-resized",
             check=False, capture=True)
        tmux("set-hook", "-wu", "-t", target, "after-select-pane", check=False, capture=True)
        tmux("select-pane", "-e", "-t", main, check=False, capture=True)
        return

    # Older layouts used side and bottom helper panes. Remove them in place so
    # the Agent touches the single-line tmux status bar without a blank gap.
    for role in decoration_roles:
        pane_id = roles.pop(role, None)
        if pane_id:
            tmux("kill-pane", "-t", pane_id, check=False, capture=True)

    for option in ("window-style", "window-active-style"):
        tmux("set-window-option", "-t", target, option, f"bg={background}",
             check=False, capture=True)
        tmux("set-option", "-p", "-t", main, option, "bg=default",
             check=False, capture=True)

    tmux("set-hook", "-wu", "-t", target, "window-resized", check=False, capture=True)
    tmux("set-hook", "-wu", "-t", target, "after-select-pane", check=False, capture=True)

    tmux("set-option", "-w", "-t", target, "@agentx_main_pane", main,
         check=False, capture=True)
    tmux("select-pane", "-e", "-t", main, check=False, capture=True)
    tmux("select-pane", "-t", main, check=False, capture=True)


def configure_status_spacing(session: str) -> None:
    """Use a compact footer so small terminals keep room for the Agent."""
    tmux("set-option", "-t", session, "status", "on")


def configure_mouse_scrolling() -> None:
    """Give fullscreen Agents without mouse reporting a native paging fallback."""
    # Fullscreen applications keep their own transcript, outside tmux history.
    # Forwarding mouse events to one which has not enabled reporting drops the
    # wheel input; entering copy mode cannot recover that application's history.
    # Target the pane under the pointer, including when it is not the active pane.
    needs_paging = (
        "#{&&:#{m/r:^agentx-,#{session_name}},"
        "#{&&:#{alternate_on},#{&&:#{!:#{mouse_any_flag}},#{!:#{pane_in_mode}}}}}"
    )
    passthrough = "send-keys -M"
    default_up = (
        'if-shell -F -t = "#{||:#{alternate_on},'
        '#{||:#{pane_in_mode},#{mouse_any_flag}}}" '
        '"send-keys -M" "copy-mode -e -t ="'
    )
    for key, page_key, fallback in (
        ("WheelUpPane", "PPage", default_up),
        ("WheelDownPane", "NPage", passthrough),
    ):
        tmux("bind-key", "-T", "root", key, "if-shell", "-F", "-t", "=",
             needs_paging, f"send-keys -t = {page_key}", fallback)


def configure_session(session: str, project: Path, config: dict[str, Any]) -> None:
    settings = config.get("settings", {})
    prefix = str(settings.get("prefix") or "C-g")
    configured_profiles = profiles(config)
    palette = AGENTX_PALETTE
    frame_enabled = bool(settings.get("frame_enabled", True))
    frame_color = str(settings.get("frame_color") or palette.primary)
    frame_background = str(settings.get("frame_background") or "default")
    if not valid_tmux_color(frame_color):
        frame_color = palette.primary
    if not valid_tmux_color(frame_background):
        frame_background = "default"
    # The footer is its own thin graphite strip, independent of the outer
    # frame colour (which may be pure black).
    status_background = "#20242e"
    project_label = tmux_format_literal(display_project_path(project))
    window_tab_options = {
        "window-status-style",
        "window-status-current-style",
        "window-status-format",
        "window-status-current-format",
        "window-status-separator",
    }
    # These cosmetic options were added after tmux 3.2.  Keep them best-effort
    # so the advertised minimum version retains a functional cockpit, while
    # newer tmux releases receive the complete AgentX chrome.
    optional_session_options = {
        "remain-on-exit-format",
        "popup-style",
        "popup-border-style",
        "menu-style",
        "menu-selected-style",
    }
    opts = [
        ("base-index", "1"),
        ("pane-base-index", "1"),
        ("renumber-windows", "on"),
        ("remain-on-exit", "on"),
        ("remain-on-exit-format", "Agent exited (status #{pane_dead_status}). Press Ctrl+G R to resume all exited Agents."),
        ("status", "on"),
        ("status-interval", "5"),
        ("status-left-length", "80"),
        ("status-right-length", "100"),
        ("status-justify", "left"),
        ("status-style", f"fg={palette.text},bg={status_background}"),
        ("message-style", palette.tmux_style("primary", "surface", bold=True)),
        ("mode-style", palette.tmux_style("background", "secondary", bold=True)),
        ("window-status-style", f"fg={palette.muted},bg={status_background}"),
        ("window-status-current-style", palette.tmux_style("background", "primary", bold=True)),
        ("window-status-format", " #I  #W "),
        ("window-status-current-format", f" #[fg={palette.background}]#I  #W "),
        ("window-status-separator", f"#[fg={palette.border},bg={status_background}] · "),
        ("status-left", f"#[fg={palette.muted},bg={status_background}] {project_label} "),
        ("set-titles", "on"),
        ("set-titles-string", f"AGENTX // {tmux_format_literal(project.name)} // #W"),
        ("status-position", "bottom"),
        ("mouse", "on"),
        ("popup-style", palette.tmux_style("text", "background")),
        # tmux 3.2 has neither display-popup -B nor popup-border-style. Newer
        # versions render this as visually borderless; 3.2 keeps its default
        # border through the best-effort option handling above.
        ("popup-border-style", palette.tmux_style("background", "background")),
        ("menu-style", palette.tmux_style("text", "surface")),
        ("menu-selected-style", palette.tmux_style("background", "secondary", bold=True)),
    ]
    for key, value in opts:
        if key in optional_session_options:
            tmux("set-option", "-t", session, key, value,
                 check=False, capture=True)
        else:
            tmux("set-option", "-t", session, key, value)
        if key in window_tab_options:
            for profile in configured_profiles:
                tmux("set-window-option", "-t", f"{session}:{profile.name}", key, value,
                     check=False, capture=True)

    # Draw the title within the Agent pane so its corner glyphs stay inside
    # the available width rather than creating protruding line ends.
    title_width = "#{e|+:11,#{e|+:#{w:window_name},#{w:#{b:@agentx_project}}}}"
    rule_width = f"#{{e|-:#{{pane_width}},{title_width}}}"
    frame_title = (
        f"#[align=left]#[bg={frame_background}]#[fg={palette.border}]╭── "
        f"#[fg={frame_color}]#[bold]#W "
        f"#[default]#[bg={frame_background}]#[fg={palette.border}]· "
        f"#[fg={palette.muted}]#{{b:@agentx_project}} "
        f"#[fg={palette.border}]#{{R:─,{rule_width}}}╮"
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
                 f"fg={palette.border},bg={frame_background}", check=False, capture=True)
            tmux("set-window-option", "-t", target_window, "pane-active-border-style",
                 f"fg={palette.border},bg={frame_background}", check=False, capture=True)
            tmux("set-window-option", "-t", target_window, "pane-border-lines",
                 "spaces", check=False, capture=True)

    windows = tmux("list-windows", "-t", session, "-F", "#{window_index}",
                   check=False, capture=True)
    if hasattr(windows, "stdout") and windows.returncode == 0:
        for index in windows.stdout.splitlines():
            if index:
                configure_window_frame(f"{session}:{index}", frame_enabled,
                                       frame_background)

    tmux("set-option", "-t", session, "@agentx_project", str(project))
    tmux("set-option", "-t", session, "@agentx_version", VERSION)
    tmux("set-option", "-t", session, "prefix", prefix)
    configure_mouse_scrolling()
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
    agent_dashboard_command = (
        f'{shell_join_command([executable, "dashboard", "--wait", "--section", "agents"])} '
        f"{runtime_session_arg}"
    )
    context_dashboard_command = (
        f'{shell_join_command([executable, "dashboard", "--wait", "--section", "context"])} '
        f"{runtime_session_arg}"
    )
    keys_command = shell_join_command([executable, "keys", "--wait"])
    refresh_command = f'{shell_join_command([executable, "context", "refresh", "--wait"])} {runtime_session_arg}'
    recover_command = f'{shell_join_command([executable, "recover", "--all"])} {runtime_session_arg}'
    statusline_command = shell_join_command([executable, "statusline", "--project", str(project)])
    tmux("set-option", "-t", session, "status-right",
         f"#{{?client_prefix,#[bg={palette.secondary},fg={status_background},bold] PREFIX "
         f"#[bg={status_background}] ,}}"
         f"#[fg={palette.secondary},bg={status_background}]#({statusline_command}) "
         f"#[fg={palette.border}]│ #[fg={palette.primary}]v{VERSION} ")
    configure_status_spacing(session)
    bind_prefix("x", shell_join_command(cockpit_popup_args()))
    bind_prefix("h", shell_join_command(["display-popup", "-E", handoff_command]))
    bind_prefix("H", shell_join_command(
        ["display-popup", "-w", "85%", "-h", "80%", "-E", context_command]))
    context_dashboard_popup = shell_join_command(
        ["display-popup", "-w", "90%", "-h", "75%", "-E", context_dashboard_command])
    agent_dashboard_popup = shell_join_command(
        ["display-popup", "-w", "80%", "-h", "65%", "-E", agent_dashboard_command])
    bind_prefix("c", context_dashboard_popup, "new-window")
    bind_prefix("a", agent_dashboard_popup)
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


def attach_session(session: str, window: str | None = None, *, cockpit: bool = False) -> None:
    target = f"{session}:{window}" if window else session
    if os.environ.get("TMUX"):
        parts = ["switch-client", "-t", target]
        if cockpit:
            parts.extend([";", *cockpit_popup_args()])
        tmux(*parts)
    else:
        parts = ["tmux", "attach-session", "-t", target]
        if cockpit:
            parts.extend([";", *cockpit_popup_args()])
        os.execvp("tmux", parts)


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
                "#{window_index}\t#{window_name}\t#{window_active}\t#{pane_dead}\t#{pane_current_command}\t#{@agentx_state}",
                capture=True)
    rows = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 6:
            rows.append(dict(index=parts[0], name=parts[1], active=parts[2], dead=parts[3],
                             command=parts[4], agentx_state=parts[5]))
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


def schedule_window_switch(target: str, message: str, client: str = "") -> None:
    """Select a window after popup teardown and show a scoped confirmation."""
    if client:
        # A session may have multiple attached terminals.  switch-client's
        # pane-target special case changes only the client which opened this
        # cockpit instead of moving every client attached to the session.
        switch_parts = ["tmux", "switch-client", "-c", client, "-t", target]
    else:
        switch_parts = ["tmux", "select-window", "-t", target]
    select_command = shell_join_command(switch_parts)
    notice_parts = ["tmux", "display-message"]
    if client:
        notice_parts.extend(["-c", client])
    notice_parts.extend(["-d", "5000", message])
    notice_command = shell_join_command(notice_parts)
    tmux("run-shell", "-b", f"sleep 0.2; {select_command}; {notice_command}")


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


def pane_waiting_for_recovery(session: str, row: dict[str, str]) -> bool:
    """Recognise both new state markers and pre-upgrade recovery prompts."""
    if row.get("agentx_state") == "exited":
        return True
    if row.get("agentx_state") == "running":
        return False
    if row.get("dead") == "1":
        return False
    target = agent_pane_target(f"{session}:{row['name']}")
    proc = tmux("capture-pane", "-p", "-t", target, "-S", "-20",
                check=False, capture=True)
    if getattr(proc, "returncode", 1) != 0:
        return False
    output = " ".join(getattr(proc, "stdout", "").split())
    return bool(re.search(
        r"Agent exited \(status [^)]*\)\. Press Enter to resume, or Ctrl\+C to stop\.",
        output,
    ))


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
    attach_session(session, window=args.profile, cockpit=args.profile is None)


def cmd_use(args: argparse.Namespace) -> None:
    config = load_config()
    find_profile(config, args.profile)
    project = project_path(args.project)
    session = create_session(project, config, resume=False)
    previous = active_agent(session)
    update_project(STATE_DIR, project, previous_agent=previous, active_agent=args.profile,
                   last_active_agent=args.profile, last_switch_at=now_iso())
    attach_session(session, window=args.profile)


def cockpit_agent_rows(config: dict[str, Any], session: str) -> list[CockpitAgent]:
    windows = {row.get("name", ""): row for row in current_window_states(session)}
    rows: list[CockpitAgent] = []
    for profile in profiles(config):
        window = windows.get(profile.name)
        if window is None:
            status = "missing"
        elif window.get("dead") == "1" or window.get("agentx_state") == "exited":
            status = "dead"
        else:
            status = "online"
        rows.append(CockpitAgent(profile.name, status=status, detail=profile.kind.upper()))
    return rows


def cmd_cockpit(args: argparse.Namespace) -> None:
    """Run the per-client Agent selector inside a full-size tmux popup."""
    project = runtime_project(session=args.session)
    config = load_config()
    configured_profiles = profiles(config)
    configured_names = {profile.name for profile in configured_profiles}
    current = args.current if args.current in configured_names else active_agent(args.session)
    if current not in configured_names:
        current = configured_profiles[0].name if configured_profiles else None
    notice: str | None = None

    while True:
        action = run_cockpit(
            project.name,
            display_project_path(project),
            VERSION,
            cockpit_agent_rows(config, args.session),
            current,
            current_theme(),
            notice=notice,
        )
        notice = None
        if action.kind == "close" or not action.target:
            return
        target_profile = find_profile(config, action.target)
        target = f"{args.session}:{target_profile.name}"
        if action.kind == "transfer":
            transfer_args = argparse.Namespace(
                profile=target_profile.name,
                project=str(project),
                defer_switch=True,
                source=current,
                client=args.client,
            )
            try:
                cmd_transfer(transfer_args)
            except AgentXError as exc:
                notice = f"Transfer failed: {exc}"
                continue
            except subprocess.CalledProcessError as exc:
                notice = f"Transfer command failed with status {exc.returncode}."
                continue
            return
        if action.kind == "switch":
            switched_at = now_iso()
            update_project(
                STATE_DIR,
                project,
                previous_agent=current,
                active_agent=target_profile.name,
                last_active_agent=target_profile.name,
                last_switch_at=switched_at,
            )
            schedule_window_switch(
                target,
                f"Switched: {current or 'source'} → {target_profile.name}",
                args.client,
            )
            return
        notice = f"Unsupported cockpit action: {action.kind}"


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
    vault = configured_memory_root(config)
    memory_notes = context_memory(
        vault, project, snapshot.branch,
        snapshot.modified_files + snapshot.staged_files + snapshot.untracked_files,
    )
    memory_metadata = {
        "vault": str(vault),
        "count": len(memory_notes),
        "notes": [{"title": note.title, "file": str(note.path)} for note in memory_notes],
    }
    markdown = format_context_pack(
        snapshot, source_agent=source, target_agent=target_agent, created_at=created_at,
        directory=directory, recent_output=recent, output_truncated=output_truncated,
        warnings=warnings, memory_notes=memory_notes, memory_root=vault,
    )
    print("\n[2/2] Saving Context Pack...")
    directory = save_context_pack(
        STATE_DIR, project, source, target_agent, created_at, markdown, snapshot, diff,
        staged_diff, recent, warnings, memory_metadata,
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
    memory = context.get("memory") or {}
    if memory:
        print(f"\nLocal Memory:\n{memory.get('count', 0)} relevant note(s)\n{memory.get('vault', '(unknown vault)')}")
    handoff_path = directory / "handoff.md"
    if not context.get("transfer") and handoff_path.exists():
        text = handoff_path.read_text(encoding="utf-8")
        goal = re.search(r"^## Current Goal\s*\n+(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
        next_steps = re.search(r"^## Next Steps\s*\n+(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
        print(f"\nHandoff:\n{goal.group(1).strip() if goal else '(not specified)'}")
        print(f"\nNext:\n{next_steps.group(1).strip() if next_steps else '(not specified)'}")
    wait_for_close(args)


def cmd_memory(args: argparse.Namespace) -> None:
    config = load_config()
    root = configured_memory_root(config)
    if args.action == "path":
        print(root)
        return
    if args.action == "init":
        initialise_memory(root)
        print(f"Local memory ready: {root}")
        print("Open this folder as an Obsidian vault, or edit its Markdown files directly.")
        return
    project = None if getattr(args, "global_scope", False) else project_path(getattr(args, "project", None))
    if args.action == "add":
        target = add_memory(root, args.title, args.note, project, args.tag)
        print(f"Saved local memory: {target}")
        return
    if args.action == "list":
        notes = (search_memory(root, project.name, project, limit=args.limit) if project else
                 read_memory_notes(root)[:max(0, args.limit)])
    else:
        notes = search_memory(root, args.query, project, limit=args.limit)
    if not notes:
        print(f"No local memory found in: {root}")
        return
    for note in notes:
        relative = note.path.relative_to(root) if note.path.is_relative_to(root) else note.path
        tags = f"  [{', '.join(note.tags)}]" if note.tags else ""
        print(f"\n{note.title}{tags}\n{relative}\n{shorten_note(note, 700)}")


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
    sys.stdout.write(render_dashboard(metadata, workspace, context, markdown, windows, current_theme(),
                                      section=getattr(args, "section", "all")))
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
    current = getattr(args, "source", None) or active_agent(session)
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
    success_notice = f"Context injected: {current or 'source'} → {target_profile.name}"
    if not args.defer_switch:
        tmux("select-window", "-t", target)
        tmux("display-message", "-d", "5000", success_notice)
    print("✓")
    update_project(STATE_DIR, project, previous_agent=current, active_agent=target_profile.name,
                   last_active_agent=target_profile.name, last_switch_at=switched_at,
                   last_transfer_at=switched_at)
    print(f"\nActive:\n{target_profile.name}")
    if args.defer_switch:
        # A display-popup restores its original window when it closes. Schedule
        # the selection just after popup teardown so it cannot undo the switch.
        schedule_window_switch(target, success_notice, getattr(args, "client", ""))
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
    # create_session styles the windows that already exist. If respawn_profile
    # had to recreate a closed window, apply the per-window tab/frame options
    # again so the new Agent does not retain tmux's default appearance.
    configure_session(session, project, config)
    attach_session(session, window=p.name)


def cmd_recover(args: argparse.Namespace) -> None:
    """Resume one or all exited Agents without replacing healthy processes."""
    config = load_config()
    project = runtime_project(args.project, getattr(args, "session", None))
    session = session_name(project)
    if not tmux_session_exists(session):
        raise AgentXError(f"No live session for: {project}")
    recover_all = bool(getattr(args, "all", False))
    if recover_all and args.profile:
        raise AgentXError("Choose a profile or --all, not both.")

    states = {row["name"]: row for row in current_window_states(session)}
    if recover_all:
        selected_profiles = profiles(config)
    else:
        profile_name = args.profile or active_agent(session)
        if not profile_name:
            raise AgentXError("Could not determine the active Agent to recover.")
        selected_profiles = [find_profile(config, profile_name)]

    recovered: list[str] = []
    missing_restored = False
    for profile in selected_profiles:
        row = states.get(profile.name)
        if row is None:
            if not recover_all:
                raise AgentXError(f"Agent window does not exist: {profile.name}")
            respawn_profile(session, profile, project, resume=True)
            recovered.append(profile.name)
            missing_restored = True
        elif row.get("dead") == "1":
            respawn_profile(session, profile, project, resume=True)
            recovered.append(profile.name)
        elif pane_waiting_for_recovery(session, row):
            target = agent_pane_target(f"{session}:{profile.name}")
            tmux("send-keys", "-t", target, "Enter")
            recovered.append(profile.name)

    if missing_restored:
        configure_session(session, project, config)

    if recovered:
        names = ", ".join(recovered)
        message = f"Resumed {len(recovered)} exited Agent(s): {names}."
    elif recover_all:
        message = "All Agents are still running; nothing was restarted."
    else:
        message = f"{selected_profiles[0].name} is still running; nothing was restarted."
    tmux("display-message", "-t", session, message)


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
    elif args.action == "set-memory-path":
        path = Path(args.value).expanduser()
        if path.exists() and not path.is_dir():
            raise AgentXError(f"Memory path is not a directory: {path}")
        config["settings"]["memory_path"] = str(path)
        save_config(config)
        print(f"memory_path = {path}")


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


def cmd_reconfigure(args: argparse.Namespace) -> None:
    """Refresh options and key bindings in every live AgentX session."""
    reconfigure_live_sessions(load_config())


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
  Ctrl+G x     open the AgentX cockpit
  Ctrl+G 1     switch to agent 1
  Ctrl+G 2     switch to agent 2
  Ctrl+G 3     switch to agent 3
  Ctrl+G h     capture a local Context Pack
  Ctrl+G H     show the current Context Pack
  Ctrl+G c     show context status
  Ctrl+G a     show agent dashboard
  Ctrl+G r     refresh workspace snapshot
  Ctrl+G R     resume all exited Agents in this project
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

    p = sub.add_parser("cockpit", help=argparse.SUPPRESS)
    p.add_argument("--session", required=True)
    p.add_argument("--current", default="")
    p.add_argument("--client", default="")
    p.set_defaults(func=cmd_cockpit)

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
    p.add_argument("--section", choices=["all", "agents", "context"], default="all",
                   help=argparse.SUPPRESS)
    p.add_argument("--wait", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("keys", help="Show AgentX tmux key bindings.")
    p.add_argument("--wait", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_keys)

    memory = sub.add_parser("memory", help="Manage local Markdown memory for AgentX context packs.")
    memory_sub = memory.add_subparsers(dest="action", required=True)
    memory_sub.add_parser("path").set_defaults(func=cmd_memory)
    memory_sub.add_parser("init").set_defaults(func=cmd_memory)
    p = memory_sub.add_parser("search", help="Search relevant local memory.")
    p.add_argument("query")
    p.add_argument("--project", "-p")
    p.add_argument("--global", dest="global_scope", action="store_true")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_memory)
    p = memory_sub.add_parser("list", help="List memory for the current project.")
    p.add_argument("--project", "-p")
    p.add_argument("--global", dest="global_scope", action="store_true")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_memory)
    p = memory_sub.add_parser("add", help="Create a local memory note.")
    p.add_argument("title")
    p.add_argument("--note", required=True)
    p.add_argument("--tag", action="append", default=[])
    p.add_argument("--project", "-p")
    p.add_argument("--global", dest="global_scope", action="store_true")
    p.set_defaults(func=cmd_memory)

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
    p = sub.add_parser("reconfigure", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_reconfigure)
    p = sub.add_parser("new", help="Restart one agent window.")
    p.add_argument("profile")
    p.add_argument("--project", "-p")
    p.add_argument("--resume", action="store_true")
    p.set_defaults(func=cmd_new)
    p = sub.add_parser("recover", help="Resume one or all Agent windows that have exited.")
    p.add_argument("profile", nargs="?")
    p.add_argument("--all", action="store_true", help="Resume every exited Agent in this project.")
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
    p = config_sub.add_parser("set-memory-path")
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
