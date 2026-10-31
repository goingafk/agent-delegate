from __future__ import annotations

import contextlib
import io
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import kb


class KanbanTests(unittest.TestCase):
    def setUp(self):
        process_patch = mock.patch("kb.subprocess.run")
        self.process = process_patch.start()
        self.addCleanup(process_patch.stop)
        which_patch = mock.patch("kb.shutil.which", return_value="/custom/bin/hermes")
        which_patch.start()
        self.addCleanup(which_patch.stop)
        self.process.return_value = subprocess.CompletedProcess([], 0, '{"id":"t_123"}', "")

    def invoke(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
            code = kb.main(argv)
        for call in self.process.call_args_list:
            self.assertEqual(call.args[0][:4], ["/custom/bin/hermes", "kanban", "--board", "agents"])
            self.assertNotIn("--assignee", call.args[0])
            self.assertNotIn("--parent", call.args[0])
            self.assertEqual(call.kwargs, {"capture_output": True, "text": True, "timeout": 20})
        return code, output.getvalue(), errors.getvalue()

    def test_start_creates_unassigned_then_claims_and_prints_only_id(self):
        code, output, errors = self.invoke([
            "start", "[Codey · claude-opus-5-5] A title", "--model", "claude-opus-5-5",
            "--provider", "anthropic", "--body", "two words\nnext line", "--ttl", "300",
        ])
        self.assertEqual((code, output, errors), (0, "t_123\n", ""))
        create, claim = [call.args[0][4:] for call in self.process.call_args_list]
        self.assertEqual(create, ["create", "[Codey · claude-opus-5-5] A title", "--model", "claude-opus-5-5",
                                  "--created-by", "delegate", "--json", "--provider", "anthropic",
                                  "--body", "two words\nnext line"])
        self.assertEqual(claim, ["claim", "t_123", "--ttl", "300"])

    def test_default_ttl(self):
        self.assertEqual(self.invoke(["start", "title", "--model", "m"])[0], 0)
        self.assertEqual(self.process.call_args.args[0][-2:], ["--ttl", "14400"])

    def test_note_done_and_block_argv(self):
        for verb, expected in (
            ("note", ["comment", "t_123", "some text"]),
            ("done", ["complete", "t_123", "--force", "--summary", "some text"]),
            ("block", ["block", "t_123", "some text"]),
        ):
            with self.subTest(verb=verb):
                self.process.reset_mock()
                self.assertEqual(self.invoke([verb, "t_123", "some text"]), (0, "", ""))
                self.assertEqual(self.process.call_args.args[0][4:], expected)

    def test_fallback_executable_is_absolute(self):
        with mock.patch("kb.shutil.which", return_value=None):
            self.assertEqual(kb.main(["note", "t_123", "text"]), 0)
        self.assertEqual(self.process.call_args.args[0][0],
                         str(Path.home() / ".hermes/hermes-agent/.hermes/bin/hermes"))

    def test_create_failure_or_invalid_reply_does_not_claim(self):
        for result in (
            subprocess.CompletedProcess([], 3, "", "board unavailable"),
            subprocess.CompletedProcess([], 0, "not json", ""),
            subprocess.CompletedProcess([], 0, "[]", ""),
            subprocess.CompletedProcess([], 0, '{"id":null}', ""),
        ):
            with self.subTest(result=result):
                self.process.reset_mock()
                self.process.return_value = result
                code, output, errors = self.invoke(["start", "title", "--model", "m"])
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertIn("kb:", errors)
                self.assertEqual(self.process.call_count, 1)

    def test_claim_failure_identifies_created_card_without_success_output(self):
        self.process.side_effect = [self.process.return_value, subprocess.CompletedProcess([], 1, "", "busy")]
        code, output, errors = self.invoke(["start", "title", "--model", "m"])
        self.assertEqual((code, output), (1, ""))
        self.assertIn("t_123 created, but claim failed", errors)
        self.assertIn("busy", errors)

    def test_missing_executable_timeout_and_nonzero_exit_are_errors(self):
        for failure in (FileNotFoundError("no hermes"), subprocess.TimeoutExpired("hermes", 20)):
            self.process.side_effect = failure
            self.assertEqual(self.invoke(["note", "t_123", "text"])[0], 1)
        self.process.side_effect = None
        self.process.return_value = subprocess.CompletedProcess([], 1, "", "could not complete")
        code, _, errors = self.invoke(["done", "t_123", "summary"])
        self.assertEqual(code, 1)
        self.assertIn("could not complete", errors)

    def test_invalid_ttl_makes_no_subprocess_call(self):
        self.assertEqual(self.invoke(["start", "title", "--model", "m", "--ttl", "0"])[0], 1)
        self.process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
