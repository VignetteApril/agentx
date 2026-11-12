#!/usr/bin/env python3
"""Regenerate the checked-in Windows icon from the project's existing logo."""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> None:
    destination = ROOT / "desktop/rust/assets/AgentX.ico"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(ROOT / "assets/agentx-logo.png") as image:
        image.convert("RGBA").save(destination, format="ICO", sizes=[(size, size) for size in SIZES])
    print(destination)


if __name__ == "__main__":
    main()
