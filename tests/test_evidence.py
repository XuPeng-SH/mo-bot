import json
from pathlib import Path
import tempfile
import unittest

from mo_bot.evidence import completed_checks, local_validation
from mo_bot.store import Store


class TestEvidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.db", "test", 1)
        self.addCleanup(self.store.close)
        self.review = {"repo": "owner/repo", "head": "a" * 40}
        work = {
            "repo": "owner/repo",
            "number": 2,
            "headRefOid": "b" * 40,
            "kind": "work",
            "trigger_key": "repair",
        }
        self.store.enqueue(work)
        self.key = self.store.work_key(work)
        self.stage = self.root / "jobs" / self.key / "implement-1"
        (self.stage / "checks").mkdir(parents=True)
        (self.stage / "checks/unit.json").write_text('{"exit_code":0}')
        (self.stage / "unit.log").write_text("PASS")
        self.final = self.stage.parent / "final_review-2"
        self.final.mkdir()
        (self.final / "ledger.json").write_text(
            '{"candidate":"' + self.review["head"] + '"}'
        )
        self.data = {
            "candidate": self.review["head"],
            "implement": {"artifact": str(self.stage)},
            "final_review": {
                "effort": "xhigh",
                "artifact": str(self.final),
                "result": {
                    "candidate_head": self.review["head"],
                    "accepted": True,
                    "validation_complete": True,
                    "evidence": ["unit.log"],
                },
            },
        }
        self.save()

    def save(self, **fields):
        self.store.update(
            self.key,
            state="done",
            phase="final_review",
            data=json.dumps(self.data),
            **fields,
        )

    def evidence(self, **fields):
        return local_validation(self.store, self.root, {**self.review, **fields})

    def test_exact_candidate_handoff_includes_retained_receipts(self):
        e = self.evidence()[0]
        self.assertEqual(e["candidate_head"], self.review["head"])
        self.assertEqual(e["job_id"], self.key)
        self.assertEqual(e["unavailable"], [])
        receipt = next(a for a in e["artifacts"] if a["path"].endswith("unit.json"))
        self.assertIn("sha256", receipt)
        self.assertEqual(self.evidence(head="c" * 40), [])
        self.assertEqual(self.evidence(repo="other/repo"), [])

    def test_retry_progress_ignores_failed_partial_and_repeated_checks(self):
        receipt = {
            "command": [
                "go",
                "test",
                "-exec=" + str(self.stage / "test-exec"),
                "./pkg/x",
            ],
            "kind": "check",
            "finished_at": 1,
            "exit_code": 1,
            "cwd": str(self.root / "slots/2/worker-source"),
            "source": {"head": "a" * 40, "delta_sha256": "same-source"},
        }
        path = self.stage / "checks/result.json"
        path.write_text(json.dumps(receipt))
        (self.stage / "checks/broken.json").write_text("{")
        self.assertEqual(completed_checks(self.stage), set())
        receipt["exit_code"] = 0
        path.write_text(json.dumps(receipt))
        first = completed_checks(self.stage)
        self.assertEqual(len(first), 1)
        (self.final / "checks").mkdir()
        # New time, cache path, slot and launcher are still the same execution scope.
        receipt.update(
            finished_at=99,
            environment={"GOCACHE": "new-cache"},
            cwd=str(self.root / "slots/3/worker-source"),
        )
        receipt["command"][2] = "-exec=" + str(self.final / "test-exec")
        (self.final / "checks/repeat.json").write_text(json.dumps(receipt))
        self.assertEqual(completed_checks(self.final), first)
        receipt["command"][-1] = "./pkg/y"
        (self.final / "checks/new.json").write_text(json.dumps(receipt))
        self.assertEqual(len(completed_checks(self.final)), 2)

    def test_setup_failure_or_unfingerprinted_receipt_never_counts_as_progress(self):
        path = self.stage / "checks/result.json"
        for receipt in (
            {"exit_code": 0, "command": ["true"]},
            {
                "exit_code": 0,
                "finished_at": 1,
                "command": ["true"],
                "source": {"head": "a", "delta_sha256": "x"},
                "setup_error": "unavailable",
            },
            [],
        ):
            path.write_text(json.dumps(receipt))
            self.assertEqual(completed_checks(self.stage), set())

    def test_incomplete_or_mismatched_gate_is_never_reused(self):
        for field, value in (
            ("accepted", False),
            ("validation_complete", False),
            ("candidate_head", "c" * 40),
        ):
            with self.subTest(field=field):
                original = self.data["final_review"]["result"][field]
                self.data["final_review"]["result"][field] = value
                self.save()
                self.assertEqual(self.evidence(), [])
                self.data["final_review"]["result"][field] = original
        self.data["final_review"]["effort"] = "low"
        self.save()
        self.assertEqual(self.evidence(), [])

    def test_unavailable_or_foreign_artifacts_do_not_block_source_review(self):
        loop = self.stage.parent / "loop"
        loop.symlink_to(loop)
        for path in (self.root / "foreign", self.stage / "missing", loop):
            with self.subTest(path=path):
                self.data["implement"]["artifact"] = str(path)
                self.save()
                e = self.evidence()[0]
                self.assertFalse(
                    any(a["path"].startswith(str(path)) for a in e["artifacts"])
                )
                self.assertTrue(e["unavailable"])
        self.data["implement"]["artifact"] = str(self.stage)
        (self.stage / "escaped.json").symlink_to(self.root / "private")
        self.save()
        e = self.evidence()[0]
        self.assertNotIn(
            str(self.stage / "escaped.json"), [a["path"] for a in e["artifacts"]]
        )
        self.assertIn(str(self.stage / "escaped.json"), e["unavailable"])
