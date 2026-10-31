---
name: supabase-staging
description: Supabase migrations and SQL: staging-only, safe patterns.
---
- Staging only. Never run SQL, migrations or functions against production, and never use a service-role key from the repo.
- Migrations are new timestamped files under `supabase/migrations/`; never edit one that has already been applied. Make them idempotent where practical (`if not exists`, `create or replace`).
- Every new table enables RLS with explicit policies. Check policies for `auth.uid()` ownership; no `using (true)` on user data.
- `security definer` functions set `search_path = ''` (or `public`) explicitly and check the caller's rights inside.
- Destructive statements (drop, truncate, delete without where) are out of scope unless the task names them.
- Provide a rollback note and a small SQL check the coordinator can run to prove the migration worked.
