---
name: agent-delegate
description: "Use when handing coding work to Codex, Claude Code, Copilot CLI or a local Ollama model. Launch every delegate through delegate.py so quota caps, permissions and model checks are enforced."
---

# agent-delegate

`delegate` (the agent-delegate wrapper) is the only way to launch another coding agent from here.
Never call `codex exec`, `claude -p` or `copilot -p` directly: the wrapper enforces quota caps,
locks down each CLI's permissions, verifies which model answered, and logs every call.

## If you were launched by delegate.py
If the environment has `DELEGATE_CHILD=1`, you ARE the delegate. Do the task yourself; do not delegate.
(The wrapper refuses to launch from inside a delegated agent anyway.)

## Normal loop
1. Check quota first: `delegate status`
2. Write a spec (TOML) for the task: goal, files in scope, acceptance criteria, verify command.
   `delegate brief --spec spec.toml` lints and previews it without spending quota.
3. Launch: `delegate start --spec spec.toml` -> prints a job id immediately.
4. `delegate wait <job> --timeout 600` (exit 6 = still running; call again), then `delegate show <job>`.
5. Follow-ups in the same agent session: `delegate resume <job> "fix X"`.
6. Read-only second opinion on a commit/range: `delegate review --rev <sha|A..B> --workdir <repo> --focus "..."`.

Minimal spec:
```toml
task = "fix-login-timeout"
agent = "codex"            # codex | claude | copilot | qwen
model = "gpt-6.1-sol"      # must be in caps.toml allowed_models
reason = "well-scoped bug fix"
mode = "implement"         # or "review" (read-only)
goal = "Login hangs when the token refresh times out; make it fail fast with an error message."
workdir = "~/code/my-app"
files = ["src/auth/**", "tests/auth/**"]
verify = "npm test -- auth"
acceptance = ["new test reproduces the hang and now passes"]
[worktree]
branch = "agent/fix-login-timeout"
base = "main"
```

## Rules
- Exit code 3 means a cap or policy blocked the run. Use the suggested next agent; never work around a cap.
- Implement mode runs in its own git worktree on an `agent/<task>` branch; protected branches are refused.
- A delegate's report is a claim, not proof. Read the diff in the job's worktree and run the tests yourself
  before accepting the work. `delegate verified <job>` tells you whether the verify result still applies.
- Never put secrets in a spec or brief (the linter rejects secret-looking text). Refer to secrets by name.
- Exit codes: 0 ok, 2 usage, 3 blocked, 4 agent failed/timeout, 5 wrong model, 6 still running,
  7 verify failed, 8 files changed outside scope.

Full reference: `WRAPPER.md` in the agent-delegate install directory (`delegate --help` for flags).
