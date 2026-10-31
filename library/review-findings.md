---
name: review-findings
description: How to do a read-only code review that is worth reading.
---
- Review the actual diff (`git diff`, `git show`) plus the surrounding code it touches. Change nothing.
- Look in this order: correctness and edge cases, security and data exposure (auth, RLS, secrets, injection), data loss and migrations, concurrency/async, error handling, missing tests, then style.
- Every finding needs: severity (high/medium/low), file:line, concrete evidence (the code or the input that breaks), and a suggested fix.
- Do not report style nits as high. Do not pad. If something is fine, say nothing about it.
- Do not claim a build or test result you did not run; the coordinator runs them. Say "unverified" where relevant.
- End with a one-line verdict: ship, ship after fixes, or do not ship.
