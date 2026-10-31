from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import delegate
import gitwork
import spec
import task_cost

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests' / 'fixtures'


class IsolationAndDocsTests(unittest.TestCase):
    def test_item7_caps_are_fixtures(self):
        source = (ROOT / 'tests/test_delegate.py').read_text()
        self.assertNotIn('ROOT / "caps.toml"', source)
        self.assertTrue((FIXTURES / 'caps.toml').is_file())

    def test_item8_docs_cover_v3(self):
        text = (ROOT / 'WRAPPER.md').read_text()
        for term in ('[effort]', 'verified', 'review --rev', 'resume --files',
                     'pre-existing', 'setting_sources', 'high_effort_budget_usd'):
            self.assertIn(term, text)


class V3Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        shutil.copy(FIXTURES / 'caps.toml', self.home / 'caps.toml')
        self.caps = delegate.load_caps(self.home / 'caps.toml')
        (self.home / 'brief.txt').write_text('Do the task.')
        for patch in (
            mock.patch.dict(os.environ, {'DELEGATE_HOME': str(self.home), 'CODEX_HOME': str(self.home / 'codex'),
                'CLAUDE_CONFIG_DIR': str(self.home / 'claude'), 'DELEGATE_NO_CODEX_PROBE': '1',
                'DELEGATE_NO_KANBAN': '1', 'DELEGATE_NO_CLAUDE_IDENTITY': '1'}),
            mock.patch('delegate.kanban_call'),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def args(self, **changes):
        values = dict(agent='claude', model='claude-opus-5-5', task='v3', reason='reason',
                      brief_file=str(self.home / 'brief.txt'), workdir=str(self.home), mode='review',
                      risk='high', effort=None, timeout=30, dry_run=False, no_card=True, card='parent',
                      board='agents', opus_reason=None)
        values.update(changes)
        return argparse.Namespace(**values)

    def run_fake(self, args, stdout=None):
        if stdout is None:
            stdout = json.dumps({'type': 'result', 'result': 'RED evidence', 'session_id': 's',
                                 'modelUsage': {args.model: {}}, 'usage': {}, 'is_error': False})
        def fake(argv, **kwargs):
            if argv[0] == 'git':
                return subprocess.CompletedProcess(argv, 0, '', '')
            return subprocess.CompletedProcess(argv, 0, stdout, '')
        out, err = io.StringIO(), io.StringIO()
        with mock.patch('delegate.subprocess.run', side_effect=fake) as run, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = delegate.run_command(args)
        return code, out.getvalue(), err.getvalue(), run

    def test_item1_policy_explicit_defaults_and_na(self):
        self.assertEqual(delegate.resolve_effort('claude', 'claude-opus-5-5', 'high', None, self.caps), ('high', 'policy'))
        self.assertEqual(delegate.resolve_effort('claude', 'claude-sonnet-5-5', 'high', None, self.caps), ('medium', 'policy'))
        self.assertEqual(delegate.resolve_effort('codex', 'gpt-6.1-sol', 'low', None, self.caps), ('medium', 'policy'))
        self.assertEqual(delegate.resolve_effort('codex', 'gpt-6.1-sol', 'high', 'low', self.caps), ('low', 'spec'))
        self.assertEqual(delegate.resolve_effort('qwen', 'm', 'high', 'high', self.caps), (None, 'n/a'))
        for agent, filename, content in [('claude', 'settings.json', '{"effortLevel":"xhigh"}'),
                                         ('codex', 'config.toml', 'model_reasoning_effort="low"')]:
            path = self.home / filename
            path.write_text(content)
            kwargs = {f'{agent}_path': path}
            self.assertEqual(delegate.resolve_effort(agent, 'new', 'low', None, {}, **kwargs),
                             ('xhigh' if agent == 'claude' else 'low', 'cli-default'))
            path.write_text('invalid')
            self.assertEqual(delegate.resolve_effort(agent, 'new', 'low', None, {}, **kwargs), (None, 'cli-default'))

    def test_item1_lint_efforts(self):
        data = dict(task='t', agent='codex', model='m', reason='r', goal='g', workdir=str(self.home))
        for agent, valid, invalid in [('codex', 'minimal', 'max'), ('claude', 'max', 'minimal')]:
            self.assertEqual(spec.lint(dict(data, agent=agent, effort=valid), delegate.AGENTS)[0], [])
            self.assertTrue(spec.lint(dict(data, agent=agent, effort=invalid), delegate.AGENTS)[0])
        for agent in ('qwen', 'copilot'):
            errors, warnings = spec.lint(dict(data, agent=agent, effort='high'), delegate.AGENTS)
            self.assertFalse(errors)
            self.assertTrue(any('effort' in w for w in warnings))

    def test_item1_visibility_budget_and_card(self):
        args = self.args()
        code, _, err, run = self.run_fake(args)
        self.assertEqual(code, 0)
        argv = next(c.args[0] for c in run.call_args_list if c.args[0][0] == 'claude')
        self.assertIn('--effort', argv)
        self.assertEqual(argv[argv.index('--effort') + 1], 'high')
        self.assertEqual(argv[argv.index('--max-budget-usd') + 1], '15')
        row = delegate.read_ledger()[0]
        self.assertEqual((row['effort'], row['effort_source']), ('high', 'policy'))
        self.assertIn('effort=high', err)
        args.no_card = False
        args.reason = 'long ' * 100
        with mock.patch.dict(os.environ, {'DELEGATE_NO_KANBAN': '0'}), \
                mock.patch('delegate.git_branch', return_value='feature'), \
                mock.patch('delegate.kanban_call', return_value=subprocess.CompletedProcess([], 0, '{"id":"child"}', '')) as kb:
            delegate.create_run_card(args)
        title = kb.call_args.args[2]
        self.assertTrue(title.startswith('[Claude · claude-opus-5-5 · high]'))
        self.assertLessEqual(len(title), 120)
        self.assertIn('Effort: high (policy, risk high)', kb.call_args.args[4])

    def test_item1_job_and_grouping(self):
        args = self.args()
        with mock.patch('delegate.launch_process'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(delegate.start_command(args), 0)
        record = delegate.jobs.list_jobs(self.home)[0]
        self.assertEqual(record['effort'], 'high')
        self.assertIn('claude-opus-5-5 high', delegate.format_job(record, 0))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            delegate.jobs_command(argparse.Namespace(all=True, json=False))
        self.assertIn('claude-opus-5-5 high', out.getvalue())
        for effort in ('medium', 'high'):
            delegate.append_ledger(dict(ts=delegate.utc_iso(), agent='claude', model='m', effort=effort))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            delegate.ledger_command(7, True)
        self.assertEqual(len(json.loads(out.getvalue())), 2)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            delegate.ledger_command(7, False)
        self.assertIn('EFFORT', out.getvalue())
        self.assertEqual(len(task_cost.aggregate_delegates(delegate.read_ledger())), 2)

    def test_item2_parent_stored_and_attributed(self):
        args = self.args()
        self.run_fake(args)
        self.assertEqual(delegate.read_ledger()[0]['parent_card'], 'parent')
        with mock.patch('delegate.launch_process'), contextlib.redirect_stdout(io.StringIO()):
            delegate.start_command(self.args())
        self.assertEqual(delegate.jobs.list_jobs(self.home)[0]['parent_card'], 'parent')
        session = dict(id='session', model='m', **{k: 0 for k in task_cost.COUNTERS})
        now = task_cost.utc_now()
        with mock.patch('task_cost.session_usage', return_value=session), \
                mock.patch('task_cost.utc_now', return_value=now - dt.timedelta(minutes=1)):
            task_cost.start_task('coordinator')
        with mock.patch('task_cost.session_usage', return_value=session), \
                mock.patch('task_cost.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')), \
                contextlib.redirect_stdout(io.StringIO()):
            report = task_cost.end_task('coordinator', card='parent')
        self.assertEqual(len(report['delegate_runs']), 1)
        self.assertNotIn('Delegates: none', report['report'])

    def test_item3_all_results_last_counters_and_foreground_rule(self):
        events = [{'type': 'result', 'result': ''},
                  {'type': 'result', 'result': 'RED substantive', 'total_cost_usd': 1.064},
                  {'type': 'result', 'result': 'Nothing new', 'total_cost_usd': 1.096,
                   'usage': {'input_tokens': 20}, 'session_id': 'last', 'modelUsage': {'last-model': {}}, 'is_error': True}]
        parsed = delegate.parse_claude_output('\n'.join(json.dumps(e) for e in events))
        self.assertEqual(parsed['result'], 'RED substantive\n\n--- later turn(s) after background notifications ---\n\nNothing new')
        self.assertEqual((parsed['cost_usd_list'], parsed['input_tokens'], parsed['session_id'], parsed['models'], parsed['failed']),
                         (1.096, 20, 'last', {'last-model'}, True))
        self.assertIn('Run every shell command in the foreground', '\n'.join(spec.DEFAULT_STANDING))
        events[1] = {'type': 'result', 'structured_output': {'findings': []}}
        parsed = delegate.parse_claude_output('\n'.join(json.dumps(e) for e in events))
        self.assertIn('"findings": []', parsed['result'])

    def test_item4_snapshot_filters_only_identical_dirty_content(self):
        self.init_repo()
        (self.repo / 'tracked').write_text('coordinator')
        (self.repo / 'asset').write_text('asset')
        (self.repo / 'deleted').unlink()
        before = gitwork.dirty_snapshot(self.repo)
        self.assertEqual(set(before), {'tracked', 'asset', 'deleted'})
        changed, ignored = gitwork.agent_changes(self.repo, gitwork.head(self.repo), before)
        self.assertEqual(changed, [])
        self.assertEqual(set(ignored), set(before))
        (self.repo / 'tracked').write_text('agent')
        (self.repo / 'new').write_text('new')
        (self.repo / 'asset').unlink()
        changed, ignored = gitwork.agent_changes(self.repo, gitwork.head(self.repo), before)
        self.assertEqual(set(changed), {'tracked', 'new', 'asset'})
        self.assertEqual(ignored, ['deleted'])

    def init_repo(self):
        self.repo = self.home / 'repo'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'feature')
        for name in ('tracked', 'deleted'):
            (self.repo / name).write_text('initial')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')

    def git(self, *args):
        return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                               '-c', 'commit.gpgsign=false', *args], cwd=self.repo,
                              check=True, capture_output=True, text=True).stdout.strip()

    def test_item4_resume_extends_scope_verify_and_effort(self):
        previous = vars(self.args(files=['old/**'], verify='old test', effort='high'))
        record = dict(id='j-old', status='done', session_id='s', task='v3', args=previous, effort='high')
        delegate.jobs.write_job(self.home, record)
        argv = ['resume', 'j-old', 'Fix it', '--files', 'new/**', '--files', 'other.py', '--verify', 'new test', '--effort', 'low']
        with mock.patch('delegate.start_command', return_value=0) as start:
            self.assertEqual(delegate.main(argv), 0)
        args = start.call_args.args[0]
        self.assertEqual((args.files, args.verify, args.effort), (['old/**', 'new/**', 'other.py'], 'new test', 'low'))
        self.assertIn('Scope extended for this follow-up', Path(args.brief_file).read_text())
        with mock.patch('delegate.start_command', return_value=0) as start:
            delegate.main(['resume', 'j-old', 'Again'])
        self.assertEqual(start.call_args.args[0].effort, 'high')

    def test_item5_review_dry_run_and_spec(self):
        context = self.home / 'context.txt'
        context.write_text('Extra context')
        with mock.patch('delegate.launch_process') as launch, contextlib.redirect_stdout(io.StringIO()) as out:
            code = delegate.main(['review', '--rev', 'abc123..def456', '--workdir', str(self.home), '--focus',
                                  'Check authorization\nFollow-up focus', '--risk', 'high', '--context-file', str(context), '--dry-run'])
        self.assertEqual(code, 0)
        launch.assert_not_called()
        for text in ('READ-ONLY review of abc123..def456', 'git diff abc123..def456', 'Extra context', 'effort=high',
                     'no network', 'file:line', 'confidence', 'smallest fix'):
            self.assertIn(text, out.getvalue())
        paths = list((self.home / 'state/specs').glob('*.toml'))
        self.assertEqual(len(paths), 1)
        data = spec.load_spec(paths[0])
        self.assertEqual((data['mode'], data['schema'], data['agent']), ('review', 'findings', 'codex'))
        self.assertEqual(spec.lint(data, delegate.AGENTS)[0], [])

    def test_item5_verify_reuse_and_last_line(self):
        self.init_repo()
        result = delegate.run_verify("printf 'first\nsummary\n\n'", self.repo, 10)
        self.assertEqual(result['last_line'], 'summary')
        fingerprint = gitwork.fingerprint(self.repo)
        record = dict(id='j-verify', status='done', workdir=str(self.repo), report={'verify': result, 'fingerprint': fingerprint})
        delegate.jobs.write_job(self.home, record)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(delegate.main(['verified', 'j-verify']), 0)
        self.assertIn('unchanged since verify: PASS (summary)', out.getvalue())
        (self.repo / 'tracked').write_text('changed')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(delegate.main(['verified', 'j-verify']), 1)
        self.assertNotEqual(gitwork.fingerprint(self.repo), fingerprint)
        (self.repo / 'tracked').write_text('initial')
        (self.repo / 'untracked').write_text('new')
        self.assertNotEqual(gitwork.fingerprint(self.repo), fingerprint)
        record['report'] = {}
        delegate.jobs.write_job(self.home, record)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(delegate.main(['verified', 'j-verify']), 2)

    def test_item5_slimming_and_setting_sources(self):
        text = (self.home / 'caps.toml').read_text().replace('setting_sources = ""', 'setting_sources = "project,local"')
        (self.home / 'caps.toml').write_text(text)
        keep = ' {"type":"system","subtype":"other"}\r\nnot json\n'
        noise = '{"type":"system","subtype":"ui_invalidate"}\n'
        final = json.dumps({'type': 'result', 'result': 'answer', 'modelUsage': {'claude-opus-5-5': {}}})
        _, _, _, run = self.run_fake(self.args(), keep + noise + final)
        row = delegate.read_ledger()[0]
        self.assertEqual(Path(row['run_file']).read_bytes(), (keep + final).encode())
        self.assertEqual(row['setting_sources'], 'project,local')
        argv = next(c.args[0] for c in run.call_args_list if c.args[0][0] == 'claude')
        self.assertEqual(argv[argv.index('--setting-sources') + 1], 'project,local')

    def test_effort_policy_matrix_and_budget_override(self):
        for model, policy in self.caps['effort'].items():
            agent = 'claude' if model.startswith('claude') else 'codex'
            for risk, expected in policy.items():
                with self.subTest(model=model, risk=risk):
                    self.assertEqual(delegate.resolve_effort(agent, model, risk, None, self.caps), (expected, 'policy'))
        for effort, explicit_budget, expected in [('high', 2.5, '2.5'), ('max', None, '15'),
                                                   ('xhigh', None, '15'), ('low', None, '8')]:
            code, _, _, run = self.run_fake(self.args(effort=effort, budget_usd=explicit_budget))
            self.assertEqual(code, 0)
            argv = next(c.args[0] for c in run.call_args_list if c.args[0][0] == 'claude')
            self.assertEqual(argv[argv.index('--max-budget-usd') + 1], expected)

    def test_cli_defaults_are_labeled_and_not_overridden(self):
        (self.home / 'caps.toml').write_text((self.home / 'caps.toml').read_text().split('[effort]')[0])
        for agent, model, config, contents, expected in [
            ('claude', 'claude-opus-5-5', 'claude/settings.json', '{"effortLevel":"high"}', 'high'),
            ('codex', 'gpt-6.1-sol', 'codex/config.toml', 'model_reasoning_effort="xhigh"', 'xhigh'),
        ]:
            path = self.home / config
            path.parent.mkdir()
            path.write_text(contents)
            args = self.args(agent=agent, model=model, dry_run=True)
            code, out, _, _ = self.run_fake(args)
            self.assertEqual(code, 0)
            self.assertEqual(delegate.effort_label(args), 'default:' + expected)
            self.assertNotIn('--effort', out)
            self.assertNotIn('model_reasoning_effort=', out)
            path.unlink()
            args = self.args(agent=agent, model=model, dry_run=True)
            self.run_fake(args)
            self.assertEqual(delegate.effort_label(args), 'default:?')

    def test_effective_flags_override_invalid_spec_effort_before_lint(self):
        path = self.home / 'spec.toml'
        data = dict(task='t', agent='codex', model='gpt-6.1-sol', reason='r', goal='g',
                    workdir=str(self.home), effort='max')
        path.write_text('\n'.join(f'{k} = {json.dumps(v)}' for k, v in data.items()))
        args = delegate.make_parser().parse_args(['start', '--spec', str(path), '--effort', 'low'])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(delegate.resolve_run_args(args))
        self.assertEqual((args.effort, args.effort_source), ('low', 'spec'))
        self.assertIsNone(delegate.resolve_run_args(args))
        self.assertEqual(args.effort_source, 'spec')
        bad = self.args(agent='codex', model='gpt-6.1-sol', effort='max')
        with contextlib.redirect_stderr(io.StringIO()), mock.patch('delegate.subprocess.run') as run:
            self.assertEqual(delegate.run_command(bad), delegate.BAD_USAGE)
        run.assert_not_called()

    def test_detached_old_job_gains_effort_and_parent_metadata(self):
        record = dict(id='j-legacy', status='queued', agent='claude', model='claude-opus-5-5',
                      task='v3', args=vars(self.args()), created_at=delegate.utc_iso())
        delegate.jobs.write_job(self.home, record)
        def fake_run(args):
            self.assertIsNone(delegate.resolve_run_args(args))
            return 0
        with mock.patch('delegate.run_command', side_effect=fake_run):
            delegate.job_runner('j-legacy')
        saved = delegate.jobs.read_job(self.home, 'j-legacy')
        self.assertEqual((saved['effort'], saved['effort_source'], saved['parent_card']), ('high', 'policy', 'parent'))

    def test_resume_keeps_resolved_policy_even_if_caps_change(self):
        args = self.args(files=['old/**'], verify='old check')
        delegate.resolve_run_args(args)
        delegate.jobs.write_job(self.home, dict(id='j-resume', status='done', session_id='s', task='v3',
                                               effort='high', effort_source='policy', args=vars(args)))
        (self.home / 'caps.toml').write_text((self.home / 'caps.toml').read_text().replace('high = "high"', 'high = "low"'))
        with mock.patch('delegate.launch_process'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(delegate.main(['resume', 'j-resume', 'continue']), 0)
        newest = next(r for r in delegate.jobs.list_jobs(self.home) if r['id'] != 'j-resume')
        self.assertEqual((newest['effort'], newest['args']['files'], newest['args']['verify']),
                         ('high', ['old/**'], 'old check'))
        with mock.patch('delegate.start_command') as start, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(delegate.main(['resume', 'j-resume', 'continue', '--files', '../escape']), 2)
        start.assert_not_called()

    def test_scope_and_verify_report_integration(self):
        self.init_repo()
        (self.repo / 'asset').write_text('coordinator')
        (self.repo / 'deleted').unlink()
        original_run = subprocess.run
        def fake(argv, **kwargs):
            if argv[0] != 'claude':
                return original_run(argv, **kwargs)
            (self.repo / 'tracked').write_text('agent edit')
            return subprocess.CompletedProcess(argv, 0, json.dumps({'type': 'result', 'result': 'done',
                'modelUsage': {'claude-opus-5-5': {}}}), '')
        args = self.args(workdir=str(self.repo), files=['tracked'], mode='implement', verify="printf 'suite passed\n'")
        with mock.patch('delegate.subprocess.run', side_effect=fake), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(delegate.run_command(args), 0)
        self.assertEqual(args.report['changed'], ['tracked'])
        self.assertEqual(args.report['pre_existing'], ['asset', 'deleted'])
        self.assertEqual(args.report['out_of_scope'], [])
        self.assertEqual(args.report['fingerprint'], gitwork.fingerprint(self.repo))
        self.assertEqual(args.report['verify']['last_line'], 'suite passed')
        shown = delegate.format_job(dict(id='j', status='done', report=args.report), 0)
        self.assertIn('scope: 1 changed, 0 outside; 2 pre-existing ignored', shown)
        self.assertIn('verify: PASS suite passed', shown)

    def test_fingerprint_handles_staged_binary_untracked_ignored_and_head(self):
        self.init_repo()
        first = gitwork.fingerprint(self.repo)
        (self.repo / 'tracked').write_bytes(b'new\x00binary')
        self.git('add', 'tracked')
        staged = gitwork.fingerprint(self.repo)
        self.assertNotEqual(first, staged)
        self.git('commit', '-qm', 'second')
        self.assertNotEqual(staged, gitwork.fingerprint(self.repo))
        (self.repo / '.git/info/exclude').write_text('ignored\n')
        before = gitwork.fingerprint(self.repo)
        (self.repo / 'ignored').write_text('scratch')
        self.assertEqual(before, gitwork.fingerprint(self.repo))
        (self.repo / 'untracked').write_text('a')
        first_untracked = gitwork.fingerprint(self.repo)
        (self.repo / 'untracked').write_text('b')
        self.assertNotEqual(first_untracked, gitwork.fingerprint(self.repo))
        self.assertIsNone(gitwork.fingerprint(self.home))

    def test_verified_failed_and_legacy_records(self):
        self.init_repo()
        for report, expected in [({}, 2), ({'verify': {'passed': True}}, 2),
                                 ({'verify': {'passed': False}, 'fingerprint': gitwork.fingerprint(self.repo)}, 1)]:
            delegate.jobs.write_job(self.home, dict(id='j-check', status='done', report=report, workdir=str(self.repo)))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(delegate.main(['verified', 'j-check']), expected)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(delegate.main(['verified', 'missing']), 2)

    def test_review_launches_normal_start_and_obeys_caps(self):
        argv = ['review', '--rev', 'abcd1234', '--workdir', str(self.home), '--focus', 'one concern',
                '--card', 'parent', '--effort', 'low', '--timeout', '70']
        with mock.patch('delegate.launch_process'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(delegate.main(argv), 0)
        record = delegate.jobs.list_jobs(self.home)[0]
        self.assertEqual((record['effort'], record['parent_card'], record['args']['timeout']), ('low', 'parent', 70))
        self.assertIn('Review: git show abcd1234', Path(record['args']['brief_file']).read_text())
        self.assertTrue(record['task'].startswith('review-abcd1234'))
        with mock.patch('delegate.cap_reason', return_value='cap reached'), mock.patch('delegate.launch_process') as launch, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(delegate.main(argv), delegate.BLOCKED)
        launch.assert_not_called()

    def test_review_rejects_bad_context_effort_and_revision(self):
        argv = ['review', '--rev', 'abc', '--workdir', str(self.home), '--focus', 'focus']
        for extra in (['--context-file', str(self.home / 'missing')], ['--effort', 'max'], ['--rev', 'abc;echo bad']):
            with mock.patch('delegate.launch_process') as launch, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(delegate.main(argv + extra), delegate.BAD_USAGE)
            launch.assert_not_called()

    def test_run_stream_is_captured_as_bytes_without_losing_other_lines(self):
        keep = b' {"type":"system","subtype":"other"}\r\ninvalid \xff\n'
        noise = b'{"type":"system","subtype":"ui_invalidate"}\n'
        final = json.dumps({'type': 'result', 'result': 'done', 'modelUsage': {'claude-opus-5-5': {}}}).encode()
        code, _, _, run = self.run_fake(self.args(), keep + noise + final)
        self.assertEqual(code, 0)
        call = next(c for c in run.call_args_list if c.args[0][0] == 'claude')
        self.assertFalse(call.kwargs.get('text', False))
        self.assertEqual(call.kwargs['input'], b'Do the task.')
        row = delegate.read_ledger()[0]
        self.assertEqual(Path(row['run_file']).read_bytes(), keep + final)
        self.assertNotIn('setting_sources', row)
        self.assertNotIn('--setting-sources', call.args[0])

    def test_transient_stderr_is_visible_to_self_review(self):
        with mock.patch('delegate.subprocess.run', return_value=subprocess.CompletedProcess([], 4, '', 'network fetch failed')), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(delegate.run_command(self.args()), delegate.AGENT_FAILED)
        self.assertIn('network fetch failed', err.getvalue())

    def test_timeout_diagnostic_can_trigger_transient_retry(self):
        def fake(argv, **kwargs):
            if argv[0] == 'git':
                return subprocess.CompletedProcess(argv, 0, '', '')
            raise subprocess.TimeoutExpired(argv, 30, output=b'partial', stderr=b'')
        with mock.patch('delegate.subprocess.run', side_effect=fake), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(delegate.run_command(self.args()), delegate.AGENT_FAILED)
        self.assertIn('timed out', err.getvalue())

    def test_scope_display_includes_ignored_files_without_declared_scope(self):
        text = delegate.format_job({'id': 'j', 'status': 'done',
                                    'report': {'changed': [], 'pre_existing': ['asset'], 'out_of_scope': None}}, 0)
        self.assertIn('1 pre-existing ignored', text)

    def test_review_shortens_a_full_sha_for_the_default_task(self):
        with mock.patch('delegate.launch_process'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(delegate.main(['review', '--rev', '12345678' * 5, '--workdir', str(self.home),
                                            '--focus', 'focus']), 0)
        self.assertEqual(delegate.jobs.list_jobs(self.home)[0]['task'], 'review-12345678')

    def test_dirty_snapshot_handles_renames_symlinks_and_restored_files(self):
        self.init_repo()
        (self.repo / 'tracked').write_text('coordinator')
        (self.repo / 'link').symlink_to('tracked')
        before = gitwork.dirty_snapshot(self.repo)
        changed, ignored = gitwork.agent_changes(self.repo, gitwork.head(self.repo), before)
        self.assertEqual(changed, [])
        self.assertEqual(ignored, ['link', 'tracked'])
        (self.repo / 'tracked').write_text('initial')
        (self.repo / 'link').unlink()
        (self.repo / 'link').symlink_to('deleted')
        self.git('mv', 'deleted', 'renamed')
        changed, ignored = gitwork.agent_changes(self.repo, gitwork.head(self.repo), before)
        self.assertEqual(set(changed), {'tracked', 'link', 'deleted', 'renamed'})
        self.assertEqual(ignored, [])

    def test_all_supported_effort_values_and_na_flags(self):
        data = dict(task='t', reason='r', goal='g', model='m', workdir=str(self.home))
        for agent, values in spec.EFFORTS.items():
            for value in values:
                self.assertFalse(spec.lint(dict(data, agent=agent, effort=value), delegate.AGENTS)[0])
            for invalid in ('', 'HIGH', 123, ['high']):
                self.assertTrue(spec.lint(dict(data, agent=agent, effort=invalid), delegate.AGENTS)[0])
        for agent in ('copilot', 'qwen'):
            args = self.args(agent=agent, effort='high')
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertIsNone(delegate.resolve_run_args(args))
            self.assertEqual((args.effort, args.effort_source, delegate.effort_label(args)), (None, 'n/a', '-'))
            self.assertIn('ignored', err.getvalue())

    def test_multiple_empty_later_turns_keep_the_separator_and_first_findings(self):
        events = [{'type': 'result', 'structured_output': {'summary': 'real', 'findings': []}},
                  {'type': 'result', 'result': ''}, {'type': 'result', 'result': 'Nothing new'}]
        result = delegate.parse_claude_output('\n'.join(json.dumps(e) for e in events))['result']
        self.assertEqual(delegate.extract_findings(result)['summary'], 'real')
        self.assertEqual(result.count('--- later turn(s) after background notifications ---'), 1)
        self.assertTrue(result.endswith('Nothing new'))


if __name__ == '__main__':
    unittest.main()
