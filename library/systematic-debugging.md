---
name: systematic-debugging
description: Root-cause debugging before any fix.
---
1. Reproduce first. Get the exact failing command/steps and the real error text. If you cannot reproduce, say so; do not patch blind.
2. Read the whole error and stack trace. Find the first frame in our code.
3. Form one hypothesis at a time. Test it with a log, a print-free probe, a narrower test or a git bisect, not by editing code speculatively.
4. Check what changed recently (`git log -p` on the file) and compare with a working path.
5. Fix the root cause, not the symptom. State the cause in one sentence in your report.
6. Add a regression test that fails before the fix. Remove any debug output before finishing.
7. If two attempted fixes fail, stop and report what you learned instead of piling on changes.
