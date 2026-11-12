"""Fullscreen, dependency-free launcher UI for AgentX agents.

The renderer in this module is deliberately pure: it can be snapshot-tested without
opening a terminal.  :func:`run_cockpit` is the small terminal adapter around it.
"""

from __future__ import annotations

import os
import select
import shutil
import signal
import sys
import termios
import tty
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .theme import Theme


@dataclass(frozen=True)
class CockpitAgent:
    """One destination shown by the cockpit.

    ``status`` is normally ``online``, ``dead``, or ``missing``.  A few common
    aliases are accepted so callers do not need to translate tmux terminology.
    ``detail`` is optional secondary text such as a model or profile label.
    """

    name: str
    status: str = "online"
    detail: str = ""


@dataclass(frozen=True)
class CockpitAction:
    """The operation selected by the user."""

    kind: str
    target: Optional[str] = None


@dataclass(frozen=True)
class CockpitState:
    """Pure interaction state, exposed to make keyboard behaviour testable."""

    selected: int = 0
    mode: str = "browse"
    confirm_index: int = 0
    error: Optional[str] = None


_PIXELS: Dict[str, Tuple[str, ...]] = {
    "A": ("01110", "10001", "11111", "10001", "10001"),
    "G": ("01111", "10000", "10111", "10001", "01110"),
    "E": ("11111", "10000", "11110", "10000", "11111"),
    "N": ("10001", "11001", "10101", "10011", "10001"),
    "T": ("11111", "00100", "00100", "00100", "00100"),
    "X": ("10001", "01010", "00100", "01010", "10001"),
}


def _clean(value: object) -> str:
    """Make external labels safe to write to a terminal."""

    result: List[str] = []
    for character in str(value):
        category = unicodedata.category(character)
        if category.startswith("C"):
            result.append(" ")
        else:
            result.append(character)
    return " ".join("".join(result).split())


def _display_width(value: str) -> int:
    width = 0
    for character in value:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return width


def _truncate(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    value = _clean(value)
    if _display_width(value) <= limit:
        return value
    if limit == 1:
        return "…"
    result: List[str] = []
    used = 0
    for character in value:
        character_width = _display_width(character)
        if used + character_width > limit - 1:
            break
        result.append(character)
        used += character_width
    return "".join(result).rstrip() + "…"


def _fit(value: str, width: int) -> str:
    value = _truncate(value, width)
    return value + " " * max(0, width - _display_width(value))


def _center(value: str, width: int) -> str:
    value = _truncate(value, width)
    remaining = max(0, width - _display_width(value))
    left = remaining // 2
    return " " * left + value + " " * (remaining - left)


def _styled_center(value: str, width: int, theme: Theme, style: str) -> str:
    value = _truncate(value, width)
    remaining = max(0, width - _display_width(value))
    left = remaining // 2
    return " " * left + theme.paint(value, style) + " " * (remaining - left)


def _styled_parts_center(parts: Sequence[Tuple[str, str]], width: int, theme: Theme) -> str:
    """Center several independently styled spans without counting ANSI bytes."""

    plain = "".join(part for part, _style in parts)
    if _display_width(plain) > width:
        return _styled_center(plain, width, theme, parts[0][1] if parts else "muted")
    remaining = width - _display_width(plain)
    left = remaining // 2
    painted = "".join(theme.paint(part, style) for part, style in parts)
    return " " * left + painted + " " * (remaining - left)


def _status(agent: CockpitAgent) -> str:
    value = _clean(agent.status).lower()
    if value in {"missing", "absent", "unavailable", "disabled"}:
        return "missing"
    if value in {"dead", "offline", "exited", "stopped"}:
        return "dead"
    return "online"


def _pixel_logo() -> List[Tuple[str, str]]:
    rows: List[Tuple[str, str]] = []
    for row in range(5):
        left: List[str] = []
        right: List[str] = []
        for index, letter in enumerate("AGENTX"):
            rendered = "".join("██" if bit == "1" else "  " for bit in _PIXELS[letter][row])
            if index < 5:
                left.append(rendered)
                if index != 4:
                    left.append("  ")
            else:
                right.append("  " + rendered)
        rows.append(("".join(left), "".join(right)))
    return rows


def _visible_range(count: int, selected: int, capacity: int) -> Tuple[int, int]:
    if count <= 0 or capacity <= 0:
        return (0, 0)
    capacity = min(count, capacity)
    selected = max(0, min(selected, count - 1))
    start = max(0, selected - capacity // 2)
    start = min(start, count - capacity)
    return (start, start + capacity)


def _agent_row(agent: CockpitAgent, absolute_index: int, current_name: str,
               width: int) -> str:
    state = _status(agent)
    current = agent.name == current_name
    marker = "◆" if current else {"online": "●", "dead": "○", "missing": "×"}[state]
    label = "CURRENT" if current else {"online": "ONLINE", "dead": "OFFLINE",
                                         "missing": "MISSING"}[state]
    key = str(absolute_index + 1) if absolute_index < 9 else "·"
    detail = _clean(agent.detail)
    name = _clean(agent.name) or "(unnamed)"
    prefix = f" {key}  {marker} "
    suffix = f"  {label} "
    middle_width = max(1, width - _display_width(prefix) - _display_width(suffix))
    middle = name + (f" · {detail}" if detail else "")
    return prefix + _fit(middle, middle_width) + suffix


def _footer(project_path: str, version: str, width: int) -> str:
    if width <= 0:
        return ""
    suffix = _clean(version)
    suffix = f"v{suffix.lstrip('v')}  " if suffix else ""
    if _display_width(suffix) >= width:
        return _fit(_truncate(suffix, width), width)
    left_limit = width - _display_width(suffix)
    left = "  " + _clean(project_path)
    left = _truncate(left, left_limit)
    gap = width - _display_width(left) - _display_width(suffix)
    return left + " " * max(0, gap) + suffix


def _effective_theme(theme: Theme) -> Theme:
    # NO_COLOR is honoured even when a caller constructed Theme(True) directly.
    return Theme(False) if os.environ.get("NO_COLOR") else theme


def render_cockpit(width: int, height: int, project_name: str, project_path: str,
                   version: str, agents: Sequence[CockpitAgent], current_name: str,
                   theme: Theme, selected: int = 0, mode: str = "browse",
                   confirm_index: int = 0, error: Optional[str] = None) -> str:
    """Render a complete cockpit frame without reading or writing the terminal.

    The returned value has exactly ``height`` lines (unless height is zero), and
    every line occupies at most ``width`` terminal columns.  ANSI styling is only
    emitted by ``theme`` and is disabled when ``NO_COLOR`` is set.
    """

    width = max(1, int(width))
    height = max(0, int(height))
    if height == 0:
        return ""
    paint = _effective_theme(theme)
    selected = max(0, min(int(selected), max(0, len(agents) - 1)))
    confirm_index = max(0, min(int(confirm_index), 2))

    # A small deterministic star field provides texture without randomness or
    # animation.  Content rows replace stars rather than colliding with them.
    canvas: List[List[str]] = [[" " for _column in range(width)] for _row in range(height)]
    stars = ((7, 5, "·"), (18, 14, "✦"), (31, 8, "·"), (46, 17, "✧"),
             (62, 4, "·"), (78, 12, "✦"), (89, 21, "·"), (96, 7, "✧"))
    for x_percent, y_percent, glyph in stars:
        column = min(width - 1, max(0, width * x_percent // 100))
        row = min(height - 1, max(0, height * y_percent // 100))
        if _display_width(glyph) == 1:
            canvas[row][column] = glyph

    rendered: Dict[int, str] = {}

    def set_center(row: int, text: str, style: str = "muted") -> None:
        if 0 <= row < height:
            rendered[row] = _styled_center(text, width, paint, style)

    def set_center_parts(row: int, parts: Sequence[Tuple[str, str]]) -> None:
        if 0 <= row < height:
            rendered[row] = _styled_parts_center(parts, width, paint)

    def set_agent(row: int, index: int, panel_width: int) -> None:
        if not (0 <= row < height and 0 <= index < len(agents)):
            return
        agent = agents[index]
        value = _agent_row(agent, index, current_name, panel_width)
        if _display_width(value) > panel_width:
            value = _truncate(value, panel_width)
            value += " " * max(0, panel_width - _display_width(value))
        state = _status(agent)
        style = "inverse" if index == selected else (
            "danger" if state == "missing" else "muted" if state == "dead" else
            "success" if agent.name == current_name else "accent"
        )
        remaining = max(0, width - _display_width(value))
        left = remaining // 2
        rendered[row] = " " * left + paint.paint(value, style) + " " * (remaining - left)

    full = width >= 80 and height >= 24
    compact = not full and width >= 48 and height >= 16
    panel_width = min(70 if full else 44, max(1, width - (8 if full else 4)))

    if full:
        content_top = max(1, (height - 20) // 2)
        set_center(content_top, _clean(project_name) or "AgentX", "muted")
        for offset, (left, right) in enumerate(_pixel_logo()):
            set_center_parts(content_top + 2 + offset,
                             ((left, "accent"), (right, "secondary")))
        title_row = content_top + 8
        list_start = title_row + 1
        help_row = height - 4
        message_row = height - 3
        tip_row = height - 2
        capacity = min(9, max(1, help_row - list_start - 1))
        start, end = _visible_range(len(agents), selected, capacity)
        range_label = f"  {start + 1}–{end} / {len(agents)}" if len(agents) > capacity else ""
        set_center(title_row, "AGENTS" + range_label, "secondary")

        if mode == "confirm" and agents:
            target = _clean(agents[selected].name)
            set_center(list_start + 1, f"Switch to {target}?", "bold")
            choices = ("Transfer + switch", "Switch only", "Cancel")
            parts: List[Tuple[str, str]] = []
            for index, choice in enumerate(choices):
                if index:
                    parts.append(("   ", "muted"))
                parts.append((f" {choice} ", "inverse" if index == confirm_index else "muted"))
            set_center_parts(list_start + 3, parts)
            set_center(list_start + 5, "←/→ choose  ·  Enter confirm  ·  Esc cancel", "muted")
        elif agents:
            for row_offset, index in enumerate(range(start, end)):
                set_agent(list_start + row_offset, index, panel_width)
        else:
            set_center(list_start + 1, "No configured agents", "warning")

        set_center(help_row, "↑↓ select   1–9 quick select   Enter open   Esc/q close", "muted")
        if error:
            set_center(message_row, _clean(error), "danger")
        set_center(tip_row, "Tip  Ctrl+G x opens this cockpit from any Agent", "secondary")
    elif compact:
        set_center(0, _clean(project_name) or "AgentX", "muted")
        set_center_parts(2, (("◆ AGENT", "accent"), ("X", "secondary")))
        title_row = 4
        list_start = 5
        help_row = height - 3
        message_row = height - 2
        capacity = min(9, max(1, help_row - list_start - 1))
        start, end = _visible_range(len(agents), selected, capacity)
        range_label = f" {start + 1}–{end}/{len(agents)}" if len(agents) > capacity else ""
        set_center(title_row, "AGENTS" + range_label, "secondary")
        if mode == "confirm" and agents:
            set_center(list_start, f"Switch to {_clean(agents[selected].name)}?", "bold")
            choices = ("Transfer", "Switch", "Cancel")
            parts = []
            for index, choice in enumerate(choices):
                if index:
                    parts.append(("  ", "muted"))
                parts.append((f" {choice} ", "inverse" if index == confirm_index else "muted"))
            set_center_parts(list_start + 2, parts)
        elif agents:
            for row_offset, index in enumerate(range(start, end)):
                set_agent(list_start + row_offset, index, panel_width)
        else:
            set_center(list_start, "No configured agents", "warning")
        set_center(help_row, "↑↓ / 1–9 select · Enter open · Esc close", "muted")
        if error:
            set_center(message_row, _clean(error), "danger")
    else:
        # Minimal terminals retain selection and action semantics while dropping
        # decorative content.  This layout is also safe for one-line terminals.
        set_center(0, "AGENTX", "accent")
        list_start = 2 if height >= 4 else 1
        footer_row = height - 1
        message_row = height - 2
        capacity = max(0, message_row - list_start)
        start, end = _visible_range(len(agents), selected, capacity)
        if mode == "confirm" and agents and capacity:
            choice = ("TRANSFER", "SWITCH", "CANCEL")[confirm_index]
            set_center(list_start, f"{choice}: {_clean(agents[selected].name)}", "inverse")
        else:
            for row_offset, index in enumerate(range(start, end)):
                set_agent(list_start + row_offset, index, max(1, width - 2))
        if error and message_row > 0:
            set_center(message_row, _clean(error), "danger")

    # Footer is deliberately last so it remains visible in extremely short views.
    rendered[height - 1] = paint.paint(_footer(project_path, version, width), "muted")
    output: List[str] = []
    for row, characters in enumerate(canvas):
        if row in rendered:
            output.append(rendered[row])
        else:
            output.append(paint.paint("".join(characters), "muted"))
    return "\n".join(output)


def _first_selectable(agents: Sequence[CockpitAgent], current_name: str) -> int:
    for index, agent in enumerate(agents):
        if agent.name == current_name:
            return index
    for index, agent in enumerate(agents):
        if _status(agent) != "missing":
            return index
    return 0


def _move_selection(agents: Sequence[CockpitAgent], selected: int, direction: int) -> int:
    if not agents:
        return 0
    candidate = selected
    for _attempt in range(len(agents)):
        candidate = (candidate + direction) % len(agents)
        if _status(agents[candidate]) != "missing":
            return candidate
    return selected


def reduce_cockpit_key(agents: Sequence[CockpitAgent], current_name: str,
                       state: CockpitState, key: str) -> Tuple[CockpitState,
                                                                Optional[CockpitAction]]:
    """Apply one decoded key to cockpit state without performing any I/O."""

    if len(key) == 1:
        key = key.lower()
    selected = max(0, min(state.selected, max(0, len(agents) - 1)))
    mode = state.mode
    confirm_index = max(0, min(state.confirm_index, 2))

    if key in {"ctrl-c", "eof"}:
        return CockpitState(selected, mode, confirm_index), CockpitAction("close")

    if mode == "confirm":
        if key in {"left", "up"}:
            confirm_index = (confirm_index - 1) % 3
        elif key in {"right", "down", "tab"}:
            confirm_index = (confirm_index + 1) % 3
        elif key == "y" and agents:
            return CockpitState(selected, mode, confirm_index), \
                CockpitAction("transfer", agents[selected].name)
        elif key == "n" and agents:
            return CockpitState(selected, mode, confirm_index), \
                CockpitAction("switch", agents[selected].name)
        elif key in {"escape", "q"}:
            mode = "browse"
            confirm_index = 0
        elif key == "enter" and agents:
            if confirm_index == 0:
                return CockpitState(selected, mode, confirm_index), \
                    CockpitAction("transfer", agents[selected].name)
            if confirm_index == 1:
                return CockpitState(selected, mode, confirm_index), \
                    CockpitAction("switch", agents[selected].name)
            mode = "browse"
            confirm_index = 0
        return CockpitState(selected, mode, confirm_index), None

    if key in {"escape", "q"}:
        return CockpitState(selected, mode, confirm_index), CockpitAction("close")
    if key == "up":
        selected = _move_selection(agents, selected, -1)
    elif key in {"down", "tab"}:
        selected = _move_selection(agents, selected, 1)
    elif len(key) == 1 and key in "123456789":
        requested = int(key) - 1
        if requested < len(agents):
            selected = requested
            if _status(agents[selected]) == "missing":
                message = (f"{agents[selected].name} is not available; create it with "
                           f"agentx new {agents[selected].name}")
                return CockpitState(selected, mode, confirm_index, message), None
    elif key == "enter" and agents:
        target = agents[selected]
        if target.name == current_name:
            return CockpitState(selected, mode, confirm_index), CockpitAction("close")
        target_status = _status(target)
        if target_status == "missing":
            message = (f"{target.name} is not available; create it with "
                       f"agentx new {target.name}")
            return CockpitState(selected, mode, confirm_index, message), None
        if target_status == "dead":
            return CockpitState(selected, mode, confirm_index), \
                CockpitAction("switch", target.name)
        mode = "confirm"
        confirm_index = 0
    return CockpitState(selected, mode, confirm_index), None


def _read_key(descriptor: int) -> str:
    first = os.read(descriptor, 1)
    if not first:
        return "eof"
    if first != b"\x1b":
        mapping = {b"\r": "enter", b"\n": "enter", b"\t": "tab",
                   b"\x03": "ctrl-c"}
        return mapping.get(first, first.decode("utf-8", "ignore"))

    arrows = {b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right",
              b"\x1b[D": "left", b"\x1bOA": "up", b"\x1bOB": "down",
              b"\x1bOC": "right", b"\x1bOD": "left"}
    sequence = bytearray(first)
    while len(sequence) < 8:
        decoded = bytes(sequence)
        if decoded in arrows:
            return arrows[decoded]
        if not any(candidate.startswith(decoded) for candidate in arrows):
            break
        readable, _writable, _exceptional = select.select([descriptor], [], [], 0.025)
        if not readable:
            break
        sequence.extend(os.read(descriptor, 1))
    return arrows.get(bytes(sequence), "escape")


def run_cockpit(project_name: str, project_path: str, version: str,
                 agents: Sequence[CockpitAgent], current_name: str,
                 theme: Theme, *, notice: Optional[str] = None) -> CockpitAction:
    """Run the cockpit in the controlling terminal and return the chosen action."""

    if not agents:
        return CockpitAction("close")
    input_stream = sys.stdin
    output_stream = sys.stdout
    if not (getattr(input_stream, "isatty", lambda: False)() and
            getattr(output_stream, "isatty", lambda: False)()):
        return CockpitAction("close")

    input_descriptor = input_stream.fileno()
    output_descriptor = output_stream.fileno()
    previous_terminal = termios.tcgetattr(input_descriptor)
    previous_winch = None
    winch_installed = False
    previous_tstp = None
    tstp_ignored = False
    resized = True
    state = CockpitState(_first_selectable(agents, current_name), error=notice)

    def on_resize(_signum: int, _frame: object) -> None:
        nonlocal resized
        resized = True

    try:
        try:
            previous_winch = signal.getsignal(signal.SIGWINCH)
            signal.signal(signal.SIGWINCH, on_resize)
            winch_installed = True
        except (AttributeError, ValueError):
            # Signal handlers are unavailable outside the main thread.
            pass
        try:
            # Suspending while cbreak and the alternate screen are active can
            # strand the terminal with a hidden cursor.  The cockpit is brief
            # and has explicit close keys, so ignore Ctrl+Z until it exits.
            previous_tstp = signal.getsignal(signal.SIGTSTP)
            signal.signal(signal.SIGTSTP, signal.SIG_IGN)
            tstp_ignored = True
        except (AttributeError, ValueError):
            pass
        tty.setcbreak(input_descriptor)
        output_stream.write("\033[?1049h\033[?25l\033[?7l\033[2J\033[H")
        output_stream.flush()

        while True:
            if resized:
                size = shutil.get_terminal_size((80, 24))
                frame = render_cockpit(size.columns, size.lines, project_name, project_path,
                                        version, agents, current_name, theme, state.selected,
                                        state.mode, state.confirm_index, state.error)
                output_stream.write("\033[H\033[2J" + frame)
                output_stream.flush()
                resized = False

            try:
                readable, _writable, _exceptional = select.select([input_descriptor], [], [], 0.2)
            except InterruptedError:
                continue
            if not readable:
                continue
            key = _read_key(input_descriptor)
            # Notices are transient: any key acknowledges the previous one.
            state = CockpitState(state.selected, state.mode, state.confirm_index)
            state, action = reduce_cockpit_key(agents, current_name, state, key)
            if action is not None:
                return action
            resized = True
    except KeyboardInterrupt:
        return CockpitAction("close")
    finally:
        try:
            # Flush any type-ahead before restoring canonical mode.  On BSD/macOS,
            # TCSANOW/TCSADRAIN can otherwise leave the dynamic PENDIN flag set.
            termios.tcsetattr(input_descriptor, termios.TCSAFLUSH, previous_terminal)
        finally:
            try:
                if winch_installed:
                    signal.signal(signal.SIGWINCH, previous_winch)
            finally:
                try:
                    if tstp_ignored:
                        signal.signal(signal.SIGTSTP, previous_tstp)
                finally:
                    output_stream.write("\033[?7h\033[?25h\033[?1049l")
                    output_stream.flush()
