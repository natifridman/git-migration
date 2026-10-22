import base64

from git_migration.providers import ProviderError
from git_migration.state import atomic_json
from git_migration.steps.smoke import recover_smoke, run_smoke


class Destination:
    url = "https://destination.example.com"
    token = "never-persist-this"

    def __init__(self):
        self.settings = {
            "id": 1,
            "default_branch": "main",
            "builds_access_level": "disabled",
            "ci_config_path": None,
        }
        self.changes = []
        self.branch = None
        self.content = None
        self.active_schedule = False
        self.fail_pipeline = False
        self.runner_id = 12
        self.job_id = 70
        self.pipeline_posts = 0
        self.job_names = []

    def project(self, identifier):
        return dict(self.settings)

    def get(self, path, params=None):
        if path == "/runners/12":
            return {
                "id": 12,
                "status": "online",
                "tag_list": ["linux", "docker"],
                "run_untagged": True,
            }
        if path.endswith("/repository/branches/main"):
            return {"name": "main", "commit": {"id": "base-sha"}, "protected": False}
        if "/repository/commits/" in path:
            return {"parent_ids": ["base-sha"]}
        if "/repository/branches/" in path:
            if self.branch is None:
                raise ProviderError(404)
            return {"name": self.branch, "commit": {"id": "smoke-sha"}, "protected": False}
        if "/repository/files/" in path:
            if self.content is None:
                raise ProviderError(404)
            return {"content": base64.b64encode(self.content.encode()).decode()}
        if path == "/projects/1/pipelines/60":
            return {"id": 60, "sha": "smoke-sha", "ref": self.branch, "status": "success"}
        if path.startswith("/projects/1/jobs/"):
            index = int(path.rsplit("/", 1)[1]) - 70
            return {
                "id": self.job_id + index,
                "name": self.job_names[index],
                "status": "success",
                "finished_at": "2026-10-21T00:00:00Z",
                "runner": {"id": self.runner_id},
                "pipeline": {"id": 60},
                "ref": self.branch,
                "commit": {"id": "smoke-sha"},
            }
        raise AssertionError(path)

    def list(self, path, params=None):
        if path.endswith("/diff"):
            return [
                {
                    "new_path": next(
                        change[2]["actions"][0]["file_path"]
                        for change in self.changes
                        if change[1].endswith("/repository/commits")
                    ),
                    "new_file": True,
                }
            ]
        if path.endswith("/pipeline_schedules"):
            return [{"active": True}] if self.active_schedule else []
        if path.endswith("/runners"):
            return [{"id": 12}]
        if path.endswith("/pipelines"):
            return []
        if path.endswith("/jobs"):
            return [{"id": 70 + index, "name": name} for index, name in enumerate(self.job_names)]
        raise AssertionError(path)

    def post(self, path, data=None):
        self.changes.append(("post", path, data))
        if path.endswith("/repository/branches"):
            self.branch = data["branch"]
            return {"name": self.branch}
        if path.endswith("/repository/commits"):
            from ruamel.yaml import YAML

            self.content = data["actions"][0]["content"]
            parsed = YAML(typ="safe").load(self.content)
            self.job_names = [name for name in parsed if name not in {"workflow", "variables"}]
            return {"id": "smoke-sha"}
        if path.endswith("/pipeline"):
            self.pipeline_posts += 1
            if self.fail_pipeline:
                raise ProviderError()
            return {"id": 60}
        raise AssertionError(path)

    def put(self, path, data=None):
        self.changes.append(("put", path, data))
        self.settings.update(data)
        return dict(self.settings)


def test_safe_smoke_each_tag_set_and_cleanup_order(tmp_path):
    destination = Destination()
    result = run_smoke(
        destination,
        {"id": 1},
        {"allowed_runner_ids": [12]},
        tmp_path,
        "pilot",
        required_tag_sets=[["linux"], ["docker"], ["linux"]],
    )
    assert result["status"] == "passed"
    assert len(result["jobs"]) == 2
    assert result["commit_sha"] == "smoke-sha"
    assert destination.settings["builds_access_level"] == "disabled"
    assert destination.settings["ci_config_path"] is None
    assert destination.changes[-2][2] == {"builds_access_level": "disabled"}
    assert destination.changes[-1][2] == {"ci_config_path": None}
    assert not list(tmp_path.glob("smoke-journal-*.json"))
    assert "include:" not in destination.content
    assert "environment:" not in destination.content
    assert "runner-smoke-ok" in destination.content
    assert "GIT_STRATEGY: none" in destination.content
    assert all("secret" not in str(change) for change in destination.changes)


def test_pipeline_unknown_write_disables_builds_and_does_not_repeat(tmp_path):
    destination = Destination()
    destination.fail_pipeline = True
    result = run_smoke(destination, {"id": 1}, {"allowed_runner_ids": [12]}, tmp_path, "pilot")
    assert result["status"] == "blocked"
    assert destination.pipeline_posts == 1
    assert destination.settings["builds_access_level"] == "disabled"
    assert destination.settings["ci_config_path"] is None


def test_recovery_disables_before_restoring_original_ci(tmp_path):
    destination = Destination()
    destination.settings.update({"builds_access_level": "enabled", "ci_config_path": ".smoke.yml"})
    journal = tmp_path / "nested" / "smoke-journal-1.json"
    atomic_json(
        journal,
        {
            "destination_url": destination.url,
            "project_id": 1,
            "original_ci_config_path": ".gitlab-ci-custom.yml",
            "original_builds_access_level": "disabled",
        },
    )
    assert journal.stat().st_mode & 0o777 == 0o600
    result = recover_smoke(destination, tmp_path)
    assert result["status"] == "passed"
    assert destination.changes[0][2] == {"builds_access_level": "disabled"}
    assert destination.changes[1][2] == {"ci_config_path": ".gitlab-ci-custom.yml"}
    assert not journal.exists()


def test_active_schedule_blocks_without_mutation(tmp_path):
    destination = Destination()
    destination.active_schedule = True
    result = run_smoke(destination, {"id": 1}, {"allowed_runner_ids": [12]}, tmp_path, "pilot")
    assert "schedule" in str(result["blockers"])
    assert destination.changes == []


def test_enabled_builds_refused_without_mutation(tmp_path):
    destination = Destination()
    destination.settings["builds_access_level"] = "enabled"
    result = run_smoke(destination, {"id": 1}, {"allowed_runner_ids": [12]}, tmp_path, "pilot")
    assert result["status"] == "blocked"
    assert destination.changes == []


def test_job_id_must_match_actual_detail(tmp_path):
    destination = Destination()
    destination.job_id = 999
    result = run_smoke(destination, {"id": 1}, {"allowed_runner_ids": [12]}, tmp_path, "pilot")
    assert result["status"] == "blocked"
    assert not result["jobs"]
    assert destination.settings["builds_access_level"] == "disabled"


def test_unapproved_actual_runner_blocks(tmp_path):
    destination = Destination()
    destination.runner_id = 99
    result = run_smoke(destination, {"id": 1}, {"allowed_runner_ids": [12]}, tmp_path, "pilot")
    assert result["status"] == "blocked"
    assert "approved runner" in str(result["blockers"])


def test_recovery_keeps_journal_if_disable_fails(tmp_path):
    destination = Destination()
    journal = tmp_path / "smoke-journal-1.json"
    atomic_json(
        journal,
        {"destination_url": destination.url, "project_id": 1, "original_ci_config_path": None},
    )

    def fail(path, data=None):
        raise ProviderError()

    destination.put = fail
    result = recover_smoke(destination, tmp_path)
    assert result["status"] == "blocked"
    assert journal.exists()


def test_protected_fleet_requires_reviewed_branch_support(tmp_path):
    destination = Destination()
    result = run_smoke(
        destination, {"id": 1}, {"allowed_runner_ids": [12]}, tmp_path, "pilot", protected=True
    )
    assert result["status"] == "blocked"
    assert "Protected-only" in str(result["blockers"])
    assert destination.changes == []


def test_keyboard_interrupt_still_restores_settings(tmp_path):
    import pytest

    destination = Destination()
    original_post = destination.post

    def interrupted_post(path, data=None):
        if path.endswith("/pipeline"):
            raise KeyboardInterrupt
        return original_post(path, data)

    destination.post = interrupted_post
    with pytest.raises(KeyboardInterrupt):
        run_smoke(destination, {"id": 1}, {"allowed_runner_ids": [12]}, tmp_path, "pilot")
    assert destination.settings["builds_access_level"] == "disabled"
    assert destination.settings["ci_config_path"] is None


def test_owned_branch_recovered_after_commit_before_parent_checkpoint(tmp_path, monkeypatch):
    import pytest

    from git_migration.steps import smoke

    destination = Destination()
    original = smoke._prepare_branch

    def commit_then_interrupt(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(smoke, "_prepare_branch", commit_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_smoke(destination, {"id": 1}, {"allowed_runner_ids": [12]}, tmp_path, "pilot")
    ownership = smoke.owned_smoke_refs(destination, 1, tmp_path)
    assert not ownership["blockers"]
    assert ownership["refs"] == {f"refs/heads/{destination.branch}": "smoke-sha"}
    assert destination.settings["builds_access_level"] == "disabled"


def test_owned_branch_tampering_is_never_ignored(tmp_path):
    from git_migration.steps.smoke import owned_smoke_refs

    destination = Destination()
    run_smoke(destination, {"id": 1}, {"allowed_runner_ids": [12]}, tmp_path, "pilot")
    original_get = destination.get

    def changed_branch(path, params=None):
        response = original_get(path, params)
        if "/repository/branches/codex" in path:
            return {**response, "commit": {"id": "unrelated-sha"}}
        return response

    destination.get = changed_branch
    ownership = owned_smoke_refs(destination, 1, tmp_path)
    assert not ownership["refs"]
    assert "changed" in str(ownership["blockers"])


def test_branch_without_ownership_record_is_never_adopted(tmp_path):
    destination = Destination()
    destination.branch = "existing-unowned"
    result = run_smoke(destination, {"id": 1}, {"allowed_runner_ids": [12]}, tmp_path, "pilot")
    assert result["status"] == "blocked"
    assert "without ownership" in str(result["blockers"])
    assert destination.changes == []
