from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import spec

AGENTS = ("qwen", "codex", "claude", "copilot")


class SpecTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def good(self, **changes):
        value = {"task": "age-sql", "agent": "codex", "model": "gpt-6.1-sol", "reason": "scoped migration",
                 "goal": "Add declare_age(year, month).", "workdir": str(self.dir), "mode": "implement",
                 "files": ["supabase/migrations/*.sql"], "verify": "true",
                 "acceptance": ["harness passes"]}
        value.update(changes)
        return value

    def lint(self, value):
        return spec.lint(value, AGENTS)

    def test_a_complete_spec_has_no_errors(self):
        errors, warnings = self.lint(self.good())
        self.assertEqual((errors, warnings), ([], []))

    def test_load_reads_toml_and_reports_bad_files_plainly(self):
        path = self.dir / "s.toml"
        path.write_text('task = "t"\ngoal = "g"\n[worktree]\nbranch = "agent/x"\n')
        loaded = spec.load_spec(path)
        self.assertEqual(loaded["worktree"]["branch"], "agent/x")
        path.write_text("task = ")
        with self.assertRaises(spec.SpecError) as ctx:
            spec.load_spec(path)
        self.assertIn("s.toml", str(ctx.exception))
        with self.assertRaises(spec.SpecError):
            spec.load_spec(self.dir / "missing.toml")

    def test_missing_required_fields_are_listed_together(self):
        errors, _ = self.lint({"task": "t"})
        text = " ".join(errors)
        for field in ("agent", "model", "reason", "goal", "workdir"):
            self.assertIn(field, text)

    def test_unknown_keys_are_rejected_so_typos_do_not_pass_silently(self):
        errors, _ = self.lint(self.good(verfiy="true"))
        self.assertTrue(any("verfiy" in e for e in errors))

    def test_agent_must_be_known_and_mode_valid(self):
        self.assertTrue(any("agent" in e for e in self.lint(self.good(agent="gpt"))[0]))
        self.assertTrue(any("mode" in e for e in self.lint(self.good(mode="yolo"))[0]))
        self.assertTrue(any("risk" in e for e in self.lint(self.good(risk="medium"))[0]))

    def test_implement_needs_a_file_scope_and_a_verify_command(self):
        errors, _ = self.lint(self.good(files=[]))
        self.assertTrue(any("files" in e for e in errors))
        errors, _ = self.lint(self.good(verify=""))
        self.assertTrue(any("verify" in e for e in errors))
        errors, _ = self.lint(self.good(mode="review", files=[], verify=""))
        self.assertEqual(errors, [])

    def test_scope_globs_must_be_relative_and_stay_inside_the_repo(self):
        for bad in ("/etc/passwd", "../x", "a/../../b", "~/x"):
            errors, _ = self.lint(self.good(files=[bad]))
            self.assertTrue(any("files" in e for e in errors), bad)

    def test_secret_looking_text_is_an_error_anywhere_in_the_spec(self):
        samples = ["-----BEGIN PRIVATE KEY-----", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc.def", "sk_live_abcdefghijklmnop",
                   "ghp_abcdefghijklmnopqrstuvwxyz0123456789", "AKIAABCDEFGHIJKLMNOP", "api_key = 'abcdef1234567890abcd'"]
        for sample in samples:
            for field, value in (("goal", f"use {sample}"), ("context", sample), ("verify", f"curl -H {sample}")):
                errors, _ = self.lint(self.good(**{field: value}))
                self.assertTrue(any("secret" in e for e in errors), (field, sample))
        errors, _ = self.lint(self.good(goal="Names only: SUPABASE_SERVICE_ROLE_KEY and ASC_KEY_ID are secrets set elsewhere."))
        self.assertEqual(errors, [])

    def test_qwen_cannot_open_repo_files_so_read_instructions_are_rejected(self):
        errors, _ = self.lint(self.good(agent="qwen", model="qwen3.8:27b", mode="review", goal="Read file lib/main.dart and explain it"))
        self.assertTrue(any("qwen" in e.lower() and "paste" in e.lower() for e in errors))
        errors, _ = self.lint(self.good(agent="qwen", model="qwen3.8:27b", mode="review", goal="Explain this function:\n\ndef f(): pass"))
        self.assertEqual(errors, [])

    def test_workdir_must_exist(self):
        errors, _ = self.lint(self.good(workdir=str(self.dir / "nope")))
        self.assertTrue(any("workdir" in e for e in errors))

    def test_worktree_needs_a_safe_unprotected_branch(self):
        self.assertEqual(self.lint(self.good(worktree={"branch": "agent/age-sql", "base": "main"}))[0], [])
        for bad in ({"base": "main"}, {"branch": "main"}, {"branch": "production"}, {"branch": "bad name"},
                    {"branch": "../x"}, {"branch": "-rf"}):
            errors, _ = self.lint(self.good(worktree=bad))
            self.assertTrue(any("worktree" in e for e in errors), bad)

    def test_warnings_for_missing_acceptance_and_oversized_goal(self):
        _, warnings = self.lint(self.good(acceptance=[]))
        self.assertTrue(any("acceptance" in w for w in warnings))
        _, warnings = self.lint(self.good(goal="x" * 7000))
        self.assertTrue(any("long" in w for w in warnings))

    def test_render_brief_has_the_standard_sections_in_a_fixed_order(self):
        text = spec.render_brief(self.good(context="Staging has the 16 Oct migrations.", preserve=["old RPC stays"],
                                           constraints=["no new tables"], tests_first="Write harness cases first."),
                                 spec.DEFAULT_STANDING)
        order = ["# Task: age-sql", "## Goal", "## Context", "## Preserve", "## Files you may change", "## Tests first",
                 "## Constraints", "## Acceptance criteria", "## Verification", "## Report"]
        positions = [text.index(h) for h in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("supabase/migrations/*.sql", text)
        self.assertIn("no new tables", text)
        self.assertIn("`true`", text)
        for line in spec.DEFAULT_STANDING:
            self.assertIn(line, text)

    def test_render_omits_empty_optional_sections(self):
        text = spec.render_brief(self.good(), spec.DEFAULT_STANDING)
        for heading in ("## Context", "## Preserve", "## Tests first"):
            self.assertNotIn(heading, text)

    def test_review_mode_brief_asks_for_findings_not_changes(self):
        text = spec.render_brief(self.good(mode="review", files=[], verify=""), spec.DEFAULT_STANDING)
        self.assertIn("read-only", text.lower())
        self.assertNotIn("## Files you may change", text)
        self.assertIn("severity", text.lower())

    def test_default_standing_rules_cover_the_owner_constraints(self):
        joined = " ".join(spec.DEFAULT_STANDING).lower()
        for needle in ("staging", "push", "production", "secret", "unrelated", "commit", "do not claim"):
            self.assertIn(needle, joined)


if __name__ == "__main__":
    unittest.main()
