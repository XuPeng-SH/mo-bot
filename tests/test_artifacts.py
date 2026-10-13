import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from mo_bot.artifacts import Artifacts, referenced_jobs, remove_scratch, scratch_env


class TestArtifacts(unittest.TestCase):
    def test_go_verification_cache_is_task_owned_and_shared_across_phases(self):
        root = Path("/state")
        first = scratch_env(root, root / "jobs/a/design-1")
        later = scratch_env(root, root / "jobs/a/implement-2")
        other = scratch_env(root, root / "jobs/b/implement-1")
        self.assertEqual(first["GOPATH"], later["GOPATH"])
        self.assertNotEqual(first["GOPATH"], other["GOPATH"])
        self.assertEqual(first["GOPATH"], "/state/jobs/a/cache/go")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gc = Artifacts(self.root)
        self.addCleanup(self.gc.close)

    def job(self, key, state):
        root = self.root / "jobs" / key
        scratch = root / "implement-1" / "tmp"
        scratch.mkdir(parents=True)
        (scratch / "large-build").write_text("disposable")
        (root / "implement-1" / "events.jsonl").write_text("evidence")
        (root / "implement-1" / "result.json").write_text("result")
        (root / "source.patch").write_text("source")
        (root / "cache" / "go-mod").mkdir(parents=True)
        return {"id": key, "state": state}, root

    def sweep(self, jobs, live=()):
        self.gc.next_scan = 0
        self.gc.poll(jobs, set(live))
        if self.gc.future:
            self.gc.future.result(timeout=5)
            self.gc.poll(jobs, set(live))

    def test_terminal_scratch_only_and_live_cleanup_fence(self):
        jobs = []
        for state in (
            "done",
            "failed",
            "superseded",
            "reviewing",
            "publishing",
            "queued",
            "canceling",
        ):
            job, _ = self.job(state, state)
            jobs.append(job)
        source = self.root / "jobs/done/saved-source/tmp"
        source.mkdir(parents=True)
        (source / "user-file").write_text("source data")
        with patch("mo_bot.artifacts.referenced_jobs", return_value=set()):
            self.sweep(jobs, live={"failed"})
        for job in jobs:
            root = self.root / "jobs" / job["id"]
            should_keep = job["state"] not in ("done", "superseded")
            self.assertEqual((root / "implement-1/tmp").exists(), should_keep)
            self.assertEqual((root / "cache").exists(), should_keep)
            self.assertEqual(
                (root / "implement-1/events.jsonl").read_text(), "evidence"
            )
            self.assertTrue((root / "source.patch").exists())
            self.assertTrue((root / "implement-1/result.json").exists())
        self.assertEqual((source / "user-file").read_text(), "source data")

    def test_readonly_module_cache_symlinks_and_retry_new_scratch(self):
        job, root = self.job("done", "done")
        outside = self.root / "valuable"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        scratch = root / "implement-1/tmp"
        (scratch / "outside").symlink_to(outside, target_is_directory=True)
        readonly = scratch / "gomod/pkg/module"
        readonly.mkdir(parents=True)
        (readonly / "file").write_text("cache")
        readonly.chmod(0o500)
        readonly.parent.chmod(0o500)
        entered, proceed = threading.Event(), threading.Event()
        original = remove_scratch

        def delayed(path, stopping):
            entered.set()
            self.assertTrue(proceed.wait(5))
            return original(path, stopping)

        with (
            patch("mo_bot.artifacts.referenced_jobs", return_value=set()),
            patch("mo_bot.artifacts.remove_scratch", side_effect=delayed),
        ):
            self.gc.poll([job], set())
            self.assertTrue(entered.wait(5))
            # Reentry can create fresh scratch after detachment; background GC
            # has no access to the replacement, even at an identical pathname.
            scratch.mkdir()
            (scratch / "new-build").write_text("new")
            proceed.set()
            self.gc.future.result(timeout=5)
        self.assertEqual((outside / "keep").read_text(), "keep")
        self.assertEqual((scratch / "new-build").read_text(), "new")

    def test_delete_failure_retried_after_restart(self):
        job, root = self.job("done", "done")
        with (
            patch("mo_bot.artifacts.referenced_jobs", return_value=set()),
            patch(
                "mo_bot.artifacts.remove_scratch",
                side_effect=PermissionError("injected"),
            ),
        ):
            self.sweep([job])
        self.assertFalse((root / "implement-1/tmp").exists())
        self.assertTrue(self.gc.status["errors"])
        self.assertTrue(list(self.gc.trash.iterdir()))
        self.gc.close()
        replacement = Artifacts(self.root)
        self.addCleanup(replacement.close)
        replacement.poll([], set())
        replacement.future.result(timeout=5)
        self.assertFalse(list(replacement.trash.iterdir()))
        self.assertTrue((root / "source.patch").exists())

    def test_open_file_blocks_gc_until_released(self):
        job, root = self.job("open", "done")
        scratch = root / "implement-1/tmp"
        # Observe real /proc fd links without unrelated host/container mounts
        # of the test runner's /tmp influencing this isolated task.
        proc = self.root / "proc" / str(os.getpid())
        proc.mkdir(parents=True)
        (proc / "fd").symlink_to(Path("/proc/self/fd"), target_is_directory=True)
        with patch(
            "mo_bot.artifacts.referenced_jobs",
            side_effect=lambda root, ids: referenced_jobs(root, ids, proc.parent),
        ):
            with (scratch / "large-build").open() as handle:
                self.sweep([job])
                self.assertFalse(handle.closed)
                self.assertTrue(scratch.exists())
            self.sweep([job])
        self.assertFalse(scratch.exists())

    def test_unexpected_background_error_does_not_stick_and_retries(self):
        job, root = self.job("worker-error", "done")
        with (
            patch("mo_bot.artifacts.referenced_jobs", return_value=set()),
            patch(
                "mo_bot.artifacts.remove_scratch",
                side_effect=RuntimeError("worker failed"),
            ),
        ):
            self.gc.poll([job], set())
            with self.assertRaisesRegex(RuntimeError, "worker failed"):
                self.gc.future.result(timeout=5)
            self.gc.poll([job], set())
        self.assertIsNone(self.gc.future)
        self.assertIn("worker failed", self.gc.status["errors"])
        with patch("mo_bot.artifacts.referenced_jobs", return_value=set()):
            self.sweep([job])
        self.assertFalse(list(self.gc.trash.iterdir()))
        self.assertTrue((root / "source.patch").exists())

    def test_container_mounts_environment_and_ancestor_bind_are_protected(self):
        root = self.root / "jobs"
        proc = self.root / "proc/123"
        proc.mkdir(parents=True)
        (proc / "environ").write_text(f"TMPDIR={root}/one/implement-1/tmp\0")
        (proc / "mountinfo").write_text(
            f"1 0 8:0 {root}/two /work rw - ext4 /dev/sda rw\n"
        )
        self.assertEqual(
            referenced_jobs(root, {"one", "two", "three"}, proc.parent), {"one", "two"}
        )
        (proc / "mountinfo").write_text(
            f"1 0 8:0 {self.root} /work rw - ext4 /dev/sda rw\n"
        )
        self.assertEqual(
            referenced_jobs(root, {"one", "two", "three"}, proc.parent),
            {"one", "two", "three"},
        )

    def test_symlinked_trash_refused_and_unknown_jobs_preserved(self):
        _, root = self.job("unknown", "done")
        self.sweep([])
        self.assertTrue((root / "implement-1/tmp").exists())
        self.gc.trash.rmdir()
        outside = self.root / "outside"
        outside.mkdir()
        self.gc.trash.symlink_to(outside, target_is_directory=True)
        self.gc.next_scan = 0
        with self.assertRaisesRegex(ValueError, "symlinked"):
            self.gc.poll([], set())

    def test_shutdown_preserves_unfinished_detached_tree(self):
        path = self.root / "scratch-trash/old"
        path.mkdir(parents=True)
        (path / "cache").write_text("cache")
        stopping = threading.Event()
        stopping.set()
        self.assertFalse(remove_scratch(path, stopping))
        self.assertTrue((path / "cache").exists())
        self.assertTrue(remove_scratch(path, threading.Event()))
        self.assertFalse(path.exists())

    def test_shared_cache_is_job_scoped_and_tmp_is_attempt_scoped(self):
        a = scratch_env(self.root, self.root / "jobs/a/design-1")
        b = scratch_env(self.root, self.root / "jobs/a/implement-2")
        other = scratch_env(self.root, self.root / "jobs/b/design-1")
        self.assertEqual(a["GOCACHE"], b["GOCACHE"])
        self.assertEqual(a["GOMODCACHE"], b["GOMODCACHE"])
        self.assertNotEqual(a["GOCACHE"], other["GOCACHE"])
        self.assertNotEqual(a["TMPDIR"], b["TMPDIR"])
