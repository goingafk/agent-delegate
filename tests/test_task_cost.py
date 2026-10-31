from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import task_cost


UTC = dt.timezone.utc
START = dt.datetime(2026, 10, 13, 9, tzinfo=UTC)
END = START + dt.timedelta(seconds=120)


@contextlib.contextmanager
def writable(path):
    """Commit on success AND close: `with sqlite3.connect(...)` alone leaves the connection open (ResourceWarning on GC)."""
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class TaskCostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.db = self.home / "sessions ?#.db"
        with writable(self.db) as connection:
            connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, input_tokens INTEGER, "
                               "output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
                               "reasoning_tokens INTEGER, estimated_cost_usd REAL, last_activity_at REAL, started_at REAL)")
            connection.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                ("old", "claude-opus-5-5", 50, 10, 10, 1, 2, .2, 1, 0),
                ("chosen", "claude-opus-5-5", 100, 20, 30, 4, 6, .5, 2, 0),
                ("newer", "claude-sonnet-5-5", 500, 50, 40, 4, 6, 2, 3, 0),
            ])
        for patch in (
            mock.patch.dict(os.environ, {"DELEGATE_HOME": str(self.home), "HERMES_SESSION_ID": "chosen"}),
            mock.patch("task_cost.SESSION_DB", self.db),
            mock.patch("task_cost.utc_now", return_value=START),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch("task_cost.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", ""))
        self.process = patch.start()
        self.addCleanup(patch.stop)

    def invoke(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
            code = task_cost.main(argv)
        return code, output.getvalue(), errors.getvalue()

    def advance(self):
        with writable(self.db) as connection:
            connection.execute("UPDATE sessions SET input_tokens=400, output_tokens=80, cache_read_tokens=130, "
                               "cache_write_tokens=14, reasoning_tokens=16, estimated_cost_usd=1.25 WHERE id='chosen'")
            # Changing the latest session must not redirect end to a different id.
            connection.execute("UPDATE sessions SET last_activity_at=100 WHERE id='old'")

    def write_ledger(self):
        def run(task="T-1", card=None, seconds=10, **changes):
            return {"ts": (START + dt.timedelta(seconds=seconds)).isoformat(), "task": task, "card_id": card,
                    "agent": "codex", "model": "gpt-6.1-sol", "input_tokens": 100, "output_tokens": 30,
                    "cached_tokens": 50, "cost_usd_list": None, "premium_requests": None,
                    "quota_after": {"five_hour_pct": 12, "weekly_pct": 3}, "duration_s": 5,
                    "status": "ok", **changes}
        rows = [
            run(seconds=-1), run(seconds=0), run(task="other", card="other"), run(seconds=121),
            run(),
            run(task="other", card="t_parent", seconds=20, agent="claude", model="claude-sonnet-5-5",
                input_tokens=10, output_tokens=20, cached_tokens=0, cost_usd_list=.125, quota_after=None),
            run(card="t_parent", seconds=30, input_tokens=20, output_tokens=10, cached_tokens=6,
                quota_after={"five_hour_pct": 20, "weekly_pct": 5}),
            run(task="retry", card="t_parent", seconds=40, agent="copilot", model="gpt-6-luna",
                input_tokens=5, output_tokens=5, cached_tokens=0, premium_requests=2,
                quota_after=None, status="failed"),
        ]
        (self.home / "ledger.jsonl").write_text("\n".join(json.dumps(row) for row in reversed(rows)) + "\n")

    def finish(self, *options):
        with mock.patch("task_cost.utc_now", return_value=END):
            return self.invoke(["end", "T-1", *options])

    def test_start_picks_most_recent_opus_and_explicit_session_overrides(self):
        self.assertEqual(self.invoke(["start", "T-1"])[0], 0)
        snapshot = json.loads((self.home / "state/task_costs/T-1.json").read_text())
        self.assertEqual(snapshot["ts"], START.isoformat())
        self.assertEqual(snapshot["session"]["id"], "chosen")
        self.assertEqual(snapshot["session"]["input_tokens"], 100)
        self.assertEqual(snapshot["session"]["estimated_cost_usd"], .5)
        self.assertEqual(self.invoke(["start", "T-2", "--session", "old"])[0], 0)
        self.assertEqual(json.loads((self.home / "state/task_costs/T-2.json").read_text())["session"]["id"], "old")
        self.process.assert_not_called()

    def test_start_uses_the_current_session_even_when_it_is_not_opus(self):
        with mock.patch.dict(os.environ, {"HERMES_SESSION_ID": "newer"}):
            self.assertEqual(self.invoke(["start", "T-S"])[0], 0)
        snapshot = json.loads((self.home / "state/task_costs/T-S.json").read_text())
        self.assertEqual((snapshot["session"]["id"], snapshot["session"]["model"]), ("newer", "claude-sonnet-5-5"))

    def test_start_without_session_env_picks_most_recent_claude_session_of_any_model(self):
        env = {k: v for k, v in os.environ.items() if k != "HERMES_SESSION_ID"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.invoke(["start", "T-N"])[0], 0)
        self.assertEqual(json.loads((self.home / "state/task_costs/T-N.json").read_text())["session"]["id"], "newer")

    def test_unknown_session_env_falls_back_to_most_recent_claude_session(self):
        with mock.patch.dict(os.environ, {"HERMES_SESSION_ID": "not-in-db"}):
            self.assertEqual(self.invoke(["start", "T-F"])[0], 0)
        self.assertEqual(json.loads((self.home / "state/task_costs/T-F.json").read_text())["session"]["id"], "newer")

    def test_non_claude_sessions_are_never_picked_by_default(self):
        with writable(self.db) as connection:
            connection.execute("INSERT INTO sessions VALUES ('qwen', 'qwen3.8:27b', 9, 9, 9, 9, 9, 0, 99, 0)")
        env = {k: v for k, v in os.environ.items() if k != "HERMES_SESSION_ID"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.invoke(["start", "T-Q"])
        self.assertEqual(json.loads((self.home / "state/task_costs/T-Q.json").read_text())["session"]["id"], "newer")

    def test_report_shows_both_models_when_the_session_model_changed_mid_task(self):
        self.invoke(["start", "T-M"])
        with writable(self.db) as connection:
            connection.execute("UPDATE sessions SET model='claude-sonnet-5-5', output_tokens=90 WHERE id='chosen'")
        code, out, err = self.invoke(["end", "T-M"])
        self.assertEqual(code, 0)
        self.assertIn("claude-opus-5-5\u2192claude-sonnet-5-5", out + err)

    def test_database_connection_is_readonly_and_no_database_created(self):
        connect = sqlite3.connect
        def checked_connect(database, **kwargs):
            self.assertTrue(database.endswith("?mode=ro"))
            self.assertIn("%3F%23", database)
            self.assertEqual(kwargs, {"uri": True})
            connection = connect(database, **kwargs)
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                connection.execute("DELETE FROM sessions")
            return connection
        with mock.patch("task_cost.sqlite3.connect", side_effect=checked_connect):
            self.assertEqual(task_cost.session_usage()["id"], "chosen")
        missing = self.home / "missing.db"
        with mock.patch("task_cost.SESSION_DB", missing):
            self.assertEqual(self.invoke(["start", "T-missing"])[0], 1)
        self.assertFalse(missing.exists())

    def test_end_deltas_attribution_report_json_and_card_comment(self):
        self.invoke(["start", "T-1"])
        self.advance()
        self.write_ledger()
        code, output, errors = self.finish("--card", "t_parent", "--outcome", "failed", "--notes", "retry exhausted")
        self.assertEqual((code, errors), (0, ""))
        row = json.loads((self.home / "task_costs.jsonl").read_text())
        orchestrator = row["orchestrator"]
        self.assertEqual([orchestrator[key] for key in task_cost.COUNTERS], [300, 60, 100, 10, 10, .75])
        self.assertEqual(orchestrator["session_id"], "chosen")
        self.assertEqual((row["delegate_tokens"], row["total_tokens"], row["duration_s"]), (200, 560, 120))
        self.assertAlmostEqual(row["delegation_share_pct"], 100 * 200 / 560)
        self.assertEqual((row["outcome"], row["notes"]), ("failed", "retry exhausted"))
        self.assertEqual(len(row["delegate_runs"]), 4)
        codex = next(group for group in row["delegates"] if group["agent"] == "codex")
        self.assertEqual((codex["runs"], codex["tokens"], codex["cached_tokens"], codex["cost_usd_list"]), (2, 160, 56, None))
        self.assertEqual(codex["quota_after"], {"five_hour_pct": 20, "weekly_pct": 5})
        copilot = next(group for group in row["delegates"] if group["agent"] == "copilot")
        self.assertEqual(copilot["status_counts"], {"failed": 1})
        for text in ("Orchestrator (claude-opus-5-5): 300/60 tok", "100 cached", "10 cache-write",
                     "10 reasoning", "~$0.750000", "codex·gpt-6.1-sol·-: 2 runs", "160 total",
                     "$0.125000", "premium 2", "Codex quota after 5h/wk 20/5%", "35.7%", "120.0s"):
            self.assertIn(text, output)
        self.assertEqual(self.process.call_args.args[0][1:], [str(task_cost.ROOT / "kb.py"), "note", "t_parent", row["report"]])

    def test_no_card_uses_only_task_and_makes_no_subprocess_call(self):
        self.invoke(["start", "T-1"])
        self.advance()
        self.write_ledger()
        self.assertEqual(self.finish()[0], 0)
        row = json.loads((self.home / "task_costs.jsonl").read_text())
        self.assertEqual(row["delegate_tokens"], 160)
        self.assertEqual(len(row["delegate_runs"]), 2)
        self.process.assert_not_called()

    def test_summary_window_and_totals(self):
        self.invoke(["start", "T-1"])
        self.advance()
        self.write_ledger()
        self.finish("--card", "t_parent")
        path = self.home / "task_costs.jsonl"
        row = json.loads(path.read_text())
        old = {**row, "task": "old", "ts": (END - dt.timedelta(days=8)).isoformat()}
        second = {**row, "task": "T-2", "outcome": "ok"}
        future = {**row, "task": "future", "ts": (END + dt.timedelta(days=1)).isoformat()}
        with path.open("a") as handle:
            for extra in (old, second, future):
                handle.write(json.dumps(extra) + "\n")
        with mock.patch("task_cost.utc_now", return_value=END):
            code, output, errors = self.invoke(["summary"])
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("| T-1 | ok | 360 | 200 | $0.750000 | $0.125000 | 2 | 120.0 |", output)
        self.assertIn("**Totals (2 tasks)** | | 720 | 400 | $1.500000 | $0.250000 | 4 | 240.0", output)
        self.assertNotIn("| old |", output)
        self.assertNotIn("| future |", output)

    def test_zero_usage_and_empty_summary(self):
        self.assertIn("Totals (0 tasks)", self.invoke(["summary"])[1])
        self.invoke(["start", "T-1"])
        code, output, _ = self.finish()
        self.assertEqual(code, 0)
        self.assertIn("Delegates: none", output)
        self.assertIn("Delegation share: 0.0%", output)

    def test_unknown_and_partial_costs_are_not_reported_as_free(self):
        rows = [{"agent": "claude", "model": "m", "ts": START.isoformat(), "cost_usd_list": cost}
                for cost in (None, 0.0, .25)]
        groups = task_cost.aggregate_delegates(rows)
        self.assertEqual((groups[0]["cost_usd_list"], groups[0]["costed_runs"]), (.25, 2))
        self.invoke(["start", "T-1"])
        _, output, _ = self.finish()
        row = json.loads((self.home / "task_costs.jsonl").read_text())
        row["delegates"] = groups
        self.assertIn("$0.250000 (+1 unpriced runs)", task_cost.format_report(row))
        groups[0]["cost_usd_list"] = None
        groups[0]["costed_runs"] = 0
        self.assertIn(", -, premium -", task_cost.format_report(row))

    def test_missing_snapshot_session_and_invalid_ids_fail_cleanly(self):
        for argv in (["end", "missing"], ["start", "missing", "--session", "absent"],
                     ["start", "../outside"], ["start", "/absolute"], ["summary", "--days", "-1"]):
            with self.subTest(argv=argv):
                code, output, errors = self.invoke(argv)
                self.assertEqual((code, output), (1, ""))
                self.assertIn("task_cost:", errors)
        self.process.assert_not_called()

    def test_start_does_not_overwrite_existing_snapshot(self):
        self.invoke(["start", "T-1"])
        original = task_cost.snapshot_path("T-1").read_bytes()
        self.advance()
        self.assertEqual(self.invoke(["start", "T-1"])[0], 1)
        self.assertEqual(task_cost.snapshot_path("T-1").read_bytes(), original)

    def test_card_comment_failure_keeps_local_report_and_returns_error(self):
        self.invoke(["start", "T-1"])
        self.process.return_value = subprocess.CompletedProcess([], 1, "", "board offline")
        code, output, errors = self.finish("--card", "t_parent")
        self.assertEqual(code, 1)
        self.assertIn("Orchestrator", output)
        self.assertIn("report saved, but card comment failed: board offline", errors)
        self.assertEqual(len(task_cost.read_jsonl(self.home / "task_costs.jsonl")), 1)

    def test_malformed_ledger_fails_without_appending_report(self):
        self.invoke(["start", "T-1"])
        (self.home / "ledger.jsonl").write_text('{"ts":"bad"}\nnot JSON\n')
        code, _, errors = self.finish()
        self.assertEqual(code, 1)
        self.assertIn("ledger.jsonl:2", errors)
        self.assertFalse((self.home / "task_costs.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
