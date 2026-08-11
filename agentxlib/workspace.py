from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable

from .models import WorkspaceSnapshot

MAX_DIFF_SIZE = 200 * 1024


def parse_git_status(lines: list[str]) -> tuple[list[str], list[str], list[str]]:
    modified: list[str] = []
    staged: list[str] = []
    untracked: list[str] = []
    for line in lines:
        if len(line) < 3:
            continue
        code, name = line[:2], line[3:]
        if code == "??":
            untracked.append(name)
            continue
        if code[0] not in {" ", "?"}:
            staged.append(name)
        if code[1] not in {" ", "?"}:
            modified.append(name)
    return modified, staged, untracked


def _git(project: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", *args], cwd=project, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=False)
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(["git", *args], 1, "", "git unavailable")


def collect_workspace(project: Path, active_agent: str | None = None,
                      max_diff_size: int = MAX_DIFF_SIZE,
                      runner: Callable[[Path, list[str]], subprocess.CompletedProcess[str]] = _git,
                      ) -> tuple[WorkspaceSnapshot, str, str]:
    project = project.expanduser().resolve()
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    root_result = runner(project, ["rev-parse", "--show-toplevel"])
    if root_result.returncode != 0:
        return WorkspaceSnapshot(str(project), timestamp, None, None, [], [], [], [], "", [],
                                 False, 0, active_agent, None), "", ""

    git_root = root_result.stdout.strip()
    def output(args: list[str]) -> str:
        result = runner(project, args)
        return result.stdout.rstrip("\n") if result.returncode == 0 else ""

    status = output(["status", "--short"]).splitlines()
    modified, staged, untracked = parse_git_status(status)
    diff = output(["diff"])
    staged_diff = output(["diff", "--cached"])
    diff_size = len(diff.encode("utf-8")) + len(staged_diff.encode("utf-8"))
    truncated = diff_size > max_diff_size
    snapshot = WorkspaceSnapshot(
        str(project), timestamp, output(["branch", "--show-current"]) or None,
        output(["rev-parse", "HEAD"]) or None, status, modified, staged, untracked,
        output(["diff", "--stat"]), output(["log", "-5", "--pretty=format:%h %s"]).splitlines(),
        truncated, diff_size, active_agent, git_root,
    )
    return snapshot, "" if truncated else diff, "" if truncated else staged_diff
