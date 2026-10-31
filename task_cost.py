#!/usr/bin/env python3
"""Snapshot orchestrator usage and report per-task delegation costs."""

from __future__ import annotations

import argparse
from contextlib import closing
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parent
SESSION_DB = Path(os.environ.get("HERMES_SESSION_DB", "~/.hermes/state.db")).expanduser()
UTC = dt.timezone.utc
TOKEN_COUNTERS = ("input_tokens", "output_tokens", "cache_read_tokens",
                  "cache_write_tokens", "reasoning_tokens")
COUNTERS = (*TOKEN_COUNTERS, "estimated_cost_usd")


def home() -> Path:
    return Path(os.environ.get("DELEGATE_HOME", ROOT)).expanduser()


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def timestamp(value: Any) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError(f"invalid timestamp: {value!r}")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def read_jsonl(path: Path) -> list[tuple[int, dict[str, Any]]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("expected a JSON object")
        except ValueError as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
        rows.append((number, row))
    return rows


def recent_rows(path: Path, days: int, now: dt.datetime) -> list[tuple[int, dict[str, Any]]]:
    cutoff = now - dt.timedelta(days=days)
    return [(number, row) for number, row in read_jsonl(path)
            if cutoff <= timestamp(row.get("ts")) <= now]


def session_usage(session_id: str | None = None) -> dict[str, Any]:
    # as_uri escapes spaces, '?' and '#' in database filenames. The connection
    # cannot create the database or mutate it, even if a caller attempts a write.
    with closing(sqlite3.connect(SESSION_DB.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        fields = ", ".join(("id", "model", *COUNTERS, "last_activity_at", "started_at"))
        row = None
        # Explicit id, else the live session Hermes exports, else the newest Claude session of ANY
        # model (the orchestrator can run on Sonnet or Opus; matching only Opus reported 0 tokens).
        wanted = session_id or os.environ.get("HERMES_SESSION_ID") or None
        if wanted:
            row = connection.execute(f"SELECT {fields} FROM sessions WHERE id = ?", (wanted,)).fetchone()
        if row is None and session_id is None:
            row = connection.execute(
                f"SELECT {fields} FROM sessions WHERE model GLOB 'claude-*' "
                "ORDER BY last_activity_at DESC, started_at DESC, id DESC LIMIT 1").fetchone()
    if row is None:
        raise ValueError(f"session {session_id!r} not found" if session_id else "no claude session found")
    result = dict(row)
    for key in COUNTERS:
        result[key] = result[key] or 0
    return result


def snapshot_path(task: str) -> Path:
    if not task.strip() or task in {".", ".."} or any(c in task for c in ("/", "\\", "\x00")):
        raise ValueError("TASK_ID must be a nonempty filename without path separators")
    return home() / "state" / "task_costs" / f"{task}.json"


def start_task(task: str, session_id: str | None = None) -> dict[str, Any]:
    path = snapshot_path(task)
    # Timestamp before reading: delegated completions during the read belong to
    # this task window rather than disappearing between the read and snapshot.
    started = utc_now().isoformat()
    snapshot = {"task": task, "ts": started, "session": session_usage(session_id)}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return snapshot


def aggregate_delegates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in sorted(rows, key=lambda item: timestamp(item["ts"])):
        key = (row.get("agent", "unknown"), row.get("model", "unknown"), row.get("effort") or "-")
        group = groups.setdefault(key, {
            "agent": key[0], "model": key[1], "effort": key[2], "runs": 0, "input_tokens": 0,
            "output_tokens": 0, "cached_tokens": 0, "tokens": 0,
            "cost_usd_list": None, "costed_runs": 0, "premium_requests": None,
            "quota_after": None, "duration_s": 0, "status_counts": {},
        })
        group["runs"] += 1
        for field in ("input_tokens", "output_tokens", "cached_tokens", "duration_s"):
            group[field] += row.get(field) or 0
        group["tokens"] = group["input_tokens"] + group["output_tokens"]
        if row.get("cost_usd_list") is not None:
            group["cost_usd_list"] = (group["cost_usd_list"] or 0) + row["cost_usd_list"]
            group["costed_runs"] += 1
        if row.get("premium_requests") is not None:
            group["premium_requests"] = (group["premium_requests"] or 0) + row["premium_requests"]
        # Keep the last observation, including unknown, rather than implying a
        # stale quota reading was measured after the latest run.
        group["quota_after"] = row.get("quota_after")
        status = row.get("status", "unknown")
        group["status_counts"][status] = group["status_counts"].get(status, 0) + 1
    return [groups[key] for key in sorted(groups)]


def money(value: float | None) -> str:
    return "-" if value is None else f"${value:.6f}"


def markdown_text(value: str) -> str:
    return " ".join(value.split()).replace("|", "\\|")


def format_report(row: dict[str, Any]) -> str:
    orchestrator = row["orchestrator"]
    parts = [f"**Cost — {markdown_text(row['task'])}**: Orchestrator ({orchestrator['model']}): "
             f"{orchestrator['input_tokens']}/{orchestrator['output_tokens']} tok, "
             f"{orchestrator['cache_read_tokens']} cached, "
             f"{orchestrator['cache_write_tokens']} cache-write, "
             f"{orchestrator['reasoning_tokens']} reasoning, ~{money(orchestrator['estimated_cost_usd'])}."]
    delegates = []
    for group in row["delegates"]:
        cost = money(group["cost_usd_list"])
        if group["costed_runs"] and group["costed_runs"] < group["runs"]:
            cost += f" (+{group['runs'] - group['costed_runs']} unpriced runs)"
        premium = group["premium_requests"] if group["premium_requests"] is not None else "-"
        item = (f"{group['agent']}·{group['model']}·{group.get('effort') or '-'}: {group['runs']} runs, "
                f"{group['input_tokens']}/{group['output_tokens']} tok ({group['tokens']} total), "
                f"{group['cached_tokens']} cached, {cost}, premium {premium}")
        if group["agent"] == "codex":
            quota = group["quota_after"] or {}
            item += f", Codex quota after 5h/wk {quota.get('five_hour_pct', '?')}/{quota.get('weekly_pct', '?')}%"
        delegates.append(item)
    parts.append("Delegates: " + ("; ".join(delegates) if delegates else "none") + ".")
    parts.append(f"Delegation share: {row['delegation_share_pct']:.1f}%. Wall time: {row['duration_s']:.1f}s.")
    return " ".join(parts)


def end_task(task: str, card: str | None = None, outcome: str = "ok", notes: str = "") -> dict[str, Any]:
    snapshot = json.loads(snapshot_path(task).read_text(encoding="utf-8"))
    before = snapshot["session"]
    after = session_usage(before["id"])
    now = utc_now()
    started = timestamp(snapshot["ts"])
    model = before["model"] if after["model"] == before["model"] else f"{before['model']}\u2192{after['model']}"
    orchestrator = {"session_id": before["id"], "model": model,
                    **{key: after[key] - before[key] for key in COUNTERS}}
    orchestrator["tokens"] = orchestrator["input_tokens"] + orchestrator["output_tokens"]
    runs = [row for _, row in read_jsonl(home() / "ledger.jsonl")
            if (row.get("task") == task or (card is not None and (row.get("card_id") == card or row.get("parent_card") == card)))
            and started < timestamp(row.get("ts")) <= now]
    delegates = aggregate_delegates(runs)
    delegate_tokens = sum(group["tokens"] for group in delegates)
    total_tokens = orchestrator["tokens"] + delegate_tokens
    row = {
        "task": task, "ts": now.isoformat(), "started_at": snapshot["ts"],
        "card_id": card, "orchestrator": orchestrator, "delegates": delegates,
        "delegate_runs": runs, "delegate_tokens": delegate_tokens, "total_tokens": total_tokens,
        "delegation_share_pct": 100 * delegate_tokens / total_tokens if total_tokens else 0,
        "duration_s": (now - started).total_seconds(), "outcome": outcome, "notes": notes,
    }
    row["report"] = format_report(row)
    with (home() / "task_costs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    print(row["report"])
    if card:
        completed = subprocess.run([sys.executable, str(ROOT / "kb.py"), "note", card, row["report"]],
                                   capture_output=True, text=True, timeout=30)
        if completed.returncode:
            raise ValueError("report saved, but card comment failed: " +
                             (completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"))
    return row


def summary(days: int) -> str:
    rows = [row for _, row in recent_rows(home() / "task_costs.jsonl", days, utc_now())]
    lines = ["| Task | Outcome | Orch tok | Delegate tok | Orch $ | Delegate list $ (known) | Premium | Wall s |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    totals = [0, 0, 0.0, None, 0, 0.0]
    for row in rows:
        groups = row["delegates"]
        known = [group["cost_usd_list"] for group in groups if group["cost_usd_list"] is not None]
        values = [row["orchestrator"]["tokens"], row["delegate_tokens"],
                  row["orchestrator"]["estimated_cost_usd"], sum(known) if known else None,
                  sum(group["premium_requests"] or 0 for group in groups), row["duration_s"]]
        lines.append(f"| {markdown_text(row['task'])} | {row['outcome']} | {values[0]} | {values[1]} | "
                     f"{money(values[2])} | {money(values[3])} | {values[4]} | {values[5]:.1f} |")
        for index, value in enumerate(values):
            if value is not None:
                totals[index] = (totals[index] or 0) + value
    lines.append(f"| **Totals ({len(rows)} tasks)** | | {totals[0]} | {totals[1]} | {money(totals[2])} | "
                 f"{money(totals[3])} | {totals[4]} | {totals[5]:.1f} |")
    lines.append("Token totals = input + output; cache/reasoning counters are reported separately. "
                 "Delegate dollars total only known list costs; '-' means unavailable.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("task")
    start.add_argument("--session")
    end = commands.add_parser("end")
    end.add_argument("task")
    end.add_argument("--card")
    end.add_argument("--outcome", choices=("ok", "failed"), default="ok")
    end.add_argument("--notes", default="")
    table = commands.add_parser("summary")
    table.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            start_task(args.task, args.session)
        elif args.command == "end":
            end_task(args.task, args.card, args.outcome, args.notes)
        else:
            if args.days < 0:
                raise ValueError("--days must be non-negative")
            print(summary(args.days))
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"task_cost: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
