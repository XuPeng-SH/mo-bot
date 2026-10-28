from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mo_bot.review_equivalence import MergeOnlyReviews


class TestMergeOnlyReviews(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.commit("shared", "initial")
        self.git("checkout", "-b", "feature")
        self.reviewed = self.commit("feature", "reviewed feature")
        self.git("checkout", "main")
        self.base = self.commit("main", "upstream")
        self.git("checkout", "feature")
        self.git("merge", "--no-edit", "main")
        self.head = self.git("rev-parse", "HEAD")
        self.checker = MergeOnlyReviews(
            SimpleNamespace(
                source_dir=self.root,
                repository="owner/repo",
                command_timeout_seconds=30,
            )
        )

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.root), *args], stderr=subprocess.DEVNULL, text=True
        ).strip()

    def commit(self, name, text):
        (self.root / name).write_text(text)
        self.git("add", name)
        self.git("commit", "-m", text)
        return self.git("rev-parse", "HEAD")

    def matches(self, **overrides):
        return self.checker.matches(
            {
                "number": 1,
                "headRefOid": self.git("rev-parse", "HEAD"),
                "baseRefName": "main",
                "_defaultBranch": {"name": "main", "oid": self.base},
                **overrides,
            },
            self.reviewed,
        )

    def test_clean_merge_and_repeated_upstream_merges_preserve_worktree(self):
        self.assertTrue(self.matches())
        self.git("checkout", "main")
        self.base = self.commit("main2", "next upstream")
        self.git("checkout", "feature")
        self.git("merge", "--no-edit", "main")
        before = self.git("rev-parse", "HEAD")
        (self.root / "untracked").write_text("keep")
        self.assertTrue(self.matches())
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual((self.root / "untracked").read_text(), "keep")

    def test_new_work_before_or_after_merge_requires_review(self):
        self.commit("new", "new work after merge")
        self.assertFalse(self.matches())
        self.git("reset", "--hard", self.reviewed)
        self.commit("new", "new work before merge")
        self.git("merge", "--no-edit", "main")
        self.assertFalse(self.matches())

    def test_extra_edits_inside_merge_require_review(self):
        (self.root / "feature").write_text("hidden merge edit")
        self.git("add", "feature")
        self.git("commit", "--amend", "--no-edit")
        self.assertFalse(self.matches())

    def test_conflict_resolution_requires_review(self):
        self.git("reset", "--hard", self.reviewed)
        self.reviewed = self.commit("shared", "feature side")
        self.git("checkout", "main")
        self.base = self.commit("shared", "main side")
        self.git("checkout", "feature")
        result = subprocess.run(
            ["git", "-C", str(self.root), "merge", "main"], capture_output=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.commit("shared", "resolved")
        self.assertFalse(self.matches())

    def test_other_branch_merge_and_missing_default_evidence_require_review(self):
        self.git("reset", "--hard", self.reviewed)
        self.git("checkout", "-b", "other")
        self.commit("other", "other feature")
        self.git("checkout", "feature")
        self.git("merge", "--no-ff", "--no-edit", "other")
        self.assertFalse(self.matches())
        self.assertFalse(self.matches(_defaultBranch=None))
        self.assertFalse(self.matches(baseRefName="release"))

    def test_unrelated_reviewed_head_and_failed_proof_do_not_suppress_review(self):
        self.reviewed = self.base
        self.assertFalse(self.matches())
        with patch.object(self.checker, "git", side_effect=TimeoutError("unavailable")):
            self.assertFalse(self.matches())


if __name__ == "__main__":
    unittest.main()
