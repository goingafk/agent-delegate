from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import delegate


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
CAPS = delegate.load_caps(FIXTURES / "caps.toml")
UTC = dt.timezone.utc


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def caps_requiring_opus_reason():
    caps = json.loads(json.dumps(CAPS))
    caps["claude"]["opus_requires_reason"] = True
    return caps


def run_args(home: Path, **changes):
    values = {
        "agent": "qwen", "model": "qwen3.8:27b", "task": "T-1", "reason": "review",
        "brief_file": str(home / "brief.txt"), "workdir": str(home), "mode": "review",
        "risk": "low", "opus_reason": None, "effort": None, "timeout": 30, "dry_run": False,
        "card": None, "board": "agents", "no_card": False,
    }
    values.update(changes)
    return argparse.Namespace(**values)


class TempHomeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        shutil.copy(FIXTURES / "caps.toml", self.home / "caps.toml")
        (self.home / "brief.txt").write_text("Do the task.", encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {
            "DELEGATE_HOME": str(self.home),
            "CODEX_HOME": str(self.home / "codex"),
            "CLAUDE_CONFIG_DIR": str(self.home / "claude"),
            "DELEGATE_NO_CODEX_PROBE": "1",
            "DELEGATE_NO_KANBAN": "1",
            "DELEGATE_NO_CLAUDE_IDENTITY": "1",
            "QWEN_SCRIPT": str(self.home / "qwen_delegate.py"),
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()


class ParserTests(unittest.TestCase):
    def test_codex_parser_extracts_result_and_usage(self):
        parsed = delegate.parse_codex_output(fixture("codex_exec.jsonl"))
        self.assertEqual(parsed["result"], "OK")
        self.assertEqual((parsed["input_tokens"], parsed["output_tokens"], parsed["cached_tokens"]),
                         (19309, 5, 12928))
        self.assertFalse(parsed["failed"])

    def test_codex_unsupported_model_is_failure(self):
        parsed = delegate.parse_codex_output(fixture("codex_exec_unsupported.jsonl"))
        self.assertTrue(parsed["failed"])

    def test_codex_session_parser_extracts_quota(self):
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp) / "sessions"
            sessions.mkdir()
            shutil.copy(FIXTURES / "codex_session.jsonl", sessions / "session.jsonl")
            quota = delegate.read_codex_quota(Path(temp), now_s=1791800000)
        self.assertEqual(quota["five_hour_pct"], 1.0)
        self.assertEqual(quota["weekly_pct"], 0.0)
        self.assertEqual(quota["five_hour_resets_at"], 1791901035)

    def test_claude_parser_extracts_all_fields(self):
        parsed = delegate.parse_claude_output(fixture("claude_stream.jsonl"))
        self.assertEqual(parsed["result"], "OK")
        self.assertEqual(parsed["models"], {"claude-sonnet-5-5"})
        self.assertEqual((parsed["input_tokens"], parsed["output_tokens"], parsed["cached_tokens"]), (2, 4, 535))
        self.assertAlmostEqual(parsed["cost_usd_list"], 0.0151175)
        quota = delegate.normalize_quota(parsed["quota_windows"], now_s=1791800000)
        self.assertEqual((quota["five_hour_pct"], quota["weekly_pct"]), (10.0, 55.0))

    def test_copilot_parser_extracts_result_model_usage_and_premium(self):
        parsed = delegate.parse_copilot_output(fixture("copilot.jsonl"))
        self.assertEqual(parsed["result"], "OK")
        self.assertIn("gpt-5-mini", parsed["models"])
        self.assertEqual((parsed["input_tokens"], parsed["output_tokens"], parsed["cached_tokens"]),
                         (13620, 112, 0))
        self.assertEqual(parsed["premium_requests"], 0)

    def test_qwen_parser_extracts_result_and_usage(self):
        parsed = delegate.parse_qwen_output("Draft complete\n", "[qwen3.8:27b | 12 in / 34 out tok | 1.2s]")
        self.assertEqual(parsed["result"], "Draft complete")
        self.assertEqual((parsed["input_tokens"], parsed["output_tokens"]), (12, 34))


class CapTests(TempHomeTest):
    def write_codex_quota(self, percent: float, reset: float):
        sessions = self.home / "codex" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        event = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": percent, "resets_at": reset},
            "secondary": {"used_percent": 0, "resets_at": reset},
        }}}
        (sessions / "one.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    def write_claude_quota(self, five: float, weekly: float, reset: float):
        target = self.home / "state" / "claude_rate.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"cached_at": "2026-10-13T00:00:00Z", "windows": {
            "five_hour": {"utilization": five, "resetsAt": reset},
            "seven_day": {"utilization": weekly, "resetsAt": reset},
        }}), encoding="utf-8")

    def test_codex_89_allowed_and_90_blocked(self):
        now = dt.datetime(2026, 1, 1, tzinfo=UTC)
        self.write_codex_quota(89, now.timestamp() + 100)
        self.assertIsNone(delegate.cap_reason("codex", "gpt-6.1-sol", CAPS, [], now))
        self.write_codex_quota(90, now.timestamp() + 100)
        self.assertIn("5-hour", delegate.cap_reason("codex", "gpt-6.1-sol", CAPS, [], now))

    def test_codex_expired_window_counts_as_zero(self):
        now = dt.datetime(2026, 1, 1, tzinfo=UTC)
        self.write_codex_quota(99, now.timestamp() - 1)
        self.assertIsNone(delegate.cap_reason("codex", "gpt-6.1-sol", CAPS, [], now))

    def test_claude_family_rate_thresholds(self):
        now = dt.datetime(2026, 1, 1, tzinfo=UTC)
        self.write_claude_quota(0.1, 0.89, now.timestamp() + 100)
        self.assertIsNone(delegate.cap_reason("claude", "claude-sonnet-5-5", CAPS, [], now))
        self.write_claude_quota(0.1, 0.90, now.timestamp() + 100)
        self.assertIn("weekly", delegate.cap_reason("claude", "claude-sonnet-5-5", CAPS, [], now))
        self.write_claude_quota(0.80, 0.1, now.timestamp() + 100)
        self.assertIn("5-hour", delegate.cap_reason("claude", "claude-opus-5-5", CAPS, [], now))

    def test_claude_opus_reason_is_required(self):
        args = run_args(self.home, agent="claude", model="claude-opus-5-5")
        reason, code = delegate.policy_reason(args, caps_requiring_opus_reason(), [])
        self.assertEqual(code, delegate.BLOCKED)
        self.assertIn("--opus-reason", reason)

    def test_owner_policy_17_oct_opus_needs_no_reason_by_default(self):
        self.assertFalse(CAPS["claude"].get("opus_requires_reason", True))
        args = run_args(self.home, agent="claude", model="claude-opus-5-5")
        self.assertEqual(delegate.policy_reason(args, CAPS, []), (None, delegate.OK))

    def test_opus_is_listed_before_sonnet_for_next_agent_suggestions(self):
        order = CAPS["routing"]["order"]
        self.assertLess(order.index("claude-opus"), order.index("claude-sonnet"))

    def test_suggest_next_can_pick_opus_without_a_reason_when_policy_allows(self):
        rows = [{"ts": dt.datetime.now(UTC).isoformat().replace("+00:00", "Z"), "agent": "codex", "model": "x"}]
        with mock.patch("delegate.cap_reason", side_effect=lambda agent, model, *a, **k: None if model == "claude-opus-5-5" else "capped"):
            self.assertEqual(delegate.suggest_next("codex", "gpt-6.1-sol", CAPS, rows, "review", "low"), "claude-opus")
            self.assertNotEqual(delegate.suggest_next("codex", "gpt-6.1-sol", caps_requiring_opus_reason(), rows, "review", "low"), "claude-opus")

    def test_claude_daily_and_weekly_call_caps(self):
        now = dt.datetime(2026, 1, 8, tzinfo=UTC)
        sonnet = [{"ts": (now-dt.timedelta(hours=2)).isoformat(), "agent": "claude", "model": "claude-sonnet-5-5"}] * 15
        self.assertIn("daily", delegate.cap_reason("claude", "claude-sonnet-5-5", CAPS, sonnet, now, False))
        opus_daily = [{"ts": (now-dt.timedelta(hours=2)).isoformat(), "agent": "claude", "model": "claude-opus-5-5"}] * 5
        self.assertIn("daily", delegate.cap_reason("claude", "claude-opus-5-5", CAPS, opus_daily, now, False))
        opus_weekly = [{"ts": (now-dt.timedelta(days=2)).isoformat(), "agent": "claude", "model": "claude-opus-5-5"}] * 15
        self.assertIn("weekly", delegate.cap_reason("claude", "claude-opus-5-5", CAPS, opus_weekly, now, False))

    def test_copilot_free_uncapped_and_premium_uses_only_last_week(self):
        now = dt.datetime(2026, 1, 8, tzinfo=UTC)
        rows = [
            {"ts": (now-dt.timedelta(days=8)).isoformat(), "agent": "copilot", "premium_requests": 100},
            {"ts": (now-dt.timedelta(days=2)).isoformat(), "agent": "copilot", "premium_requests": 25},
        ]
        self.assertIsNone(delegate.cap_reason("copilot", "gpt-5-mini", CAPS, rows, now))
        self.assertIn("25/25", delegate.cap_reason("copilot", "claude-sonnet-5.5", CAPS, rows, now))


class PolicyTests(TempHomeTest):
    def test_qwen_high_risk_and_implement_are_blocked(self):
        high = run_args(self.home, risk="high")
        implement = run_args(self.home, mode="implement")
        self.assertEqual(delegate.policy_reason(high, CAPS, [])[1], delegate.BLOCKED)
        self.assertEqual(delegate.policy_reason(implement, CAPS, [])[1], delegate.BLOCKED)

    def test_qwen_held_lock_is_blocked_without_ledger(self):
        lock_path = self.home / "state" / "qwen.lock"
        lock_path.parent.mkdir(parents=True)
        with lock_path.open("a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                code = delegate.run_command(run_args(self.home))
        self.assertEqual(code, delegate.BLOCKED)
        self.assertIn("qwen busy", errors.getvalue())
        self.assertFalse((self.home / "ledger.jsonl").exists())

    def test_implement_on_main_is_blocked(self):
        args = run_args(self.home, agent="codex", model="gpt-6.1-sol", mode="implement")
        completed = subprocess.CompletedProcess([], 0, "main\n", "")
        with mock.patch("delegate.subprocess.run", return_value=completed):
            reason, code = delegate.policy_reason(args, CAPS, [])
        self.assertEqual(code, delegate.BLOCKED)
        self.assertIn("main", reason)

    def test_block_message_suggests_next_unblocked_route(self):
        args = run_args(self.home, risk="high")
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = delegate.run_command(args)
        self.assertEqual(code, delegate.BLOCKED)
        self.assertIn("suggested next agent: copilot-free", errors.getvalue())


class ArgvTests(TempHomeTest):
    def test_codex_disables_every_mcp_and_selects_sandbox(self):
        config = self.home / "codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('[mcp_servers.safe]\ncommand="x"\n[mcp_servers."prod.db"]\ncommand="y"\n', encoding="utf-8")
        review = delegate.build_argv("codex", "gpt-6.1-sol", "review", self.home, self.home/"run.jsonl", "x")
        implement = delegate.build_argv("codex", "gpt-6.1-sol", "implement", self.home, self.home/"run.jsonl", "x", "high")
        self.assertIn("mcp_servers.safe.enabled=false", review)
        self.assertIn('mcp_servers."prod.db".enabled=false', review)
        self.assertEqual(review[review.index("--sandbox")+1], "read-only")
        self.assertEqual(implement[implement.index("--sandbox")+1], "workspace-write")
        self.assertIn("model_reasoning_effort=high", implement)
        self.assertEqual(review[-1], "-")

    def test_codex_disables_apps_and_non_dart_plugins(self):
        config = self.home / "codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('[plugins."dart-flutter@dart-flutter"]\nenabled=true\n'
                          '[plugins."unified-computer-use@openai-bundled"]\nenabled=true\n', encoding="utf-8")
        argv = delegate.build_argv("codex", "gpt-6.1-sol", "implement", self.home, self.home/"run.jsonl", "x")
        self.assertIn('plugins."unified-computer-use@openai-bundled".enabled=false', argv)
        self.assertFalse(any("dart-flutter" in a for a in argv))
        self.assertIn('mcp_servers.cua_repl={command="/usr/bin/false",enabled=false}', argv)
        for feature in ("apps", "computer_use", "in_app_browser"):
            self.assertIn(f"features.{feature}=false", argv)

    def test_codex_unreadable_config_fails_closed(self):
        config = self.home / "codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text("[mcp_servers.prod\nbroken", encoding="utf-8")
        with self.assertRaises(SystemExit):
            delegate.build_argv("codex", "gpt-6.1-sol", "review", self.home, self.home/"run.jsonl", "x")

    def test_claude_and_copilot_argv_have_only_constrained_flags(self):
        for agent, model in (("claude", "claude-sonnet-5-5"), ("copilot", "gpt-5-mini")):
            for mode in ("review", "implement"):
                argv = delegate.build_argv(agent, model, mode, self.home, self.home/"run.jsonl", "brief")
                self.assertNotIn("--dangerously-skip-permissions", argv)
                self.assertNotIn("--allow-all", argv)
                self.assertNotIn("--yolo", argv)
                if mode == "implement":
                    self.assertTrue(any("git push" in arg for arg in argv))


class RunTests(TempHomeTest):
    @staticmethod
    def fake_process(argv, **kwargs):
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, "feature/test\n", "")
        return subprocess.CompletedProcess(argv, 0, "Draft complete\n", "[qwen3.8:27b | 12 in / 4 out tok | 1.0s]\n")

    def test_success_writes_complete_ledger_line(self):
        with mock.patch("delegate.subprocess.run", side_effect=self.fake_process):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = delegate.run_command(run_args(self.home))
        self.assertEqual(code, 0)
        rows = delegate.read_ledger()
        self.assertEqual(len(rows), 1)
        required = {"ts", "agent", "model", "task", "reason", "opus_reason", "mode", "risk",
                    "workdir", "branch", "status", "exit_code", "duration_s", "input_tokens",
                    "output_tokens", "cached_tokens", "cost_usd_list", "premium_requests",
                    "quota_after", "run_file", "card_id", "parent_card", "effort", "effort_source"}
        self.assertEqual(set(rows[0]), required)
        self.assertEqual(rows[0]["status"], "ok")
        self.assertEqual((rows[0]["input_tokens"], rows[0]["output_tokens"]), (12, 4))
        self.assertTrue(Path(rows[0]["run_file"]).exists())

    def test_dry_run_prints_argv_and_writes_no_ledger(self):
        args = run_args(self.home, dry_run=True)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = delegate.run_command(args)
        self.assertEqual(code, 0)
        self.assertIn("qwen_delegate.py", output.getvalue())
        self.assertFalse((self.home / "ledger.jsonl").exists())

    def test_claude_model_mismatch_is_exit_5_and_cached(self):
        lines = []
        for line in fixture("claude_stream.jsonl").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                lines.append(line)
                continue
            if obj.get("type") == "result":
                obj["modelUsage"] = {"claude-opus-5-5": obj["modelUsage"]["claude-sonnet-5-5"]}
            lines.append(json.dumps(obj))
        stream = "\n".join(lines)

        def fake(argv, **kwargs):
            if argv[0] == "git":
                return subprocess.CompletedProcess(argv, 0, "feature/test\n", "")
            return subprocess.CompletedProcess(argv, 0, stream, "")

        args = run_args(self.home, agent="claude", model="claude-sonnet-5-5")
        with mock.patch("delegate.subprocess.run", side_effect=fake):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = delegate.run_command(args)
        self.assertEqual(code, delegate.MODEL_MISMATCH)
        self.assertEqual(delegate.read_ledger()[0]["status"], "model_mismatch")
        self.assertTrue((self.home / "state" / "claude_rate.json").exists())

    def test_codex_error_event_is_exit_4(self):
        def fake(argv, **kwargs):
            if argv[0] == "git":
                return subprocess.CompletedProcess(argv, 0, "feature/test\n", "")
            return subprocess.CompletedProcess(argv, 0, fixture("codex_exec_unsupported.jsonl"), "")
        args = run_args(self.home, agent="codex", model="gpt-6.1-sol")
        with mock.patch("delegate.subprocess.run", side_effect=fake):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = delegate.run_command(args)
        self.assertEqual(code, delegate.AGENT_FAILED)
        self.assertEqual(delegate.read_ledger()[0]["status"], "failed")


class KanbanTests(TempHomeTest):
    def setUp(self):
        super().setUp()
        os.environ["DELEGATE_NO_KANBAN"] = "0"
        self.calls = []
        self.fail_verb = None
        self.failure = None
        self.agent_failed = False
        self.invalid_json = False
        self.process = mock.patch("delegate.subprocess.run", side_effect=self.fake_process)
        self.process.start()
        self.addCleanup(self.process.stop)

    def fake_process(self, argv, **kwargs):
        if argv[0] == "hermes":
            self.calls.append(argv)
            self.assertEqual(kwargs["timeout"], 20)
            self.assertNotIn("--assignee", argv)
            self.assertNotIn("--parent", argv)
            if argv[4] == self.fail_verb:
                if self.failure:
                    raise self.failure
                return subprocess.CompletedProcess(argv, 1, "", "Kanban unavailable\n")
            output = json.dumps({"id": "t_run"}) if argv[4] == "create" else ""
            if self.invalid_json and argv[4] == "create":
                output = "invalid JSON"
            return subprocess.CompletedProcess(argv, 0, output, "")
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, "feature/test\n", "")
        return subprocess.CompletedProcess(argv, int(self.agent_failed), "Draft complete\n",
                                           "[qwen3.8:27b | 12 in / 4 out tok | 1.0s]\n")

    def delegate_run(self, **changes):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            code = delegate.run_command(run_args(self.home, **changes))
        return code, errors.getvalue()

    def test_success_creates_claims_comments_and_completes(self):
        code, errors = self.delegate_run(card="t_parent", board="activity", timeout=30.5)
        self.assertEqual(code, 0)
        self.assertEqual([call[4] for call in self.calls], ["create", "claim", "comment", "complete"])
        self.assertTrue(all(call[3] == "activity" for call in self.calls))
        create = self.calls[0]
        self.assertEqual(create[5], "[Qwen · qwen3.8:27b · -] T-1: review")
        self.assertEqual(create[create.index("--created-by") + 1], "delegate")
        self.assertNotIn("--provider", create)
        body = create[create.index("--body") + 1]
        for text in ("Task: T-1", "Reason: review", "Mode: review", "Risk: low",
                     f"Workdir: {self.home.resolve()}", "Branch: feature/test", "Part of t_parent"):
            self.assertIn(text, body)
        self.assertEqual(self.calls[1][-2:], ["--ttl", "631"])
        summary = self.calls[2][-1]
        self.assertIn(summary, errors)
        self.assertEqual(self.calls[3][5:], ["t_run", "--force", "--summary", summary])
        self.assertEqual(delegate.read_ledger()[0]["card_id"], "t_run")

    def test_cap_refusal_creates_blocked_card_with_route(self):
        with mock.patch("delegate.cap_reason", return_value="codex 5-hour quota is 90%"):
            code, _ = self.delegate_run(agent="codex", model="gpt-6.1-sol")
        self.assertEqual(code, delegate.BLOCKED)
        self.assertEqual([call[4] for call in self.calls], ["create", "block"])
        self.assertIn("codex 5-hour quota is 90%", self.calls[-1][-1])
        self.assertIn("suggested next agent:", self.calls[-1][-1])
        self.assertEqual(self.calls[0][-2:], ["--provider", "openai-codex"])
        self.assertFalse((self.home / "ledger.jsonl").exists())

    def test_policy_and_busy_refusals_are_visible(self):
        for changes in ({"risk": "high"}, {"mode": "implement"}):
            with self.subTest(changes=changes):
                self.calls.clear()
                self.assertEqual(self.delegate_run(**changes)[0], delegate.BLOCKED)
                self.assertEqual([call[4] for call in self.calls], ["create", "block"])
        self.calls.clear()
        with mock.patch("delegate.acquire_qwen_lock", return_value=None):
            self.assertEqual(self.delegate_run()[0], delegate.BLOCKED)
        self.assertIn("qwen busy", self.calls[-1][-1])

    def test_dry_run_and_usage_errors_create_no_card(self):
        for changes, expected in (({"dry_run": True}, 0), ({"dry_run": True, "risk": "high"}, 3),
                                  ({"model": "invalid"}, 2), ({"brief_file": "missing-file"}, 2),
                                  ({"timeout": float("inf")}, 2)):
            with self.subTest(changes=changes):
                self.assertEqual(self.delegate_run(**changes)[0], expected)
                self.assertEqual(self.calls, [])

    def test_disabled_cards_and_board_parser_defaults(self):
        self.assertEqual(self.delegate_run(no_card=True)[0], 0)
        self.assertEqual(self.calls, [])
        with mock.patch.dict(os.environ, {"DELEGATE_NO_KANBAN": "1"}):
            self.assertEqual(self.delegate_run()[0], 0)
        self.assertEqual(self.calls, [])
        self.assertIsNone(delegate.read_ledger()[0]["card_id"])
        with mock.patch.dict(os.environ, {"DELEGATE_BOARD": "custom"}):
            args = delegate.make_parser().parse_args(["run", "--agent", "qwen", "--model", "qwen3.8:27b",
                "--task", "T", "--reason", "R", "--brief-file", "brief", "--workdir", str(self.home)])
        self.assertEqual(args.board, "custom")

    def test_each_kanban_failure_preserves_success_exit(self):
        for verb in ("create", "claim", "comment", "complete"):
            with self.subTest(verb=verb):
                self.fail_verb = verb
                self.calls.clear()
                code, errors = self.delegate_run()
                self.assertEqual(code, 0)
                self.assertEqual(errors.count("[delegate] kanban:"), 1)
                row = delegate.read_ledger()[-1]
                self.assertEqual(row["card_id"], None if verb == "create" else "t_run")

    def test_timeout_missing_cli_and_invalid_json_do_not_break_run(self):
        self.fail_verb = "create"
        for failure in (subprocess.TimeoutExpired("hermes", 20), FileNotFoundError("hermes")):
            self.failure = failure
            self.assertEqual(self.delegate_run()[0], 0)
        self.failure = None
        self.invalid_json = True
        self.fail_verb = None
        code, errors = self.delegate_run()
        self.assertEqual(code, 0)
        self.assertIn("invalid JSON response", errors)

    def test_failed_agent_is_commented_and_blocked(self):
        self.agent_failed = True
        code, _ = self.delegate_run()
        self.assertEqual(code, delegate.AGENT_FAILED)
        self.assertEqual([call[4] for call in self.calls], ["create", "claim", "comment", "block"])
        self.assertEqual(self.calls[-1][-1], "status=failed exit=4")
        self.fail_verb = "block"
        self.assertEqual(self.delegate_run()[0], delegate.AGENT_FAILED)

    def test_titles_providers_and_opus_metadata(self):
        for agent, model, name, provider in (("codex", "gpt-6.1-sol", "Codex", "openai-codex"),
            ("claude", "claude-opus-5-5", "Claude", "anthropic"),
            ("copilot", "gpt-5-mini", "Copilot", "copilot"), ("qwen", "qwen3.8:27b", "Qwen", None)):
            with self.subTest(agent=agent):
                self.calls.clear()
                delegate.create_run_card(run_args(self.home, agent=agent, model=model,
                                                 reason="x" * 200, opus_reason="architecture"))
                create = self.calls[0]
                self.assertEqual(len(create[5]), 120)
                self.assertTrue(create[5].startswith(f"[{name} · {model} · -] T-1: "))
                self.assertIn("Opus reason: architecture", create[create.index("--body") + 1])
                if provider:
                    self.assertEqual(create[create.index("--provider") + 1], provider)
                else:
                    self.assertNotIn("--provider", create)


class ReportingTests(TempHomeTest):
    def test_ledger_json_aggregates_agent_and_model(self):
        now = delegate.utc_now().isoformat()
        delegate.append_ledger({"ts": now, "agent": "qwen", "model": "qwen3.8:27b", "status": "ok",
                                "input_tokens": 2, "output_tokens": 3, "cost_usd_list": None,
                                "premium_requests": None})
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(delegate.ledger_command(7, True), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data[0]["calls"], 1)
        self.assertEqual(data[0]["output_tokens"], 3)

    def test_status_json_does_not_need_network(self):
        qwen = {"allowed": True, "lock_state": "available", "endpoint_reachable": True, "reason": "reachable"}
        with mock.patch("delegate.qwen_state", return_value=qwen):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(delegate.status_command(True), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(set(data), {"qwen", "codex", "claude", "copilot"})
        self.assertTrue(data["copilot"]["free_models_allowed"])


ME = {"account": "aaaaaaaaaaaa", "plan": "pro"}
OTHER = {"account": "bbbbbbbbbbbb", "plan": "max"}


def claude_rows(model: str, count: int, account=None, now=None):
    now = now or dt.datetime.now(UTC)
    rows = []
    for _ in range(count):
        row = {"ts": now.isoformat().replace("+00:00", "Z"), "agent": "claude", "model": model}
        if account:
            row["account"] = account
        rows.append(row)
    return rows


class AccountIdentityTests(TempHomeTest):
    def status_json(self, org="org-secret-123", plan="pro", email="owner@example.com"):
        return json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": email,
                           "orgId": org, "orgName": email, "subscriptionType": plan})

    def test_account_hash_is_short_stable_and_hides_the_org_id(self):
        first = delegate.account_hash("org-secret-123")
        self.assertRegex(first, r"^[0-9a-f]{12}$")
        self.assertEqual(first, delegate.account_hash("org-secret-123"))
        self.assertNotEqual(first, delegate.account_hash("org-other"))
        self.assertNotIn("org-secret", first)

    def test_identity_comes_from_claude_auth_status_without_the_email(self):
        completed = subprocess.CompletedProcess(["claude"], 0, self.status_json(), "")
        with mock.patch.dict(os.environ, {"DELEGATE_NO_CLAUDE_IDENTITY": "0"}):
            with mock.patch("delegate.subprocess.run", return_value=completed) as run:
                identity = delegate.claude_identity()
        self.assertEqual(run.call_args[0][0][:3], ["claude", "auth", "status"])
        self.assertEqual(identity, {"account": delegate.account_hash("org-secret-123"), "plan": "pro"})
        self.assertNotIn("example.com", json.dumps(identity))

    def test_identity_is_none_when_logged_out_broken_or_disabled(self):
        env = {"DELEGATE_NO_CLAUDE_IDENTITY": "0"}
        bad = [subprocess.CompletedProcess(["claude"], 0, json.dumps({"loggedIn": False}), ""),
               subprocess.CompletedProcess(["claude"], 1, "boom", ""),
               subprocess.CompletedProcess(["claude"], 0, "not json", ""),
               subprocess.CompletedProcess(["claude"], 0, json.dumps({"loggedIn": True}), "")]
        with mock.patch.dict(os.environ, env):
            for completed in bad:
                with mock.patch("delegate.subprocess.run", return_value=completed):
                    self.assertIsNone(delegate.claude_identity())
            with mock.patch("delegate.subprocess.run", side_effect=OSError("no claude")):
                self.assertIsNone(delegate.claude_identity())
        with mock.patch("delegate.subprocess.run") as run:  # disabled by env (the default in tests)
            self.assertIsNone(delegate.claude_identity())
        run.assert_not_called()


class AccountQuotaTests(TempHomeTest):
    WINDOWS = {"five_hour": {"utilization": 0.95, "resetsAt": 4102444800},
               "seven_day": {"utilization": 0.5, "resetsAt": 4102444800}}

    def test_cache_records_which_account_it_belongs_to(self):
        delegate.write_claude_quota(self.WINDOWS, identity=ME)
        raw = json.loads((self.home / "state" / "claude_rate.json").read_text())
        self.assertEqual((raw["account"], raw["plan"]), ("aaaaaaaaaaaa", "pro"))
        self.assertEqual(delegate.read_claude_quota(identity=ME)["five_hour_pct"], 95)

    def test_cache_from_another_or_unknown_account_is_ignored(self):
        delegate.write_claude_quota(self.WINDOWS, identity=OTHER)
        self.assertIsNone(delegate.read_claude_quota(identity=ME))
        delegate.write_claude_quota(self.WINDOWS)  # legacy cache with no account
        self.assertIsNone(delegate.read_claude_quota(identity=ME))
        self.assertEqual(delegate.read_claude_quota(identity=None)["five_hour_pct"], 95)  # identity unknown: old behaviour

    def test_cache_account_helper_reports_owner(self):
        self.assertIsNone(delegate.claude_cache_account())
        delegate.write_claude_quota(self.WINDOWS, identity=OTHER)
        self.assertEqual(delegate.claude_cache_account(), "bbbbbbbbbbbb")

    def test_other_account_cache_does_not_block_but_warns(self):
        delegate.write_claude_quota(self.WINDOWS, identity=OTHER)
        err = io.StringIO()
        with mock.patch("delegate.claude_identity", return_value=ME), contextlib.redirect_stderr(err):
            self.assertIsNone(delegate.cap_reason("claude", "claude-sonnet-5-5", CAPS, []))
        self.assertIn("account changed", err.getvalue())
        self.assertIn("probe", err.getvalue())

    def test_matching_account_with_unreadable_windows_is_not_called_an_account_change(self):
        (self.home / "state").mkdir(exist_ok=True)
        (self.home / "state" / "claude_rate.json").write_text(json.dumps(
            {"cached_at": "2026-10-13T00:00:00Z", "account": "aaaaaaaaaaaa", "windows": {}}), encoding="utf-8")
        err = io.StringIO()
        with mock.patch("delegate.claude_identity", return_value=ME), contextlib.redirect_stderr(err):
            self.assertIsNone(delegate.cap_reason("claude", "claude-sonnet-5-5", CAPS, []))
        self.assertNotIn("account changed", err.getvalue())

    def test_same_account_cache_still_blocks(self):
        delegate.write_claude_quota(self.WINDOWS, identity=ME)
        with mock.patch("delegate.claude_identity", return_value=ME):
            self.assertIn("5-hour", delegate.cap_reason("claude", "claude-sonnet-5-5", CAPS, []))

    def test_call_counters_only_count_the_current_account(self):
        old = claude_rows("claude-sonnet-5-5", 15) + claude_rows("claude-sonnet-5-5", 15, "bbbbbbbbbbbb")
        with mock.patch("delegate.claude_identity", return_value=ME):
            self.assertIsNone(delegate.cap_reason("claude", "claude-sonnet-5-5", CAPS, old, warn=False))
            mine = old + claude_rows("claude-sonnet-5-5", 15, "aaaaaaaaaaaa")
            self.assertIn("daily", delegate.cap_reason("claude", "claude-sonnet-5-5", CAPS, mine, warn=False))
        with mock.patch("delegate.claude_identity", return_value=None):  # identity unknown: count everything
            self.assertIn("daily", delegate.cap_reason("claude", "claude-sonnet-5-5", CAPS, old, warn=False))

    def test_opus_weekly_counter_is_per_account_too(self):
        rows = claude_rows("claude-opus-5-5", 15, "bbbbbbbbbbbb")
        with mock.patch("delegate.claude_identity", return_value=ME):
            self.assertIsNone(delegate.cap_reason("claude", "claude-opus-5-5", CAPS, rows, warn=False))


class ClaudeHelpers:
    def fake_run(self, model="claude-sonnet-5-5"):
        lines = []
        for line in fixture("claude_stream.jsonl").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                lines.append(line)
                continue
            if obj.get("type") == "result":
                obj["modelUsage"] = {model: obj["modelUsage"]["claude-sonnet-5-5"]}
            lines.append(json.dumps(obj))
        stream = "\n".join(lines)

        def fake(argv, **kwargs):
            if argv[0] == "git":
                return subprocess.CompletedProcess(argv, 0, "feature/test\n", "")
            return subprocess.CompletedProcess(argv, 0, stream, "")
        return fake

    def status(self, identity):
        qwen = {"allowed": True, "lock_state": "available", "endpoint_reachable": True, "reason": "reachable"}
        with mock.patch("delegate.qwen_state", return_value=qwen), mock.patch("delegate.claude_identity", return_value=identity):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                delegate.status_command(True)
                data = json.loads(out.getvalue())
            with contextlib.redirect_stdout(io.StringIO()) as text:
                delegate.status_command(False)
        return data["claude"], text.getvalue()


class AccountRunAndStatusTests(ClaudeHelpers, TempHomeTest):
    def test_claude_run_stamps_account_on_ledger_row_and_cache(self):
        args = run_args(self.home, agent="claude", model="claude-sonnet-5-5")
        with mock.patch("delegate.claude_identity", return_value=ME):
            with mock.patch("delegate.subprocess.run", side_effect=self.fake_run()):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(delegate.run_command(args), 0)
        self.assertEqual(delegate.read_ledger()[0]["account"], "aaaaaaaaaaaa")
        raw = json.loads((self.home / "state" / "claude_rate.json").read_text())
        self.assertEqual(raw["account"], "aaaaaaaaaaaa")

    def test_non_claude_rows_have_no_account_field(self):
        args = run_args(self.home)  # qwen
        with mock.patch("delegate.parse_qwen_output", return_value={"result": "x", "failed": False, "models": set(),
                                                                    "input_tokens": 1, "output_tokens": 1, "cached_tokens": 0}):
            with mock.patch("delegate.subprocess.run", return_value=subprocess.CompletedProcess(["q"], 0, "x", "")):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    delegate.run_command(args)
        self.assertNotIn("account", delegate.read_ledger()[0])

    def test_status_shows_account_plan_and_flags_a_changed_account(self):
        delegate.write_claude_quota(AccountQuotaTests.WINDOWS, identity=OTHER)
        claude, text = self.status(ME)
        self.assertEqual((claude["account"], claude["plan"]), ("aaaaaaaaaaaa", "pro"))
        self.assertEqual(claude["quota_account"], "bbbbbbbbbbbb")
        self.assertIsNone(claude["quota"])
        self.assertIn("account changed", text)
        self.assertIn("probe", text)
        self.assertIn("aaaaaaaaaaaa", text)

    def test_status_with_matching_cache_shows_quota(self):
        delegate.write_claude_quota(AccountQuotaTests.WINDOWS, identity=ME)
        claude, text = self.status(ME)
        self.assertEqual(claude["quota"]["five_hour_pct"], 95)
        self.assertNotIn("account changed", text)

    def test_status_counts_only_current_account_calls(self):
        for row in claude_rows("claude-sonnet-5-5", 4, "bbbbbbbbbbbb") + claude_rows("claude-sonnet-5-5", 2, "aaaaaaaaaaaa"):
            delegate.append_ledger(row)
        claude, _ = self.status(ME)
        self.assertEqual(claude["sonnet_calls_today"], 2)


class ProbeCommandTests(ClaudeHelpers, TempHomeTest):
    def test_probe_runs_opus_without_opus_reason_and_logs_it(self):
        with mock.patch("delegate.claude_identity", return_value=ME):
            with mock.patch("delegate.subprocess.run", side_effect=self.fake_run("claude-opus-5-5")) as run:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = delegate.main(["probe", "--model", "claude-opus-5-5"])
        self.assertEqual(code, 0)
        row = delegate.read_ledger()[0]
        self.assertEqual((row["task"], row["model"], row["agent"], row["status"]), ("probe", "claude-opus-5-5", "claude", "ok"))
        self.assertEqual(row["account"], "aaaaaaaaaaaa")
        self.assertIsNone(row["card_id"])
        claude_calls = [c for c in run.call_args_list if c[0][0][0] == "claude"]
        self.assertEqual(len(claude_calls), 1)
        argv = claude_calls[0][0][0]
        self.assertIn("claude-opus-5-5", argv)
        self.assertIn(b"OK", claude_calls[0][1]["input"])
        self.assertTrue(Path(claude_calls[0][1]["cwd"]).is_dir())
        self.assertTrue((self.home / "state" / "claude_rate.json").exists())

    def test_probe_refreshes_the_quota_cache_for_the_current_account(self):
        delegate.write_claude_quota(AccountQuotaTests.WINDOWS, identity=OTHER)
        with mock.patch("delegate.claude_identity", return_value=ME):
            with mock.patch("delegate.subprocess.run", side_effect=self.fake_run()):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    delegate.main(["probe", "--model", "claude-sonnet-5-5"])
        self.assertEqual(delegate.claude_cache_account(), "aaaaaaaaaaaa")

    def test_probe_rejects_models_that_are_not_allowed_claude_models(self):
        err = io.StringIO()
        with mock.patch("delegate.subprocess.run") as run, contextlib.redirect_stderr(err):
            code = delegate.main(["probe", "--model", "claude-fable-9"])
        self.assertEqual(code, delegate.BAD_USAGE)
        run.assert_not_called()

    def test_probe_still_obeys_caps(self):
        rows = claude_rows("claude-opus-5-5", 5)  # opus daily cap is 5
        for row in rows:
            delegate.append_ledger(row)
        with mock.patch("delegate.subprocess.run") as run:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = delegate.main(["probe", "--model", "claude-opus-5-5"])
        self.assertEqual(code, delegate.BLOCKED)
        run.assert_not_called()

    def test_run_command_requires_opus_reason_when_policy_says_so(self):
        (self.home / "caps.toml").write_text((FIXTURES / "caps.toml").read_text().replace(
            "opus_requires_reason = false", "opus_requires_reason = true"), encoding="utf-8")
        args = run_args(self.home, agent="claude", model="claude-opus-5-5")
        with mock.patch("delegate.subprocess.run") as run:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(delegate.run_command(args), delegate.BLOCKED)
        run.assert_not_called()


def reap(process):
    """Kill (if still running) and wait, so no test leaves a child or a ResourceWarning behind."""
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def job_record(job_id="j20261017-000000-aaaa", **changes):
    value = {"id": job_id, "status": "running", "pid": os.getpid(), "agent": "codex", "model": "gpt-6.1-sol",
             "task": "T-1", "reason": "r", "created_at": "2026-10-17T00:00:00Z",
             "args": {"agent": "codex", "model": "gpt-6.1-sol", "task": "T-1", "reason": "r", "brief_file": "b",
                      "workdir": ".", "mode": "review", "risk": "low", "opus_reason": None, "effort": None,
                      "timeout": 30, "dry_run": False, "card": None, "board": "agents", "no_card": True}}
    value.update(changes)
    return value


class JobCommandTests(TempHomeTest):
    def start_args(self, **changes):
        values = {"command": "start", **vars(run_args(self.home, agent="codex", model="gpt-6.1-sol", no_card=True))}
        values.update(changes)
        return argparse.Namespace(**values)

    def popen(self):
        process = mock.Mock()
        process.pid = os.getpid()
        return mock.patch("delegate.launch_process", return_value=process)

    def test_launch_process_detaches_from_stdin_and_the_session(self):
        process = mock.Mock()
        with mock.patch("delegate.subprocess.Popen", return_value=process) as popen:
            self.assertIs(delegate.launch_process(["x", "y"], "HANDLE"), process)
        self.assertEqual(popen.call_args[0][0], ["x", "y"])
        kwargs = popen.call_args[1]
        self.assertEqual((kwargs["stdin"], kwargs["stdout"], kwargs["stderr"]), (subprocess.DEVNULL, "HANDLE", "HANDLE"))
        self.assertTrue(kwargs["start_new_session"])

    def test_parser_knows_the_job_verbs(self):
        parser = delegate.make_parser()
        run_flags = ["--agent", "codex", "--model", "gpt-6.1-sol", "--task", "T", "--reason", "r",
                     "--brief-file", "b", "--workdir", "."]
        self.assertEqual(parser.parse_args(["start", *run_flags]).command, "start")
        self.assertEqual(parser.parse_args(["wait", "j1", "j2", "--timeout", "5"]).job_ids, ["j1", "j2"])
        self.assertEqual(parser.parse_args(["show", "j1", "--lines", "3"]).lines, 3)
        self.assertEqual(parser.parse_args(["cancel", "j1"]).job_id, "j1")
        self.assertTrue(parser.parse_args(["jobs", "--all"]).all)
        self.assertTrue(parser.parse_args(["gc", "--dry-run"]).dry_run)

    def test_start_launches_a_detached_job_and_returns_immediately(self):
        out = io.StringIO()
        with self.popen() as popen, contextlib.redirect_stdout(out):
            code = delegate.start_command(self.start_args())
        self.assertEqual(code, 0)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[1:3], [str(Path(delegate.__file__).resolve()), "_job"])
        records = delegate.jobs.list_jobs(self.home)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual((record["status"], record["agent"], record["model"], record["task"]),
                         ("queued", "codex", "gpt-6.1-sol", "T-1"))
        self.assertEqual(argv[3], record["id"])
        self.assertEqual(record["args"]["brief_file"], str(self.home / "brief.txt"))
        self.assertIn(record["id"], out.getvalue())
        self.assertLessEqual(len(out.getvalue().splitlines()), 3)

    def test_start_is_blocked_by_policy_without_spawning(self):
        err = io.StringIO()
        with self.popen() as popen, contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = delegate.start_command(self.start_args(agent="qwen", model="qwen3.8:27b", mode="implement"))
        self.assertEqual(code, delegate.BLOCKED)
        self.assertIn("blocked:", err.getvalue())
        popen.assert_not_called()
        self.assertEqual(delegate.jobs.list_jobs(self.home), [])

    def test_start_is_blocked_when_the_agent_has_no_free_slot(self):
        delegate.jobs.write_job(self.home, job_record("j-a"))
        delegate.jobs.write_job(self.home, job_record("j-b"))
        err = io.StringIO()
        with self.popen() as popen, contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = delegate.start_command(self.start_args())
        self.assertEqual(code, delegate.BLOCKED)
        self.assertIn("j-a", err.getvalue())
        self.assertIn("wait", err.getvalue())
        popen.assert_not_called()

    def test_start_rejects_missing_brief_or_workdir(self):
        for changes in ({"brief_file": str(self.home / "nope.txt")}, {"workdir": str(self.home / "nope")}):
            with self.popen() as popen, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(delegate.start_command(self.start_args(**changes)), delegate.BAD_USAGE)
            popen.assert_not_called()

    def test_start_prunes_old_logs_at_most_daily(self):
        with self.popen(), mock.patch("delegate.jobs.maybe_prune", return_value={"jobs": 0, "runs": 0}) as prune:
            with contextlib.redirect_stdout(io.StringIO()):
                delegate.start_command(self.start_args())
        prune.assert_called_once()

    def run_job(self, record, run_code=0, stdout="RESULT TEXT\n", stderr="", raises=None, card=None):
        delegate.jobs.write_job(self.home, record)

        def fake(args):
            if card:
                args.created_card = card
            print(stdout, end="")
            print(stderr, end="", file=sys.stderr)
            if raises:
                raise raises
            return run_code
        with mock.patch("delegate.run_command", side_effect=fake):
            code = delegate.job_runner(record["id"])
        return code, delegate.jobs.read_job(self.home, record["id"])

    def test_job_runner_records_result_summary_and_card(self):
        summary = "[delegate] agent=codex model=gpt-6.1-sol status=ok 5h=1% wk=2% tokens=1in/2out/0cached cost=$- premium=-\n"
        code, record = self.run_job(job_record(), stderr=summary, card="t_card")
        self.assertEqual(code, 0)
        self.assertEqual((record["status"], record["exit_code"], record["card_id"]), ("done", 0, "t_card"))
        self.assertEqual(Path(record["result_file"]).read_text(), "RESULT TEXT\n")
        self.assertIn("status=ok", record["summary"])
        self.assertTrue(record["started_at"] and record["finished_at"])
        self.assertEqual(record["pid"], os.getpid())

    def test_card_id_is_saved_as_soon_as_the_card_exists_so_cancel_can_close_it(self):
        seen = {}
        record = job_record()
        delegate.jobs.write_job(self.home, record)

        def fake(args):
            args.on_card("t_early")
            seen["mid_run"] = delegate.jobs.read_job(self.home, record["id"])["card_id"]
            return 0
        with mock.patch("delegate.run_command", side_effect=fake):
            delegate.job_runner(record["id"])
        self.assertEqual(seen["mid_run"], "t_early")

    def test_cancel_blocks_the_card_recorded_mid_run(self):
        process = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(reap, process)
        delegate.jobs.write_job(self.home, job_record(pid=process.pid, card_id="t_card"))
        with mock.patch.dict(os.environ, {"DELEGATE_NO_KANBAN": "0"}), mock.patch("delegate.kanban_call") as kanban:
            with contextlib.redirect_stdout(io.StringIO()):
                delegate.cancel_command(argparse.Namespace(job_id="j20261017-000000-aaaa"))
        kanban.assert_called_once_with("agents", "block", "t_card", "cancelled by coordinator")

    def test_start_writes_the_record_once_and_never_after_the_child_launched(self):
        with self.popen(), mock.patch("delegate.jobs.write_job", wraps=delegate.jobs.write_job) as write:
            with contextlib.redirect_stdout(io.StringIO()):
                delegate.start_command(self.start_args())
        self.assertEqual(write.call_count, 1)

    def test_job_runner_does_not_run_a_job_cancelled_before_it_started(self):
        delegate.jobs.write_job(self.home, job_record(status="cancelled"))
        with mock.patch("delegate.run_command") as run:
            delegate.job_runner("j20261017-000000-aaaa")
        run.assert_not_called()
        self.assertEqual(delegate.jobs.read_job(self.home, "j20261017-000000-aaaa")["status"], "cancelled")

    def test_job_runner_turns_a_malformed_record_into_a_failed_job(self):
        record = job_record()
        del record["args"]
        delegate.jobs.write_job(self.home, record)
        code = delegate.job_runner(record["id"])
        saved = delegate.jobs.read_job(self.home, record["id"])
        self.assertEqual((code, saved["status"]), (delegate.AGENT_FAILED, "failed"))
        self.assertIn("args", saved["error"])

    def test_job_runner_records_its_process_start_for_pid_reuse_checks(self):
        _, record = self.run_job(job_record(status="queued", pid=None))
        self.assertEqual(record["proc_started"], delegate.jobs.proc_start(os.getpid()))

    def test_cancel_refuses_to_kill_a_recycled_pid(self):
        process = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(reap, process)
        delegate.jobs.write_job(self.home, job_record(pid=process.pid, proc_started="Thu Jan  1 00:00:00 1970"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            delegate.cancel_command(argparse.Namespace(job_id="j20261017-000000-aaaa"))
        self.assertIsNone(process.poll())
        self.assertEqual(delegate.jobs.read_job(self.home, "j20261017-000000-aaaa")["status"], "lost")

    def test_job_runner_maps_outcomes_to_statuses(self):
        cases = [(delegate.AGENT_FAILED, "[delegate] agent=codex status=timeout x\n", "timeout"),
                 (delegate.AGENT_FAILED, "[delegate] agent=codex status=failed x\n", "failed"),
                 (delegate.MODEL_MISMATCH, "[delegate] agent=codex status=model_mismatch x\n", "model_mismatch"),
                 (delegate.BLOCKED, "blocked: nope\n", "blocked"),
                 (delegate.BAD_USAGE, "delegate: bad\n", "failed")]
        for index, (code, stderr, expected) in enumerate(cases):
            _, record = self.run_job(job_record(f"j-m{index}"), run_code=code, stderr=stderr)
            self.assertEqual(record["status"], expected, stderr)
            self.assertEqual(record["exit_code"], code)

    def test_job_runner_survives_an_exception_in_the_run(self):
        code, record = self.run_job(job_record(), raises=RuntimeError("boom"))
        self.assertEqual(code, delegate.AGENT_FAILED)
        self.assertEqual(record["status"], "failed")
        self.assertIn("boom", record["error"])

    def test_wait_prints_a_short_summary_and_returns_the_job_exit_code(self):
        long_result = "".join(f"line {i}\n" for i in range(100))
        self.run_job(job_record(), stdout=long_result, stderr="[delegate] agent=codex status=ok x\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = delegate.wait_command(argparse.Namespace(job_ids=["j20261017-000000-aaaa"], timeout=5))
        self.assertEqual(code, 0)
        lines = out.getvalue().splitlines()
        self.assertLessEqual(len(lines), 14)
        self.assertIn("done", lines[0])
        self.assertIn("line 0", out.getvalue())
        self.assertNotIn("line 99", out.getvalue())
        self.assertIn(".result.txt", out.getvalue())

    def test_wait_times_out_with_exit_6_when_the_job_is_still_running(self):
        delegate.jobs.write_job(self.home, job_record())
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"DELEGATE_POLL_S": "0.01"}), contextlib.redirect_stdout(out):
            code = delegate.wait_command(argparse.Namespace(job_ids=["j20261017-000000-aaaa"], timeout=0.05))
        self.assertEqual(code, delegate.STILL_RUNNING)
        self.assertIn("still running", out.getvalue())

    def test_wait_sees_a_job_finish_while_waiting(self):
        import threading
        delegate.jobs.write_job(self.home, job_record())

        def finish():
            time.sleep(0.05)
            delegate.jobs.write_job(self.home, job_record(status="done", exit_code=0, summary="s"))
        threading.Thread(target=finish).start()
        with mock.patch.dict(os.environ, {"DELEGATE_POLL_S": "0.01"}), contextlib.redirect_stdout(io.StringIO()):
            code = delegate.wait_command(argparse.Namespace(job_ids=["j20261017-000000-aaaa"], timeout=5))
        self.assertEqual(code, 0)

    def test_wait_on_unknown_job_is_bad_usage_and_failures_propagate(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(delegate.wait_command(argparse.Namespace(job_ids=["nope"], timeout=1)), delegate.BAD_USAGE)
        delegate.jobs.write_job(self.home, job_record("j-ok", status="done", exit_code=0))
        delegate.jobs.write_job(self.home, job_record("j-bad", status="failed", exit_code=4))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(delegate.wait_command(argparse.Namespace(job_ids=["j-ok", "j-bad"], timeout=1)), 4)

    def test_show_trims_to_the_requested_lines_and_points_at_the_file(self):
        self.run_job(job_record(), stdout="".join(f"line {i}\n" for i in range(10)))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(delegate.show_command(argparse.Namespace(job_id="j20261017-000000-aaaa", lines=3)), 0)
        text = out.getvalue()
        self.assertIn("line 2", text)
        self.assertNotIn("line 3", text)
        self.assertIn("7 more lines", text)
        self.assertIn(".result.txt", text)

    def test_cancel_stops_the_real_process_group_and_marks_the_job(self):
        process = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(reap, process)
        delegate.jobs.write_job(self.home, job_record(pid=process.pid))
        with contextlib.redirect_stdout(io.StringIO()):
            code = delegate.cancel_command(argparse.Namespace(job_id="j20261017-000000-aaaa"))
        self.assertEqual(code, 0)
        process.wait(timeout=10)
        self.assertEqual(delegate.jobs.read_job(self.home, "j20261017-000000-aaaa")["status"], "cancelled")

    def test_cancel_unknown_or_finished_jobs(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(delegate.cancel_command(argparse.Namespace(job_id="nope")), delegate.BAD_USAGE)
        delegate.jobs.write_job(self.home, job_record("j-done", status="done"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(delegate.cancel_command(argparse.Namespace(job_id="j-done")), 0)
        self.assertIn("already done", out.getvalue())

    def test_jobs_lists_active_by_default_and_everything_with_all(self):
        delegate.jobs.write_job(self.home, job_record("j-run"))
        delegate.jobs.write_job(self.home, job_record("j-fin", status="done", exit_code=0, created_at="2026-10-16T00:00:00Z"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            delegate.jobs_command(argparse.Namespace(all=False, json=False))
        self.assertIn("j-run", out.getvalue())
        self.assertNotIn("j-fin", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            delegate.jobs_command(argparse.Namespace(all=True, json=True))
        self.assertEqual({r["id"] for r in json.loads(out.getvalue())}, {"j-run", "j-fin"})

    def test_gc_reports_counts_and_honours_dry_run(self):
        delegate.jobs.write_job(self.home, job_record("j-old", status="done", finished_at="2020-01-01T00:00:00Z"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            delegate.gc_command(argparse.Namespace(days=14, dry_run=True))
        self.assertIn("1 job", out.getvalue())
        self.assertIsNotNone(delegate.jobs.read_job(self.home, "j-old"))
        with contextlib.redirect_stdout(io.StringIO()):
            delegate.gc_command(argparse.Namespace(days=14, dry_run=False))
        self.assertIsNone(delegate.jobs.read_job(self.home, "j-old"))


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items()) + " }"
    return json.dumps(str(value))


def git_in(cwd, *args):
    done = subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
                          cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


class SpecHelpers:
    def write_spec(self, name="s.toml", **changes):
        values = {"task": "T-spec", "agent": "codex", "model": "gpt-6.1-sol", "reason": "because",
                  "goal": "Review the thing.", "workdir": str(self.home), "mode": "review"}
        values.update(changes)
        path = self.home / name
        path.write_text("\n".join(f"{k} = {toml_value(v)}" for k, v in values.items()) + "\n", encoding="utf-8")
        return str(path)

    def parse(self, *argv):
        return delegate.make_parser().parse_args(list(argv))


class SpecIntegrationTests(SpecHelpers, TempHomeTest):
    def test_parser_accepts_a_spec_instead_of_the_individual_flags(self):
        args = self.parse("run", "--spec", "x.toml")
        self.assertEqual((args.spec, args.agent), ("x.toml", None))

    def test_resolve_fills_the_run_arguments_from_the_spec_and_renders_the_brief(self):
        args = self.parse("start", "--spec", self.write_spec())
        self.assertIsNone(delegate.resolve_run_args(args))
        self.assertEqual((args.agent, args.model, args.task, args.mode, args.risk, args.timeout),
                         ("codex", "gpt-6.1-sol", "T-spec", "review", "low", 1800))
        brief = Path(args.brief_file).read_text()
        self.assertIn("# Task: T-spec", brief)
        self.assertIn("## Goal", brief)
        self.assertIn("Staging only", brief)

    def test_explicit_flags_override_the_spec(self):
        args = self.parse("start", "--spec", self.write_spec(), "--model", "gpt-6-astra", "--timeout", "99")
        self.assertIsNone(delegate.resolve_run_args(args))
        self.assertEqual((args.model, args.timeout), ("gpt-6-astra", 99))

    def test_spec_errors_are_printed_and_stop_the_run(self):
        err = io.StringIO()
        args = self.parse("start", "--spec", self.write_spec(goal=""))
        with contextlib.redirect_stderr(err):
            self.assertEqual(delegate.resolve_run_args(args), delegate.BAD_USAGE)
        self.assertIn("goal", err.getvalue())

    def test_a_missing_spec_file_is_a_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(delegate.resolve_run_args(self.parse("start", "--spec", str(self.home / "no.toml"))), delegate.BAD_USAGE)
        self.assertIn("no.toml", err.getvalue())

    def test_without_a_spec_the_old_flags_are_still_required(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = delegate.resolve_run_args(self.parse("start", "--agent", "codex"))
        self.assertEqual(code, delegate.BAD_USAGE)
        for flag in ("--model", "--task", "--reason", "--brief-file", "--workdir"):
            self.assertIn(flag, err.getvalue())

    def test_complete_flag_style_args_are_left_untouched(self):
        args = self.parse("run", "--agent", "qwen", "--model", "qwen3.8:27b", "--task", "T", "--reason", "R",
                          "--brief-file", str(self.home / "brief.txt"), "--workdir", str(self.home))
        self.assertIsNone(delegate.resolve_run_args(args))
        self.assertEqual((args.mode, args.risk, args.timeout, args.brief_file), ("review", "low", 1800, str(self.home / "brief.txt")))

    def test_implement_spec_carries_verify_and_scope_into_the_args(self):
        path = self.write_spec(mode="implement", files=["app/**"], verify="true", acceptance=["ok"])
        args = self.parse("start", "--spec", path)
        self.assertIsNone(delegate.resolve_run_args(args))
        self.assertEqual((args.verify, args.files), ("true", ["app/**"]))

    def test_caps_can_replace_the_standing_rules(self):
        caps = (self.home / "caps.toml")
        caps.write_text(caps.read_text() + '\n[brief]\nstanding = ["Only this rule."]\n')
        args = self.parse("start", "--spec", self.write_spec())
        delegate.resolve_run_args(args)
        brief = Path(args.brief_file).read_text()
        self.assertIn("Only this rule.", brief)
        self.assertNotIn("Staging only", brief)

    def test_brief_command_previews_and_lints_without_spending_anything(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(delegate.main(["brief", "--spec", self.write_spec()]), 0)
        self.assertIn("## Goal", out.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(delegate.main(["brief", "--spec", self.write_spec(goal="")]), delegate.BAD_USAGE)
        target = self.home / "out.md"
        with contextlib.redirect_stdout(io.StringIO()):
            delegate.main(["brief", "--spec", self.write_spec(), "--out", str(target)])
        self.assertIn("# Task: T-spec", target.read_text())

    def test_start_accepts_a_spec_and_launches_with_the_rendered_brief(self):
        process = mock.Mock()
        process.pid = os.getpid()
        with mock.patch("delegate.launch_process", return_value=process), contextlib.redirect_stdout(io.StringIO()):
            code = delegate.main(["start", "--spec", self.write_spec(), "--no-card"])
        self.assertEqual(code, 0)
        record = delegate.jobs.list_jobs(self.home)[0]
        self.assertEqual((record["agent"], record["task"]), ("codex", "T-spec"))
        self.assertIn("# Task: T-spec", Path(record["args"]["brief_file"]).read_text())


class WorktreeCommandTests(SpecHelpers, TempHomeTest):
    def setUp(self):
        super().setUp()
        self.repo = Path(self.temp.name).resolve() / "repo"
        (self.repo / "app").mkdir(parents=True)
        git_in(self.repo, "init", "-q", "-b", "main")
        (self.repo / "app" / "a.txt").write_text("a\n")
        git_in(self.repo, "add", "-A")
        git_in(self.repo, "commit", "-q", "-m", "init")

    def implement_spec(self, **changes):
        return self.write_spec(mode="implement", files=["app/**"], verify="true", acceptance=["ok"],
                               workdir=str(self.repo / "app"), worktree={"branch": "agent/x", "base": "main"}, **changes)

    def start(self, spec_path):
        process = mock.Mock()
        process.pid = os.getpid()
        err = io.StringIO()
        with mock.patch("delegate.launch_process", return_value=process), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            return delegate.main(["start", "--spec", spec_path, "--no-card"]), err.getvalue()

    def test_a_spec_with_a_worktree_runs_in_a_new_agent_branch_not_in_main(self):
        code, _ = self.start(self.implement_spec())
        self.assertEqual(code, 0)
        record = delegate.jobs.list_jobs(self.home)[0]
        self.assertTrue(record["workdir"].startswith(str(self.home / "worktrees")))
        self.assertTrue(record["workdir"].endswith("/app"))
        self.assertEqual(record["args"]["workdir"], record["workdir"])
        self.assertEqual(record["worktree"]["branch"], "agent/x")
        self.assertIsNone(record["args"].get("worktree_branch"))
        self.assertIn("agent/x", git_in(self.repo, "branch", "--list", "agent/x"))
        self.assertEqual(git_in(self.repo, "status", "--porcelain"), "")
        self.assertEqual(git_in(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_worktree_flags_work_without_a_spec(self):
        process = mock.Mock()
        process.pid = os.getpid()
        with mock.patch("delegate.launch_process", return_value=process), contextlib.redirect_stdout(io.StringIO()):
            code = delegate.main(["start", "--agent", "codex", "--model", "gpt-6.1-sol", "--task", "T", "--reason", "r",
                                  "--brief-file", str(self.home / "brief.txt"), "--workdir", str(self.repo), "--mode", "implement",
                                  "--worktree", "agent/flag", "--base", "main", "--no-card"])
        self.assertEqual(code, 0)
        self.assertTrue(delegate.jobs.list_jobs(self.home)[0]["workdir"].startswith(str(self.home / "worktrees")))

    def test_a_bad_base_ref_is_reported_and_nothing_is_launched(self):
        bad = self.write_spec(mode="implement", files=["app/**"], verify="true", acceptance=["ok"],
                              workdir=str(self.repo), worktree={"branch": "agent/x", "base": "no-such-ref"})
        code, err = self.start(bad)
        self.assertEqual(code, delegate.BAD_USAGE)
        self.assertIn("no-such-ref", err)
        self.assertEqual(delegate.jobs.list_jobs(self.home), [])

    def test_implement_in_the_main_checkout_without_a_worktree_is_still_blocked(self):
        direct = self.write_spec(mode="implement", files=["app/**"], verify="true", acceptance=["ok"], workdir=str(self.repo))
        code, err = self.start(direct)
        self.assertEqual(code, delegate.BLOCKED)
        self.assertIn("protected branch", err)

    def test_worktree_list_and_remove_commands(self):
        self.start(self.implement_spec())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(delegate.main(["worktree", "list"]), 0)
        self.assertIn("agent/x", out.getvalue())
        path = delegate.gitwork.list_worktrees(self.home)[0]["path"]
        (Path(path) / "scratch.txt").write_text("x")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(delegate.main(["worktree", "rm", "agent/x"]), 0)
        self.assertTrue(Path(path).exists())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(delegate.main(["worktree", "rm", "agent/x", "--force"]), 0)
        self.assertFalse(Path(path).exists())


class VerifyReportTests(SpecHelpers, TempHomeTest):
    def test_run_verify_reports_pass_fail_tail_and_runs_in_the_workdir(self):
        ok = delegate.run_verify("echo hello && touch made.txt", self.home, 30)
        self.assertEqual((ok["passed"], ok["exit"]), (True, 0))
        self.assertTrue((self.home / "made.txt").exists())
        bad = delegate.run_verify("for i in $(seq 1 60); do echo line$i; done; exit 3", self.home, 30)
        self.assertEqual((bad["passed"], bad["exit"]), (False, 3))
        self.assertIn("line60", bad["tail"])
        self.assertNotIn("line1\n", bad["tail"] + "\n")
        self.assertLessEqual(len(bad["tail"].splitlines()), 25)

    def test_run_verify_times_out_cleanly(self):
        result = delegate.run_verify("sleep 30", self.home, 0.3)
        self.assertEqual((result["passed"], result["timed_out"]), (False, True))

    def codex_run(self, changed, verify_result=None, **changes):
        stream = fixture("codex_exec.jsonl")

        def fake(argv, **kwargs):
            if argv[0] == "git":
                return subprocess.CompletedProcess(argv, 0, "feature/test\n", "")
            return subprocess.CompletedProcess(argv, 0, stream, "")
        args = run_args(self.home, agent="codex", model="gpt-6.1-sol", mode="implement", verify="true",
                        files=["app/**"], **changes)
        verify = mock.Mock(return_value=verify_result or {"cmd": "true", "passed": True, "exit": 0, "timed_out": False,
                                                          "tail": "", "duration_s": 0.1})
        err = io.StringIO()
        with mock.patch("delegate.subprocess.run", side_effect=fake), mock.patch("delegate.gitwork.head", return_value="abc123"), \
                mock.patch("delegate.gitwork.changed_files", return_value=changed) as changed_mock, \
                mock.patch("delegate.gitwork.fingerprint", return_value="fingerprint"), \
                mock.patch("delegate.gitwork.dirty_snapshot", return_value={}), \
                mock.patch("delegate.run_verify", verify), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = delegate.run_command(args)
        return code, args, err.getvalue(), verify, changed_mock

    def test_clean_run_passes_verify_and_stays_in_scope(self):
        code, args, err, verify, changed = self.codex_run(["app/a.txt"])
        self.assertEqual(code, 0)
        verify.assert_called_once()
        self.assertEqual(changed.call_args[1].get("since"), "abc123")
        self.assertIn("verify=pass", err)
        self.assertIn("scope=ok", err)
        row = delegate.read_ledger()[0]
        self.assertEqual((row["verify"], row["out_of_scope"]), ("pass", []))
        report = json.loads(Path(args.report_file).read_text())
        self.assertEqual((report["changed"], report["out_of_scope"], report["verify"]["passed"]), (["app/a.txt"], [], True))
        self.assertEqual(args.report["changed"], ["app/a.txt"])

    def test_files_outside_the_declared_scope_are_a_scope_violation(self):
        code, args, err, _, _ = self.codex_run(["app/a.txt", "pubspec.lock"])
        self.assertEqual(code, delegate.SCOPE_VIOLATION)
        self.assertIn("scope=1-out", err)
        self.assertEqual(delegate.read_ledger()[0]["out_of_scope"], ["pubspec.lock"])

    def test_a_failing_verify_beats_a_scope_violation(self):
        failed = {"cmd": "true", "passed": False, "exit": 1, "timed_out": False, "tail": "boom", "duration_s": 0.1}
        code, _, err, _, _ = self.codex_run(["pubspec.lock"], verify_result=failed)
        self.assertEqual(code, delegate.VERIFY_FAILED)
        self.assertIn("verify=fail", err)
        self.assertEqual(delegate.read_ledger()[0]["verify"], "fail")

    def test_verify_is_skipped_when_the_agent_itself_failed(self):
        def fake(argv, **kwargs):
            if argv[0] == "git":
                return subprocess.CompletedProcess(argv, 0, "feature/test\n", "")
            return subprocess.CompletedProcess(argv, 1, "", "agent crashed")
        args = run_args(self.home, agent="codex", model="gpt-6.1-sol", mode="implement", verify="true", files=["app/**"])
        verify = mock.Mock()
        with mock.patch("delegate.subprocess.run", side_effect=fake), mock.patch("delegate.gitwork.head", return_value="abc"), \
                mock.patch("delegate.gitwork.changed_files", return_value=[]), mock.patch("delegate.run_verify", verify), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = delegate.run_command(args)
        self.assertEqual(code, delegate.AGENT_FAILED)
        verify.assert_not_called()
        self.assertEqual(delegate.read_ledger()[0]["verify"], "skipped")

    def test_runs_without_verify_or_scope_are_unchanged(self):
        with mock.patch("delegate.gitwork.changed_files") as changed, mock.patch("delegate.run_verify") as verify, \
                mock.patch("delegate.subprocess.run", side_effect=RunTests.fake_process), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = delegate.run_command(run_args(self.home))
        self.assertEqual(code, 0)
        changed.assert_not_called()
        verify.assert_not_called()
        self.assertNotIn("verify", delegate.read_ledger()[0])

    def test_job_statuses_for_verify_and_scope_failures_and_the_job_report(self):
        for index, (code, expected) in enumerate(((delegate.VERIFY_FAILED, "verify_failed"), (delegate.SCOPE_VIOLATION, "scope_violation"))):
            record = job_record(f"j-v{index}")
            delegate.jobs.write_job(self.home, record)

            def fake(args, code=code):
                args.report = {"changed": ["a", "b"], "out_of_scope": ["b"], "verify": {"passed": False, "exit": 1, "tail": "t1\nt2", "duration_s": 2.0, "timed_out": False}}
                print("agent said hi")
                return code
            with mock.patch("delegate.run_command", side_effect=fake):
                delegate.job_runner(record["id"])
            saved = delegate.jobs.read_job(self.home, record["id"])
            self.assertEqual(saved["status"], expected)
            self.assertEqual(saved["report"]["out_of_scope"], ["b"])
            text = delegate.format_job(saved, 5)
            self.assertIn("verify: FAIL", text)
            self.assertIn("scope: 2 changed, 1 outside", text)
            self.assertIn("b", text)
            self.assertIn("t2", text)



class RecursionGuardTests(unittest.TestCase):
    def test_launch_commands_refuse_inside_a_delegated_agent(self):
        with mock.patch.dict(os.environ, {"DELEGATE_CHILD": "1"}), contextlib.redirect_stderr(io.StringIO()) as err:
            for command in (["run", "--agent", "codex", "--model", "m", "--task", "t", "--reason", "r",
                             "--brief-file", "b", "--workdir", "."],
                            ["start", "--spec", "s.toml"], ["resume", "j", "x"]):
                self.assertEqual(delegate.main(command), delegate.BLOCKED)
        self.assertIn("DELEGATE_CHILD", err.getvalue())

    def test_agent_subprocess_is_marked_as_child(self):
        source = (Path(delegate.__file__)).read_text(encoding="utf-8")
        self.assertIn('env={**os.environ, "DELEGATE_CHILD": "1"}', source)

if __name__ == "__main__":
    unittest.main()
