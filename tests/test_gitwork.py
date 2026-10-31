from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

import gitwork


def git(cwd, *args):
    out = subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
                         cwd=cwd, capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError(f"git {args}: {out.stderr}")
    return out.stdout.strip()


class GlobTests(unittest.TestCase):
    def test_single_star_stays_inside_one_directory(self):
        self.assertTrue(gitwork.glob_match("supabase/migrations/*.sql", "supabase/migrations/a.sql"))
        self.assertFalse(gitwork.glob_match("supabase/migrations/*.sql", "supabase/migrations/old/a.sql"))
        self.assertFalse(gitwork.glob_match("supabase/migrations/*.sql", "supabase/migrations/a.sqlx"))

    def test_double_star_crosses_directories(self):
        self.assertTrue(gitwork.glob_match("lib/**", "lib/a/b/c.dart"))
        self.assertTrue(gitwork.glob_match("lib/**/*.dart", "lib/a/b/c.dart"))
        self.assertTrue(gitwork.glob_match("lib/**/*.dart", "lib/c.dart"))
        self.assertFalse(gitwork.glob_match("lib/**/*.dart", "test/c.dart"))

    def test_trailing_slash_means_everything_under_it_and_spaces_are_fine(self):
        self.assertTrue(gitwork.glob_match("My App/lib/", "My App/lib/x/y.dart"))
        self.assertFalse(gitwork.glob_match("My App/lib/", "My App/test/y.dart"))

    def test_exact_paths_and_question_mark_and_regex_characters_are_literal(self):
        self.assertTrue(gitwork.glob_match("a/b.txt", "a/b.txt"))
        self.assertFalse(gitwork.glob_match("a/b.txt", "a/bxtxt"))
        self.assertTrue(gitwork.glob_match("a/?.txt", "a/b.txt"))
        self.assertFalse(gitwork.glob_match("a/?.txt", "a/bb.txt"))
        self.assertFalse(gitwork.glob_match("a+b/(x).txt", "aab/x.txt"))

    def test_out_of_scope_lists_only_files_no_pattern_covers(self):
        changed = ["lib/a.dart", "lib/x/b.dart", "pubspec.lock", "docs/readme.md"]
        self.assertEqual(gitwork.out_of_scope(changed, ["lib/**"]), ["pubspec.lock", "docs/readme.md"])
        self.assertEqual(gitwork.out_of_scope(changed, []), changed)


class RepoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.repo = root / "My Repo"
        self.repo.mkdir()
        self.home = root / "home"
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "app").mkdir()
        (self.repo / "app" / "a.txt").write_text("a\n")
        (self.repo / "b.txt").write_text("b\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        self.base = git(self.repo, "rev-parse", "HEAD")

    def test_changed_files_sees_modified_staged_untracked_and_renamed(self):
        (self.repo / "app" / "a.txt").write_text("changed\n")
        (self.repo / "new file.txt").write_text("n\n")
        git(self.repo, "mv", "b.txt", "c.txt")
        self.assertEqual(gitwork.changed_files(self.repo), ["app/a.txt", "b.txt", "c.txt", "new file.txt"])

    def test_changed_files_since_a_sha_includes_commits_the_agent_made(self):
        (self.repo / "app" / "a.txt").write_text("changed\n")
        git(self.repo, "commit", "-q", "-am", "agent commit")
        (self.repo / "late.txt").write_text("x\n")
        self.assertEqual(gitwork.changed_files(self.repo, since=self.base), ["app/a.txt", "late.txt"])
        self.assertEqual(gitwork.changed_files(self.repo), ["late.txt"])

    def test_changed_files_from_a_subdirectory_still_reports_repo_relative_paths(self):
        (self.repo / "app" / "a.txt").write_text("changed\n")
        self.assertEqual(gitwork.changed_files(self.repo / "app"), ["app/a.txt"])

    def test_repo_root_and_head(self):
        self.assertEqual(gitwork.repo_root(self.repo / "app"), self.repo)
        self.assertEqual(gitwork.head(self.repo), self.base)
        self.assertIsNone(gitwork.repo_root(Path(self.temp.name) / "home"))

    def test_create_worktree_makes_a_branch_from_the_base_under_the_home(self):
        info = gitwork.create_worktree(self.home, self.repo / "app", "agent/age-sql", "main")
        self.assertTrue(info["created"])
        self.assertTrue(str(info["path"]).startswith(str(self.home / "worktrees")))
        self.assertEqual(Path(info["workdir"]), Path(info["path"]) / "app")
        self.assertEqual(git(info["path"], "rev-parse", "--abbrev-ref", "HEAD"), "agent/age-sql")
        self.assertEqual(git(info["path"], "rev-parse", "HEAD"), self.base)
        self.assertTrue((Path(info["workdir"]) / "a.txt").exists())

    def test_create_worktree_twice_reuses_it(self):
        first = gitwork.create_worktree(self.home, self.repo, "agent/x", "main")
        second = gitwork.create_worktree(self.home, self.repo, "agent/x", "main")
        self.assertFalse(second["created"])
        self.assertEqual(first["path"], second["path"])

    def test_create_worktree_for_an_existing_branch_without_a_base(self):
        git(self.repo, "branch", "agent/old")
        info = gitwork.create_worktree(self.home, self.repo, "agent/old")
        self.assertEqual(git(info["path"], "rev-parse", "--abbrev-ref", "HEAD"), "agent/old")

    def test_create_worktree_refuses_protected_branches_unknown_base_and_non_repos(self):
        for branch in ("main", "master", "production"):
            with self.assertRaises(gitwork.GitWorkError):
                gitwork.create_worktree(self.home, self.repo, branch, "main")
        with self.assertRaises(gitwork.GitWorkError):
            gitwork.create_worktree(self.home, self.repo, "agent/y", "no-such-ref")
        with self.assertRaises(gitwork.GitWorkError):
            gitwork.create_worktree(self.home, self.repo, "agent/z")  # new branch needs a base
        with self.assertRaises(gitwork.GitWorkError):
            gitwork.create_worktree(self.home, Path(self.temp.name) / "home", "agent/q", "main")

    def test_list_shows_branch_and_dirty_state(self):
        info = gitwork.create_worktree(self.home, self.repo, "agent/x", "main")
        listing = gitwork.list_worktrees(self.home)
        self.assertEqual([(w["branch"], w["dirty"]) for w in listing], [("agent/x", False)])
        (Path(info["path"]) / "scratch.txt").write_text("x")
        self.assertTrue(gitwork.list_worktrees(self.home)[0]["dirty"])

    def test_remove_needs_force_when_dirty_and_never_deletes_the_branch(self):
        info = gitwork.create_worktree(self.home, self.repo, "agent/x", "main")
        (Path(info["path"]) / "scratch.txt").write_text("x")
        with self.assertRaises(gitwork.GitWorkError):
            gitwork.remove_worktree(self.home, "agent/x")
        self.assertTrue(Path(info["path"]).exists())
        gitwork.remove_worktree(self.home, "agent/x", force=True)
        self.assertFalse(Path(info["path"]).exists())
        self.assertIn("agent/x", git(self.repo, "branch", "--list", "agent/x"))

    def test_remove_clean_worktree_by_branch_or_path_and_unknown_target_fails(self):
        info = gitwork.create_worktree(self.home, self.repo, "agent/x", "main")
        gitwork.remove_worktree(self.home, str(info["path"]))
        self.assertFalse(Path(info["path"]).exists())
        with self.assertRaises(gitwork.GitWorkError):
            gitwork.remove_worktree(self.home, "agent/nope")


if __name__ == "__main__":
    unittest.main()
