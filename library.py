"""Delegate skill library: curated, delegate-safe playbooks injected into briefs.

Each playbook is library/<name>.md with `name:` and `description:` frontmatter. A job spec lists
`skills = ["name", ...]`; the wrapper appends those playbooks to the brief, so every agent
(codex, claude, copilot, qwen) gets them the same way. Over-budget requests fail loudly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

LIBRARY_DIR = Path(__file__).resolve().parent / "library"
DEFAULT_MAX_CHARS = 12000
MAX_SKILL_CHARS = 2500
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
FRONT = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)


class LibraryError(ValueError):
    """A requested playbook is unknown, unsafe or over budget."""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str


def _parse(path: Path) -> Skill | None:
    match = FRONT.match(path.read_text(encoding="utf-8"))
    if not match:
        return None
    meta = dict(line.split(":", 1) for line in match.group(1).splitlines() if ":" in line)
    name = meta.get("name", "").strip()
    if name != path.stem:
        return None
    return Skill(name, meta.get("description", "").strip(), match.group(2).strip())


def list_skills(directory: Path | str | None = None) -> list[Skill]:
    base = Path(directory) if directory else LIBRARY_DIR
    found = [skill for path in sorted(base.glob("*.md")) if (skill := _parse(path))]
    return sorted(found, key=lambda s: s.name)


def check_names(names: list[str], directory: Path | str | None = None) -> list[str]:
    """Return one error string per bad name (for spec lint)."""
    known = {s.name for s in list_skills(directory)}
    return [f"skills: {n!r} is not in the delegate library (known: {', '.join(sorted(known)) or 'none'})"
            for n in names if not isinstance(n, str) or not NAME.match(n) or n not in known]


def render(names: list[str], directory: Path | str | None = None, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    unique = list(dict.fromkeys(names))
    if not unique:
        return ""
    errors = check_names(unique, directory)
    if errors:
        raise LibraryError("; ".join(errors))
    by_name = {s.name: s for s in list_skills(directory)}
    sections = [f"### {n}\n{by_name[n].body}" for n in unique]
    text = "## Playbooks\nFollow these working methods; the task above and the repo's own AGENTS.md take precedence.\n\n" \
           + "\n\n".join(sections)
    if len(text) > max_chars:
        raise LibraryError(f"playbooks total {len(text)} chars, over the {max_chars} budget; drop one or shorten it")
    return text


def _secret_patterns() -> tuple:
    """spec.SECRET_PATTERNS, loaded by sibling path so this works with or without delegation/ on sys.path."""
    import importlib.util
    import sys
    module = sys.modules.get("spec")
    if module is None or not hasattr(module, "SECRET_PATTERNS"):
        found = importlib.util.spec_from_file_location("delegation_spec", Path(__file__).with_name("spec.py"))
        module = importlib.util.module_from_spec(found)
        sys.modules["delegation_spec"] = module
        found.loader.exec_module(module)
    return module.SECRET_PATTERNS


def save(name: str, description: str, body: str, directory: Path | str | None = None,
         overwrite: bool = False) -> Path:
    """Validate and write library/<name>.md. Refuses bad names, oversize text, secrets and silent overwrite."""
    patterns = _secret_patterns()
    base = Path(directory) if directory else LIBRARY_DIR
    description, body = description.strip(), body.strip()
    if not NAME.match(name or ""):
        raise LibraryError(f"name {name!r} must be lowercase letters, digits, - or _")
    if not description or "\n" in description:
        raise LibraryError("description must be one non-empty line")
    if not body:
        raise LibraryError("body is empty")
    if len(body) > MAX_SKILL_CHARS:
        raise LibraryError(f"body is {len(body)} chars; keep playbooks under {MAX_SKILL_CHARS}")
    if any(p.search(f"{description}\n{body}") for p in patterns):
        raise LibraryError("looks like a secret value; refer to secrets by name only")
    path = base / f"{name}.md"
    if path.exists() and not overwrite:
        raise LibraryError(f"{name!r} already exists; pass overwrite to replace it")
    base.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8")
    return path
