# delegate.py reference

One entry point for Codex, Claude Code and Copilot. Everything below is enforced by code, not memory.
Qwen (local Ollama via `qwen_delegate.py`) is review/draft-only and not in the default routing order.

## The normal loop
```
D=~/.local/share/agent-delegate   # wherever you cloned it
python3 $D/delegate.py brief  --spec specs/<task>.toml          # lint + preview the brief (spends nothing)
python3 $D/delegate.py start  --spec specs/<task>.toml --card <my card>   # returns a job id in <1 s
python3 $D/delegate.py wait   <job> [--timeout 600]             # short summary; exit 6 = still running
python3 $D/delegate.py show   <job> [--lines 30]                # head of the result + paths
python3 $D/delegate.py resume <job> "fix X"                     # same agent session, cheaper than a new run
python3 $D/delegate.py cancel <job> | jobs [--all] | gc | doctor | status | ledger | probe --model M
python3 $D/delegate.py worktree list | rm <branch> [--force]    # branches are never deleted
```
`start` never blocks, so no `terminal(background=true)` wrapper is needed. `run` (same flags) still exists and blocks.
Put flags after free text in `resume`. Review the diff in the job's worktree yourself; the wrapper's verify is a floor, not a review.

## Effort and follow-ups
`caps.toml [effort]` selects effort by model and risk: high for high-risk Opus/Codex, medium for routine work and Sonnet.
An explicit spec `effort` or `--effort` wins; use low for mechanical work. Values are validated for each CLI;
Copilot/Qwen ignore effort and show `-`. Unlisted models show `default:<value>` (or `default:?` if unreadable),
read from the CLI settings, and receive no effort override. Policy/spec values are always passed explicitly.
Effort appears in card titles/bodies, ledger rows/tables, job records, start/summary lines, `jobs`/`show`, and task cost groups.
Parent cards passed with `--card` are recorded separately so task cost reports include child runs.
Claude defaults to `high_effort_budget_usd = 15.0` for high/xhigh/max, otherwise `default_budget_usd`; explicit budgets win.

`resume --files` extends scope: `delegate.py resume <job> "fix X" --files 'tests/**' --files 'lib/**' --verify 'new check'`.
`--verify` replaces the inherited command; effort is inherited unless `--effort` overrides it.
Unchanged pre-existing dirty files (including deletions) are ignored for scope and listed separately in the report and `show`.

## Review and verify reuse
`delegate.py review --rev <sha|A..B> --workdir <repo> --focus "check X"` generates a findings spec in `state/specs/`
and starts it through the normal policy, caps and card flow. Default: Codex / gpt-6.1-sol, low risk.
Optional: `--agent`, `--model`, `--risk`, `--card`, `--context-file`, `--task`, `--effort`, `--timeout`.
`--dry-run` prints the read-only brief and resolved agent/model/effort without launching.

`delegate.py verified <job>` checks HEAD, the tracked diff and untracked file hashes against the state after verify.
It prints `unchanged since verify: PASS (<last line>)` and exits 0 when reusable, `changed since verify` and exits 1
otherwise, or `no verify recorded` and exits 2 for older/unverified jobs. `show` includes the verify summary's last line.

Claude's `caps.toml [claude] setting_sources = ""` leaves setting sources unchanged. Set it to `"project,local"`
to pass `--setting-sources project,local` and record the choice in the ledger; measure with a probe first.
Run logs omit only Claude `system/ui_invalidate` events. Multiple result events retain the initial report and later turns;
usage/cost counters come from the final cumulative event. Delegates are instructed to keep all shell commands in the foreground.

## Spec file (TOML)
Required: `task agent model reason goal workdir`. Optional: `mode` (review|implement, default review), `risk` (low|high),
`context`, `preserve[]`, `files[]` (scope globs, repo-root-relative), `tests_first`, `constraints[]`, `acceptance[]`,
`verify` (shell command, runs in `workdir` after the agent), `verify_timeout`, `budget_usd` (claude only; default from caps),
`schema = "findings"` (structured review), `worktree = {branch, base}`, `effort`, `timeout`, `card`, `no_card`, `opus_reason`.
Implement mode requires `files` and `verify`. Unknown keys, secret-looking text, absolute/`..` scope globs, protected branches and
"read file X" asks to Qwen are rejected before any quota is spent. The standing rules (staging only, no push/deploy/prod writes,
no secrets, no commits, leave unrelated files alone, never claim unrun results) are added to every brief from `caps.toml [brief]`.

```toml
task = "age-check"
agent = "codex"
model = "gpt-6.1-sol"
mode = "implement"
risk = "high"
reason = "scoped migration + test harness"
goal = "Add declare_age(year, month) and a SQL test covering every age-band boundary."
workdir = "~/code/my-app"
files = ["db/migrations/*.sql", "db/tests/age_check.sql"]
verify = "test -f db/tests/age_check.sql"
acceptance = ["harness covers every band boundary"]
[worktree]
branch = "agent/age-check"
base = "main"
```

## What the wrapper does for you
- Policy and caps (quota %, daily/weekly call counts per Claude account, Opus reason only if `opus_requires_reason`), concurrency slots
  per agent (`caps.toml [jobs]`), protected-branch refusal, per-task worktrees under `delegation/worktrees/`.
- After an implement run it records changed files, flags files outside `files` (scope), and runs `verify` itself.
- Reviews with `schema = "findings"` come back as counts and a findings list (`<run>.findings.json`).
- Claude runs get `--max-budget-usd` using the effort rules above and persist their session so `resume` works.
- Cards: created, claimed and closed inside the detached job; cancel blocks the card. Old run logs prune after 14 days.

## Exit codes
0 ok, 2 bad usage, 3 blocked (cap/policy/slots; take the suggested agent), 4 agent failed or timed out, 5 wrong model answered,
6 still running (`wait`), 7 verify failed, 8 files changed outside scope. Job statuses: queued running done failed timeout blocked
cancelled lost model_mismatch verify_failed scope_violation.

## State (all under delegation/)
`state/jobs/<id>.json|.result.txt|.log`, `state/briefs/` (rendered briefs), `runs/` (raw agent output, reports, findings),
`worktrees/`, `ledger.jsonl`, `caps.toml`, `lessons.md`. Tests: `python3 -m unittest discover -s tests`.
Weekly self-review retries transient exit-4 failures up to three times, ten minutes apart, and falls back once from a capped
Copilot run to Codex / gpt-6.1-sol. Each attempt is logged; proposals still require coordinator review.

## Skill library (playbooks)

`library/*.md` holds curated, delegate-safe playbooks (frontmatter `name`/`description`, body is plain instructions, no Hermes-only tools).
A spec's `skills = ["tdd-red-green", ...]` appends them to the brief as a `## Playbooks` section, so every agent gets them identically.
`caps.toml [library.defaults]` adds a base set per mode (`skills = []` opts out); `[library] max_chars` caps the section and the wrapper
fails loudly rather than truncating. `delegate.py skills` lists the library. Unknown names fail spec lint.
Add a playbook: write `library/<name>.md` (name must match the filename), keep it under ~1 KB, test with `delegate.py brief --spec ...`.

### Kanban cards (optional, Hermes only)
When the `hermes` CLI is on PATH, each run creates, claims and closes an unassigned card on the `agents` board
(override with `--board` / `DELEGATE_BOARD`; author shown via `DELEGATE_CREATED_BY`). `--no-card` or
`DELEGATE_NO_KANBAN=1` turns cards off; `DELEGATE_NO_KANBAN=0` forces them on. Without Hermes, cards are skipped silently.

## Environment variables
| Variable | Default | Purpose |
|---|---|---|
| `DELEGATE_HOME` | the repo directory | where `caps.toml`, `ledger.jsonl`, `runs/`, `state/`, `worktrees/` live |
| `CODEX_HOME` | `~/.codex` | Codex config/session lookup |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude settings lookup (effort default) |
| `QWEN_SCRIPT` / `QWEN_HOST` | `./qwen_delegate.py` / `http://localhost:11434` | local Ollama delegate |
| `DELEGATE_BOARD`, `DELEGATE_CREATED_BY`, `DELEGATE_NO_KANBAN` | `agents`, `delegate`, auto | Hermes kanban cards |
| `DELEGATE_PREFLIGHT` | unset | script that `doctor` runs with `--now` to report CLI logins/versions |
| `DELEGATE_CHILD` | set to `1` for delegated agents | recursion guard: launch commands refuse inside a delegated agent |
