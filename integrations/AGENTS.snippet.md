<!-- agent-delegate:start -->
## Delegating to other coding agents

Launch other coding agents (Codex, Claude Code, Copilot CLI, local Ollama) only through the
`delegate` wrapper (agent-delegate), never by calling their CLIs directly. It enforces quota caps,
restricts each agent's permissions and logs every run.

- If `DELEGATE_CHILD=1` is set you are already a delegated agent: do the task yourself, do not delegate.
- `delegate status` shows remaining quota; `delegate brief --spec x.toml` previews a task for free.
- `delegate start --spec x.toml`, then `delegate wait <job>` and `delegate show <job>`.
- Exit code 3 = blocked by a cap or policy: take the suggested next agent, do not work around it.
- Treat a delegate's report as a claim: read its diff and run the tests yourself before accepting it.
- Never put secrets in a spec or brief.

Reference: run `delegate --help`, or read WRAPPER.md in the agent-delegate install directory.
<!-- agent-delegate:end -->
