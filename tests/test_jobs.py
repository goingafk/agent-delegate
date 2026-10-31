from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import jobs

UTC = dt.timezone.utc


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def record(self, job_id="j1", **changes):
        value = {"id": job_id, "status": "running", "pid": os.getpid(), "agent": "codex", "model": "gpt-6.1-sol",
                 "task": "T", "created_at": "2026-10-17T00:00:00Z"}
        value.update(changes)
        return value

    def test_ids_are_unique_sortable_and_filename_safe(self):
        now = dt.datetime(2026, 10, 17, 1, 2, 3, tzinfo=UTC)
        ids = {jobs.new_job_id(now) for _ in range(50)}
        self.assertEqual(len(ids), 50)
        for job_id in ids:
            self.assertRegex(job_id, r"^j20261017-010203-[0-9a-f]{8}$")

    def test_write_then_read_round_trips_atomically(self):
        jobs.write_job(self.home, self.record())
        self.assertEqual(jobs.read_job(self.home, "j1")["task"], "T")
        self.assertEqual([p.name for p in (self.home / "state" / "jobs").iterdir()], ["j1.json"])  # no temp files left

    def test_missing_or_corrupt_records_read_as_none(self):
        self.assertIsNone(jobs.read_job(self.home, "nope"))
        (self.home / "state" / "jobs").mkdir(parents=True)
        (self.home / "state" / "jobs" / "bad.json").write_text("{not json")
        self.assertIsNone(jobs.read_job(self.home, "bad"))

    def test_job_ids_cannot_escape_the_jobs_directory(self):
        for bad in ("../x", "a/b", "", ".."):
            with self.assertRaises(ValueError):
                jobs.job_path(self.home, bad)

    def test_pid_alive(self):
        self.assertTrue(jobs.pid_alive(os.getpid()))
        child = subprocess.Popen(["true"])
        child.wait()
        self.assertFalse(jobs.pid_alive(child.pid))
        self.assertFalse(jobs.pid_alive(None))
        self.assertFalse(jobs.pid_alive(0))

    def test_running_job_with_a_dead_process_becomes_lost_and_is_saved(self):
        child = subprocess.Popen(["true"])
        child.wait()
        jobs.write_job(self.home, self.record(pid=child.pid))
        refreshed = jobs.refresh(self.home, jobs.read_job(self.home, "j1"))
        self.assertEqual(refreshed["status"], "lost")
        self.assertEqual(jobs.read_job(self.home, "j1")["status"], "lost")

    def test_a_just_queued_job_without_a_pid_is_not_declared_lost(self):
        fresh = dt.datetime.now(UTC).isoformat().replace("+00:00", "Z")
        jobs.write_job(self.home, self.record(status="queued", pid=None, created_at=fresh))
        self.assertEqual(jobs.refresh(self.home, jobs.read_job(self.home, "j1"))["status"], "queued")
        jobs.write_job(self.home, self.record(status="queued", pid=None, created_at="2020-01-01T00:00:00Z"))
        self.assertEqual(jobs.refresh(self.home, jobs.read_job(self.home, "j1"))["status"], "lost")

    def test_read_job_needs_an_id_matching_the_file_name(self):
        (self.home / "state" / "jobs").mkdir(parents=True)
        (self.home / "state" / "jobs" / "noid.json").write_text(json.dumps({"status": "done"}))
        (self.home / "state" / "jobs" / "other.json").write_text(json.dumps({"id": "different", "status": "done"}))
        self.assertIsNone(jobs.read_job(self.home, "noid"))
        self.assertIsNone(jobs.read_job(self.home, "other"))

    def test_proc_start_identifies_a_process_and_is_none_for_dead_pids(self):
        # ps is denied by some sandboxes; test its contract without depending on host permissions.
        stamp = "Wed Oct 28 12:00:00 2026"
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stamp + "\n", "")) as run:
            self.assertEqual(jobs.proc_start(123), stamp)
            self.assertEqual(run.call_args.args[0], ["ps", "-o", "lstart=", "-p", "123"])
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.assertIsNone(jobs.proc_start(123))
        with mock.patch("subprocess.run", side_effect=PermissionError):
            self.assertIsNone(jobs.proc_start(123))

    def test_a_recycled_pid_is_not_mistaken_for_the_job(self):
        jobs.write_job(self.home, self.record(proc_started="Thu Jan  1 00:00:00 1970"))
        self.assertEqual(jobs.refresh(self.home, jobs.read_job(self.home, "j1"))["status"], "lost")

    def test_matching_proc_start_keeps_the_job_running(self):
        jobs.write_job(self.home, self.record(proc_started=jobs.proc_start(os.getpid())))
        self.assertEqual(jobs.refresh(self.home, jobs.read_job(self.home, "j1"))["status"], "running")

    def test_finished_jobs_are_not_touched_by_refresh(self):
        jobs.write_job(self.home, self.record(status="done", pid=99999999))
        self.assertEqual(jobs.refresh(self.home, jobs.read_job(self.home, "j1"))["status"], "done")

    def test_list_is_newest_first_and_active_filter_works(self):
        jobs.write_job(self.home, self.record("j1", created_at="2026-10-17T00:00:01Z", status="done"))
        jobs.write_job(self.home, self.record("j2", created_at="2026-10-17T00:00:03Z"))
        jobs.write_job(self.home, self.record("j3", created_at="2026-10-17T00:00:02Z", agent="claude"))
        self.assertEqual([r["id"] for r in jobs.list_jobs(self.home)], ["j2", "j3", "j1"])
        self.assertEqual([r["id"] for r in jobs.active_jobs(self.home)], ["j2", "j3"])
        self.assertEqual([r["id"] for r in jobs.active_jobs(self.home, "claude")], ["j3"])

    def test_age_strings_are_short(self):
        self.assertEqual(jobs.age_str(5), "5s")
        self.assertEqual(jobs.age_str(125), "2m05s")
        self.assertEqual(jobs.age_str(3 * 3600 + 120), "3h02m")
        self.assertEqual(jobs.age_str(50 * 3600), "2d02h")

    def test_prune_removes_only_old_finished_jobs_and_old_run_logs(self):
        now = dt.datetime(2026, 10, 30, tzinfo=UTC)
        old = (now - dt.timedelta(days=20)).timestamp()
        new = (now - dt.timedelta(days=1)).timestamp()
        jobs.write_job(self.home, self.record("old", status="done", finished_at="2026-10-01T00:00:00Z"))
        jobs.write_job(self.home, self.record("recent", status="done", finished_at="2026-10-29T00:00:00Z"))
        jobs.write_job(self.home, self.record("active", status="running", created_at="2026-09-01T00:00:00Z"))
        for name in ("old.result.txt", "old.log"):
            (self.home / "state" / "jobs" / name).write_text("x")
        runs = self.home / "runs"
        runs.mkdir()
        for name, stamp in (("a.jsonl", old), ("a-last.txt", old), ("b.jsonl", new)):
            (runs / name).write_text("x")
            os.utime(runs / name, (stamp, stamp))
        dry = jobs.prune(self.home, 14, now, dry_run=True)
        self.assertEqual(dry, {"jobs": 1, "runs": 2})
        self.assertTrue((runs / "a.jsonl").exists())
        result = jobs.prune(self.home, 14, now)
        self.assertEqual(result, {"jobs": 1, "runs": 2})
        self.assertFalse((self.home / "state" / "jobs" / "old.json").exists())
        self.assertFalse((self.home / "state" / "jobs" / "old.result.txt").exists())
        self.assertFalse((self.home / "state" / "jobs" / "old.log").exists())
        self.assertTrue((self.home / "state" / "jobs" / "recent.json").exists())
        self.assertTrue((self.home / "state" / "jobs" / "active.json").exists())
        self.assertEqual(sorted(p.name for p in runs.iterdir()), ["b.jsonl"])

    def test_prune_also_clears_old_rendered_briefs(self):
        now = dt.datetime(2026, 10, 30, tzinfo=UTC)
        old = (now - dt.timedelta(days=20)).timestamp()
        briefs = self.home / "state" / "briefs"
        briefs.mkdir(parents=True)
        for name, stamp in (("old.md", old), ("new.md", now.timestamp())):
            (briefs / name).write_text("x")
            os.utime(briefs / name, (stamp, stamp))
        self.assertEqual(jobs.prune(self.home, 14, now)["runs"], 1)
        self.assertEqual([p.name for p in briefs.iterdir()], ["new.md"])

    def test_maybe_prune_runs_at_most_once_a_day(self):
        now = dt.datetime(2026, 10, 30, tzinfo=UTC)
        self.assertIsNotNone(jobs.maybe_prune(self.home, 14, now))
        self.assertIsNone(jobs.maybe_prune(self.home, 14, now + dt.timedelta(hours=3)))
        self.assertIsNotNone(jobs.maybe_prune(self.home, 14, now + dt.timedelta(hours=25)))

    def test_limits_from_caps_with_defaults(self):
        self.assertEqual(jobs.limits({}), (4, {}))
        total, per_agent = jobs.limits({"jobs": {"max_running": 3, "per_agent": {"codex": 1}}})
        self.assertEqual((total, per_agent), (3, {"codex": 1}))

    def test_slot_check_reports_which_jobs_hold_the_slots(self):
        jobs.write_job(self.home, self.record("j1"))
        jobs.write_job(self.home, self.record("j2"))
        caps = {"jobs": {"max_running": 4, "per_agent": {"codex": 2, "claude": 2}}}
        reason = jobs.slot_block(self.home, "codex", caps)
        self.assertIn("codex", reason)
        self.assertIn("j1", reason)
        self.assertIn("j2", reason)
        self.assertIsNone(jobs.slot_block(self.home, "claude", caps))
        caps["jobs"]["max_running"] = 2
        self.assertIn("total", jobs.slot_block(self.home, "claude", caps))


if __name__ == "__main__":
    unittest.main()
