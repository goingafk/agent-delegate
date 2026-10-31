"""Git helpers for delegated work: per-task worktrees, changed-file lists, scope globs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Iterable

PROTECTED = {"main", "master", "production"}
BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class GitWorkError(RuntimeError):
    """A git operation could not be completed safely."""


def _git(cwd: Path | str, *args: str, check: bool = True, input_text: str | None = None) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=120, env=env,
                              input=input_text)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitWorkError(f"git {' '.join(args[:2])}: {exc}") from exc
    if check and done.returncode != 0:
        raise GitWorkError(f"git {' '.join(args[:2])}: {(done.stderr or done.stdout).strip()}")
    return done.stdout if done.returncode == 0 else ""


def glob_match(pattern: str, path: str) -> bool:
    """Match a repo-relative path: `*` stays in a directory, `**` crosses directories, trailing `/` = everything below."""
    if pattern.endswith("/"):
        pattern += "**"
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.fullmatch("".join(out), path) is not None


def out_of_scope(changed: Iterable[str], patterns: Iterable[str]) -> list[str]:
    patterns = list(patterns)
    return [path for path in changed if not any(glob_match(p, path) for p in patterns)]


def repo_root(path: Path | str) -> Path | None:
    try:
        out = _git(path, "rev-parse", "--show-toplevel", check=False).strip()
    except GitWorkError:
        return None
    return Path(out) if out else None


def head(path: Path | str) -> str | None:
    try:
        return _git(path, "rev-parse", "HEAD", check=False).strip() or None
    except GitWorkError:
        return None


def changed_files(workdir: Path | str, since: str | None = None) -> list[str]:
    """Files modified, staged, untracked or renamed in the working tree (and committed since `since`), repo-relative."""
    root = repo_root(workdir)
    if root is None:
        return []
    found: set[str] = set()
    records = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").split("\0")
    i = 0
    while i < len(records):
        record = records[i]
        i += 1
        if len(record) < 4:
            continue
        status, name = record[:2], record[3:]
        found.add(name)
        if "R" in status or "C" in status:
            if i < len(records) and records[i]:
                found.add(records[i])
            i += 1
    if since:
        for name in _git(root, "diff", "--name-only", "--no-renames", "-z", f"{since}..HEAD").split("\0"):
            if name:
                found.add(name)
    return sorted(found)


def _symlink_hash(root: Path, name: str) -> str:
    done = subprocess.run(["git", "hash-object", "--stdin"], cwd=root,
                          input=os.fsencode(os.readlink(root / name)), capture_output=True, timeout=120)
    if done.returncode:
        raise GitWorkError(f"cannot hash symlink {name}")
    return done.stdout.decode().strip()


def _dir_hash(root: Path, name: str) -> str:
    """A directory entry (untracked nested repo, dirty submodule): its HEAD plus its own status."""
    path = root / name
    nested_head = _git(path, "rev-parse", "HEAD", check=False).strip()
    status = _git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all", check=False)
    return "dir:" + hashlib.sha256((nested_head + "\0" + status).encode()).hexdigest()


def hash_many(root: Path, names: Iterable[str]) -> dict[str, str]:
    """Content hashes for repo-relative names: one batched `git hash-object` for regular files."""
    result: dict[str, str] = {}
    files: list[str] = []
    for name in names:
        path = root / name
        if path.is_symlink():
            result[name] = _symlink_hash(root, name)
        elif not path.exists():
            result[name] = "deleted"
        elif path.is_dir():
            result[name] = _dir_hash(root, name)
        elif "\n" in name:
            result[name] = _git(root, "hash-object", "--", name).strip()
        else:
            files.append(name)
    if files:
        out = _git(root, "hash-object", "--stdin-paths", input_text="\n".join(files) + "\n").split()
        if len(out) != len(files):
            raise GitWorkError("git hash-object --stdin-paths returned an unexpected number of hashes")
        result.update(zip(files, out))
    return result


def content_hash(root: Path, name: str) -> str:
    return hash_many(root, [name])[name]


def dirty_snapshot(workdir: Path | str) -> dict[str, str]:
    root = repo_root(workdir)
    if root is None:
        return {}
    return hash_many(root, changed_files(root))


def agent_changes(workdir: Path | str, since: str | None,
                  before: dict[str, str]) -> tuple[list[str], list[str]]:
    changed = set(changed_files(workdir, since=since))
    root = repo_root(workdir)
    ignored = []
    if root is not None:
        now = hash_many(root, before)
        for name, original in before.items():
            if now[name] == original:
                changed.discard(name)
                ignored.append(name)
            else:
                changed.add(name)  # also catches a removed pre-existing untracked file
    return sorted(changed), sorted(ignored)


def fingerprint(workdir: Path | str) -> str | None:
    """Hash the repository HEAD, complete tracked diff and non-ignored untracked content."""
    root = repo_root(workdir)
    if root is None or not head(root):
        return None
    digest = hashlib.sha256()
    digest.update(_git(root, "rev-parse", "HEAD").strip().encode())
    diff = subprocess.run(["git", "diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv"],
                          cwd=root, capture_output=True, timeout=120)
    if diff.returncode:
        raise GitWorkError("cannot fingerprint git diff HEAD")
    digest.update(b"\0" + diff.stdout)
    names = sorted(filter(None, _git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")))
    hashes = hash_many(root, names)
    for name in names:
        digest.update(b"\0" + json.dumps([name, hashes[name]], ensure_ascii=True).encode())
    return digest.hexdigest()


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "x"


def worktree_path(home: Path, root: Path, branch: str) -> Path:
    return home / "worktrees" / _slug(root.name) / _slug(branch.replace("/", "-"))


def _registered(root: Path) -> dict[str, Path]:
    """branch name -> worktree path for every worktree git knows about in this repo."""
    result: dict[str, Path] = {}
    path = None
    for line in _git(root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
        elif line.startswith("branch refs/heads/") and path is not None:
            result[line[len("branch refs/heads/"):]] = path
    return result


def create_worktree(home: Path, workdir: Path | str, branch: str, base: str | None = None) -> dict:
    if not BRANCH.match(branch) or ".." in branch or branch.endswith(("/", ".lock")):
        raise GitWorkError(f"{branch!r} is not a safe branch name")
    if branch in PROTECTED:
        raise GitWorkError(f"branch {branch!r} is protected; agents work on agent/<task> branches")
    workdir = Path(workdir).expanduser().resolve()
    root = repo_root(workdir)
    if root is None:
        raise GitWorkError(f"{workdir} is not inside a git repository")
    root = root.resolve()
    try:
        rel = workdir.relative_to(root)
    except ValueError as exc:
        raise GitWorkError(f"{workdir} is outside {root}") from exc
    registered = _registered(root)
    if branch in registered:
        path = registered[branch].resolve()
        if path == root:
            raise GitWorkError(f"branch {branch!r} is checked out in the main checkout; use a separate agent/ branch")
        created = False
    else:
        path = worktree_path(home, root, branch)
        if path.exists():
            raise GitWorkError(f"{path} already exists but is not a worktree of {root.name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=str(root)).returncode == 0
        if exists:
            _git(root, "worktree", "add", str(path), branch)
        else:
            if not base:
                raise GitWorkError(f"branch {branch!r} does not exist; give a base ref to create it from")
            if subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], cwd=str(root),
                              capture_output=True).returncode != 0:
                raise GitWorkError(f"base ref {base!r} not found in {root.name}")
            _git(root, "worktree", "add", "-b", branch, str(path), base)
        created = True
    return {"path": path, "workdir": path / rel, "branch": branch, "created": created,
            "base_sha": head(path), "repo": root}


def _managed(home: Path) -> list[Path]:
    base = home / "worktrees"
    if not base.is_dir():
        return []
    return sorted(p for repo in base.iterdir() if repo.is_dir() for p in repo.iterdir() if (p / ".git").exists())


def list_worktrees(home: Path) -> list[dict]:
    result = []
    for path in _managed(home):
        branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
        dirty = bool(_git(path, "status", "--porcelain", check=False).strip())
        result.append({"path": str(path), "branch": branch, "dirty": dirty, "head": head(path)})
    return result


def remove_worktree(home: Path, target: str, force: bool = False) -> str:
    """Remove a managed worktree (by branch name or path). Never deletes the branch."""
    chosen = None
    for item in list_worktrees(home):
        if target in (item["branch"], item["path"]):
            chosen = item
            break
    if chosen is None:
        raise GitWorkError(f"no managed worktree for {target!r}")
    if chosen["dirty"] and not force:
        raise GitWorkError(f"{chosen['path']} has uncommitted changes; review them or pass --force")
    path = Path(chosen["path"])
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    root = Path(common).parent
    _git(root, "worktree", "remove", *(["--force"] if force else []), str(path))
    return f"removed {path} (branch {chosen['branch']} kept)"
