from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import WorkspaceSnapshot
from .projects import ensure_project, read_json, write_json

CURRENT_FILES = ("context-pack.md", "handoff.md", "context.json", "workspace.json",
                 "workspace.diff", "workspace-staged.diff", "recent-output.txt")


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value, encoding="utf-8")
    tmp.replace(path)


def archive_current(directory: Path, from_agent: str, timestamp: str | None = None) -> Path | None:
    existing = [directory / name for name in CURRENT_FILES if (directory / name).exists()]
    if not existing:
        return None
    stamp = timestamp or datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    history = directory / "history" / f"{stamp}-{from_agent}"
    suffix = 1
    while history.exists():
        history = directory / "history" / f"{stamp}-{from_agent}-{suffix}"
        suffix += 1
    history.mkdir(parents=True)
    for source in existing:
        shutil.copy2(source, history / source.name)
    return history


def save_workspace(state_dir: Path, project: Path, snapshot: WorkspaceSnapshot,
                   diff: str, staged_diff: str, recent_output: str | None = None) -> Path:
    directory, _ = ensure_project(state_dir, project)
    write_json(directory / "workspace.json", snapshot.to_dict())
    atomic_write_text(directory / "workspace.diff", diff)
    atomic_write_text(directory / "workspace-staged.diff", staged_diff)
    if recent_output is not None:
        atomic_write_text(directory / "recent-output.txt", recent_output)
    return directory


def save_context_pack(state_dir: Path, project: Path, from_agent: str,
                      to_agent: str | None, created_at: str, markdown: str,
                      snapshot: WorkspaceSnapshot, diff: str, staged_diff: str,
                      recent_output: str, warnings: list[str] | None = None) -> Path:
    directory, metadata = ensure_project(state_dir, project)
    archive_current(directory, str(metadata.get("last_active_agent") or from_agent))
    save_workspace(state_dir, project, snapshot, diff, staged_diff, recent_output)
    atomic_write_text(directory / "context-pack.md", markdown.rstrip() + "\n")
    context: dict[str, Any] = {
        "version": 2,
        "project": {"path": str(project.resolve()), "branch": snapshot.branch,
                    "head": snapshot.head},
        "transfer": {"from_agent": from_agent, "to_agent": to_agent,
                     "created_at": created_at, "file": "context-pack.md",
                     "status": "captured", "warnings": list(warnings or [])},
        "workspace": {"file": "workspace.json", "diff_file": "workspace.diff",
                      "staged_diff_file": "workspace-staged.diff",
                      "recent_output_file": "recent-output.txt"},
    }
    write_json(directory / "context.json", context)
    metadata.update({"last_active_agent": from_agent, "active_agent": from_agent,
                     "last_context_at": created_at})
    write_json(directory / "project.json", metadata)
    return directory


def mark_context_pack_transferred(directory: Path, transferred_at: str) -> None:
    context_path = directory / "context.json"
    context = read_json(context_path, {}) or {}
    transfer = context.get("transfer")
    if not isinstance(transfer, dict):
        raise ValueError(f"Context Pack metadata is missing: {context_path}")
    transfer["status"] = "injected"
    transfer["transferred_at"] = transferred_at
    write_json(context_path, context)


def save_handoff(state_dir: Path, project: Path, from_agent: str, created_at: str,
                 markdown: str, snapshot: WorkspaceSnapshot, diff: str, staged_diff: str,
                 recent_output: str) -> Path:
    directory, metadata = ensure_project(state_dir, project)
    archive_current(directory, str(metadata.get("last_active_agent") or from_agent))
    save_workspace(state_dir, project, snapshot, diff, staged_diff, recent_output)
    atomic_write_text(directory / "handoff.md", markdown.rstrip() + "\n")
    context: dict[str, Any] = {
        "version": 1,
        "project": {"path": str(project.resolve()), "branch": snapshot.branch, "head": snapshot.head},
        "handoff": {"from_agent": from_agent, "created_at": created_at, "file": "handoff.md"},
        "workspace": {"file": "workspace.json", "diff_file": "workspace.diff",
                      "staged_diff_file": "workspace-staged.diff"},
    }
    write_json(directory / "context.json", context)
    metadata.update({"last_active_agent": from_agent, "active_agent": from_agent,
                     "last_handoff_at": created_at})
    write_json(directory / "project.json", metadata)
    return directory
