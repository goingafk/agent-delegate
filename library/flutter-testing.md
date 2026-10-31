---
name: flutter-testing
description: Flutter/Dart analyze and test conventions.
---
- Run `flutter analyze` on the package and fix new warnings you introduce (do not silence with ignores unless justified in the report).
- Unit/widget tests live under `test/`; mirror the lib path. Run one file with `flutter test test/path/x_test.dart`, then the whole suite.
- Widget tests: `tester.pumpWidget` inside a `MaterialApp`; use `pump`/`pumpAndSettle` deliberately; find by key or semantics label, not by text that may be localised.
- Fake network/Supabase/RevenueCat at the repository boundary; tests must not call real services or need secrets.
- Do not run integration tests, device builds or `flutter build` unless the task says so.
- Keep widgets small, state in the existing state-management pattern; do not add a new package without saying so in the report.
