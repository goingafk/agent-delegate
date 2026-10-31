---
name: engineering-discipline
description: Baseline working rules for any implement task.
---
- Read the code and the repo's AGENTS.md / CLAUDE.md before editing. Match existing style, naming and patterns.
- Make the smallest change that meets the acceptance criteria. No drive-by refactors, renames or reformatting.
- Edit only the files in your scope. If the task needs something outside it, stop and say why.
- Run the verify command (or the narrowest test that covers your change) before you report. Quote what it really printed.
- If a test, build or analyzer cannot run in your sandbox, say so plainly. Never write "should pass".
- Do not guess third-party API shapes or schemas. Read the source or docs available locally; list any assumption you had to make.
- Handle errors and empty/null inputs at boundaries; do not swallow exceptions silently.
- Never print, log or commit secret values. Refer to secrets by name.
