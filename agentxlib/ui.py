from __future__ import annotations

import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import PurePath
from typing import Any

from .theme import Theme


def terminal_width(default: int = 88) -> int:
    return max(48, min(120, shutil.get_terminal_size((default, 24)).columns))


def display_width(value: str) -> int:
    """Return the number of terminal columns occupied by a Unicode string."""
    width = 0
    for char in value:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def truncate_display(value: str, limit: int) -> str:
    """Trim a string to a terminal-column limit without splitting wide glyphs."""
    if limit <= 0:
        return ""
    if display_width(value) <= limit:
        return value
    if limit == 1:
        return "…"
    out: list[str] = []
    used = 0
    for char in value:
        char_width = display_width(char)
        if used + char_width > limit - 1:
            break
        out.append(char)
        used += char_width
    return "".join(out).rstrip() + "…"


def pad_display(value: str, limit: int) -> str:
    return value + " " * max(0, limit - display_width(value))


def human_age(value: str | None, now: datetime | None = None) -> str:
    if not value:
        return "never"
    try:
        then = datetime.fromisoformat(value)
        current = now or datetime.now().astimezone()
        seconds = max(0, int((current - then).total_seconds()))
    except (ValueError, TypeError):
        return value
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def handoff_section(markdown: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\s*\n+(.*?)(?=^## |\Z)", markdown,
                      re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else ""


def one_line(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return truncate_display(text, limit)


def change_counts(workspace: dict[str, Any]) -> tuple[int, int, int]:
    git = workspace.get("git") or {}
    return (len(git.get("modified_files") or []), len(git.get("staged_files") or []),
            len(git.get("untracked_files") or []))


def panel_rule(title: str, width: int, theme: Theme, top: bool = True) -> str:
    left, right = ("╭", "╮") if top else ("╰", "╯")
    label = f"─[ {title} ]" if title else ""
    return theme.paint(left + label + "─" * max(0, width - display_width(label) - 2) + right, "panel")


def panel_row(content: str, width: int, theme: Theme, style: str | None = None) -> str:
    content = content.replace("\r", " ").replace("\n", " ")
    limit = max(1, width - 4)
    content = truncate_display(content, limit)
    padded = " " + pad_display(content, max(1, width - 3))
    value = theme.paint(padded, style) if style else padded
    return theme.paint("│", "panel") + value + theme.paint("│", "panel")


def metric(label: str, value: str) -> str:
    return f"{label.upper():<10} {value}"


def compact_agent_row(row: dict[str, str], width: int) -> str:
    active = row.get("active") == "1"
    dead = row.get("dead") == "1"
    marker = "◆" if active else "◇"
    state = "OFFLINE" if dead else ("ACTIVE" if active else "READY")
    name_limit = max(1, width - 4 - display_width(f"{marker}  · {state}"))
    return f"{marker} {truncate_display(row.get('name', '?'), name_limit)} · {state}"


def context_state(context_event: dict[str, Any]) -> str:
    status = str(context_event.get("status") or "legacy").upper()
    labels = {
        "INJECTED": "✓ INJECTED",
        "CAPTURED": "● CAPTURED — READY TO TRANSFER",
        "LEGACY": "◌ LEGACY HANDOFF",
    }
    return labels.get(status, status)


def render_dashboard(project: dict[str, Any], workspace: dict[str, Any], context: dict[str, Any],
                     markdown: str, windows: list[dict[str, str]], theme: Theme,
                     width: int | None = None, section: str = "all") -> str:
    width = width or terminal_width()
    compact = width < 68
    git = workspace.get("git") or {}
    branch = git.get("branch") or "non-git"
    modified, staged, untracked = change_counts(workspace)
    changes = modified + staged + untracked
    project_name = one_line(str(project.get("name", "project")), max(12, width // 3))
    git_state = f"{branch}  //  Δ {changes}" if git else "NON-GIT  //  Δ 0"
    lines = [panel_rule("AGENTX // CONTROL DECK", width, theme),
             panel_row(f"PROJECT  {project_name}", width, theme, "bold"),
             panel_row(f"SIGNAL   {git_state}", width, theme, "accent"),
             panel_rule("", width, theme, top=False), "",
             ]

    if section in {"all", "agents"}:
        lines.append(panel_rule("AGENT MATRIX", width, theme))
        if windows:
            for row in windows:
                active = row.get("active") == "1"
                dead = row.get("dead") == "1"
                if compact:
                    text = compact_agent_row(row, width)
                else:
                    marker = "◆" if active else "◇"
                    state = "OFFLINE" if dead else "ONLINE"
                    mode = "FOCUS" if active else "STANDBY"
                    name = pad_display(truncate_display(row.get("name", "?"), 18), 18)
                    text = f"{marker}  {name} {mode:<9} {state}"
                style = "danger" if dead else ("success" if active else "muted")
                lines.append(panel_row(text, width, theme, style))
        else:
            lines.append(panel_row("◇  NO LIVE TMUX SIGNAL", width, theme, "warning"))
        lines.extend([panel_rule("", width, theme, top=False), ""])

    transfer = context.get("transfer") or {}
    legacy_handoff = context.get("handoff") or {}
    context_event = transfer or legacy_handoff
    source = context_event.get("from_agent", "(none)")
    destination = transfer.get("to_agent")
    route = f"{source} → {destination}" if destination else source
    if section in {"all", "context"}:
        pack = str(context_event.get("file", "(none)"))
        if compact and pack != "(none)":
            pack = PurePath(pack).name
        snapshot = (f"Δ {changes} · M{modified} S{staged} U{untracked}" if compact else
                    f"{modified} modified · {staged} staged · {untracked} untracked")
        memory = context.get("memory") or {}
        memory_count = int(memory.get("count") or 0)
        context_rows = [panel_rule("CONTEXT BUS", width, theme),
                      panel_row(metric("state", context_state(context_event)), width, theme,
                                "success" if transfer.get("status") == "injected" else "secondary"),
                      panel_row(metric("route", f"{route}  //  {human_age(context_event.get('created_at'))}"),
                                width, theme, "secondary"),
                      panel_row(metric("pack", pack), width, theme),
                      panel_row(metric("snapshot", snapshot), width, theme),
                      panel_row(metric("updated", human_age(workspace.get("timestamp"))), width, theme,
                                "muted")]
        if memory:
            context_rows.append(panel_row(metric("memory", f"{memory_count} relevant note(s)"),
                                          width, theme, "secondary"))
        lines.extend(context_rows)
        if legacy_handoff and not transfer:
            goal = handoff_section(markdown, "Current Goal")
            next_steps = handoff_section(markdown, "Next Steps")
            lines.extend([panel_row(metric("legacy goal", goal or "(not specified)"), width, theme),
                          panel_row(metric("legacy next", next_steps or "(not specified)"), width, theme)])
        if git.get("diff_truncated"):
            lines.append(panel_row(f"⚠  DIFF OMITTED  //  {git.get('diff_size', 0)} BYTES",
                                   width, theme, "warning"))
        lines.extend([panel_rule("", width, theme, top=False), ""])
    truth_flow = one_line("  WORKTREE  ──▶  SNAPSHOT  ──▶  CONTEXT PACK", width)
    command_bar = one_line("  ^SPACE SWITCH   ^G+a AGENTS   ^G+c CONTEXT   ^G+? HELP", width)
    lines.extend([theme.paint(truth_flow, "muted"), theme.paint(command_bar, "accent")])
    return "\n".join(lines) + "\n"


def render_keys(theme: Theme, width: int | None = None) -> str:
    width = width or terminal_width(72)
    rows = [
        ("Ctrl+Space", "Choose context transfer, then switch to next Agent"),
        ("x", "Open the AgentX cockpit"),
        ("h", "Capture a local Context Pack"),
        ("H", "Show the current Context Pack"),
        ("c", "Open the Context Dashboard"),
        ("a", "Open the Agent Dashboard"),
        ("r", "Refresh the Workspace Snapshot"),
        ("R", "Resume all exited Agents in this project"),
        ("Ctrl+D", "Send Ctrl+D (EOF) intentionally"),
        ("?", "Show this help"),
        ("d", "Detach; keep agents running"),
        ("s", "Open tmux session tree"),
        ("q", "Show tmux pane numbers"),
    ]
    lines = [panel_rule("AGENTX // COMMAND MATRIX", width, theme),
             panel_row("PREFIX  Ctrl+G   //   DIRECT  Ctrl+Space", width, theme, "accent"),
             panel_rule("", width, theme, top=False), ""]
    for key, description in rows:
        lines.append(f" {theme.paint(f'{key:<12}', 'secondary')}  {description}")
    lines.extend(["", panel_rule("PROTOCOL", width, theme),
                  panel_row("Ctrl+Space opens transfer routing before switching.", width, theme),
                  panel_row("Bare Ctrl+D is shielded; Ctrl+G Ctrl+D sends EOF.", width, theme, "warning"),
                  panel_row("CLI  agentx transfer <agent>", width, theme, "muted"),
                  panel_rule("", width, theme, top=False)])
    return "\n".join(lines) + "\n"


def render_statusline(workspace: dict[str, Any], context: dict[str, Any]) -> str:
    git = workspace.get("git") or {}
    branch = git.get("branch") or "non-git"
    counts = change_counts(workspace)
    changes = sum(counts)
    transfer = context.get("transfer") or context.get("handoff") or {}
    transfer_state = human_age(transfer.get("created_at")) if transfer else "no context"
    snapshot_state = human_age(workspace.get("timestamp"))
    return f"⌁ {branch}  ◈ Δ{changes}  ↻ {snapshot_state}  ⟲ {transfer_state}"
