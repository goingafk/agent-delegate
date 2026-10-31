from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock
import unittest

import delegate
import spec as jobspec
from test_delegate import SpecHelpers, TempHomeTest, fixture, job_record, run_args


class SessionIdTests(unittest.TestCase):
    def test_session_ids_are_parsed_from_every_agent(self):
        self.assertEqual(delegate.parse_claude_output(fixture("claude_stream.jsonl"))["session_id"], "00000000-0000-4000-8000-000000000003")
        self.assertEqual(delegate.parse_codex_output(fixture("codex_exec.jsonl"))["session_id"], "00000000-0000-4000-8000-000000000016")
        self.assertEqual(delegate.parse_copilot_output(fixture("copilot.jsonl"))["session_id"], "00000000-0000-4000-8000-000000000044")

    def test_no_session_id_is_none(self):
        self.assertIsNone(delegate.parse_codex_output("")["session_id"])
        self.assertIsNone(delegate.parse_qwen_output("x", "")["session_id"] if "session_id" in delegate.parse_qwen_output("x", "") else None)


class ArgvTests(TempHomeTest):
    def argv(self, agent, model, mode="implement", **kw):
        return delegate.build_argv(agent, model, mode, self.home, self.home / "run.jsonl", "brief", **kw)

    def test_codex_resume_uses_exec_resume_with_the_session_id_and_keeps_the_sandbox(self):
        argv = self.argv("codex", "gpt-6.1-sol", resume="sess-1")
        self.assertEqual(argv[:4], ["codex", "exec", "resume", "sess-1"])
        self.assertEqual(argv[-1], "-")
        self.assertIn("--json", argv)
        self.assertNotIn("--sandbox", argv)  # resume does not accept it; the policy goes in via -c
        self.assertIn('sandbox_mode="workspace-write"', argv)
        self.assertIn('sandbox_mode="read-only"', self.argv("codex", "gpt-6.1-sol", "review", resume="sess-1"))
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.1-sol")

    def test_codex_resume_still_disables_connected_apps_and_plugins(self):
        argv = " ".join(self.argv("codex", "gpt-6.1-sol", resume="s"))
        self.assertIn("features.apps=false", argv)
        self.assertIn("features.computer_use=false", argv)

    def test_claude_resume_and_persistence(self):
        resumed = self.argv("claude", "claude-opus-5-5", resume="sess-2")
        self.assertEqual(resumed[resumed.index("--resume") + 1], "sess-2")
        self.assertNotIn("--no-session-persistence", resumed)
        self.assertNotIn("--no-session-persistence", self.argv("claude", "claude-opus-5-5"))  # persisted so it can be resumed
        self.assertIn("--no-session-persistence", self.argv("claude", "claude-opus-5-5", persist=False))

    def test_copilot_resume(self):
        self.assertIn("--resume=sess-3", self.argv("copilot", "gpt-6-luna", resume="sess-3"))
        self.assertFalse(any(a.startswith("--resume") for a in self.argv("copilot", "gpt-6-luna")))

    def test_budget_applies_to_claude_only(self):
        argv = self.argv("claude", "claude-sonnet-5-5", budget_usd=3.5)
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "3.5")
        self.assertNotIn("--max-budget-usd", self.argv("claude", "claude-sonnet-5-5"))
        self.assertNotIn("--max-budget-usd", self.argv("codex", "gpt-6.1-sol", budget_usd=3.5))

    def test_findings_schema_is_passed_to_claude_and_codex(self):
        claude = self.argv("claude", "claude-opus-5-5", "review", schema=jobspec.SCHEMAS["findings"])
        schema = json.loads(claude[claude.index("--json-schema") + 1])
        self.assertIn("findings", schema["properties"])
        codex = self.argv("codex", "gpt-6.1-sol", "review", schema=jobspec.SCHEMAS["findings"])
        path = Path(codex[codex.index("--output-schema") + 1])
        self.assertTrue(path.exists())
        self.assertIn("findings", json.loads(path.read_text())["properties"])
        self.assertNotIn("--output-schema", self.argv("codex", "gpt-6.1-sol", "review"))


class BudgetDefaultTests(TempHomeTest):
    def dry_argv(self, **changes):
        args = run_args(self.home, agent="claude", model="claude-sonnet-5-5", dry_run=True, **changes)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("delegate.claude_identity", return_value=None):
            delegate.run_command(args)
        return out.getvalue()

    def test_claude_runs_get_the_default_budget_from_caps(self):
        text = self.dry_argv()
        self.assertIn("--max-budget-usd 8", text)

    def test_an_explicit_budget_wins(self):
        self.assertIn("--max-budget-usd 2.5", self.dry_argv(budget_usd=2.5))

    def test_lint_checks_budget(self):
        base = {"task": "t", "agent": "claude", "model": "claude-sonnet-5-5", "reason": "r", "goal": "g", "workdir": str(self.home)}
        self.assertEqual(jobspec.lint({**base, "budget_usd": 3}, delegate.AGENTS)[0], [])
        self.assertTrue(jobspec.lint({**base, "budget_usd": -1}, delegate.AGENTS)[0])
        self.assertTrue(jobspec.lint({**base, "budget_usd": "lots"}, delegate.AGENTS)[0])
        _, warnings = jobspec.lint({**base, "agent": "codex", "model": "gpt-6.1-sol", "budget_usd": 3}, delegate.AGENTS)
        self.assertTrue(any("budget_usd" in w and "claude" in w for w in warnings))

    def test_resolve_takes_budget_from_the_spec_and_the_flag(self):
        helper = SpecHelpers()
        helper.home = self.home
        path = helper.write_spec(budget_usd=4)
        args = delegate.make_parser().parse_args(["start", "--spec", path])
        delegate.resolve_run_args(args)
        self.assertEqual(args.budget_usd, 4)
        args = delegate.make_parser().parse_args(["start", "--spec", path, "--budget-usd", "1.5"])
        delegate.resolve_run_args(args)
        self.assertEqual(args.budget_usd, 1.5)


class RunSessionTests(TempHomeTest):
    def codex_run(self):
        stream = fixture("codex_exec.jsonl")

        def fake(argv, **kwargs):
            if argv[0] == "git":
                return subprocess.CompletedProcess(argv, 0, "feature/test\n", "")
            return subprocess.CompletedProcess(argv, 0, stream, "")
        args = run_args(self.home, agent="codex", model="gpt-6.1-sol")
        with mock.patch("delegate.subprocess.run", side_effect=fake), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = delegate.run_command(args)
        return code, args

    def test_session_id_reaches_the_args_the_ledger_and_the_job_record(self):
        code, args = self.codex_run()
        self.assertEqual(code, 0)
        self.assertEqual(args.session_id, "00000000-0000-4000-8000-000000000016")
        self.assertEqual(delegate.read_ledger()[0]["session_id"], "00000000-0000-4000-8000-000000000016")
        record = job_record()
        delegate.jobs.write_job(self.home, record)

        def fake(a):
            a.session_id = "sid-9"
            return 0
        with mock.patch("delegate.run_command", side_effect=fake):
            delegate.job_runner(record["id"])
        self.assertEqual(delegate.jobs.read_job(self.home, record["id"])["session_id"], "sid-9")


class ResumeCommandTests(TempHomeTest):
    def finished(self, **changes):
        record = job_record(status="done", exit_code=0, session_id="sess-1", task="T-1")
        record["args"].update(brief_file=str(self.home / "brief.txt"), workdir=str(self.home), verify="true", files=["app/**"])
        record.update(changes)
        delegate.jobs.write_job(self.home, record)
        return record["id"]

    def resume(self, job_id, *words):
        captured = {}

        def fake_start(args):
            captured["args"] = args
            return 0
        with mock.patch("delegate.start_command", side_effect=fake_start), contextlib.redirect_stdout(io.StringIO()):
            code = delegate.main(["resume", job_id, *words])
        return code, captured.get("args")

    def test_resume_starts_a_follow_up_job_in_the_same_session_and_setup(self):
        code, args = self.resume(self.finished(), "fix", "the", "failing", "test")
        self.assertEqual(code, 0)
        self.assertEqual((args.resume_session, args.agent, args.model, args.task), ("sess-1", "codex", "gpt-6.1-sol", "T-1-r2"))
        self.assertEqual((args.verify, args.files, args.workdir), ("true", ["app/**"], str(self.home)))
        self.assertIn("fix the failing test", Path(args.brief_file).read_text())

    def test_resume_drops_the_structured_schema_unless_asked_for_again(self):
        job_id = self.finished()
        record = delegate.jobs.read_job(self.home, job_id)
        record["args"]["schema"] = "findings"
        delegate.jobs.write_job(self.home, record)
        self.assertIsNone(self.resume(job_id, "plain", "text", "please")[1].schema)
        captured = {}
        with mock.patch("delegate.start_command", side_effect=lambda a: captured.setdefault("a", a) and 0), contextlib.redirect_stdout(io.StringIO()):
            delegate.main(["resume", job_id, "again", "--schema", "findings"])
        self.assertEqual(captured["a"].schema, "findings")

    def test_resume_numbers_the_rounds(self):
        _, args = self.resume(self.finished(task="T-1-r2"), "again")
        self.assertEqual(args.task, "T-1-r3")

    def test_resume_refuses_without_a_session_or_while_running_or_for_unknown_jobs(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.resume(self.finished(session_id=None), "x")[0], delegate.BAD_USAGE)
            delegate.jobs.write_job(self.home, job_record("j-run", session_id="s"))
            self.assertEqual(self.resume("j-run", "x")[0], delegate.BAD_USAGE)
            self.assertEqual(self.resume("nope", "x")[0], delegate.BAD_USAGE)

    def test_resume_end_to_end_creates_a_job_that_carries_the_session(self):
        process = mock.Mock()
        process.pid = os.getpid()
        job_id = self.finished()
        with mock.patch("delegate.launch_process", return_value=process), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(delegate.main(["resume", job_id, "please", "continue", "--no-card"]), 0)
        newest = next(r for r in delegate.jobs.list_jobs(self.home) if r["id"] != job_id)
        self.assertEqual(newest["args"]["resume_session"], "sess-1")

    def test_a_resumed_run_builds_its_command_with_the_session(self):
        args = run_args(self.home, agent="codex", model="gpt-6.1-sol", dry_run=True, resume_session="sess-1")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            delegate.run_command(args)
        self.assertIn("exec resume sess-1", out.getvalue())


FINDINGS = {"summary": "two issues", "findings": [
    {"severity": "high", "file": "a.py", "line": 3, "evidence": "no auth check", "fix": "add one"},
    {"severity": "low", "file": "b.py", "line": 9, "evidence": "typo", "fix": "fix typo"}]}


class FindingsTests(TempHomeTest):
    def test_schema_registry_and_spec_lint(self):
        self.assertEqual(jobspec.SCHEMAS["findings"]["required"], ["summary", "findings"])
        base = {"task": "t", "agent": "codex", "model": "gpt-6.1-sol", "reason": "r", "goal": "g", "workdir": str(self.home)}
        self.assertEqual(jobspec.lint({**base, "schema": "findings"}, delegate.AGENTS)[0], [])
        self.assertTrue(any("schema" in e for e in jobspec.lint({**base, "schema": "nope"}, delegate.AGENTS)[0]))

    def test_brief_asks_for_the_json_format_for_every_agent(self):
        text = jobspec.render_brief({"task": "t", "goal": "g", "mode": "review", "schema": "findings"}, jobspec.DEFAULT_STANDING)
        self.assertIn("## Output format", text)
        self.assertIn("severity", text)
        self.assertIn('"findings"', text)

    def test_extract_handles_plain_fenced_and_embedded_json(self):
        body = json.dumps(FINDINGS)
        for text in (body, f"```json\n{body}\n```", f"Here you go:\n{body}\nHope that helps."):
            result = delegate.extract_findings(text)
            self.assertEqual(result["counts"], {"high": 1, "medium": 0, "low": 1})
            self.assertEqual(result["items"][0]["file"], "a.py")
            self.assertEqual(result["summary"], "two issues")

    def test_extract_rejects_garbage_and_normalises_odd_items(self):
        self.assertIsNone(delegate.extract_findings("no json here"))
        self.assertIsNone(delegate.extract_findings(json.dumps({"other": 1})))
        odd = delegate.extract_findings(json.dumps({"summary": "s", "findings": [{"severity": "CRITICAL", "file": "x"}, "bad", {"severity": "medium"}]}))
        self.assertEqual(odd["counts"]["medium"], 1)
        self.assertEqual(len(odd["items"]), 2)  # the string entry is dropped
        self.assertEqual(odd["items"][0]["severity"], "high")  # unknown severities are treated as the most severe

    def test_claude_structured_output_becomes_the_result(self):
        stream = json.dumps({"type": "result", "result": "", "structured_output": FINDINGS, "modelUsage": {"claude-opus-5-5": {}},
                             "usage": {}, "session_id": "s"})
        self.assertEqual(json.loads(delegate.parse_claude_output(stream)["result"])["summary"], "two issues")

    def test_run_collects_findings_into_args_summary_and_a_file(self):
        stream = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "t1"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(FINDINGS)}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}), ""])

        def fake(argv, **kwargs):
            if argv[0] == "git":
                return subprocess.CompletedProcess(argv, 0, "feature/test\n", "")
            return subprocess.CompletedProcess(argv, 0, stream, "")
        args = run_args(self.home, agent="codex", model="gpt-6.1-sol", schema="findings")
        err = io.StringIO()
        with mock.patch("delegate.subprocess.run", side_effect=fake), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = delegate.run_command(args)
        self.assertEqual(code, 0)
        self.assertEqual(args.findings["counts"], {"high": 1, "medium": 0, "low": 1})
        self.assertIn("findings=H1/M0/L1", err.getvalue())
        self.assertEqual(json.loads(Path(args.findings_file).read_text())["items"][1]["file"], "b.py")

    def test_job_record_and_format_show_findings(self):
        record = job_record()
        delegate.jobs.write_job(self.home, record)

        def fake(a):
            a.findings = delegate.extract_findings(json.dumps(FINDINGS))
            print(json.dumps(FINDINGS))
            return 0
        with mock.patch("delegate.run_command", side_effect=fake):
            delegate.job_runner(record["id"])
        saved = delegate.jobs.read_job(self.home, record["id"])
        text = delegate.format_job(saved, 0)
        self.assertIn("findings: 1 high, 0 medium, 1 low", text)
        self.assertNotIn('"summary"', delegate.format_job(saved, 30))  # raw JSON is not echoed into the reader's context
        self.assertIn("[high] a.py:3 no auth check", text)


class DoctorTests(TempHomeTest):
    def setUp(self):
        super().setUp()
        os.environ["DELEGATE_PREFLIGHT"] = __file__
        self.addCleanup(os.environ.pop, "DELEGATE_PREFLIGHT", None)

    def test_doctor_combines_preflight_account_jobs_runs_and_worktrees(self):
        delegate.jobs.write_job(self.home, job_record("j-run"))
        delegate.jobs.write_job(self.home, job_record("j-lost", status="lost"))
        (self.home / "runs").mkdir()
        (self.home / "runs" / "a.jsonl").write_text("x" * 1000)
        out = io.StringIO()
        fake = subprocess.CompletedProcess([], 0, "Delegate preflight (session start): all OK.\n- codex 1.0: logged in\n", "")
        with mock.patch("delegate.subprocess.run", return_value=fake), \
                mock.patch("delegate.claude_identity", return_value={"account": "aaaaaaaaaaaa", "plan": "pro"}), \
                mock.patch("delegate.gitwork.list_worktrees", return_value=[{"branch": "agent/x", "dirty": True, "path": "p", "head": "h"}]), \
                contextlib.redirect_stdout(out):
            code = delegate.main(["doctor"])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("all OK", text)
        self.assertIn("account=aaaaaaaaaaaa plan=pro", text)
        self.assertIn("jobs: 1 active, 1 lost", text)
        self.assertIn("runs: 1 file", text)
        self.assertIn("worktrees: 1 (1 dirty)", text)

    def test_doctor_returns_nonzero_when_preflight_reports_problems(self):
        fake = subprocess.CompletedProcess([], 0, "Delegate preflight (session start): needs attention: claude.\n", "")
        with mock.patch("delegate.subprocess.run", return_value=fake), mock.patch("delegate.claude_identity", return_value=None), \
                mock.patch("delegate.gitwork.list_worktrees", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(delegate.main(["doctor"]), delegate.BLOCKED)


if __name__ == "__main__":
    unittest.main()
