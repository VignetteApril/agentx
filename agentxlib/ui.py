from __future__ import annotations

import re
import shutil
from datetime import datetime
from typing import Any

from .theme import Theme


def terminal_width(default: int = 88) -> int:
    return max(48, min(120, shutil.get_terminal_size((default, 24)).columns))


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
    if len(text) <= limit:
        return text
    return text[:max(1, limit - 1)].rstrip() + "…"


def change_counts(workspace: dict[str, Any]) -> tuple[int, int, int]:
    git = workspace.get("git") or {}
    return (len(git.get("modified_files") or []), len(git.get("staged_files") or []),
            len(git.get("untracked_files") or []))


def panel_rule(title: str, width: int, theme: Theme, top: bool = True) -> str:
    left, right = ("╭", "╮") if top else ("╰", "╯")
    label = f"─[ {title} ]" if title else ""
    return theme.paint(left + label + "─" * max(0, width - len(label) - 2) + right, "panel")


def panel_row(content: str, width: int, theme: Theme, style: str | None = None) -> str:
    content = content.replace("\r", " ").replace("\n", " ")
    limit = max(1, width - 4)
    if len(content) > limit:
        content = content[:max(1, limit - 1)].rstrip() + "…"
    padded = f" {content:<{max(1, width - 3)}}"
    value = theme.paint(padded, style) if style else padded
    return theme.paint("│", "panel") + value + theme.paint("│", "panel")


def metric(label: str, value: str) -> str:
    return f"{label.upper():<10} {value}"


def render_dashboard(project: dict[str, Any], workspace: dict[str, Any], context: dict[str, Any],
                     markdown: str, windows: list[dict[str, str]], theme: Theme,
                     width: int | None = None) -> str:
    width = width or terminal_width()
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
             panel_rule("AGENT MATRIX", width, theme)]

    if windows:
        for row in windows:
            active = row.get("active") == "1"
            dead = row.get("dead") == "1"
            marker = "◆" if active else "◇"
            state = "OFFLINE" if dead else "ONLINE"
            mode = "FOCUS" if active else "STANDBY"
            text = f"{marker}  {row.get('name', '?'):<18} {mode:<9} {state}"
            style = "danger" if dead else ("success" if active else "muted")
            lines.append(panel_row(text, width, theme, style))
    else:
        lines.append(panel_row("◇  NO LIVE TMUX SIGNAL", width, theme, "warning"))
    lines.append(panel_rule("", width, theme, top=False))

    transfer = context.get("transfer") or {}
    legacy_handoff = context.get("handoff") or {}
    context_event = transfer or legacy_handoff
    source = context_event.get("from_agent", "(none)")
    destination = transfer.get("to_agent")
    route = f"{source} → {destination}" if destination else source
    lines.extend(["", panel_rule("CONTEXT BUS", width, theme),
                  panel_row(metric("route", f"{route}  //  {human_age(context_event.get('created_at'))}"),
                            width, theme, "secondary"),
                  panel_row(metric("pack", context_event.get("file", "(none)")), width, theme),
                  panel_row(metric("snapshot", f"{modified} modified · {staged} staged · {untracked} untracked"),
                            width, theme)])
    if legacy_handoff and not transfer:
        goal = handoff_section(markdown, "Current Goal")
        next_steps = handoff_section(markdown, "Next Steps")
        lines.extend([panel_row(metric("legacy goal", goal or "(not specified)"), width, theme),
                      panel_row(metric("legacy next", next_steps or "(not specified)"), width, theme)])
    if git.get("diff_truncated"):
        lines.append(panel_row(f"⚠  DIFF OMITTED  //  {git.get('diff_size', 0)} BYTES",
                               width, theme, "warning"))
    truth_flow = one_line("  WORKTREE  ──▶  SNAPSHOT  ──▶  CONTEXT PACK", width)
    command_bar = one_line("  ^SPACE SWITCH   ^G+h CAPTURE   ^G+r SYNC   ^G+? HELP", width)
    lines.extend([panel_rule("", width, theme, top=False), "",
                  theme.paint(truth_flow, "muted"), theme.paint(command_bar, "accent")])
    return "\n".join(lines) + "\n"


def render_keys(theme: Theme, width: int | None = None) -> str:
    width = width or terminal_width(72)
    rows = [
        ("Ctrl+Space", "Choose context transfer, then switch to next Agent"),
        ("h", "Capture a local Context Pack"),
        ("H", "Show the current Context Pack"),
        ("c", "Open the Context Dashboard"),
        ("a", "Open the Agent Dashboard"),
        ("r", "Refresh the Workspace Snapshot"),
        ("R", "Resume the current Agent if its pane exited"),
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
    return f"⌁ {branch}  ◈ Δ{changes}  ⟲ {transfer_state}"
