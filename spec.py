"""Job spec files: the structured way to brief a delegate.

A spec is a small TOML file (goal, file scope, constraints, acceptance, verify command, agent/model).
`lint` catches mistakes before any quota is spent; `render_brief` turns a spec into the same brief
layout every time, with the standing rules always included.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib
from typing import Any, Iterable

REQUIRED = ("task", "agent", "model", "reason", "goal", "workdir")
OPTIONAL = ("mode", "risk", "context", "preserve", "files", "tests_first", "constraints", "acceptance", "verify",
            "verify_timeout", "opus_reason", "effort", "timeout", "card", "board", "no_card", "worktree",
            "budget_usd", "schema", "skills")
ALLOWED = set(REQUIRED) | set(OPTIONAL)
MODES = ("implement", "review")
RISKS = ("low", "high")
EFFORTS = {"claude": ("low", "medium", "high", "xhigh", "max"),
           "codex": ("minimal", "low", "medium", "high", "xhigh")}
PROTECTED_BRANCHES = {"main", "master", "production"}
LONG_GOAL = 6000

SCHEMAS: dict[str, dict[str, Any]] = {
    "findings": {
        "type": "object", "additionalProperties": False, "required": ["summary", "findings"],
        "properties": {
            "summary": {"type": "string"},
            "findings": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["severity", "file", "line", "evidence", "fix"],
                "properties": {"severity": {"type": "string", "enum": ["high", "medium", "low"]},
                               "file": {"type": "string"}, "line": {"type": "integer"},
                               "evidence": {"type": "string"}, "fix": {"type": "string"}}}},
        },
    },
}

DEFAULT_STANDING = (
    "Run every shell command in the foreground and wait for it to finish; do not start background shells or background tasks (their late notifications overwrite your final report).",
    "Staging only: no git push, no deploy, no production writes, no network calls beyond what the task needs.",
    "Never read, print or write secret values; refer to secrets by name only.",
    "Do not commit; leave your changes in the working tree for the coordinator to review.",
    "Leave unrelated files and unrelated code alone; no drive-by refactors or reformatting.",
    "Do not claim test, build or analyzer results you did not actually obtain; say plainly what you could not run.",
)

SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}"),
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{10,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9/+_-]{16,}"),
)
QWEN_READ = re.compile(r"(?i)\b(?:read|open|look at|see|check|inspect)\s+(?:the\s+)?(?:file|files)\b")
BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class SpecError(ValueError):
    """The spec file could not be read or parsed."""


def load_spec(path: Path | str) -> dict[str, Any]:
    path = Path(path).expanduser()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise SpecError(f"cannot read spec {path}: {exc.strerror or exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise SpecError(f"{path.name}: invalid TOML ({exc})") from exc
    return data


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _bad_glob(pattern: Any) -> bool:
    if not isinstance(pattern, str) or not pattern.strip():
        return True
    return pattern.startswith(("/", "~")) or ".." in pattern.replace("\\", "/").split("/")


def lint(spec: dict[str, Any], agents: Iterable[str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    missing = [key for key in REQUIRED if not spec.get(key)]
    if missing:
        errors.append("missing required field(s): " + ", ".join(missing))
    for key in sorted(set(spec) - ALLOWED):
        errors.append(f"unknown key {key!r} (typo?); allowed: {', '.join(sorted(ALLOWED))}")
    agent = spec.get("agent")
    if agent and agent not in tuple(agents):
        errors.append(f"agent {agent!r} must be one of {', '.join(agents)}")
    effort = spec.get("effort")
    if effort is not None:
        if agent in EFFORTS and effort not in EFFORTS[agent]:
            errors.append(f"effort {effort!r} is invalid for {agent}; use {', '.join(EFFORTS[agent])}")
        elif agent in ("copilot", "qwen"):
            warnings.append(f"effort is ignored for {agent}")
    mode = spec.get("mode", "review")
    if mode not in MODES:
        errors.append(f"mode {mode!r} must be one of {', '.join(MODES)}")
    if spec.get("risk", "low") not in RISKS:
        errors.append(f"risk {spec.get('risk')!r} must be one of {', '.join(RISKS)}")
    files = spec.get("files", [])
    if not isinstance(files, list):
        errors.append("files must be a list of scope globs")
        files = []
    for pattern in files:
        if _bad_glob(pattern):
            errors.append(f"files: {pattern!r} must be a glob relative to the repo root (no leading /, ~ or ..)")
    if mode == "implement":
        if not files:
            errors.append("implement mode needs files (the scope the agent may change)")
        if not str(spec.get("verify", "")).strip():
            errors.append("implement mode needs verify (a command the wrapper runs itself after the agent finishes)")
        if not spec.get("acceptance"):
            warnings.append("no acceptance criteria listed; the agent cannot tell when it is done")
    for text in _strings(spec):
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            errors.append("looks like a secret value inside the spec; use the secret's name only")
            break
    if agent == "qwen" and any(QWEN_READ.search(text) for text in _strings({k: spec.get(k) for k in ("goal", "context", "constraints", "acceptance")})):
        errors.append("qwen sees only the brief text and cannot open repo files: paste the content into the spec (context) "
                      "or pipe it through qwen_delegate.py instead of asking it to read a file")
    workdir = spec.get("workdir")
    if workdir and not Path(str(workdir)).expanduser().is_dir():
        errors.append(f"workdir {workdir!r} is not a directory")
    worktree = spec.get("worktree")
    if worktree is not None:
        branch = worktree.get("branch") if isinstance(worktree, dict) else None
        if not isinstance(branch, str) or not branch:
            errors.append("worktree needs a branch name")
        elif not BRANCH.match(branch) or ".." in branch or branch.endswith(("/", ".lock")):
            errors.append(f"worktree branch {branch!r} is not a safe branch name")
        elif branch in PROTECTED_BRANCHES:
            errors.append(f"worktree branch {branch!r} is protected; use an agent/<task> branch")
    budget = spec.get("budget_usd")
    if budget is not None:
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
            errors.append("budget_usd must be a positive number (US dollars)")
        elif agent and agent != "claude":
            warnings.append(f"budget_usd only applies to claude; {agent} ignores it (its timeout still applies)")
    if spec.get("schema") is not None and spec["schema"] not in SCHEMAS:
        errors.append(f"schema {spec['schema']!r} must be one of {', '.join(SCHEMAS)}")
    skills = spec.get("skills")
    if skills is not None:
        if not isinstance(skills, list):
            errors.append("skills must be a list of library playbook names")
        else:
            import library
            errors.extend(library.check_names(skills))
    if len(str(spec.get("goal", ""))) > LONG_GOAL:
        warnings.append(f"goal is very long ({len(str(spec['goal']))} chars); long briefs cost tokens and dilute the ask")
    return errors, warnings


def _bullets(items: Iterable[Any]) -> str:
    return "\n".join(f"- {item}" for item in items)


def render_brief(spec: dict[str, Any], standing: Iterable[str], playbooks: str = "") -> str:
    mode = spec.get("mode", "review")
    parts = [f"# Task: {spec['task']}", f"Mode: {mode}. Risk: {spec.get('risk', 'low')}.", "## Goal\n" + str(spec["goal"]).strip()]
    if spec.get("context"):
        parts.append("## Context\n" + str(spec["context"]).strip())
    if spec.get("preserve"):
        parts.append("## Preserve (do not change behaviour)\n" + _bullets(spec["preserve"]))
    if mode == "implement" and spec.get("files"):
        parts.append("## Files you may change\n" + _bullets(f"`{p}`" for p in spec["files"]) +
                     "\n(Paths are relative to the repo root. If the task needs a change outside this list, stop and say why instead of editing.)")
    if spec.get("tests_first"):
        parts.append("## Tests first\n" + str(spec["tests_first"]).strip())
    parts.append("## Constraints\n" + _bullets([*standing, *spec.get("constraints", [])]))
    if spec.get("acceptance"):
        parts.append("## Acceptance criteria\n" + _bullets(spec["acceptance"]))
    if mode == "implement" and str(spec.get("verify", "")).strip():
        parts.append(f"## Verification\nThe wrapper runs `{str(spec['verify']).strip()}` itself after you finish and records the result. "
                     "Run it yourself first if your sandbox allows; if it cannot run, say so plainly and do not claim it passed.")
    if spec.get("schema") in SCHEMAS:
        parts.append("## Output format\nReply with ONLY one JSON object (no prose around it) that matches this JSON Schema. "
                     "Use severity high/medium/low, an integer line (0 if unknown), concrete evidence and a suggested fix:\n"
                     + json.dumps(SCHEMAS[spec["schema"]], indent=2))
    if playbooks:
        parts.append(playbooks.strip())
    if mode == "review":
        parts.append("## Report\nThis is a read-only review: change nothing. List findings, each with severity (high/medium/low), "
                     "file:line, evidence, and a suggested fix. Do not speculate about compile or test results; the coordinator runs them. "
                     "Put the whole report in your final message.")
    else:
        parts.append("## Report\nPut your report in your final message (do not write a report file into the repo): files changed, "
                     "the commands you actually ran with their real results, assumptions you made about third-party APIs or schemas, "
                     "and anything unresolved or out of scope that you left unchanged.")
    return "\n\n".join(parts) + "\n"
