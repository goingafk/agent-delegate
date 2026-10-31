#!/usr/bin/env python3
"""Create and update unassigned Agent Activity cards."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def kanban(*args: str) -> str:
    executable = shutil.which("hermes")
    executable = str(Path(executable).absolute()) if executable else str(
        Path("~/.hermes/hermes-agent/.hermes/bin/hermes").expanduser())
    result = subprocess.run([executable, "kanban", "--board", "agents", *args],
                            capture_output=True, text=True, timeout=20)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ValueError(f"{args[0]}: {detail}")
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("title")
    start.add_argument("--model", required=True)
    start.add_argument("--provider")
    start.add_argument("--body")
    start.add_argument("--ttl", type=int, default=14400)
    for verb, field in (("note", "text"), ("done", "summary"), ("block", "reason")):
        command = commands.add_parser(verb)
        command.add_argument("id")
        command.add_argument(field)
    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            if args.ttl <= 0:
                raise ValueError("--ttl must be positive")
            fields = ["create", args.title, "--model", args.model, "--created-by", os.environ.get("DELEGATE_CREATED_BY", "delegate"), "--json"]
            for option in ("provider", "body"):
                value = getattr(args, option)
                if value is not None:
                    fields.extend([f"--{option}", value])
            created = json.loads(kanban(*fields))
            if not isinstance(created, dict) or not isinstance(created.get("id"), str) or not created["id"]:
                raise ValueError("create: response has no card id")
            card_id = created["id"]
            try:
                kanban("claim", card_id, "--ttl", str(args.ttl))
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                raise ValueError(f"card {card_id} created, but claim failed: {exc}") from exc
            print(card_id)
        elif args.command == "note":
            kanban("comment", args.id, args.text)
        elif args.command == "done":
            kanban("complete", args.id, "--force", "--summary", args.summary)
        else:
            kanban("block", args.id, args.reason)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"kb: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
