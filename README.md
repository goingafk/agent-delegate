# agent-delegate

A quota-aware wrapper for handing coding work from one AI agent (the "orchestrator") to others:
**OpenAI Codex CLI**, **Claude Code**, **GitHub Copilot CLI**, or a **local Ollama** model.

One command, `delegate`, sits in front of all of them and enforces by code what is easy to forget in a prompt:

- **Quota caps** per agent and model (Codex 5-hour/weekly %, Claude 5-hour/weekly % plus daily/weekly call counts,
  Copilot premium requests per week). A capped run is refused with exit code 3 and a suggested next agent.
- **Locked-down permissions** for each CLI: no `git push`, `reset`, `rm -rf`, `curl`, PR merges; Codex apps/MCP
  servers disabled; Claude spending limited with `--max-budget-usd`.
- **Model verification**: the run fails (exit 5) if a different model answered than the one requested.
- **Isolated worktrees**: implement-mode tasks run on an `agent/<task>` branch in their own git worktree; protected
  branches are refused. After the run it lists changed files, flags anything outside the declared scope (exit 8)
  and runs your verify command (exit 7 on failure).
- **Structured briefs** from a TOML spec with standing rules always appended (no push/deploy/prod writes, no secrets,
  no commits, don't claim unrun tests). Secret-looking text in a spec is rejected before any quota is spent.
- **Background jobs**: `start` returns a job id in under a second; `wait`, `show`, `resume`, `cancel`.
- **Ledger** of every call (`ledger.jsonl`) with tokens and cost where the CLI reports them.
- **Recursion guard**: agents launched by the wrapper get `DELEGATE_CHILD=1`, and the wrapper refuses to launch
  from inside one, so instructions like "delegate via `delegate`" in a shared `AGENTS.md` can't cause a loop.
- Optional **Hermes Agent kanban cards** for each run (auto-enabled only when the `hermes` CLI is installed).

Python 3.11+ standard library only. macOS and Linux (uses `fcntl`). No network calls of its own except the
optional Ollama health check.

## Install

```sh
git clone https://github.com/goingafk/agent-delegate.git ~/.local/share/agent-delegate
cd ~/.local/share/agent-delegate
./install.sh                       # puts a `delegate` command in ~/.local/bin
python3 -m unittest discover -s tests   # optional: 260+ offline tests, no agent or network calls
```

Then:

1. **Edit `caps.toml`** for your own plans: allowed models, % thresholds, daily/weekly call caps, Copilot premium
   budget, effort per model. The shipped values are examples.
2. **Log in to the CLIs you want to delegate to** (you only need the ones you use):
   `codex login`, `claude auth login`, `copilot` (then `/login`), and for local models run Ollama
   (`QWEN_HOST` defaults to `http://localhost:11434`).
3. Check it works: `delegate status`, then `delegate probe --model <model>` for each model you plan to use.

The wrapper keeps its state next to the code (`runs/`, `state/`, `worktrees/`, `ledger.jsonl`, all git-ignored).
Set `DELEGATE_HOME` to keep state elsewhere (copy `caps.toml` there).

## Teach your agents to use it

The orchestrating agent needs to know the wrapper exists and that it must be used instead of calling CLIs directly.
`install.sh` can add the instructions for you; it is safe to re-run (the block is replaced, not duplicated).

| Orchestrator | What gets installed | Per project | For all projects |
|---|---|---|---|
| **Claude Code** | Skill `agent-delegate` (`SKILL.md`) | `./install.sh --for claude --project ~/code/app` → `.claude/skills/agent-delegate/` | `./install.sh --for claude --global` → `~/.claude/skills/agent-delegate/` |
| **Codex CLI** | Instruction block in `AGENTS.md` | `./install.sh --for codex --project ~/code/app` → `AGENTS.md` | `./install.sh --for codex --global` → `~/.codex/AGENTS.md` |
| **Hermes Agent** | Skill `agent-delegate` | — (skills are per user/profile) | `./install.sh --for hermes --global` → `~/.hermes/skills/autonomous-ai-agents/agent-delegate/` (set `HERMES_HOME` for a profile, e.g. `~/.hermes/profiles/<name>`) |
| **Gemini CLI** | Block in `GEMINI.md` | `--for gemini --project DIR` | `--for gemini --global` → `~/.gemini/GEMINI.md` |
| **Anything that reads `AGENTS.md`** (Cursor, OpenCode, Aider, Zed, Jules, …) | Block in `AGENTS.md` | `--for agents --project DIR` | — |

Combine targets: `./install.sh --for claude,codex,agents --project ~/code/app`. Add `--dry-run` to see what would change.

**Manual install.** The two templates are plain files:
- [`integrations/SKILL.md`](integrations/SKILL.md): Agent Skills format (Claude Code, Hermes, and other skill-aware
  agents). Copy it to the agent's skills directory as `agent-delegate/SKILL.md`.
- [`integrations/AGENTS.snippet.md`](integrations/AGENTS.snippet.md): paste into `AGENTS.md`, `CLAUDE.md`,
  `GEMINI.md`, `.cursorrules` or whatever instruction file your agent reads.

Hermes users: run cards appear on the `agents` board automatically when `hermes` is on PATH. Set
`DELEGATE_CREATED_BY=<your profile>` so cards show who launched them; `DELEGATE_NO_KANBAN=1` turns them off.

## Quick start

```sh
cat > /tmp/fix.toml <<'EOF'
task = "fix-login-timeout"
agent = "codex"
model = "gpt-6.1-sol"
reason = "well-scoped bug fix"
mode = "implement"
goal = "Login hangs when the token refresh times out; make it fail fast with a clear error."
workdir = "~/code/my-app"
files = ["src/auth/**", "tests/auth/**"]
verify = "npm test -- auth"
acceptance = ["a new test reproduces the hang and now passes"]
[worktree]
branch = "agent/fix-login-timeout"
base = "main"
EOF

delegate brief --spec /tmp/fix.toml     # lint + preview the brief; spends nothing
delegate start --spec /tmp/fix.toml     # -> job id
delegate wait <job> --timeout 600       # exit 6 = still running
delegate show <job>                     # report, changed files, scope + verify results
delegate resume <job> "also handle the 401 case"

delegate review --rev HEAD~1..HEAD --workdir ~/code/my-app --focus "auth edge cases"   # read-only review
delegate status                         # quota per agent/model
delegate ledger --days 7                # what you spent
```

Then review the diff in the job's worktree and run the tests yourself. The wrapper's verify step is a floor, not a review.

## Exit codes

0 ok · 2 bad usage · 3 blocked by cap/policy/slots · 4 agent failed or timed out · 5 wrong model answered ·
6 still running (`wait`) · 7 verify failed · 8 files changed outside scope

## More

- [`WRAPPER.md`](WRAPPER.md): full command, spec, effort, review and environment-variable reference.
- `library/*.md`: short playbooks appended to briefs (`skills = ["tdd-red-green"]` in a spec; defaults in
  `caps.toml [library.defaults]`). `delegate skills` lists them. Add your own as `library/<name>.md`.
- `kb.py` (Hermes kanban card helper) and `task_cost.py` (orchestrator-vs-delegate token report from a Hermes
  session database, `HERMES_SESSION_DB`) are optional Hermes extras.

The CLIs change their flags and model names often. If a run fails after a CLI update, run
`delegate probe --model <model>` and check `delegate --help` against the CLI's own `--help`.

## License

MIT
