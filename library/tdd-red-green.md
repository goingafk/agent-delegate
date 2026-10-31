---
name: tdd-red-green
description: Tests first: failing test, minimal fix, refactor.
---
1. Write the test for the new behaviour or bug first. Run it and confirm it FAILS for the right reason (assertion on the behaviour, not an import/typo error). Keep that failure output for your report.
2. Write the minimum code that makes it pass. Run the test again and confirm green.
3. Refactor only while green. Re-run after each step.
4. Run the wider suite for the area you touched; report real counts (passed/failed/skipped).
5. A bug fix needs a regression test that fails without your fix. A test you never saw fail proves nothing.
6. Test behaviour, not implementation. One reason to fail per test; names say what is expected.
