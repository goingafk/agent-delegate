"""Job store for detached delegate runs.

One JSON record per job in <home>/state/jobs/<id>.json (plus <id>.result.txt and <id>.log).
Pure file helpers: no dependency on delegate.py, so they are easy to test and reuse.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from typing import Any

UTC = dt.timezone.utc
ACTIVE = {"queued", "running"}
TERMINAL = {"done", "failed", "timeout", "blocked", "cancelled", "lost", "model_mismatch", "verify_failed", "scope_violation"}
DEFAULT_MAX_RUNNING = 4
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def jobs_dir(home: Path) -> Path:
    return home / "state" / "jobs"


def job_path(home: Path, job_id: str, suffix: str = ".json") -> Path:
    if not job_id or not _ID.match(job_id) or ".." in job_id:
        raise ValueError(f"invalid job id {job_id!r}")
    return jobs_dir(home) / f"{job_id}{suffix}"


def new_job_id(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(UTC)
    return f"j{now.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"


def write_job(home: Path, record: dict[str, Any]) -> None:
    target = job_path(home, str(record["id"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=".job-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(record, out, sort_keys=True)
            out.write("\n")
        os.replace(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def read_job(home: Path, job_id: str) -> dict[str, Any] | None:
    try:
        value = json.loads(job_path(home, job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("id") == job_id else None


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A reaped-but-unwaited child (zombie) still answers kill(0); treat it as gone.
    try:
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists() and stat.read_text().split(") ")[-1][:1] == "Z":
            return False
    except OSError:
        pass
    try:
        import subprocess
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
        if state.startswith("Z"):
            return False
    except Exception:
        pass
    return True


def proc_start(pid: Any) -> str | None:
    """Start time of a process as `ps` reports it; identifies a process across pid reuse."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        import subprocess
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return None
    return out or None


def job_alive(record: dict[str, Any]) -> bool:
    """The job's process is alive AND is the same process the job started (not a recycled pid)."""
    pid = record.get("pid")
    if not pid_alive(pid):
        return False
    recorded = record.get("proc_started")
    return not recorded or proc_start(pid) == recorded


def refresh(home: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Mark an active job whose process has died as 'lost' (persisted)."""
    if record.get("status") in ACTIVE and not job_alive(record):
        created = _parse(record.get("created_at"))
        if record.get("pid") is None and created and dt.datetime.now(UTC) - created < dt.timedelta(seconds=30):
            return record  # just queued: the launcher has not recorded the pid yet
        record = dict(record, status="lost", finished_at=record.get("finished_at") or _utc_iso())
        write_job(home, record)
    return record


def list_jobs(home: Path) -> list[dict[str, Any]]:
    directory = jobs_dir(home)
    if not directory.is_dir():
        return []
    records = []
    for path in directory.glob("*.json"):
        record = read_job(home, path.stem)
        if record and record.get("id"):
            records.append(refresh(home, record))
    return sorted(records, key=lambda r: (str(r.get("created_at", "")), str(r.get("id"))), reverse=True)


def active_jobs(home: Path, agent: str | None = None) -> list[dict[str, Any]]:
    return [r for r in list_jobs(home) if r.get("status") in ACTIVE and (agent is None or r.get("agent") == agent)]


def age_str(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    if seconds < 48 * 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def limits(caps: dict[str, Any]) -> tuple[int, dict[str, int]]:
    section = caps.get("jobs")
    if not isinstance(section, dict):
        section = {}
    total = int(section.get("max_running", DEFAULT_MAX_RUNNING))
    per_agent = {str(k): int(v) for k, v in (section.get("per_agent") or {}).items()}
    return total, per_agent


def slot_block(home: Path, agent: str, caps: dict[str, Any]) -> str | None:
    """Why a new job for `agent` may not start now, or None."""
    total, per_agent = limits(caps)
    active = active_jobs(home)
    mine = [r for r in active if r.get("agent") == agent]
    if agent in per_agent and len(mine) >= per_agent[agent]:
        ids = ", ".join(str(r["id"]) for r in mine)
        return f"{agent} already has {len(mine)}/{per_agent[agent]} jobs running ({ids}); wait for one"
    if len(active) >= total:
        ids = ", ".join(str(r["id"]) for r in active)
        return f"total running jobs {len(active)}/{total} ({ids}); wait for one"
    return None


def _utc_iso(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(UTC)).isoformat().replace("+00:00", "Z")


def _parse(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def prune(home: Path, days: int, now: dt.datetime | None = None, dry_run: bool = False) -> dict[str, int]:
    """Delete finished job records/results/logs and raw run logs older than `days`."""
    now = now or dt.datetime.now(UTC)
    cutoff = now - dt.timedelta(days=days)
    removed_jobs = removed_runs = 0
    directory = jobs_dir(home)
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            record = read_job(home, path.stem)
            if not record or record.get("status") not in TERMINAL:
                continue
            stamp = _parse(record.get("finished_at")) or _parse(record.get("created_at"))
            if stamp is None or stamp >= cutoff:
                continue
            removed_jobs += 1
            if not dry_run:
                for suffix in (".json", ".result.txt", ".log"):
                    try:
                        job_path(home, path.stem, suffix).unlink()
                    except (OSError, ValueError):
                        pass
    for folder in (home / "runs", home / "state" / "briefs"):
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            try:
                old = (path.is_file() and path.suffix in (".jsonl", ".txt", ".md", ".json")
                       and dt.datetime.fromtimestamp(path.stat().st_mtime, UTC) < cutoff)
            except OSError:  # vanished between listing and stat
                continue
            if old:
                removed_runs += 1
                if not dry_run:
                    try:
                        path.unlink()
                    except OSError:
                        pass
    return {"jobs": removed_jobs, "runs": removed_runs}


def maybe_prune(home: Path, days: int, now: dt.datetime | None = None) -> dict[str, int] | None:
    """prune() at most once per 24 hours; returns None when skipped."""
    now = now or dt.datetime.now(UTC)
    marker = home / "state" / "last_prune"
    last = None
    try:
        last = dt.datetime.fromtimestamp(float(marker.read_text().strip()), UTC)
    except (OSError, ValueError):
        pass
    if last is not None and now - last < dt.timedelta(hours=24):
        return None
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(now.timestamp()))
    return prune(home, days, now)
