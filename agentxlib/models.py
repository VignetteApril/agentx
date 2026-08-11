from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class WorkspaceSnapshot:
    project_path: str
    timestamp: str
    branch: str | None
    head: str | None
    status: list[str]
    modified_files: list[str]
    staged_files: list[str]
    untracked_files: list[str]
    diff_stat: str
    recent_commits: list[str]
    diff_truncated: bool
    diff_size: int
    active_agent: str | None = None
    git_root: str | None = None

    def to_dict(self) -> dict[str, Any]:
        git = None
        if self.git_root is not None:
            git = {
                "branch": self.branch,
                "head": self.head,
                "status": self.status,
                "modified_files": self.modified_files,
                "staged_files": self.staged_files,
                "untracked_files": self.untracked_files,
                "diff_stat": self.diff_stat,
                "recent_commits": self.recent_commits,
                "diff_truncated": self.diff_truncated,
                "diff_size": self.diff_size,
            }
        return {
            "timestamp": self.timestamp,
            "project": {"path": self.project_path, "git_root": self.git_root},
            "git": git,
            "agent": {"active": self.active_agent},
        }


@dataclass
class Handoff:
    from_agent: str
    timestamp: str
    current_goal: str
    completed: str
    decisions: str
    problems: str
    next_steps: str
    important_notes: str
