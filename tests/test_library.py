from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import library
import spec

AGENTS = ("qwen", "codex", "claude", "copilot")


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        (self.dir / "tdd.md").write_text("---\nname: tdd\ndescription: Tests first.\n---\n# TDD\nWrite a failing test.\n")
        (self.dir / "big.md").write_text("---\nname: big\ndescription: Large.\n---\n" + "x" * 5000)
        (self.dir / "broken.md").write_text("no frontmatter here\n")

    def test_list_skips_files_without_frontmatter(self):
        names = [s.name for s in library.list_skills(self.dir)]
        self.assertEqual(names, ["big", "tdd"])

    def test_render_strips_frontmatter_and_titles_each_playbook(self):
        text = library.render(["tdd"], self.dir, max_chars=1000)
        self.assertIn("## Playbooks", text)
        self.assertIn("### tdd", text)
        self.assertIn("Write a failing test.", text)
        self.assertNotIn("description:", text)

    def test_unknown_name_is_an_error(self):
        with self.assertRaises(library.LibraryError):
            library.render(["nope"], self.dir, max_chars=1000)

    def test_over_budget_is_an_error_not_silent_truncation(self):
        with self.assertRaises(library.LibraryError):
            library.render(["big", "tdd"], self.dir, max_chars=1000)

    def test_duplicates_collapse_and_empty_is_blank(self):
        text = library.render(["tdd", "tdd"], self.dir, max_chars=1000)
        self.assertEqual(text.count("### tdd"), 1)
        self.assertEqual(library.render([], self.dir, max_chars=1000), "")

    def test_names_cannot_escape_the_library(self):
        with self.assertRaises(library.LibraryError):
            library.render(["../tdd"], self.dir, max_chars=1000)

    def test_lint_checks_skill_names_and_type(self):
        base = {"task": "t", "agent": "codex", "model": "m", "reason": "r", "goal": "g", "workdir": str(self.dir)}
        errors, _ = spec.lint({**base, "skills": "tdd"}, AGENTS)
        self.assertTrue(any("skills must be a list" in e for e in errors))
        errors, _ = spec.lint({**base, "skills": ["tdd-red-green"]}, AGENTS)
        self.assertEqual(errors, [])

    def test_render_brief_includes_playbooks(self):
        data = {"task": "t", "goal": "g", "mode": "review"}
        out = spec.render_brief(data, ("rule",), playbooks="## Playbooks\n### tdd\nbody")
        self.assertIn("### tdd", out)
        self.assertLess(out.index("## Constraints"), out.index("## Playbooks"))


if __name__ == "__main__":
    unittest.main()


class WrapperIntegrationTests(unittest.TestCase):
    def test_spec_brief_gets_defaults_plus_requested_and_empty_list_opts_out(self):
        import delegate
        caps = delegate.load_caps()
        data = {"task": "t", "goal": "g", "mode": "implement", "skills": ["tdd-red-green"]}
        out = delegate.render_spec_brief(data, caps)
        self.assertIn("### engineering-discipline", out)
        self.assertIn("### tdd-red-green", out)
        off = delegate.render_spec_brief({**data, "skills": []}, caps)
        self.assertNotIn("## Playbooks", off)
        review = delegate.render_spec_brief({"task": "t", "goal": "g", "mode": "review"}, caps)
        self.assertIn("### review-findings", review)


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def test_save_writes_a_listable_playbook_and_refuses_silent_overwrite(self):
        path = library.save("my-skill", "Does a thing.", "- step one\n- step two", self.dir)
        self.assertTrue(path.is_file())
        self.assertEqual([s.name for s in library.list_skills(self.dir)], ["my-skill"])
        with self.assertRaises(library.LibraryError):
            library.save("my-skill", "Again.", "- changed", self.dir)
        library.save("my-skill", "Again.", "- changed", self.dir, overwrite=True)
        self.assertIn("changed", library.list_skills(self.dir)[0].body)

    def test_save_rejects_bad_names_oversize_empty_and_secrets(self):
        for args in (("../x", "d", "b"), ("Bad Name", "d", "b"), ("ok", "", "b"), ("ok", "d", ""),
                     ("ok", "d", "x" * (library.MAX_SKILL_CHARS + 1)),
                     ("ok", "line1\nline2", "b"),
                     ("ok", "d", "token = abcdefghijklmnop1234567890")):
            with self.assertRaises(library.LibraryError, msg=args):
                library.save(*args, self.dir)
        self.assertEqual(list(self.dir.glob("*.md")), [])

    def test_body_cannot_forge_frontmatter(self):
        library.save("fm", "Desc.", "---\nname: evil\n---\nbody", self.dir)
        self.assertEqual([s.name for s in library.list_skills(self.dir)], ["fm"])
