#!/usr/bin/env python3
"""Quota-aware, policy-constrained wrapper for coding-agent CLIs."""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import fcntl
import contextlib
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import tomllib
import urllib.request
from typing import Any, Iterable

import gitwork
import jobs
import library as skill_library
import spec as jobspec


OK = 0
BAD_USAGE = 2
BLOCKED = 3
AGENT_FAILED = 4
MODEL_MISMATCH = 5
STILL_RUNNING = 6
VERIFY_FAILED = 7
SCOPE_VIOLATION = 8
UTC = dt.timezone.utc
AGENTS = ("qwen", "codex", "claude", "copilot")


def delegate_home() -> Path:
    return Path(os.environ.get("DELEGATE_HOME", Path(__file__).resolve().parent)).expanduser()


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def qwen_script() -> Path:
    default = Path(__file__).resolve().parent / "qwen_delegate.py"
    return Path(os.environ.get("QWEN_SCRIPT", default)).expanduser()


def qwen_host() -> str:
    return os.environ.get("QWEN_HOST", "http://localhost:11434").rstrip("/")


def created_by() -> str:
    return os.environ.get("DELEGATE_CREATED_BY", "delegate")


def kanban_enabled() -> bool:
    """Cards need the Hermes CLI. DELEGATE_NO_KANBAN=1 disables them, =0 forces them on;
    unset means "on only if `hermes` is on PATH", so other installs never see kanban warnings."""
    flag = os.environ.get("DELEGATE_NO_KANBAN")
    if flag == "1":
        return False
    if flag == "0":
        return True
    return shutil.which("hermes") is not None


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def load_caps(path: Path | None = None) -> dict[str, Any]:
    target = path or delegate_home() / "caps.toml"
    try:
        with target.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read caps config {target}: {exc}") from exc


def resolve_effort(agent: str, model: str, risk: str, explicit: str | None,
                   caps: dict[str, Any], *, claude_path: Path | None = None,
                   codex_path: Path | None = None) -> tuple[str | None, str]:
    if agent not in jobspec.EFFORTS:
        return None, "n/a"
    if explicit is not None:
        return explicit, "spec"
    policy = caps.get("effort", {}).get(model, {}).get(risk)
    if policy is not None:
        return policy, "policy"
    try:
        if agent == "claude":
            path = claude_path or Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / "settings.json"
            value = json.loads(path.read_text(encoding="utf-8")).get("effortLevel")
        else:
            path = codex_path or codex_home() / "config.toml"
            with path.open("rb") as handle:
                value = tomllib.load(handle).get("model_reasoning_effort")
        return (value if value in jobspec.EFFORTS[agent] else None), "cli-default"
    except (OSError, ValueError, AttributeError):
        return None, "cli-default"


def effort_label(args: argparse.Namespace) -> str:
    value = getattr(args, "effort", None)
    if getattr(args, "effort_source", None) == "cli-default":
        return f"default:{value or '?'}"
    return value or "-"


def jsonl_objects(text: str) -> Iterable[dict[str, Any]]:
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            yield value


def read_ledger(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or delegate_home() / "ledger.jsonl"
    try:
        return list(jsonl_objects(target.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return []
    except OSError as exc:
        print(f"warning: cannot read ledger {target}: {exc}", file=sys.stderr)
        return []


def parse_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    except ValueError:
        return None


def active_percent(window: Any, now_s: float | None = None) -> float:
    if not isinstance(window, dict):
        return 0.0
    reset = window.get("resets_at", window.get("resetsAt"))
    if isinstance(reset, (int, float)) and reset <= (now_s if now_s is not None else time.time()):
        return 0.0
    value = window.get("used_percent", window.get("utilization", 0))
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(number * 100, 10) if "utilization" in window else number


def normalize_quota(rate_limits: dict[str, Any] | None, now_s: float | None = None) -> dict[str, Any] | None:
    if not rate_limits:
        return None
    primary = rate_limits.get("primary") or rate_limits.get("five_hour") or {}
    secondary = rate_limits.get("secondary") or rate_limits.get("seven_day") or {}
    return {
        "five_hour_pct": active_percent(primary, now_s),
        "weekly_pct": active_percent(secondary, now_s),
        "five_hour_resets_at": primary.get("resets_at", primary.get("resetsAt")),
        "weekly_resets_at": secondary.get("resets_at", secondary.get("resetsAt")),
    }


def read_codex_quota(home: Path | None = None, now_s: float | None = None) -> dict[str, Any] | None:
    sessions = (home or codex_home()) / "sessions"
    try:
        files = sorted(sessions.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in files:
        latest = None
        try:
            for obj in jsonl_objects(path.read_text(encoding="utf-8", errors="replace")):
                payload = obj.get("payload", {})
                if obj.get("type") == "event_msg" and payload.get("type") == "token_count" and payload.get("rate_limits"):
                    latest = payload["rate_limits"]
        except OSError:
            continue
        if latest:
            return normalize_quota(latest, now_s)
    return None


def account_hash(org_id: str) -> str:
    """Short stable id for a Claude org; the org id and email are never stored."""
    return hashlib.sha256(org_id.encode("utf-8")).hexdigest()[:12]


def claude_identity() -> dict[str, str] | None:
    """Which Claude account the CLI is signed in to ({account, plan}), or None if unknown.

    The CLI and the orchestrating agent can use different Claude accounts, so quota readings and call counts
    only mean something for the account that produced them."""
    if os.environ.get("DELEGATE_NO_CLAUDE_IDENTITY") == "1":  # unit tests: no real CLI calls
        return None
    try:
        completed = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    out = completed.stdout
    try:
        data = json.loads(out[out.index("{"):])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get("loggedIn") or not data.get("orgId"):
        return None
    return {"account": account_hash(str(data["orgId"])), "plan": str(data.get("subscriptionType") or "unknown")}


def account_rows(rows: Iterable[dict[str, Any]], identity: dict[str, str] | None) -> list[dict[str, Any]]:
    """Ledger rows that belong to the current Claude account (all rows when it is unknown)."""
    if not identity:
        return list(rows)
    return [r for r in rows if r.get("account") == identity["account"]]


def claude_cache_raw(path: Path | None = None) -> dict[str, Any] | None:
    target = path or delegate_home() / "state" / "claude_rate.json"
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def claude_cache_account(path: Path | None = None) -> str | None:
    raw = claude_cache_raw(path)
    account = raw.get("account") if raw else None
    return account if isinstance(account, str) else None


def read_claude_quota(path: Path | None = None, now_s: float | None = None,
                      identity: dict[str, str] | None = None) -> dict[str, Any] | None:
    raw = claude_cache_raw(path)
    if raw is None:
        return None
    if identity and raw.get("account") != identity["account"]:
        return None  # another account's (or a pre-account-tracking) reading
    windows = raw.get("windows", raw)
    result = normalize_quota(windows, now_s)
    if result is not None:
        result["cached_at"] = raw.get("cached_at")
    return result


def write_claude_quota(windows: dict[str, Any], path: Path | None = None,
                       identity: dict[str, str] | None = None) -> None:
    target = path or delegate_home() / "state" / "claude_rate.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"cached_at": utc_now().isoformat().replace("+00:00", "Z"), "windows": windows}
    if identity:
        payload["account"], payload["plan"] = identity["account"], identity["plan"]
    target.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def parse_codex_output(text: str) -> dict[str, Any]:
    session_id = None
    result = ""
    usage: dict[str, Any] = {}
    failed = False
    models: set[str] = set()
    for obj in jsonl_objects(text):
        kind = obj.get("type")
        item = obj.get("item", {})
        if kind == "item.completed" and item.get("type") == "agent_message":
            result = str(item.get("text", ""))
        elif kind == "turn.completed":
            usage = obj.get("usage") or {}
        elif kind == "turn.failed" or kind == "error":
            failed = True
        elif kind == "item.completed" and item.get("type") == "error":
            if not str(item.get("message", "")).startswith("Model metadata for"):
                failed = True
        if kind == "thread.started" and obj.get("thread_id"):
            session_id = str(obj["thread_id"])
        if isinstance(obj.get("model"), str):
            models.add(obj["model"])
    return {
        "session_id": session_id,
        "result": result,
        "failed": failed,
        "models": models,
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cached_tokens": int(usage.get("cached_input_tokens", 0) or 0),
    }


def parse_claude_output(text: str) -> dict[str, Any]:
    result_event: dict[str, Any] = {}
    windows = None
    results: list[str] = []
    for obj in jsonl_objects(text):
        if obj.get("type") == "rate_limit_event":
            windows = (obj.get("rate_limit_info") or {}).get("unifiedWindows")
        elif obj.get("type") == "result":
            result_event = obj
            structured = obj.get("structured_output")
            result = json.dumps(structured, indent=2) if structured is not None else str(obj.get("result") or "")
            if results or result.strip():
                results.append(result)
    usage = result_event.get("usage") or {}
    return {
        "session_id": result_event.get("session_id"),
        "result": ((results[0] + "\n\n--- later turn(s) after background notifications ---\n\n" +
                    "\n\n".join(results[1:])) if len(results) > 1 else results[0] if results else ""),
        "failed": bool(result_event.get("is_error", False)) or not result_event,
        "models": set((result_event.get("modelUsage") or {}).keys()),
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cached_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
        "cost_usd_list": result_event.get("total_cost_usd"),
        "quota_windows": windows,
    }


def safe_git(fn: Any, *args: Any, default: Any = None) -> Any:
    """Scope/fingerprint bookkeeping must never abort a run: on a git error, degrade to `default`."""
    try:
        return fn(*args)
    except (OSError, gitwork.GitWorkError, subprocess.SubprocessError) as exc:
        print(f"[delegate] scope bookkeeping skipped: {exc}", file=sys.stderr)
        return default


def toml_line(key: str, value: Any) -> str:
    """key = value as TOML; ensure_ascii keeps DEL and every non-ASCII character escaped."""
    return f"{key} = {json.dumps(value, ensure_ascii=True)}"


def slim_claude_output(text: bytes) -> bytes:
    kept = []
    for line in text.splitlines(keepends=True):
        try:
            obj = json.loads(line)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and obj.get("type") == "system" and obj.get("subtype") == "ui_invalidate":
            continue
        kept.append(line)
    return b"".join(kept)


def parse_copilot_output(text: str) -> dict[str, Any]:
    result = ""
    models: set[str] = set()
    input_tokens = output_tokens = cached_tokens = premium = 0
    failed = saw_total_premium = False
    session_id = None
    for obj in jsonl_objects(text):
        kind, data = obj.get("type"), obj.get("data") or {}
        if kind == "assistant.message":
            result = str(data.get("content", result))
            if data.get("model"):
                models.add(str(data["model"]))
        elif kind == "model.call_final_result":
            if data.get("model"):
                models.add(str(data["model"]))
            failed |= data.get("result") not in (None, "success")
        elif kind == "session.usage_checkpoint":
            premium = int(data.get("totalPremiumRequests", premium) or 0)
            saw_total_premium |= "totalPremiumRequests" in data
            snapshot = data.get("accountingSnapshot") or {}
            details = snapshot.get("tokenDetails") or {}
            input_tokens = int((details.get("input") or {}).get("tokenCount", input_tokens) or 0)
            output_tokens = int((details.get("output") or {}).get("tokenCount", output_tokens) or 0)
            cached_tokens = int((details.get("cache_read") or {}).get("tokenCount", cached_tokens) or 0)
            for metric in data.get("providerModelMetrics") or []:
                if metric.get("modelId"):
                    models.add(str(metric["modelId"]))
            models.update((snapshot.get("modelMetrics") or {}).keys())
        elif kind == "result":
            failed |= int(obj.get("exitCode", 0) or 0) != 0
            session_id = obj.get("sessionId") or session_id
            if not saw_total_premium:
                premium = int((obj.get("usage") or {}).get("premiumRequests", premium) or 0)
        elif kind == "error":
            failed = True
    return {"result": result, "failed": failed, "models": models, "session_id": session_id,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cached_tokens": cached_tokens, "premium_requests": premium}


def parse_qwen_output(stdout: str, stderr: str = "") -> dict[str, Any]:
    match = re.search(r"\|\s*(\d+)\s+in\s*/\s*(\d+)\s+out\s+tok", stderr)
    return {"result": stdout.strip(), "failed": False, "models": set(),
            "input_tokens": int(match.group(1)) if match else 0,
            "output_tokens": int(match.group(2)) if match else 0, "cached_tokens": 0}


def model_family(model: str) -> str:
    return "opus" if model.startswith("claude-opus") else "sonnet"


def recent_rows(rows: Iterable[dict[str, Any]], since: dt.datetime) -> list[dict[str, Any]]:
    return [row for row in rows if (stamp := parse_timestamp(row.get("ts"))) is not None and stamp >= since]


def cap_reason(agent: str, model: str, caps: dict[str, Any], rows: list[dict[str, Any]],
               now: dt.datetime | None = None, warn: bool = True) -> str | None:
    now = now or utc_now()
    if agent == "codex":
        quota = read_codex_quota(now_s=now.timestamp())
        if quota is None:
            if warn:
                print("warning: no Codex quota data found; allowing run", file=sys.stderr)
            return None
        if quota["five_hour_pct"] >= caps["codex"]["five_hour_max_pct"]:
            return f"codex 5-hour quota is {quota['five_hour_pct']:g}%"
        if quota["weekly_pct"] >= caps["codex"]["weekly_max_pct"]:
            return f"codex weekly quota is {quota['weekly_pct']:g}%"
    elif agent == "claude":
        family = model_family(model)
        limits = caps["claude"][family]
        identity = claude_identity()
        quota = read_claude_quota(now_s=now.timestamp(), identity=identity)
        if quota is None:
            if warn:
                if identity and claude_cache_raw() is not None and claude_cache_account() != identity["account"]:
                    print("warning: Claude account changed (or the quota cache predates account tracking): "
                          "cache ignored, allowing; run `delegate.py probe --model claude-sonnet-5-5` to refresh",
                          file=sys.stderr)
                else:
                    print("warning: no Claude quota cache found; allowing by rate quota", file=sys.stderr)
        else:
            if quota["five_hour_pct"] >= limits["five_hour_max_pct"]:
                return f"claude {family} 5-hour quota is {quota['five_hour_pct']:g}%"
            if quota["weekly_pct"] >= limits["weekly_max_pct"]:
                return f"claude {family} weekly quota is {quota['weekly_pct']:g}%"
        family_rows = [r for r in account_rows(rows, identity)
                       if r.get("agent") == "claude" and model_family(str(r.get("model", ""))) == family]
        daily = len(recent_rows(family_rows, now - dt.timedelta(days=1)))
        if daily >= limits["daily_calls"]:
            return f"claude {family} daily call cap reached ({daily}/{limits['daily_calls']})"
        if family == "opus":
            weekly = len(recent_rows(family_rows, now - dt.timedelta(days=7)))
            if weekly >= limits["weekly_calls"]:
                return f"claude opus weekly call cap reached ({weekly}/{limits['weekly_calls']})"
    elif agent == "copilot" and model not in caps["copilot"]["free_models"]:
        used = sum(int(r.get("premium_requests", 0) or 0) for r in recent_rows(rows, now - dt.timedelta(days=7))
                   if r.get("agent") == "copilot")
        if used >= caps["copilot"]["premium_weekly_max"]:
            return f"copilot premium weekly cap reached ({used}/{caps['copilot']['premium_weekly_max']})"
    return None


def route_target(name: str, caps: dict[str, Any]) -> tuple[str, str]:
    if name == "qwen":
        return "qwen", caps["qwen"]["allowed_models"][0]
    if name == "copilot-free":
        return "copilot", caps["copilot"]["free_models"][0]
    if name == "copilot-premium":
        free = set(caps["copilot"]["free_models"])
        return "copilot", next(m for m in caps["copilot"]["allowed_models"] if m not in free)
    if name == "codex":
        return "codex", caps["codex"]["default_model"]
    family = name.split("-", 1)[1]
    return "claude", next(m for m in caps["claude"]["allowed_models"] if family in m)


def suggest_next(agent: str, model: str, caps: dict[str, Any], rows: list[dict[str, Any]],
                 mode: str, risk: str, now: dt.datetime | None = None,
                 opus_reason: str | None = None, workdir: str | None = None) -> str:
    order = caps["routing"]["order"]
    current = agent
    if agent == "claude":
        current = f"claude-{model_family(model)}"
    elif agent == "copilot":
        current = "copilot-free" if model in caps["copilot"]["free_models"] else "copilot-premium"
    try:
        start = order.index(current) + 1
    except ValueError:
        start = 0
    if mode == "implement" and workdir and git_branch(Path(workdir)) in {None, "main", "master", "production"}:
        return "none"
    for name in order[start:] + order[:start]:
        candidate, candidate_model = route_target(name, caps)
        if candidate == agent and candidate_model == model:
            continue
        if candidate == "qwen" and (mode == "implement" or risk == "high"):
            continue
        if candidate == "qwen":
            lock = acquire_qwen_lock()
            if lock is None:
                continue
            lock.close()
        if candidate_model.startswith("claude-opus") and not opus_reason and caps["claude"].get("opus_requires_reason", True):
            continue
        if cap_reason(candidate, candidate_model, caps, rows, now, warn=False) is None:
            return name
    return "none"


def git_branch(workdir: Path) -> str | None:
    try:
        completed = subprocess.run(["git", "-C", str(workdir), "rev-parse", "--abbrev-ref", "HEAD"],
                                   capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def policy_reason(args: argparse.Namespace, caps: dict[str, Any], rows: list[dict[str, Any]],
                  now: dt.datetime | None = None) -> tuple[str | None, int]:
    if args.model not in caps[args.agent]["allowed_models"]:
        return f"model {args.model!r} is not allowed for {args.agent}", BAD_USAGE
    if args.agent == "qwen" and args.risk == "high":
        return "local model is barred from high-risk work: payments/auth/RLS/security/migrations", BLOCKED
    if args.agent == "qwen" and args.mode == "implement":
        return "qwen is read/draft only and cannot run in implement mode", BLOCKED
    if (args.agent == "claude" and args.model.startswith("claude-opus") and not args.opus_reason
            and caps["claude"].get("opus_requires_reason", True) and not getattr(args, "probe", False)):
        return "Claude Opus requires --opus-reason", BLOCKED
    reason = cap_reason(args.agent, args.model, caps, rows, now)
    if reason:
        return reason, BLOCKED
    if args.mode == "implement" and not getattr(args, "worktree_branch", None):  # a worktree guarantees an agent/ branch
        branch = git_branch(Path(args.workdir))
        if branch is None:
            return "implement mode requires --workdir inside a git repository", BLOCKED
        if branch in {"main", "master", "production"}:
            return f"implement mode is barred on protected branch {branch}", BLOCKED
    return None, OK


def quote_toml_key(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return name
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


CODEX_PLUGIN_ALLOW = ("dart-flutter@",)  # local Dart/Flutter tooling only; every other plugin is disabled
CODEX_FEATURES_OFF = ("apps", "computer_use", "in_app_browser")  # ChatGPT connected apps (GitHub/Gmail/deploys), computer use


def codex_config(config_path: Path | None = None) -> dict:
    path = config_path or codex_home() / "config.toml"
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        # Fail closed: an unreadable config could hide a production MCP server we must disable.
        raise SystemExit(f"[delegate] cannot read {path} ({exc}); refusing to launch codex")


def mcp_server_names(config_path: Path | None = None) -> list[str]:
    servers = codex_config(config_path).get("mcp_servers", {})
    return sorted(servers) if isinstance(servers, dict) else []


def codex_plugin_names(config_path: Path | None = None) -> list[str]:
    plugins = codex_config(config_path).get("plugins", {})
    if not isinstance(plugins, dict):
        return []
    return sorted(p for p in plugins if not p.startswith(CODEX_PLUGIN_ALLOW))


CODEX_MCP_ALLOW = ("dart-mcp-server", "dart_mcp_server")
CODEX_KNOWN_PLUGIN_MCP = ("codex_app", "cua_repl")  # shadowed even if `codex mcp list` fails


def codex_plugin_mcp_names(config_names: list[str]) -> list[str]:
    """MCP servers injected by plugins (not in config.toml). Disabling the plugin does not stop
    `codex exec` loading them (verified 2026-10-13), so each is shadowed with a disabled stub."""
    names = set(CODEX_KNOWN_PLUGIN_MCP)
    try:
        if os.environ.get("DELEGATE_NO_CODEX_PROBE") == "1":  # unit tests: no real CLI calls
            raise OSError("probe disabled")
        out = subprocess.run(["codex", "mcp", "list", "--json"], capture_output=True, text=True, timeout=20)
        names.update(s["name"] for s in json.loads(out.stdout) if isinstance(s, dict) and s.get("name"))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
        pass
    return sorted(n for n in names if n not in config_names and n not in CODEX_MCP_ALLOW)


def build_argv(agent: str, model: str, mode: str, workdir: Path, run_base: Path,
               brief: str, effort: str | None = None, resume: str | None = None,
               budget_usd: float | None = None, schema: dict[str, Any] | None = None,
               persist: bool = True, setting_sources: str = "") -> list[str]:
    if agent == "codex":
        last = str(run_base.with_name(run_base.stem + "-last.txt"))
        sandbox = "workspace-write" if mode == "implement" else "read-only"
        if resume:  # `exec resume` takes no --sandbox/-C: the policy goes in as config, the cwd is the process cwd
            argv = ["codex", "exec", "resume", resume, "-m", model, "--json", "--skip-git-repo-check", "-o", last,
                    "-c", f'sandbox_mode="{sandbox}"']
        else:
            argv = ["codex", "exec", "-m", model, "--json", "--skip-git-repo-check", "-C", str(workdir),
                    "-o", last, "--sandbox", sandbox]
        if effort:
            argv += ["-c", f"model_reasoning_effort={effort}"]
        if schema is not None:
            schema_file = run_base.with_name(run_base.stem + "-schema.json")
            schema_file.parent.mkdir(parents=True, exist_ok=True)
            schema_file.write_text(json.dumps(schema), encoding="utf-8")
            argv += ["--output-schema", str(schema_file)]
        config_names = mcp_server_names()
        for name in config_names:
            argv += ["-c", f"mcp_servers.{quote_toml_key(name)}.enabled=false"]
        for name in codex_plugin_mcp_names(config_names):
            argv += ["-c", f'mcp_servers.{quote_toml_key(name)}={{command="/usr/bin/false",enabled=false}}']
        for feature in CODEX_FEATURES_OFF:
            argv += ["-c", f"features.{feature}=false"]
        for plugin in codex_plugin_names():
            argv += ["-c", f'plugins."{plugin}".enabled=false']
        return argv + ["-"]
    if agent == "claude":
        argv = ["claude", "-p", "--model", model, "--output-format", "stream-json", "--verbose",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        if setting_sources:
            argv += ["--setting-sources", setting_sources]
        if not persist:
            argv.append("--no-session-persistence")
        if resume:
            argv += ["--resume", resume]
        if budget_usd is not None:
            argv += ["--max-budget-usd", f"{budget_usd:g}"]
        if schema is not None:
            argv += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
        if mode == "review":
            argv += ["--tools", "Read,Glob,Grep,Bash", "--allowedTools",
                     "Read,Glob,Grep,Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git status:*)"]
        else:
            argv += ["--permission-mode", "acceptEdits", "--allowedTools", "Read,Glob,Grep,Edit,Write,Bash",
                     "--disallowedTools", "Bash(git push:*),Bash(git reset:*),Bash(git checkout main:*),Bash(rm -rf:*),Bash(supabase:*),Bash(gh pr merge:*),Bash(curl:*)"]
        if effort:
            argv += ["--effort", effort]
        return argv
    if agent == "copilot":
        argv = ["copilot", "-p", brief, "--model", model, "--output-format", "json"]
        if resume:
            argv.append(f"--resume={resume}")
        if mode == "review":
            for tool in ("shell(git diff:*)", "shell(git log:*)", "shell(git show:*)", "shell(git status:*)"):
                argv += ["--allow-tool", tool]
            argv += ["--deny-tool", "write"]
        else:
            argv += ["--allow-all-tools"]
            for tool in ("shell(git push)", "shell(git reset)", "shell(rm)", "shell(supabase)",
                         "shell(gh pr merge)", "shell(curl)", "write(.env)"):
                argv += ["--deny-tool", tool]
        return argv
    return ["python3", str(qwen_script()), "--model", model, "Follow the brief on stdin."]


def acquire_qwen_lock() -> Any:
    path = delegate_home() / "state" / "qwen.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    return handle


def append_ledger(row: dict[str, Any], path: Path | None = None) -> None:
    target = path or delegate_home() / "ledger.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def kanban_call(board: str, *args: str) -> subprocess.CompletedProcess | None:
    try:
        completed = subprocess.run(["hermes", "kanban", "--board", board, *args],
                                   capture_output=True, text=True, timeout=20)
        if completed.returncode != 0:
            raise ValueError(completed.stderr.strip() or completed.stdout.strip()
                             or f"exit {completed.returncode}")
        return completed
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        detail = " ".join(str(exc).split())
        print(f"[delegate] kanban: {args[0]}: {detail}", file=sys.stderr)
        return None


def create_run_card(args: argparse.Namespace) -> str | None:
    if args.dry_run or args.no_card or not kanban_enabled():
        return None
    names = {"codex": "Codex", "claude": "Claude", "copilot": "Copilot", "qwen": "Qwen"}
    providers = {"codex": "openai-codex", "claude": "anthropic", "copilot": "copilot"}
    title = f"[{names[args.agent]} · {args.model} · {effort_label(args)}] {args.task}: {args.reason}"[:120]
    body = (f"Task: {args.task}\nReason: {args.reason}\nMode: {args.mode}\nRisk: {args.risk}\n"
            f"Workdir: {Path(args.workdir).resolve()}\nBranch: {git_branch(Path(args.workdir))}")
    source = getattr(args, "effort_source", "n/a")
    source = f"policy, risk {args.risk}" if source == "policy" else "cli default" if source == "cli-default" else source
    body += f"\nEffort: {effort_label(args)} ({source})"
    if args.opus_reason:
        body += f"\nOpus reason: {args.opus_reason}"
    if args.card:
        body += f"\nPart of {args.card}"
    fields = ["create", title, "--body", body, "--model", args.model, "--created-by", created_by(), "--json"]
    if args.agent in providers:
        fields += ["--provider", providers[args.agent]]
    completed = kanban_call(args.board, *fields)
    if completed is None:
        return None
    try:
        card_id = json.loads(completed.stdout)["id"]
        if not isinstance(card_id, str) or not card_id:
            raise ValueError("missing card id")
        return card_id
    except (ValueError, KeyError, TypeError) as exc:
        print(f"[delegate] kanban: create: invalid JSON response ({exc})", file=sys.stderr)
        return None


def blocked_run_card(args: argparse.Namespace, reason: str, suggestion: str) -> None:
    card_id = create_run_card(args)
    if card_id:
        kanban_call(args.board, "block", card_id, f"{reason}; suggested next agent: {suggestion}")


def run_command(args: argparse.Namespace) -> int:
    resolved = resolve_run_args(args)
    if resolved is not None:
        return resolved
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        print("delegate: --timeout must be positive and finite", file=sys.stderr)
        return BAD_USAGE
    try:
        caps = load_caps()
    except ValueError as exc:
        print(f"delegate: {exc}", file=sys.stderr)
        return BAD_USAGE
    rows = read_ledger()
    reason, code = policy_reason(args, caps, rows)
    if reason:
        if code == BLOCKED:
            suggestion = suggest_next(args.agent, args.model, caps, rows, args.mode, args.risk,
                                      opus_reason=args.opus_reason, workdir=args.workdir)
            print(f"blocked: {reason}; suggested next agent: {suggestion}", file=sys.stderr)
            blocked_run_card(args, reason, suggestion)
        else:
            print(f"delegate: {reason}", file=sys.stderr)
        return code
    prepared = prepare_workdir(args)
    if prepared is not None:
        return prepared
    lock = None
    if args.agent == "qwen":
        lock = acquire_qwen_lock()
        if lock is None:
            suggestion = suggest_next(args.agent, args.model, caps, rows, args.mode, args.risk,
                                      opus_reason=args.opus_reason, workdir=args.workdir)
            print(f"blocked: qwen busy; suggested next agent: {suggestion}", file=sys.stderr)
            blocked_run_card(args, "qwen busy", suggestion)
            return BLOCKED
    try:
        brief = Path(args.brief_file).read_text(encoding="utf-8")
    except OSError as exc:
        if lock:
            lock.close()
        print(f"delegate: cannot read brief file: {exc}", file=sys.stderr)
        return BAD_USAGE
    home = delegate_home()
    stamp = utc_now().strftime("%Y%m%dT%H%M%S.%fZ")
    run_file = home / "runs" / f"{stamp}-{args.agent}.jsonl"
    budget = getattr(args, "budget_usd", None)
    if budget is None and args.agent == "claude":
        budget_key = "high_effort_budget_usd" if args.effort in ("high", "xhigh", "max") else "default_budget_usd"
        budget = caps["claude"].get(budget_key, caps["claude"].get("default_budget_usd"))
    setting_sources = caps.get("claude", {}).get("setting_sources", "") if args.agent == "claude" else ""
    passed_effort = None if args.effort_source == "cli-default" else args.effort
    argv = build_argv(args.agent, args.model, args.mode, Path(args.workdir), run_file, brief, passed_effort,
                      resume=getattr(args, "resume_session", None), budget_usd=budget,
                      schema=jobspec.SCHEMAS.get(str(getattr(args, "schema", None))),
                      persist=not getattr(args, "probe", False), setting_sources=setting_sources)
    if args.dry_run:
        print(shlex.join(argv))
        if lock:
            lock.close()
        return OK
    run_file.parent.mkdir(parents=True, exist_ok=True)
    want_report = bool(getattr(args, "verify", None) or getattr(args, "files", None))
    head_before = gitwork.head(args.workdir) if want_report else None
    dirty_before = safe_git(gitwork.dirty_snapshot, args.workdir, default={}) if want_report else {}
    identity = claude_identity() if args.agent == "claude" else None
    card_id = create_run_card(args)
    args.created_card = card_id
    on_card = getattr(args, "on_card", None)
    if on_card and card_id:
        on_card(card_id)
    if card_id:
        kanban_call(args.board, "claim", card_id, "--ttl", str(math.ceil(args.timeout + 600)))
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(argv, cwd=args.workdir, input=None if args.agent == "copilot" else brief.encode("utf-8"),
                                   capture_output=True, timeout=args.timeout,
                                   env={**os.environ, "DELEGATE_CHILD": "1"})
        stdout, stderr, process_code = completed.stdout, completed.stderr, completed.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout, stderr = exc.stdout or b"", exc.stderr or b""
        process_code = AGENT_FAILED
    except OSError as exc:
        stdout, stderr, process_code = "", str(exc), AGENT_FAILED
    finally:
        if lock:
            lock.close()
    duration = time.monotonic() - started
    stdout_bytes = stdout.encode("utf-8") if isinstance(stdout, str) else stdout
    stderr_bytes = stderr.encode("utf-8") if isinstance(stderr, str) else stderr
    raw = stdout_bytes + ((b"\n" if stdout_bytes and stderr_bytes else b"") + stderr_bytes if stderr_bytes else b"")
    run_file.write_bytes(slim_claude_output(raw) if args.agent == "claude" else raw)
    stdout, stderr = stdout_bytes.decode("utf-8", errors="replace"), stderr_bytes.decode("utf-8", errors="replace")
    if args.agent == "codex":
        parsed = parse_codex_output(stdout)
    elif args.agent == "claude":
        parsed = parse_claude_output(stdout)
        if parsed.get("quota_windows"):
            write_claude_quota(parsed["quota_windows"], identity=identity)
    elif args.agent == "copilot":
        parsed = parse_copilot_output(stdout)
    else:
        parsed = parse_qwen_output(stdout, stderr)
    args.session_id = parsed.get("session_id")
    findings = None
    if getattr(args, "schema", None) == "findings":
        findings = extract_findings(str(parsed.get("result", "")))
        if findings is not None:
            findings_file = run_file.with_name(run_file.stem + ".findings.json")
            findings_file.write_text(json.dumps(findings, indent=2, sort_keys=True), encoding="utf-8")
            args.findings, args.findings_file = findings, str(findings_file)
    if timed_out:
        status, exit_code = "timeout", AGENT_FAILED
    elif process_code != 0 or parsed.get("failed"):
        status, exit_code = "failed", AGENT_FAILED
    else:
        used = parsed.get("models", set())
        mismatch = False
        if args.agent == "claude":
            mismatch = args.model not in used or any(m != args.model and "haiku" not in m for m in used)
        elif args.agent == "copilot":
            # Copilot runs internal helper models (e.g. copilot-search-a for code search); they are not substitutes.
            mismatch = args.model not in used or any(m != args.model and not m.startswith("copilot-") for m in used)
        if mismatch:
            status, exit_code = "model_mismatch", MODEL_MISMATCH
        else:
            status, exit_code = "ok", OK
    report, verify_label, out_of_scope = None, None, None
    if want_report:
        changed, pre_existing = safe_git(gitwork.agent_changes, args.workdir, head_before, dirty_before,
                                         default=(gitwork.changed_files(args.workdir, since=head_before), []))
        out_of_scope = gitwork.out_of_scope(changed, args.files) if getattr(args, "files", None) else None
        verify_result = None
        if status == "ok" and getattr(args, "verify", None):
            verify_result = run_verify(args.verify, Path(args.workdir), float(getattr(args, "verify_timeout", None) or 900))
        verify_label = ("skipped" if status != "ok" else "-" if verify_result is None
                        else "pass" if verify_result["passed"] else "fail")
        report = {"changed": changed, "out_of_scope": out_of_scope, "verify": verify_result, "head_before": head_before,
                  "pre_existing": pre_existing,
                  "fingerprint": safe_git(gitwork.fingerprint, args.workdir) if verify_result else None}
        report_file = run_file.with_name(run_file.stem + ".report.json")
        report_file.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        args.report, args.report_file = report, str(report_file)
        if exit_code == OK:
            if verify_label == "fail":
                exit_code = VERIFY_FAILED
            elif out_of_scope:
                exit_code = SCOPE_VIOLATION
    quota = None
    if args.agent == "codex":
        quota = read_codex_quota()
    elif args.agent == "claude":
        quota = read_claude_quota(identity=identity)
    row = {
        "ts": utc_now().isoformat().replace("+00:00", "Z"), "agent": args.agent, "model": args.model,
        "task": args.task, "reason": args.reason, "opus_reason": args.opus_reason, "mode": args.mode,
        "risk": args.risk, "workdir": str(Path(args.workdir).resolve()), "branch": git_branch(Path(args.workdir)),
        "status": status, "exit_code": exit_code, "duration_s": round(duration, 3),
        "input_tokens": parsed.get("input_tokens", 0), "output_tokens": parsed.get("output_tokens", 0),
        "cached_tokens": parsed.get("cached_tokens", 0),
        "cost_usd_list": parsed.get("cost_usd_list") if args.agent == "claude" else None,
        "premium_requests": parsed.get("premium_requests") if args.agent == "copilot" else None,
        "quota_after": ({"five_hour_pct": quota["five_hour_pct"], "weekly_pct": quota["weekly_pct"]}
                        if quota else None), "run_file": str(run_file), "card_id": card_id, "parent_card": args.card,
        "effort": effort_label(args), "effort_source": args.effort_source,
    }
    if setting_sources:
        row["setting_sources"] = setting_sources
    if identity:
        row["account"] = identity["account"]
    if args.session_id:
        row["session_id"] = args.session_id
    if findings is not None:
        row["findings"] = findings["counts"]
    if report is not None:
        row["verify"], row["out_of_scope"] = verify_label, out_of_scope
    append_ledger(row)
    if parsed.get("result"):
        print(parsed["result"])
    elif stdout and status != "ok":
        print(stdout, end="" if stdout.endswith("\n") else "\n")
    if status != "ok" and stderr:
        print(stderr, file=sys.stderr, end="" if stderr.endswith("\n") else "\n")
    if timed_out:
        print(f"[delegate] agent timed out after {args.timeout:g}s", file=sys.stderr)
    q5 = f"{quota['five_hour_pct']:g}" if quota else "?"
    qw = f"{quota['weekly_pct']:g}" if quota else "?"
    tokens = f"{row['input_tokens']}in/{row['output_tokens']}out/{row['cached_tokens']}cached"
    cost = f"{row['cost_usd_list']:.6f}" if isinstance(row["cost_usd_list"], (int, float)) else "-"
    premium = row["premium_requests"] if row["premium_requests"] is not None else "-"
    summary = (f"[delegate] agent={args.agent} model={args.model} effort={effort_label(args)} status={status} 5h={q5}% wk={qw}% "
               f"tokens={tokens} cost=${cost} premium={premium}")
    if report is not None:
        scope = "-" if out_of_scope is None else "ok" if not out_of_scope else f"{len(out_of_scope)}-out"
        summary += f" verify={verify_label} scope={scope}"
    if getattr(args, "schema", None) == "findings":
        counts = findings["counts"] if findings else None
        summary += (f" findings=H{counts['high']}/M{counts['medium']}/L{counts['low']}" if counts else " findings=unparsed")
    print(summary, file=sys.stderr)
    if card_id:
        kanban_call(args.board, "comment", card_id, summary)
        if status == "ok":
            kanban_call(args.board, "complete", card_id, "--force", "--summary", summary)
        else:
            kanban_call(args.board, "block", card_id, f"status={status} exit={exit_code}")
    return exit_code


def format_reset(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    return dt.datetime.fromtimestamp(value, UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def qwen_state() -> dict[str, Any]:
    lock = acquire_qwen_lock()
    lock_state = "busy" if lock is None else "available"
    if lock:
        lock.close()
    try:
        with urllib.request.urlopen(qwen_host() + "/api/tags", timeout=3) as response:
            reachable = response.status == 200
            detail = "reachable" if reachable else f"HTTP {response.status}"
    except Exception as exc:
        reachable, detail = False, f"unreachable ({exc})"
    return {"allowed": lock_state == "available" and reachable, "lock_state": lock_state,
            "endpoint_reachable": reachable, "reason": detail if lock_state == "available" else f"busy; {detail}"}


def status_command(as_json: bool) -> int:
    try:
        caps = load_caps()
    except ValueError as exc:
        print(f"delegate: {exc}", file=sys.stderr)
        return BAD_USAGE
    now, rows = utc_now(), read_ledger()
    identity = claude_identity()
    cq = read_codex_quota(now_s=now.timestamp())
    aq = read_claude_quota(now_s=now.timestamp(), identity=identity)
    claude_rows_now = account_rows(rows, identity)
    sonnet = [r for r in claude_rows_now if r.get("agent") == "claude" and model_family(str(r.get("model", ""))) == "sonnet"]
    opus = [r for r in claude_rows_now if r.get("agent") == "claude" and model_family(str(r.get("model", ""))) == "opus"]
    premium = sum(int(r.get("premium_requests", 0) or 0) for r in recent_rows(rows, now-dt.timedelta(days=7)) if r.get("agent") == "copilot")
    qwen = qwen_state()
    codex_reason = cap_reason("codex", caps["codex"]["default_model"], caps, rows, now, False)
    claude_reason = cap_reason("claude", caps["claude"]["allowed_models"][0], caps, rows, now, False)
    data = {
        "qwen": qwen,
        "codex": {"allowed": codex_reason is None, "reason": codex_reason or ("quota unknown" if cq is None else "within caps"), "quota": cq},
        "claude": {"allowed": claude_reason is None, "reason": claude_reason or ("quota unknown" if aq is None else "within caps"),
                   "quota": aq,
                   "account": identity["account"] if identity else None,
                   "plan": identity["plan"] if identity else None,
                   "quota_account": claude_cache_account(),
                   "cache_age_s": ((now - parse_timestamp(aq.get("cached_at"))).total_seconds()
                                   if aq and parse_timestamp(aq.get("cached_at")) else None),
                   "sonnet_calls_today": len(recent_rows(sonnet, now-dt.timedelta(days=1))),
                   "sonnet_daily_cap": caps["claude"]["sonnet"]["daily_calls"],
                   "opus_calls_today": len(recent_rows(opus, now-dt.timedelta(days=1))),
                   "opus_daily_cap": caps["claude"]["opus"]["daily_calls"],
                   "opus_calls_week": len(recent_rows(opus, now-dt.timedelta(days=7))),
                   "opus_weekly_cap": caps["claude"]["opus"]["weekly_calls"]},
        "copilot": {"allowed": True, "reason": "free models available",
                    "premium_models_allowed": premium < caps["copilot"]["premium_weekly_max"], "free_models_allowed": True,
                    "premium_used_week": premium, "premium_weekly_cap": caps["copilot"]["premium_weekly_max"]},
    }
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return OK
    print(f"qwen    allowed={'yes' if qwen['allowed'] else 'no':3} lock={qwen['lock_state']} endpoint={qwen['reason']}")
    if cq:
        print(f"codex   allowed={'yes' if data['codex']['allowed'] else 'no':3} 5h={cq['five_hour_pct']:g}% reset={format_reset(cq['five_hour_resets_at'])}; weekly={cq['weekly_pct']:g}% reset={format_reset(cq['weekly_resets_at'])}")
    else:
        print("codex   allowed=yes quota unknown")
    if aq:
        age = data["claude"]["cache_age_s"]
        print(f"claude  allowed={'yes' if data['claude']['allowed'] else 'no':3} 5h={aq['five_hour_pct']:g}% reset={format_reset(aq['five_hour_resets_at'])}; weekly={aq['weekly_pct']:g}% reset={format_reset(aq['weekly_resets_at'])}; cache_age={age:.0f}s" if age is not None else "claude  quota cache has no timestamp")
    elif identity and claude_cache_raw() is not None and claude_cache_account() != identity["account"]:
        print(f"claude  allowed=yes quota unknown: account changed since the last reading "
              f"(cache is for {data['claude']['quota_account'] or 'no recorded account'}); "
              f"run `delegate.py probe --model claude-sonnet-5-5` to refresh")
    else:
        print("claude  allowed=yes quota unknown; no cache")
    if identity:
        print(f"         account={identity['account']} plan={identity['plan']}")
    print(f"         sonnet today {data['claude']['sonnet_calls_today']}/{data['claude']['sonnet_daily_cap']}; opus today {data['claude']['opus_calls_today']}/{data['claude']['opus_daily_cap']}, week {data['claude']['opus_calls_week']}/{data['claude']['opus_weekly_cap']}")
    print(f"copilot allowed=yes free models uncapped; premium_allowed={'yes' if data['copilot']['premium_models_allowed'] else 'no'} week {premium}/{data['copilot']['premium_weekly_cap']}")
    return OK


def ledger_command(days: int, as_json: bool) -> int:
    cutoff = utc_now() - dt.timedelta(days=days)
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in recent_rows(read_ledger(), cutoff):
        key = (str(row.get("agent", "")), str(row.get("model", "")), str(row.get("effort") or "-"))
        group = groups.setdefault(key, {"agent": key[0], "model": key[1], "effort": key[2], "calls": 0, "ok": 0,
                                        "failed": 0, "input_tokens": 0, "output_tokens": 0,
                                        "cost_usd_list": 0.0, "premium_requests": 0})
        group["calls"] += 1
        group["ok" if row.get("status") == "ok" else "failed"] += 1
        group["input_tokens"] += int(row.get("input_tokens", 0) or 0)
        group["output_tokens"] += int(row.get("output_tokens", 0) or 0)
        group["cost_usd_list"] += float(row.get("cost_usd_list", 0) or 0)
        group["premium_requests"] += int(row.get("premium_requests", 0) or 0)
    result = [groups[key] for key in sorted(groups)]
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return OK
    print(f"{'AGENT':<9} {'MODEL':<24} {'EFFORT':<16} {'CALLS':>5} {'OK':>4} {'FAIL':>5} {'INPUT':>10} {'OUTPUT':>10} {'COST':>10} {'PREMIUM':>7}")
    for item in result:
        print(f"{item['agent']:<9} {item['model']:<24} {item['effort']:<16} {item['calls']:>5} {item['ok']:>4} {item['failed']:>5} {item['input_tokens']:>10} {item['output_tokens']:>10} {item['cost_usd_list']:>10.4f} {item['premium_requests']:>7}")
    return OK


def probe_command(model: str, timeout: float) -> int:
    """One-word call to a Claude model: proves it answers, refreshes the quota cache for the
    current account and logs to the ledger. Obeys the same caps as `run`; needs no opus reason."""
    probe_dir = delegate_home() / "probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    brief = probe_dir / "brief.txt"
    brief.write_text("Reply with the single word OK.\n", encoding="utf-8")
    args = argparse.Namespace(agent="claude", model=model, task="probe", reason="model/account probe",
                              brief_file=str(brief), workdir=str(probe_dir), mode="review", risk="low",
                              opus_reason=None, effort=None, timeout=timeout, dry_run=False, card=None,
                              board="agents", no_card=True, probe=True)
    code = run_command(args)
    identity = claude_identity()
    if identity:
        print(f"[delegate] probe account={identity['account']} plan={identity['plan']}", file=sys.stderr)
    return code


def run_verify(cmd: str, workdir: Path, timeout: float) -> dict[str, Any]:
    """Run the spec's verify command ourselves, after the agent finished; its claims are never the record."""
    started = time.monotonic()
    proc = subprocess.Popen(["bash", "-c", cmd], cwd=str(workdir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        output, _ = proc.communicate()
    lines = (output or "").splitlines()[-25:]
    tail = "\n".join(lines)[-4000:]
    code = None if timed_out else proc.returncode
    return {"cmd": cmd, "passed": code == 0 and not timed_out, "exit": code, "timed_out": timed_out, "tail": tail,
            "last_line": next((line.strip() for line in reversed((output or "").splitlines()) if line.strip()), ""),
            "duration_s": round(time.monotonic() - started, 2)}


def default_board() -> str:
    return os.environ.get("DELEGATE_BOARD", "agents")


def standing_rules(caps: dict[str, Any]) -> tuple[str, ...]:
    section = caps.get("brief")
    rules = section.get("standing") if isinstance(section, dict) else None
    return tuple(str(r) for r in rules) if isinstance(rules, list) and rules else jobspec.DEFAULT_STANDING


def render_spec_brief(data: dict[str, Any], caps: dict[str, Any]) -> str:
    section = caps.get("library") if isinstance(caps.get("library"), dict) else {}
    defaults = (section.get("defaults") or {}).get(data.get("mode", "review"), [])
    names = [*defaults, *data.get("skills", [])] if data.get("skills") != [] else []
    playbooks = skill_library.render(names, max_chars=int(section.get("max_chars", skill_library.DEFAULT_MAX_CHARS)))
    return jobspec.render_brief(data, standing_rules(caps), playbooks)


def resolve_run_args(args: argparse.Namespace) -> int | None:
    """Fill run/start arguments from --spec (flags win), apply defaults, require the rest. None = ok."""
    for name in ("agent", "model", "task", "reason", "brief_file", "workdir", "mode", "risk", "opus_reason", "effort",
                 "timeout", "card", "spec", "verify", "files", "verify_timeout", "worktree_branch", "worktree_base",
                 "budget_usd", "schema", "resume_session"):
        if not hasattr(args, name):
            setattr(args, name, None)
    if not hasattr(args, "no_card"):
        args.no_card = False
    if not hasattr(args, "board") or args.board is None:
        args.board = default_board()
    spec_path = args.spec
    if spec_path:
        try:
            data = jobspec.load_spec(spec_path)
            caps = load_caps()
        except (jobspec.SpecError, ValueError) as exc:
            print(f"delegate: {exc}", file=sys.stderr)
            return BAD_USAGE
        for name in ("agent", "model", "task", "reason", "workdir", "mode", "risk", "opus_reason", "effort",
                     "timeout", "card", "budget_usd", "schema"):
            if getattr(args, name) is not None:
                data[name] = getattr(args, name)
        errors, warnings = jobspec.lint(data, AGENTS)
        for warning in warnings:
            print(f"warning: spec: {warning}", file=sys.stderr)
        if errors:
            for error in errors:
                print(f"delegate: spec: {error}", file=sys.stderr)
            return BAD_USAGE
        for name in ("agent", "model", "task", "reason", "mode", "risk", "opus_reason", "effort", "timeout", "card"):
            if getattr(args, name) is None and data.get(name) is not None:
                setattr(args, name, data[name])
        if args.workdir is None:
            args.workdir = str(Path(str(data["workdir"])).expanduser())
        if data.get("no_card") and not args.no_card:
            args.no_card = True
        if data.get("board") and args.board == default_board():
            args.board = str(data["board"])
        if args.budget_usd is None and data.get("budget_usd") is not None:
            args.budget_usd = float(data["budget_usd"])
        if args.schema is None and data.get("schema"):
            args.schema = data["schema"]
        args.verify = data.get("verify") or None
        args.verify_timeout = data.get("verify_timeout")
        args.files = list(data.get("files") or []) or None
        worktree = data.get("worktree") or {}
        if args.worktree_branch is None and worktree.get("branch"):
            args.worktree_branch, args.worktree_base = worktree["branch"], worktree.get("base")
        if args.brief_file is None:
            briefs = delegate_home() / "state" / "briefs"
            briefs.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(data["task"])).strip("-") or "task"
            target = briefs / f"{slug}-{utc_now().strftime('%Y%m%dT%H%M%S')}.md"
            try:
                target.write_text(render_spec_brief(data, caps), encoding="utf-8")
            except skill_library.LibraryError as exc:
                print(f"delegate: spec: {exc}", file=sys.stderr)
                return BAD_USAGE
            args.brief_file = str(target)
        args.spec_path, args.spec = spec_path, None
    missing = [f"--{name.replace('_', '-')}" for name in ("agent", "model", "task", "reason", "brief_file", "workdir")
               if getattr(args, name) is None]
    if missing:
        print(f"delegate: missing required flag(s): {', '.join(missing)} (or pass --spec <file.toml>)", file=sys.stderr)
        return BAD_USAGE
    args.mode, args.risk = args.mode or "review", args.risk or "low"
    args.timeout = 1800.0 if args.timeout is None else args.timeout
    if not getattr(args, "effort_source", None):
        try:
            caps = load_caps()
        except ValueError as exc:
            print(f"delegate: {exc}", file=sys.stderr)
            return BAD_USAGE
        if args.effort is not None and args.agent in ("qwen", "copilot"):
            print(f"warning: effort is ignored for {args.agent}", file=sys.stderr)
        args.effort, args.effort_source = resolve_effort(args.agent, args.model, args.risk, args.effort, caps)
    if args.agent in jobspec.EFFORTS and args.effort is not None and args.effort not in jobspec.EFFORTS[args.agent]:
        print(f"delegate: effort {args.effort!r} is invalid for {args.agent}", file=sys.stderr)
        return BAD_USAGE
    return None


def prepare_workdir(args: argparse.Namespace) -> int | None:
    """Create the per-task worktree when asked; afterwards args.workdir points inside it. None = ok."""
    branch = getattr(args, "worktree_branch", None)
    if not branch:
        return None
    try:
        info = gitwork.create_worktree(delegate_home(), args.workdir, branch, getattr(args, "worktree_base", None))
    except gitwork.GitWorkError as exc:
        print(f"delegate: worktree: {exc}", file=sys.stderr)
        return BAD_USAGE
    args.workdir = str(info["workdir"])
    args.worktree_info = {"path": str(info["path"]), "branch": info["branch"], "created": info["created"],
                          "base_sha": info["base_sha"], "repo": str(info["repo"])}
    args.worktree_branch = None
    return None


def brief_command(args: argparse.Namespace) -> int:
    try:
        data = jobspec.load_spec(args.spec)
        caps = load_caps()
    except (jobspec.SpecError, ValueError) as exc:
        print(f"delegate: {exc}", file=sys.stderr)
        return BAD_USAGE
    errors, warnings = jobspec.lint(data, AGENTS)
    for warning in warnings:
        print(f"warning: spec: {warning}", file=sys.stderr)
    if errors:
        for error in errors:
            print(f"delegate: spec: {error}", file=sys.stderr)
        return BAD_USAGE
    try:
        text = render_spec_brief(data, caps)
    except skill_library.LibraryError as exc:
        print(f"delegate: spec: {exc}", file=sys.stderr)
        return BAD_USAGE
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({len(text)} chars)")
    else:
        print(text, end="")
    return OK


def worktree_command(args: argparse.Namespace) -> int:
    home = delegate_home()
    if args.action == "list":
        items = gitwork.list_worktrees(home)
        if not items:
            print("no managed worktrees")
        for item in items:
            print(f"{item['branch']:<28} {'dirty' if item['dirty'] else 'clean'}  {str(item['head'])[:8]}  {item['path']}")
        return OK
    try:
        print(gitwork.remove_worktree(home, args.target, args.force))
    except gitwork.GitWorkError as exc:
        print(f"delegate: worktree: {exc}", file=sys.stderr)
        return BAD_USAGE
    return OK


SEVERITIES = ("high", "medium", "low")


def extract_findings(text: str) -> dict[str, Any] | None:
    """Pull a findings object out of an agent's final message (plain JSON, a fenced block, or JSON inside prose)."""
    candidates: list[Any] = []
    stripped = text.strip()
    try:
        candidates.append(json.loads(stripped))
    except ValueError:
        pass
    for block in re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL):
        with contextlib.suppress(ValueError):
            candidates.append(json.loads(block))
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        with contextlib.suppress(ValueError):
            value, _ = decoder.raw_decode(text[match.start():])
            candidates.append(value)
    for value in candidates:
        if isinstance(value, dict) and isinstance(value.get("findings"), list):
            items = []
            for raw in value["findings"]:
                if not isinstance(raw, dict):
                    continue
                severity = str(raw.get("severity", "")).lower()
                line = raw.get("line")
                items.append({"severity": severity if severity in SEVERITIES else "high", "file": str(raw.get("file", "")),
                              "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
                              "evidence": str(raw.get("evidence", "")), "fix": str(raw.get("fix", ""))})
            counts = {name: sum(1 for i in items if i["severity"] == name) for name in SEVERITIES}
            return {"counts": counts, "items": items, "summary": str(value.get("summary", ""))}
    return None


def resume_command(args: argparse.Namespace) -> int:
    record = load_job(args.job_id)
    if record is None:
        print(f"delegate: unknown job {args.job_id}", file=sys.stderr)
        return BAD_USAGE
    if record.get("status") in jobs.ACTIVE:
        print(f"delegate: job {args.job_id} is still running; wait for it first", file=sys.stderr)
        return BAD_USAGE
    session = record.get("session_id")
    if not session:
        print(f"delegate: job {args.job_id} has no session id to resume (it never reached the agent, or the agent reported none)",
              file=sys.stderr)
        return BAD_USAGE
    text = " ".join(args.message or []).strip() or (args.message_flag or "").strip()
    if not text:
        print("delegate: say what to do next: delegate.py resume <job> <follow-up text>", file=sys.stderr)
        return BAD_USAGE
    previous = dict(record.get("args") or {})
    prior_task = str(record.get("task") or previous.get("task"))
    match = re.search(r"-r(\d+)$", prior_task)
    base = re.sub(r"-r\d+$", "", prior_task)
    task = f"{base}-r{(int(match.group(1)) + 1) if match else 2}"
    try:
        caps = load_caps()
    except ValueError as exc:
        print(f"delegate: {exc}", file=sys.stderr)
        return BAD_USAGE
    briefs = delegate_home() / "state" / "briefs"
    briefs.mkdir(parents=True, exist_ok=True)
    target = briefs / f"{re.sub(r'[^A-Za-z0-9._-]+', '-', task)}-{utc_now().strftime('%Y%m%dT%H%M%S')}.md"
    rules = "\n".join(f"- {rule}" for rule in standing_rules(caps))
    added = getattr(args, "files", None) or []
    if any(jobspec._bad_glob(pattern) for pattern in added):
        print("delegate: --files must be repo-relative globs (no /, ~ or ..)", file=sys.stderr)
        return BAD_USAGE
    extension = "\n\n## Scope extended for this follow-up\n" + "\n".join(f"- `{p}`" for p in added) if added else ""
    target.write_text(f"# Follow-up ({task}), same session\n\n{text}{extension}\n\n## Constraints\n{rules}\n", encoding="utf-8")
    if added:
        previous["files"] = list(dict.fromkeys([*(previous.get("files") or []), *added]))
    if getattr(args, "verify", None) is not None:
        previous["verify"] = args.verify
    if getattr(args, "effort", None) is not None:
        previous["effort"] = args.effort
        previous.pop("effort_source", None)
    elif previous.get("effort") is None and record.get("effort"):
        label = record["effort"]
        previous["effort"] = label.removeprefix("default:") if label not in ("-", "default:?") else None
        previous["effort_source"] = record.get("effort_source") or ("cli-default" if label.startswith("default:") else "spec")
    previous.update(brief_file=str(target), task=task, reason=f"resume {args.job_id}: {text[:60]}", resume_session=session,
                    spec=None, dry_run=False, schema=args.schema)  # follow-ups are free text unless --schema is given again
    if args.no_card:
        previous["no_card"] = True
    if args.card:
        previous["card"] = args.card
    return start_command(argparse.Namespace(command="start", **previous))


REVIEW_CONTEXT = """Read-only: no edits, commits, checkouts or pushes. Git read commands and file reads only;
scratch only in /tmp; no network. The coordinator runs tests on the host.
Report only real problems with file:line, triggering sequence, consequence, confidence and the smallest fix.
Say 'none' if none (use an empty findings list and summary 'none')."""


def review_command(args: argparse.Namespace) -> int:
    rev = args.rev
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/~^{}@+-]*", rev):
        print("delegate: --rev must be a revision or A..B range", file=sys.stderr)
        return BAD_USAGE
    workdir = str(Path(args.workdir).expanduser().resolve())
    task = args.task or "review-" + re.sub(r"[^A-Za-z0-9_-]", "-", rev[:8])
    context = REVIEW_CONTEXT + f"\n\nReview: git {'diff' if '..' in rev else 'show'} {rev}\n\n" + args.focus
    try:
        if args.context_file:
            context += "\n\n" + Path(args.context_file).expanduser().read_text(encoding="utf-8")
        caps = load_caps()
    except (OSError, ValueError) as exc:
        print(f"delegate: {exc}", file=sys.stderr)
        return BAD_USAGE
    data = {"task": task, "agent": args.agent, "model": args.model, "mode": "review", "schema": "findings",
            "reason": "read-only findings review", "risk": args.risk, "workdir": workdir,
            "goal": f"READ-ONLY review of {rev} in {workdir}: {args.focus.splitlines()[0] if args.focus else ''}",
            "context": context}
    for name in ("effort", "card", "timeout"):
        if getattr(args, name) is not None:
            data[name] = getattr(args, name)
    errors, warnings = jobspec.lint(data, AGENTS)
    for warning in warnings:
        print(f"warning: spec: {warning}", file=sys.stderr)
    if errors:
        for error in errors:
            print(f"delegate: spec: {error}", file=sys.stderr)
        return BAD_USAGE
    directory = delegate_home() / "state" / "specs"
    directory.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", task).strip(".") or "review"
    path = directory / f"{slug}-{utc_now().strftime('%Y%m%dT%H%M%S.%fZ')}.toml"
    # All generated fields are strings or numbers; JSON strings are TOML basic strings.
    path.write_text("\n".join(toml_line(key, value) for key, value in data.items()) + "\n",
                    encoding="utf-8")
    run_args = make_parser().parse_args(["start", "--spec", str(path)])
    if args.dry_run:
        code = resolve_run_args(run_args)
        if code is not None:
            return code
        print(render_spec_brief(data, caps), end="")
        print(f"[delegate] agent={run_args.agent} model={run_args.model} effort={effort_label(run_args)}")
        return OK
    return start_command(run_args)


def verified_command(args: argparse.Namespace) -> int:
    record = load_job(args.job_id)
    report = (record or {}).get("report") or {}
    verify = report.get("verify")
    if not verify or not report.get("fingerprint"):
        print("no verify recorded")
        return 2
    try:
        current = gitwork.fingerprint(record["workdir"])
    except (OSError, gitwork.GitWorkError):
        current = None
    if verify.get("passed") and current == report["fingerprint"]:
        print(f"unchanged since verify: PASS ({verify.get('last_line') or ''})")
        return 0
    print("changed since verify")
    return 1


def doctor_command(args: argparse.Namespace) -> int:
    hook = Path(os.environ.get("DELEGATE_PREFLIGHT", "")).expanduser() if os.environ.get("DELEGATE_PREFLIGHT") else None
    code = OK
    try:
        if hook is None:
            raise FileNotFoundError("set DELEGATE_PREFLIGHT to a script that prints a login/version report")
        done = subprocess.run([sys.executable, str(hook), "--now"], capture_output=True, text=True, timeout=90)
        report = (done.stdout or done.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        report = f"preflight could not run: {exc}"
        code = BLOCKED
    print(report)
    if "needs attention" in report:
        code = BLOCKED
    identity = claude_identity()
    print(f"account={identity['account']} plan={identity['plan']}" if identity else "account: claude not logged in or unknown")
    home = delegate_home()
    records = jobs.list_jobs(home)
    active = sum(1 for r in records if r.get("status") in jobs.ACTIVE)
    lost = sum(1 for r in records if r.get("status") == "lost")
    print(f"jobs: {active} active, {lost} lost, {len(records)} total")
    runs = home / "runs"
    files = [f for f in runs.iterdir() if f.is_file()] if runs.is_dir() else []
    size = sum(f.stat().st_size for f in files)
    print(f"runs: {len(files)} file(s), {size / 1_000_000:.1f} MB (delegate.py gc prunes after 14 days)")
    trees = gitwork.list_worktrees(home)
    print(f"worktrees: {len(trees)} ({sum(1 for t in trees if t['dirty'])} dirty)")
    return code


# ---------------------------------------------------------------------------------------------
# Detached jobs: `start` returns a job id at once; `wait` / `show` / `cancel` / `jobs` manage it.
# ---------------------------------------------------------------------------------------------

def utc_iso() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def launch_process(argv: list[str], log_handle: Any) -> Any:
    """Start argv detached (own session/process group, no stdin) with output going to log_handle."""
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log_handle, stderr=log_handle,
                            start_new_session=True, close_fds=True, env=os.environ.copy())


def start_command(args: argparse.Namespace) -> int:
    resolved = resolve_run_args(args)
    if resolved is not None:
        return resolved
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        print("delegate: --timeout must be positive and finite", file=sys.stderr)
        return BAD_USAGE
    try:
        caps = load_caps()
    except ValueError as exc:
        print(f"delegate: {exc}", file=sys.stderr)
        return BAD_USAGE
    brief, workdir = os.path.abspath(args.brief_file), os.path.abspath(args.workdir)
    if not os.path.isfile(brief):
        print(f"delegate: cannot read brief file: {args.brief_file}", file=sys.stderr)
        return BAD_USAGE
    if not os.path.isdir(workdir):
        print(f"delegate: --workdir is not a directory: {args.workdir}", file=sys.stderr)
        return BAD_USAGE
    rows = read_ledger()
    reason, code = policy_reason(args, caps, rows)
    if reason:
        if code == BLOCKED:
            suggestion = suggest_next(args.agent, args.model, caps, rows, args.mode, args.risk,
                                      opus_reason=args.opus_reason, workdir=args.workdir)
            print(f"blocked: {reason}; suggested next agent: {suggestion}", file=sys.stderr)
            blocked_run_card(args, reason, suggestion)
        else:
            print(f"delegate: {reason}", file=sys.stderr)
        return code
    home = delegate_home()
    slot = jobs.slot_block(home, args.agent, caps)
    if slot:
        print(f"blocked: {slot}", file=sys.stderr)
        return BLOCKED
    jobs.maybe_prune(home, 14)
    prepared = prepare_workdir(args)
    if prepared is not None:
        return prepared
    workdir = args.workdir
    job_id = jobs.new_job_id()
    stored = {k: v for k, v in vars(args).items()
              if k not in ("command", "created_card", "worktree_info", "report", "report_file", "on_card")}
    stored.update(brief_file=brief, workdir=workdir)
    log = jobs.job_path(home, job_id, ".log")
    record = {"id": job_id, "status": "queued", "pid": None, "agent": args.agent, "model": args.model,
              "task": args.task, "reason": args.reason, "risk": args.risk, "mode": args.mode, "workdir": workdir,
              "created_at": utc_iso(), "log_file": str(log), "args": stored,
              "effort": effort_label(args), "effort_source": args.effort_source, "parent_card": args.card}
    if getattr(args, "worktree_info", None):
        record["worktree"] = args.worktree_info
    jobs.write_job(home, record)
    try:
        with log.open("ab") as handle:
            process = launch_process([sys.executable, str(Path(__file__).resolve()), "_job", job_id], handle)
    except OSError as exc:
        jobs.write_job(home, dict(record, status="failed", error=str(exc), finished_at=utc_iso()))
        print(f"delegate: could not launch job: {exc}", file=sys.stderr)
        return AGENT_FAILED
    # The child records its own pid/state; the parent must not write again (it could clobber the child).
    print(f"job {job_id} started: {args.agent} {args.model} {effort_label(args)} {args.task}")
    print(f"  delegate.py wait {job_id}   |   show {job_id}   |   cancel {job_id}")
    return OK


SUMMARY_STATUS = re.compile(r"^\[delegate\] agent=\S+.*?\bstatus=(\w+)", re.MULTILINE)


def job_status_for(code: int, err_text: str) -> str:
    if code == OK:
        return "done"
    if code == MODEL_MISMATCH:
        return "model_mismatch"
    if code == BLOCKED:
        return "blocked"
    if code == VERIFY_FAILED:
        return "verify_failed"
    if code == SCOPE_VIOLATION:
        return "scope_violation"
    if code == AGENT_FAILED:
        match = SUMMARY_STATUS.search(err_text)
        return "timeout" if match and match.group(1) == "timeout" else "failed"
    return "failed"


def job_runner(job_id: str) -> int:
    """Entry point of the detached process (`delegate.py _job <id>`)."""
    home = delegate_home()
    record = jobs.read_job(home, job_id)
    if record is None:
        print(f"delegate: unknown job {job_id}", file=sys.stderr)
        return BAD_USAGE
    if record.get("status") in jobs.TERMINAL:  # cancelled (or otherwise finished) before this process started
        return OK
    record.update(status="running", pid=os.getpid(), proc_started=jobs.proc_start(os.getpid()), started_at=utc_iso())
    jobs.write_job(home, record)

    def remember_card(card_id: str) -> None:  # lets `cancel` close the card even if this process is killed
        current = jobs.read_job(home, job_id) or record
        jobs.write_job(home, dict(current, card_id=card_id))
    out, err = io.StringIO(), io.StringIO()
    error = None
    try:
        args = argparse.Namespace(**record["args"])
        args.on_card = remember_card
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_command(args)
    except Exception as exc:  # the job record must always reach a terminal state
        args = None
        code, error = AGENT_FAILED, f"{type(exc).__name__}: {exc}"
    result = jobs.job_path(home, job_id, ".result.txt")
    result.write_text(out.getvalue(), encoding="utf-8")
    err_text = err.getvalue()
    with contextlib.suppress(OSError):
        with jobs.job_path(home, job_id, ".log").open("a", encoding="utf-8") as log:
            log.write(err_text)
    match = SUMMARY_STATUS.search(err_text)
    summary_line = next((ln for ln in err_text.splitlines() if ln.startswith("[delegate] agent=")), None)
    if summary_line is None:
        lines = [ln for ln in err_text.splitlines() if ln.strip()]
        summary_line = lines[-1] if lines else ""
    latest = jobs.read_job(home, job_id) or record
    final = dict(latest, exit_code=code, finished_at=utc_iso(), summary=summary_line, result_file=str(result),
                 card_id=getattr(args, "created_card", None) or latest.get("card_id"), error=error,
                 report=getattr(args, "report", None), session_id=getattr(args, "session_id", None),
                 findings=getattr(args, "findings", None))
    if args is not None and getattr(args, "effort_source", None):
        final.update(effort=effort_label(args), effort_source=args.effort_source,
                     parent_card=getattr(args, "card", None))
        final["args"] = dict(latest["args"], effort=args.effort, effort_source=args.effort_source)
    if latest.get("status") != "cancelled":
        final["status"] = job_status_for(code, err_text) if error is None else "failed"
    jobs.write_job(home, final)
    return code


def job_exit_code(record: dict[str, Any]) -> int:
    value = record.get("exit_code")
    if isinstance(value, int):
        return value
    return OK if record.get("status") == "done" else AGENT_FAILED


def job_duration(record: dict[str, Any]) -> str:
    start, end = jobs._parse(record.get("started_at")), jobs._parse(record.get("finished_at"))
    return jobs.age_str((end - start).total_seconds()) if start and end else "-"


def format_job(record: dict[str, Any], result_lines: int) -> str:
    head = (f"{record['id']} {record.get('status')} exit={record.get('exit_code', '-')} | {record.get('agent')} "
            f"{record.get('model')} {record.get('effort') or '-'} | {record.get('task')} | {job_duration(record)}")
    if record.get("card_id"):
        head += f" | card {record['card_id']}"
    lines = [head]
    summary = str(record.get("summary") or "").replace("[delegate] ", "", 1)
    if summary:
        lines.append(summary)
    if record.get("error"):
        lines.append(f"error: {record['error']}")
    report = record.get("report")
    if isinstance(report, dict):
        verify = report.get("verify")
        if verify:
            label = "TIMEOUT" if verify.get("timed_out") else "PASS" if verify.get("passed") else "FAIL"
            lines.append(f"verify: {label} {verify.get('last_line') or ''} (exit {verify.get('exit')}, {verify.get('duration_s')}s)")
            if label != "PASS" and verify.get("tail"):
                lines.extend("  | " + ln for ln in str(verify["tail"]).splitlines()[-5:])
        elif record.get("status") != "done":
            lines.append("verify: skipped")
        changed, out = report.get("changed") or [], report.get("out_of_scope")
        pre_existing = len(report.get("pre_existing") or [])
        if out is None:
            lines.append(f"scope: {len(changed)} changed (no scope declared); {pre_existing} pre-existing ignored")
        else:
            lines.append(f"scope: {len(changed)} changed, {len(out)} outside; {pre_existing} pre-existing ignored" +
                         (": " + ", ".join(out[:5]) if out else ""))
    findings = record.get("findings")
    if isinstance(findings, dict):
        counts = findings.get("counts", {})
        lines.append(f"findings: {counts.get('high', 0)} high, {counts.get('medium', 0)} medium, {counts.get('low', 0)} low")
        for item in (findings.get("items") or [])[:5]:
            where = f"{item.get('file')}:{item['line']}" if item.get("line") is not None else str(item.get("file"))
            lines.append(f"  [{item.get('severity')}] {where} {item.get('evidence')}")
    path = record.get("result_file")
    if isinstance(findings, dict):
        result_lines = 0  # the structured summary above replaces the raw JSON; the file has the rest
    if path:
        try:
            text = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            text = []
        lines.extend(text[:result_lines])
        if len(text) > result_lines:
            lines.append(f"... ({len(text) - result_lines} more lines)")
        lines.append(f"full result: {path}")
    return "\n".join(lines)


def load_job(job_id: str) -> dict[str, Any] | None:
    try:
        record = jobs.read_job(delegate_home(), job_id)
    except ValueError:
        return None
    return jobs.refresh(delegate_home(), record) if record else None


def wait_command(args: argparse.Namespace) -> int:
    poll = float(os.environ.get("DELEGATE_POLL_S", "2"))
    pending = list(args.job_ids)
    for job_id in pending:
        if load_job(job_id) is None:
            print(f"delegate: unknown job {job_id}", file=sys.stderr)
            return BAD_USAGE
    deadline = time.monotonic() + args.timeout
    finished: dict[str, dict[str, Any]] = {}
    while True:
        for job_id in list(pending):
            record = load_job(job_id)
            if record is not None and record.get("status") in jobs.TERMINAL:
                finished[job_id] = record
                pending.remove(job_id)
        remaining = deadline - time.monotonic()
        if not pending or remaining <= 0:
            break
        time.sleep(min(poll, remaining))
    for job_id in args.job_ids:
        if job_id in finished:
            print(format_job(finished[job_id], 8))
        else:
            record = load_job(job_id) or {}
            started = jobs._parse(record.get("created_at"))
            age = jobs.age_str((utc_now() - started).total_seconds()) if started else "?"
            print(f"{job_id} still running ({age}) | {record.get('agent')} {record.get('model')} | {record.get('task')}"
                  f" | wait again, or `delegate.py show {job_id}`")
    if pending:
        return STILL_RUNNING
    return next((job_exit_code(finished[j]) for j in args.job_ids if job_exit_code(finished[j]) != OK), OK)


def show_command(args: argparse.Namespace) -> int:
    record = load_job(args.job_id)
    if record is None:
        print(f"delegate: unknown job {args.job_id}", file=sys.stderr)
        return BAD_USAGE
    print(format_job(record, args.lines))
    return OK


def cancel_command(args: argparse.Namespace) -> int:
    record = load_job(args.job_id)
    if record is None:
        print(f"delegate: unknown job {args.job_id}", file=sys.stderr)
        return BAD_USAGE
    if record.get("status") not in jobs.ACTIVE:
        print(f"{args.job_id} already {record.get('status')}")
        return OK
    pid = record.get("pid")
    if isinstance(pid, int) and pid != os.getpid() and jobs.pid_alive(pid) and not jobs.job_alive(record):
        # the pid now belongs to some other process: never signal it
        home = delegate_home()
        latest = jobs.read_job(home, args.job_id) or record
        jobs.write_job(home, dict(latest, status="lost", finished_at=utc_iso()))
        print(f"{args.job_id} is gone (its pid was reused by another process); marked lost, nothing was killed")
        return OK
    if isinstance(pid, int) and pid != os.getpid() and jobs.pid_alive(pid):
        try:
            if os.getpgid(pid) == pid:  # only kill a group we created (start_new_session), never a recycled pid
                os.killpg(pid, signal.SIGTERM)
                for _ in range(50):
                    if not jobs.pid_alive(pid):
                        break
                    time.sleep(0.1)
                else:
                    os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    home = delegate_home()
    latest = jobs.read_job(home, args.job_id) or record
    jobs.write_job(home, dict(latest, status="cancelled", finished_at=utc_iso()))
    if record.get("card_id") and os.environ.get("DELEGATE_NO_KANBAN") != "1":
        kanban_call(str((record.get("args") or {}).get("board", "agents")), "block", str(record["card_id"]), "cancelled by coordinator")
    print(f"{args.job_id} cancelled")
    return OK


def jobs_command(args: argparse.Namespace) -> int:
    records = jobs.list_jobs(delegate_home())
    if not args.all:
        records = [r for r in records if r.get("status") in jobs.ACTIVE]
    if args.json:
        print(json.dumps([{k: v for k, v in r.items() if k != "args"} for r in records], indent=2, sort_keys=True))
        return OK
    if not records:
        print("no active jobs (use --all for finished ones)" if not args.all else "no jobs")
        return OK
    now = utc_now()
    for record in records[:15]:
        created = jobs._parse(record.get("created_at"))
        age = jobs.age_str((now - created).total_seconds()) if created else "?"
        print(f"{record['id']}  {str(record.get('status')):<14} {record.get('agent')} {record.get('model')} {record.get('effort') or '-'}  "
              f"{str(record.get('task'))[:28]}  {age} ago")
    if len(records) > 15:
        print(f"... {len(records) - 15} more")
    return OK


def gc_command(args: argparse.Namespace) -> int:
    result = jobs.prune(delegate_home(), args.days, dry_run=args.dry_run)
    verb = "would remove" if args.dry_run else "removed"
    print(f"{verb} {result['jobs']} job(s) and {result['runs']} run log(s) older than {args.days} days")
    return OK


def add_run_arguments(run: argparse.ArgumentParser) -> None:
    run.add_argument("--spec", help="job spec file (TOML); explicit flags override it")
    run.add_argument("--agent", choices=AGENTS)
    run.add_argument("--model")
    run.add_argument("--task")
    run.add_argument("--reason")
    run.add_argument("--brief-file")
    run.add_argument("--workdir")
    run.add_argument("--mode", choices=("implement", "review"))
    run.add_argument("--risk", choices=("low", "high"))
    run.add_argument("--opus-reason", choices=("architecture", "security", "hard-debug", "sonnet-failed"))
    run.add_argument("--effort")
    run.add_argument("--timeout", type=float)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--card", help="Parent task id to reference in the card body")
    run.add_argument("--board", default=os.environ.get("DELEGATE_BOARD", "agents"))
    run.add_argument("--no-card", action="store_true")
    run.add_argument("--worktree", dest="worktree_branch", metavar="BRANCH",
                     help="run in a new git worktree on this agent/<task> branch")
    run.add_argument("--base", dest="worktree_base", metavar="REF", help="ref to create the worktree branch from")
    run.add_argument("--budget-usd", dest="budget_usd", type=float, help="claude only: stop the run at this API-equivalent spend")
    run.add_argument("--schema", choices=tuple(jobspec.SCHEMAS), help="ask for a structured answer (reviews: findings)")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run a delegated task and wait for it (blocking)")
    add_run_arguments(run)
    start = commands.add_parser("start", help="start a delegated task in the background; prints a job id at once")
    add_run_arguments(start)
    review = commands.add_parser("review", help="generate and start a read-only findings review")
    review.add_argument("--rev", required=True)
    review.add_argument("--workdir", required=True)
    review.add_argument("--focus", required=True)
    review.add_argument("--agent", choices=AGENTS, default="codex")
    review.add_argument("--model", default="gpt-6.1-sol")
    review.add_argument("--risk", choices=jobspec.RISKS, default="low")
    review.add_argument("--card")
    review.add_argument("--context-file")
    review.add_argument("--task")
    review.add_argument("--effort")
    review.add_argument("--timeout", type=float)
    review.add_argument("--dry-run", action="store_true")
    verified = commands.add_parser("verified", help="check whether a job's successful verify still applies")
    verified.add_argument("job_id")
    wait = commands.add_parser("wait", help="wait for job(s); prints a short summary")
    wait.add_argument("job_ids", nargs="+")
    wait.add_argument("--timeout", type=float, default=600, help="seconds; exit 6 if still running")
    show = commands.add_parser("show", help="show a job and the head of its result")
    show.add_argument("job_id")
    show.add_argument("--lines", type=int, default=30)
    cancel = commands.add_parser("cancel", help="stop a running job")
    cancel.add_argument("job_id")
    listing = commands.add_parser("jobs", help="list jobs (active by default)")
    listing.add_argument("--all", action="store_true")
    listing.add_argument("--json", action="store_true")
    gc = commands.add_parser("gc", help="delete old job records and run logs")
    gc.add_argument("--days", type=int, default=14)
    gc.add_argument("--dry-run", action="store_true")
    commands.add_parser("skills", help="list the delegate playbook library (spec `skills = [...]`)")
    brief = commands.add_parser("brief", help="lint a job spec and print the brief it renders (spends nothing)")
    brief.add_argument("--spec", required=True)
    brief.add_argument("--out", help="write the brief to this file instead of printing it")
    worktree = commands.add_parser("worktree", help="manage per-task git worktrees")
    worktree_actions = worktree.add_subparsers(dest="action", required=True)
    worktree_actions.add_parser("list")
    remove = worktree_actions.add_parser("rm")
    remove.add_argument("target", help="branch name or path")
    remove.add_argument("--force", action="store_true", help="remove even with uncommitted changes (the branch is always kept)")
    resume = commands.add_parser("resume", help="continue a finished job in the same agent session (cheaper than a fresh run)")
    resume.add_argument("job_id")
    resume.add_argument("message", nargs="*", help="what to do next (put flags after the text)")
    resume.add_argument("--message", dest="message_flag", help="the follow-up text, if you prefer a flag")
    resume.add_argument("--no-card", action="store_true")
    resume.add_argument("--card")
    resume.add_argument("--effort")
    resume.add_argument("--files", action="append", metavar="GLOB")
    resume.add_argument("--verify", metavar="CMD")
    resume.add_argument("--schema", choices=tuple(jobspec.SCHEMAS), help="ask for the structured answer again")
    commands.add_parser("doctor", help="preflight + account + jobs + disk + worktrees in one report")
    hidden = commands.add_parser("_job", help=argparse.SUPPRESS)
    hidden.add_argument("job_id")
    probe = commands.add_parser("probe", help="one-word Claude call: verify a model and refresh quota for the current account")
    probe.add_argument("--model", required=True)
    probe.add_argument("--timeout", type=float, default=120)
    status = commands.add_parser("status", help="show current caps and availability")
    status.add_argument("--json", action="store_true")
    ledger = commands.add_parser("ledger", help="summarize recent ledger entries")
    ledger.add_argument("--days", type=int, default=7)
    ledger.add_argument("--json", action="store_true")
    return parser


LAUNCH_COMMANDS = ("run", "start", "review", "resume")


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    if args.command in LAUNCH_COMMANDS and os.environ.get("DELEGATE_CHILD") == "1":
        # Recursion guard: an agent launched by delegate.py reads the same AGENTS.md/CLAUDE.md
        # telling agents to delegate; it must do the work itself instead of spawning more agents.
        print("blocked: already running inside a delegated agent (DELEGATE_CHILD=1); do the task yourself",
              file=sys.stderr)
        return BLOCKED
    if args.command == "run":
        return run_command(args)
    if args.command == "start":
        return start_command(args)
    if args.command == "review":
        return review_command(args)
    if args.command == "verified":
        return verified_command(args)
    if args.command == "_job":
        return job_runner(args.job_id)
    if args.command == "resume":
        return resume_command(args)
    if args.command == "doctor":
        return doctor_command(args)
    if args.command == "brief":
        return brief_command(args)
    if args.command == "skills":
        for skill in skill_library.list_skills():
            print(f"{skill.name:24} {len(skill.body):5} chars  {skill.description}")
        return OK
    if args.command == "worktree":
        return worktree_command(args)
    if args.command == "wait":
        return wait_command(args)
    if args.command == "show":
        return show_command(args)
    if args.command == "cancel":
        return cancel_command(args)
    if args.command == "jobs":
        return jobs_command(args)
    if args.command == "gc":
        return gc_command(args)
    if args.command == "probe":
        return probe_command(args.model, args.timeout)
    if args.command == "status":
        return status_command(args.json)
    if args.days < 0:
        print("delegate: --days must be non-negative", file=sys.stderr)
        return BAD_USAGE
    return ledger_command(args.days, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
