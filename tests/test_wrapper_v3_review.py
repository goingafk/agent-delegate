from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock

import delegate
import gitwork
import spec

ROOT = Path(__file__).resolve().parents[1]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                           '-c', 'commit.gpgsign=false', *args], cwd=repo,
                          check=True, capture_output=True, text=True).stdout.strip()


class ReviewFindingsTests(unittest.TestCase):
    """Sonnet review of ed1e715 (wrapper v3): 1 medium, 3 low."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name) / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'feature')
        (self.repo / 'tracked').write_text('initial')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'initial')

    def nested_repo(self):
        nested = self.repo / 'sub'
        nested.mkdir()
        git(nested, 'init', '-q', '-b', 'main')
        (nested / 'f').write_text('x')
        git(nested, 'add', '.')
        git(nested, 'commit', '-qm', 'n')

    def test_untracked_nested_repo_does_not_break_snapshot_changes_or_fingerprint(self):
        self.nested_repo()
        before = gitwork.dirty_snapshot(self.repo)
        self.assertIn('sub/', before)
        changed, ignored = gitwork.agent_changes(self.repo, gitwork.head(self.repo), before)
        self.assertEqual(changed, [])
        self.assertEqual(ignored, ['sub/'])
        self.assertIsNotNone(gitwork.fingerprint(self.repo))
        # A new commit inside the nested repo is a change.
        (self.repo / 'sub' / 'f').write_text('y')
        git(self.repo / 'sub', 'commit', '-qam', 'm')
        changed, _ = gitwork.agent_changes(self.repo, gitwork.head(self.repo), before)
        self.assertEqual(changed, ['sub/'])

    def test_hashing_is_batched(self):
        for i in range(30):
            (self.repo / f'u{i}').write_text(str(i))
        real = gitwork._git
        calls = []

        def counting(root, *args, **kw):
            calls.append(args)
            return real(root, *args, **kw)
        with mock.patch('gitwork._git', side_effect=counting):
            snap = gitwork.dirty_snapshot(self.repo)
        self.assertEqual(len(snap), 30)
        self.assertLessEqual(sum(1 for a in calls if a and a[0] == 'hash-object'), 1)
        # Same answers as hashing one by one.
        for name, value in snap.items():
            self.assertEqual(value, real(self.repo, 'hash-object', '--', name).strip())

    def test_run_survives_a_git_error_in_the_scope_report(self):
        with mock.patch('gitwork.dirty_snapshot', side_effect=gitwork.GitWorkError('boom')):
            self.assertEqual(delegate.safe_git(gitwork.dirty_snapshot, self.repo, default={}), {})

    def test_review_spec_escapes_del(self):
        line = delegate.toml_line('context', 'a\x7fb\nc')
        self.assertEqual(tomllib.loads(line)['context'], 'a\x7fb\nc')


class LiveCapsTests(unittest.TestCase):
    def test_live_caps_cover_every_allowed_model_with_valid_efforts(self):
        caps = delegate.load_caps(ROOT / 'caps.toml')
        policy = caps.get('effort', {})
        for agent in ('claude', 'codex'):
            for model in caps[agent]['allowed_models']:
                self.assertIn(model, policy, f'{agent} model {model} has no [effort] entry')
                for risk in spec.RISKS:
                    self.assertIn(policy[model][risk], spec.EFFORTS[agent], f'{model} {risk}')
        for key in ('default_budget_usd', 'high_effort_budget_usd', 'setting_sources'):
            self.assertIn(key, caps['claude'])


if __name__ == '__main__':
    unittest.main()
