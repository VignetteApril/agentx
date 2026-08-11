from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def project_id(path: Path | str) -> str:
    absolute = str(Path(path).expanduser().resolve())
    return hashlib.sha1(absolute.encode("utf-8")).hexdigest()[:10]


def project_dir(state_dir: Path, path: Path | str) -> Path:
    return state_dir / "projects" / project_id(path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def ensure_project(state_dir: Path, path: Path | str) -> tuple[Path, dict[str, Any]]:
    absolute = Path(path).expanduser().resolve()
    directory = project_dir(state_dir, absolute)
    directory.mkdir(parents=True, exist_ok=True)
    metadata_path = directory / "project.json"
    metadata = read_json(metadata_path, {}) or {}
    metadata.setdefault("project_id", project_id(absolute))
    metadata.setdefault("path", str(absolute))
    metadata.setdefault("name", absolute.name)
    metadata.setdefault("created_at", now_iso())
    metadata.setdefault("active_agent", None)
    metadata.setdefault("previous_agent", None)
    metadata.setdefault("last_switch_at", None)
    metadata.setdefault("last_handoff_at", None)
    metadata.setdefault("last_transfer_at", None)
    metadata.setdefault("last_context_at", None)
    metadata.setdefault("last_active_agent", metadata.get("active_agent"))
    write_json(metadata_path, metadata)
    return directory, metadata


def update_project(state_dir: Path, path: Path | str, **changes: Any) -> dict[str, Any]:
    directory, metadata = ensure_project(state_dir, path)
    metadata.update(changes)
    write_json(directory / "project.json", metadata)
    return metadata
