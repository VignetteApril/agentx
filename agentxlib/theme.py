from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import ClassVar, Dict, Optional


@dataclass(frozen=True)
class Palette:
    """AgentX's semantic 256-colour palette.

    Values use tmux's native ``colourNNN`` spelling so callers configuring a
    session can use them without translation.  :meth:`ansi_code` is the
    corresponding adapter for terminal-rendered UI.
    """

    background: str = "colour233"
    surface: str = "colour234"
    border: str = "colour238"
    primary: str = "colour51"
    secondary: str = "colour141"
    text: str = "colour252"
    muted: str = "colour244"
    success: str = "colour48"
    warning: str = "colour220"
    danger: str = "colour203"

    _aliases: ClassVar[Dict[str, str]] = {
        # Backwards-compatible renderer names.
        "accent": "primary",
        "panel": "border",
        "bg": "background",
        # These composite ANSI styles still have a useful tmux colour.
        "bold": "text",
        "inverse": "primary",
    }

    def _semantic_name(self, style: str) -> str:
        name = self._aliases.get(style, style)
        if name not in self.__dataclass_fields__:
            raise KeyError(style)
        return name

    def tmux_color(self, style: str) -> str:
        """Return the tmux colour for a semantic style name."""

        return getattr(self, self._semantic_name(style))

    def ansi_code(self, style: str) -> str:
        """Return an SGR code for a semantic or legacy renderer style."""

        if style == "bold":
            return f"1;{self._ansi_foreground('text')}"
        if style == "inverse":
            background = self._colour_index(self.primary)
            return f"1;{self._ansi_foreground('background')};48;5;{background}"

        name = self._semantic_name(style)
        index = self._colour_index(self.tmux_color(name))
        channel = 48 if name in {"background", "surface"} else 38
        return f"{channel};5;{index}"

    def tmux_style(
        self,
        foreground: str = "text",
        background: Optional[str] = None,
        *,
        bold: bool = False,
    ) -> str:
        """Build a tmux style string from semantic palette names."""

        parts = [f"fg={self.tmux_color(foreground)}"]
        if background is not None:
            parts.append(f"bg={self.tmux_color(background)}")
        if bold:
            parts.append("bold")
        return ",".join(parts)

    def _ansi_foreground(self, style: str) -> str:
        return f"38;5;{self._colour_index(self.tmux_color(style))}"

    @staticmethod
    def _colour_index(colour: str) -> int:
        if not colour.startswith("colour"):
            raise ValueError(f"not a 256-colour tmux value: {colour!r}")
        try:
            index = int(colour[6:])
        except ValueError as exc:
            raise ValueError(f"not a 256-colour tmux value: {colour!r}") from exc
        if not 0 <= index <= 255:
            raise ValueError(f"tmux colour index out of range: {colour!r}")
        return index


AGENTX_PALETTE = Palette()


@dataclass(frozen=True)
class Theme:
    enabled: bool
    palette: Palette = AGENTX_PALETTE

    def paint(self, text: str, style: str) -> str:
        if not self.enabled or not text:
            return text
        return f"\033[{self.palette.ansi_code(style)}m{text}\033[0m"


def use_color(stream: object = sys.stdout) -> bool:
    return not os.environ.get("NO_COLOR") and bool(getattr(stream, "isatty", lambda: False)())


def current_theme(stream: object = sys.stdout) -> Theme:
    return Theme(use_color(stream))
