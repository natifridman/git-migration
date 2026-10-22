import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_migration.git import GitError
from git_migration.steps.verify import verify_merge_requests, verify_repository, verify_runner_smoke


class Provider:
    def __init__(self, mrs=(), discussions=()):
        self.mrs, self.discussions = list(mrs), list(discussions)

    def list(self, path, params=None):
        assert path.startswith("/projects/")
        if "/issues" in path:
            return []
        return self.discussions if path.endswith("/discussions") else self.mrs


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.project = {"id": 1, "default_branch": "main", "import_status": "finished"}
        self.snapshot = {
            "refs": {"refs/heads/main": "a" * 40},
            "default_branch": "main",
            "lfs_oids": [],
        }

    def verify(self, target, baseline=None):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("git_migration.steps.verify.snapshot_repository", return_value=target),
        ):
            return verify_repository(
                Provider(),
                Provider(),
                self.project,
                self.project,
                Path(folder),
                expected_refs=baseline or self.snapshot,
            )

    def test_ref_mismatch_is_blocker_even_with_equal_counts(self):
        target = dict(self.snapshot, refs={"refs/heads/main": "b" * 40})
        result = self.verify(target)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["refs"]["changed"], ["refs/heads/main"])

    def test_missing_and_extra_refs_are_reported(self):
        result = self.verify(dict(self.snapshot, refs={"refs/heads/other": "a" * 40}))
        self.assertEqual(result["refs"]["missing"], ["refs/heads/main"])
        self.assertEqual(result["refs"]["extra"], ["refs/heads/other"])

    def test_lfs_mismatch_and_missing_lfs_tool_block_verification(self):
        self.assertEqual(self.verify(dict(self.snapshot, lfs_oids=["1" * 64]))["status"], "blocked")
        with (
            tempfile.TemporaryDirectory() as folder,
            patch(
                "git_migration.steps.verify.snapshot_repository",
                side_effect=GitError("Git operation failed (exit code 1)"),
            ),
        ):
            result = verify_repository(
                Provider(), Provider(), self.project, self.project, Path(folder), self.snapshot
            )
        self.assertEqual(result["status"], "blocked")

    def test_success_still_requires_new_smoke(self):
        result = self.verify(self.snapshot)
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["smoke_required"])

    def test_merge_request_state_and_discussion_changes_block(self):
        mr = {
            "iid": 2,
            "title": "fix",
            "source_branch": "topic",
            "target_branch": "main",
            "state": "opened",
        }
        result = verify_merge_requests(
            Provider([mr], [{"notes": [{"body": "original"}]}]),
            Provider([dict(mr, state="closed")], [{"notes": [{"body": "changed"}]}]),
            self.project,
            self.project,
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(len(result["blockers"]), 2)

    def test_missing_merge_request_blocks(self):
        result = verify_merge_requests(
            Provider([{"iid": 2, "title": "fix"}]), Provider(), self.project, self.project
        )
        self.assertEqual(result["status"], "blocked")

    def test_iid_mapping_and_placeholder_warning(self):
        mr = {"iid": 2, "title": "fix", "source_branch": "topic", "target_branch": "main"}
        result = verify_merge_requests(
            Provider([mr]),
            Provider([dict(mr, iid=8, author={"username": "placeholder_1"})]),
            self.project,
            self.project,
        )
        self.assertEqual(result["iid_mapping"], {"2": 8})
        self.assertTrue(any("placeholder" in warning for warning in result["warnings"]))

    def test_import_url_changes_and_added_system_notes_are_tolerated(self):
        mr = {"iid": 2, "title": "fix", "source_branch": "topic", "target_branch": "main"}
        old = [{"notes": [{"body": "see https://old.example/group/project/-/issues/1"}]}]
        new = [
            {"notes": [{"body": "see https://new.example/group/project/-/issues/1"}]},
            {"notes": [{"body": "imported from old server", "system": True}]},
        ]
        result = verify_merge_requests(
            Provider([mr], old),
            Provider([mr], new),
            dict(self.project, web_url="https://old.example/group/project"),
            dict(self.project, web_url="https://new.example/group/project"),
        )
        self.assertEqual(result["status"], "passed")
        self.assertTrue(any("system note" in warning for warning in result["warnings"]))

    def test_only_explicit_tool_refs_and_merge_requests_are_ignored(self):
        target = dict(
            self.snapshot, refs={**self.snapshot["refs"], "refs/heads/tool-change": "b" * 40}
        )
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("git_migration.steps.verify.snapshot_repository", return_value=target),
        ):
            result = verify_repository(
                Provider(),
                Provider([{"iid": 42}]),
                self.project,
                self.project,
                Path(folder),
                self.snapshot,
                ignored_refs={"refs/heads/tool-change"},
                ignored_mr_iids={42},
            )
        self.assertEqual(result["status"], "passed")

    def test_smoke_requires_completed_job_on_approved_runner(self):
        job = {
            "id": 3,
            "status": "success",
            "finished_at": "2026-10-21T10:00:00Z",
            "runner": {"id": 4},
        }
        self.assertEqual(verify_runner_smoke(job, [4])["status"], "passed")
        self.assertEqual(verify_runner_smoke(job, [5])["status"], "blocked")
        self.assertEqual(verify_runner_smoke(dict(job, finished_at=None), [4])["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
