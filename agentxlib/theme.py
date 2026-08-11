from __future__ import annotations

import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    enabled: bool

    _codes = {
        "accent": "38;5;51",
        "secondary": "38;5;141",
        "success": "38;5;48",
        "warning": "38;5;220",
        "danger": "38;5;203",
        "muted": "38;5;244",
        "panel": "38;5;38",
        "bold": "1;38;5;231",
        "inverse": "1;38;5;16;48;5;51",
    }

    def paint(self, text: str, style: str) -> str:
        if not self.enabled or not text:
            return text
        return f"\033[{self._codes[style]}m{text}\033[0m"


def use_color(stream: object = sys.stdout) -> bool:
    return not os.environ.get("NO_COLOR") and bool(getattr(stream, "isatty", lambda: False)())


def current_theme(stream: object = sys.stdout) -> Theme:
    return Theme(use_color(stream))
