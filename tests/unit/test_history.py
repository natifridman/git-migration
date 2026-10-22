import copy

from git_migration.steps.verify import verify_issues, verify_merge_requests


class History:
    def __init__(self, author=1):
        self.issue = {
            "iid": 1,
            "title": "Fixture issue",
            "description": "History",
            "state": "closed",
            "author": {"id": author},
            "confidential": True,
        }
        self.note = {
            "id": 5,
            "body": "A comment",
            "author": {"id": author},
            "internal": True,
            "created_at": "2026-01-01T00:00:00Z",
        }
        self.mr = {
            "iid": 2,
            "title": "Fixture MR",
            "source_branch": "topic",
            "target_branch": "main",
            "state": "merged",
            "author": {"id": author},
        }

    def list(self, path, params=None):
        if path.endswith("/issues"):
            return [self.issue]
        if path.endswith("/notes"):
            return [self.note]
        if path.endswith("/merge_requests"):
            return [self.mr]
        if path.endswith("/discussions"):
            return [{"notes": [dict(self.note, resolvable=True, resolved=True)]}]
        raise AssertionError(path)


def test_issues_preserve_comments_metadata_and_report_explicit_identity_mapping():
    source, target = History(1), History(10)
    result = verify_issues(source, target, {"id": 1}, {"id": 2}, {1: 10})
    assert not result["blockers"]
    assert result["comments_verified"] == 1
    assert result["coverage"] == ["issue_comments", "issues"]
    assert all(item["status"] == "mapped" for item in result["identities"])
    target.note["body"] = "Changed"
    assert verify_issues(source, target, {"id": 1}, {"id": 2})["blockers"]


def test_issue_comment_visibility_and_dates_are_required_preservation():
    source, target = History(), History()
    for field, changed in (("internal", False), ("created_at", "2026-02-01T00:00:00Z")):
        target.note = copy.deepcopy(source.note)
        target.note[field] = changed
        assert verify_issues(source, target, {"id": 1}, {"id": 2})["blockers"]


def test_mr_and_note_authors_are_separate_from_content_fidelity():
    source, target = History(1), History(10)
    result = verify_merge_requests(source, target, {"id": 1}, {"id": 2}, user_mapping={1: 10})
    assert not result["blockers"]
    assert {i["status"] for i in result["identities"]} == {"mapped"}
    assert set(result["coverage"]) == {"mr_merged", "discussions", "resolved_discussions"}
    target.note["author"] = {"id": 77, "username": "placeholder_77"}
    result = verify_merge_requests(source, target, {"id": 1}, {"id": 2}, user_mapping={1: 10})
    assert not result["blockers"]
    assert result["identities"][-1]["status"] == "placeholder"


def test_mr_timestamp_loss_is_a_preservation_failure():
    source, target = History(), History()
    source.mr["merged_at"] = "2026-01-01T00:00:00Z"
    result = verify_merge_requests(source, target, {"id": 1}, {"id": 2})
    assert any("metadata or state" in value for value in result["blockers"])
