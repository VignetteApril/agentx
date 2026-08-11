from __future__ import annotations

import re
from pathlib import Path

from .models import WorkspaceSnapshot

MAX_RECENT_OUTPUT_BYTES = 128 * 1024

_ANSI_ESCAPE = re.compile(
    r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])"
)


def clean_terminal_output(text: str, *, max_lines: int = 300,
                          max_bytes: int = MAX_RECENT_OUTPUT_BYTES) -> tuple[str, bool]:
    """Normalize captured terminal output and retain only its most recent bounded tail."""
    normalized = _ANSI_ESCAPE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    normalized = "".join(
        char for char in normalized if char in {"\n", "\t"} or ord(char) >= 32
    )
    lines = normalized.splitlines()
    truncated = len(lines) > max(0, max_lines)
    if max_lines <= 0:
        return "", bool(lines)
    cleaned = "\n".join(lines[-max_lines:]).strip()
    encoded = cleaned.encode("utf-8")
    if len(encoded) > max(0, max_bytes):
        truncated = True
        if max_bytes <= 0:
            return "", True
        encoded = encoded[-max_bytes:]
        cleaned = encoded.decode("utf-8", errors="ignore")
        first_newline = cleaned.find("\n")
        if first_newline >= 0:
            cleaned = cleaned[first_newline + 1:]
    return cleaned.strip(), truncated


def format_context_pack(snapshot: WorkspaceSnapshot, *, source_agent: str,
                        target_agent: str | None, created_at: str, directory: Path,
                        recent_output: str, output_truncated: bool,
                        warnings: list[str] | None = None) -> str:
    git_lines: list[str]
    if snapshot.git_root is None:
        git_lines = ["Git repository: unavailable"]
    else:
        git_lines = [
            f"Branch: {snapshot.branch or '(detached or unknown)'}",
            f"HEAD: {snapshot.head or '(unknown)'}",
            f"Changed files: {len(snapshot.status)}",
            f"Diff bytes: {snapshot.diff_size}",
            f"Diff stored: {'no (size limit exceeded)' if snapshot.diff_truncated else 'yes'}",
        ]
    status = "\n".join(snapshot.status) if snapshot.status else "(clean or unavailable)"
    diff_stat = snapshot.diff_stat or "(none or unavailable)"
    commits = "\n".join(snapshot.recent_commits) if snapshot.recent_commits else "(unavailable)"
    terminal = recent_output or "(terminal output unavailable)"
    warning_lines = list(warnings or [])
    if output_truncated:
        warning_lines.append("Recent terminal output was truncated to its configured tail limit.")
    warning_block = "\n".join(f"- {item}" for item in warning_lines) or "- None"
    return f"""# AgentX Context Pack

Source Agent: {source_agent}
Target Agent: {target_agent or '(not specified)'}
Created: {created_at}
Project: {snapshot.project_path}

## Instructions

The working tree is the source of truth. Verify this pack against `git status`, `git diff`, and the
actual files. Use the recent terminal output to recover the active request and decisions, briefly
state what remains, then continue the task. Do not undo existing work without a clear reason.

## Workspace

{chr(10).join(git_lines)}

### Status

```text
{status}
```

### Diff Stat

```text
{diff_stat}
```

### Recent Commits

```text
{commits}
```

## Local Artifacts

- Workspace snapshot: {directory / 'workspace.json'}
- Unstaged diff: {directory / 'workspace.diff'}
- Staged diff: {directory / 'workspace-staged.diff'}
- Recent terminal output: {directory / 'recent-output.txt'}

## Collection Warnings

{warning_block}

## Recent Source Terminal Output

````text
{terminal}
````
"""


def context_pack_injection_prompt(context_dir: str) -> str:
    return f"""[AgentX Context Transfer]

Read the deterministic Context Pack at:
{context_dir}/context-pack.md

The project working tree is the source of truth. Verify the pack with `git status`, `git diff`, and
the actual files. Briefly state your understanding of the current request and remaining work, then
continue without waiting for another handoff.
"""


# Compatibility name for external imports. New code should use context_pack_injection_prompt.
injection_prompt = context_pack_injection_prompt
