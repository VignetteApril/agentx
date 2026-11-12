from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


MEMORY_FILE_LIMIT = 400
MEMORY_NOTE_CHARS = 1_800
MEMORY_TOTAL_CHARS = 6_000


@dataclass(frozen=True)
class MemoryNote:
    path: Path
    title: str
    project: str
    tags: tuple[str, ...]
    body: str
    score: int = 0


def memory_root(value: str | None, default: Path) -> Path:
    return Path(value).expanduser() if value else default


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    values: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip():
            values[key.strip().lower()] = value.strip().strip("\"'")
    return values, text[end + 5:]


def _clean_text(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines()).strip()


def _terms(query: str) -> set[str]:
    return {part.casefold() for part in re.findall(r"[\w.-]{2,}", query)}


def _title(body: str, fallback: str) -> str:
    for line in body.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip() or fallback
    return fallback


def read_memory_notes(root: Path) -> list[MemoryNote]:
    """Read a local Markdown vault without requiring an Obsidian installation."""
    if not root.is_dir():
        return []
    notes: list[MemoryNote] = []
    for path in list(root.rglob("*.md"))[:MEMORY_FILE_LIMIT]:
        if ".obsidian" in path.parts:
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        frontmatter, body = _frontmatter(raw)
        body = _clean_text(body)
        notes.append(MemoryNote(
            path=path,
            title=frontmatter.get("title") or _title(body, path.stem.replace("-", " ")),
            project=frontmatter.get("project", ""),
            tags=tuple(part.strip() for part in frontmatter.get("tags", "").split(",") if part.strip()),
            body=body,
        ))
    return notes


def _matches_project(note: MemoryNote, project: Path | None) -> bool:
    if project is None or not note.project:
        return False
    configured = note.project.rstrip("/")
    return configured in {str(project.resolve()), project.name}


def search_memory(root: Path, query: str, project: Path | None = None,
                  limit: int = 5) -> list[MemoryNote]:
    """Return the most relevant notes, preferring exact project-scoped memory."""
    terms = _terms(query)
    ranked: list[MemoryNote] = []
    for note in read_memory_notes(root):
        project_match = _matches_project(note, project)
        haystack = f"{note.title}\n{' '.join(note.tags)}\n{note.body}".casefold()
        title = note.title.casefold()
        tag_text = " ".join(note.tags).casefold()
        score = 100 if project_match else 0
        for term in terms:
            score += title.count(term) * 12 + tag_text.count(term) * 6 + haystack.count(term)
        if score:
            ranked.append(MemoryNote(note.path, note.title, note.project, note.tags, note.body, score))
    ranked.sort(key=lambda item: (-item.score, item.path.name.casefold()))
    return ranked[:max(0, limit)]


def context_query(project: Path, branch: str | None, changed_files: list[str]) -> str:
    parts = [project.name, branch or ""]
    parts.extend(Path(path).stem for path in changed_files[:12])
    return " ".join(part for part in parts if part)


def context_memory(root: Path, project: Path, branch: str | None,
                   changed_files: list[str]) -> list[MemoryNote]:
    return search_memory(root, context_query(project, branch, changed_files), project, limit=3)


def shorten_note(note: MemoryNote, limit: int = MEMORY_NOTE_CHARS) -> str:
    text = note.body
    if len(text) <= limit:
        return text
    return text[:max(1, limit - 1)].rstrip() + "…"


def format_memory_section(notes: list[MemoryNote], root: Path) -> str:
    if not notes:
        return "No matching local memory was found."
    blocks: list[str] = [f"Vault: {root}"]
    remaining = MEMORY_TOTAL_CHARS
    for note in notes:
        body = shorten_note(note, min(MEMORY_NOTE_CHARS, remaining))
        if not body:
            break
        relative = note.path.relative_to(root) if note.path.is_relative_to(root) else note.path
        tags = f" · tags: {', '.join(note.tags)}" if note.tags else ""
        blocks.append(f"### {note.title}\nSource: {relative}{tags}\n\n{body}")
        remaining -= len(body)
    return "\n\n".join(blocks)


def initialise_memory(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(
            "# AgentX Local Memory\n\n"
            "This is a local Markdown vault. Open this directory in Obsidian if you want a visual editor.\n\n"
            "Project notes can use frontmatter such as:\n\n"
            "```yaml\n---\nproject: /absolute/path/to/project\ntags: architecture, decision\n---\n```\n",
            encoding="utf-8",
        )


def add_memory(root: Path, title: str, note: str, project: Path | None,
               tags: list[str]) -> Path:
    initialise_memory(root)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-.") or "memory"
    target_dir = root / "projects" / project.name if project else root / "global"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{slug}.md"
    suffix = 2
    while target.exists():
        target = target_dir / f"{slug}-{suffix}.md"
        suffix += 1
    frontmatter = ["---", f"title: {title}"]
    if project:
        frontmatter.append(f"project: {project.resolve()}")
    if tags:
        frontmatter.append(f"tags: {', '.join(tags)}")
    frontmatter.append("---")
    target.write_text("\n".join(frontmatter) + f"\n\n# {title}\n\n{note.strip()}\n", encoding="utf-8")
    return target
