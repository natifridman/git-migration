import json

from git_migration.steps.environments import reconcile_environments
from git_migration.steps.pages import reconcile_pages
from git_migration.steps.schedules import reconcile_schedules


class API:
    def __init__(self, records=None):
        self.records = records or {}
        self.writes = []

    def list(self, path, params=None):
        return self.records.get(path, [])

    def get(self, path, params=None):
        return self.records[path]

    def post(self, path, data):
        self.writes.append((path, data))
        return {"id": 7}

    put = post
    patch = post


def test_schedule_secrets_inactive_and_inputs():
    schedule = {
        "id": 1,
        "description": "nightly",
        "cron": "0 1 * * *",
        "ref": "refs/heads/main",
        "active": True,
        "inputs": [{"name": "region", "value": "eu"}],
        "variables": [{"key": "TOKEN", "value": "secret"}],
    }
    source = API(
        {"/projects/1/pipeline_schedules": [schedule], "/projects/1/pipeline_schedules/1": schedule}
    )
    dest = API()
    result = reconcile_schedules(source, dest, 1, 2)
    assert dest.writes[0][1]["active"] is False
    assert dest.writes[0][1]["inputs"] == schedule["inputs"]
    assert "secret" not in json.dumps(result)
    assert not any("/play" in path for path, _ in dest.writes)


def test_missing_protection_mapping_blocks_before_environment_writes():
    source = API(
        {
            "/projects/1/environments": [{"name": "production"}],
            "/projects/1/protected_environments": [
                {"name": "production", "deploy_access_levels": [{"user_id": 8}]}
            ],
        }
    )
    dest = API()
    result = reconcile_environments(source, dest, 1, 2)
    assert result["status"] == "blocked"
    assert not dest.writes


def test_environment_configuration_and_mapped_protections_only():
    source = API(
        {
            "/projects/1/environments": [
                {
                    "name": "production",
                    "external_url": "https://example",
                    "tier": "production",
                    "last_deployment": {"id": 1},
                }
            ],
            "/projects/1/protected_environments": [
                {"name": "production", "deploy_access_levels": [{"user_id": 8}]}
            ],
        }
    )
    dest = API()
    reconcile_environments(source, dest, 1, 2, user_map={8: 88})
    assert "last_deployment" not in dest.writes[0][1]
    assert dest.writes[1][1]["deploy_access_levels"][0]["user_id"] == 88


def test_pages_settings_never_deploy_or_copy_tls():
    source = API(
        {
            "/projects/1/pages": {
                "force_https": True,
                "is_unique_domain_enabled": True,
                "primary_domain": "https://custom.example",
            }
        }
    )
    dest = API({"/projects/2/pages": {"force_https": False, "is_unique_domain_enabled": True}})
    result = reconcile_pages(source, dest, 1, 2)
    assert dest.writes == [("/projects/2/pages", {"pages_https_only": True})]
    assert len(result["manual_actions"]) == 3


def test_imported_schedule_is_deduplicated_and_deactivated():
    schedule = {
        "id": 1,
        "description": "nightly",
        "cron": "0 1 * * *",
        "ref": "refs/heads/main",
        "active": False,
    }
    imported = {**schedule, "id": 9, "active": True, "owner": {"id": 88}}
    source = API(
        {"/projects/1/pipeline_schedules": [schedule], "/projects/1/pipeline_schedules/1": schedule}
    )
    dest = API(
        {"/projects/2/pipeline_schedules": [imported], "/projects/2/pipeline_schedules/9": imported}
    )
    result = reconcile_schedules(source, dest, 1, 2)
    assert result["schedules"][0]["destination_id"] == 9
    assert result["schedules"][0]["destination_owner_id"] == 88
    assert result["schedules"][0]["owner_review_required"] is True
    assert dest.writes == [("/projects/2/pipeline_schedules/9", {"active": False})]


def test_active_schedule_pauses_before_missing_variables_then_reactivates():
    schedule = {
        "id": 1,
        "description": "nightly",
        "cron": "0 1 * * *",
        "ref": "refs/heads/main",
        "active": True,
        "variables": [{"key": "TOKEN", "value": "secret"}],
    }
    imported = {**schedule, "id": 9, "variables": []}
    source = API(
        {"/projects/1/pipeline_schedules": [schedule], "/projects/1/pipeline_schedules/1": schedule}
    )
    dest = API(
        {"/projects/2/pipeline_schedules": [imported], "/projects/2/pipeline_schedules/9": imported}
    )
    reconcile_schedules(source, dest, 1, 2, activate=True)
    assert dest.writes[0] == ("/projects/2/pipeline_schedules/9", {"active": False})
    assert dest.writes[1][0].endswith("/variables")
    assert dest.writes[2] == ("/projects/2/pipeline_schedules/9", {"active": True})


def test_pages_unsupported_capability_reports_blocker():
    class Unavailable(Exception):
        status_code = 404

    def unavailable(*args, **kwargs):
        raise Unavailable()

    source, dest = API(), API()
    source.get = unavailable
    result = reconcile_pages(source, dest, 1, 2)
    assert result["status"] == "blocked"
    assert not dest.writes


def test_schedule_verification_compares_secrets_without_writes_or_disclosure():
    from git_migration.steps.schedules import verify_schedules

    schedule = {
        "id": 1,
        "description": "nightly",
        "cron": "0 1 * * *",
        "ref": "refs/heads/main",
        "active": False,
        "variables": [{"key": "TOKEN", "value": "confidential"}],
    }
    imported = {**schedule, "id": 9, "owner": {"id": 88}}
    source = API(
        {"/projects/1/pipeline_schedules": [schedule], "/projects/1/pipeline_schedules/1": schedule}
    )
    dest = API(
        {"/projects/2/pipeline_schedules": [imported], "/projects/2/pipeline_schedules/9": imported}
    )
    result = verify_schedules(source, dest, 1, 2, {1: 9})
    assert not result["blockers"]
    assert result["schedules"][0]["destination_owner_id"] == 88
    assert "confidential" not in json.dumps(result)
    assert not dest.writes
    imported["variables"] = [{"key": "TOKEN", "value": "wrong"}]
    assert verify_schedules(source, dest, 1, 2, {"1": 9})["blockers"]
    assert not dest.writes


def test_schedule_verification_rejects_active_extra_and_aliased_schedules():
    from git_migration.steps.schedules import verify_schedules

    schedule = {
        "id": 1,
        "description": "nightly",
        "cron": "0 1 * * *",
        "ref": "refs/heads/main",
        "active": False,
    }
    imported = {**schedule, "id": 9, "active": True}
    source = API(
        {"/projects/1/pipeline_schedules": [schedule], "/projects/1/pipeline_schedules/1": schedule}
    )
    dest = API(
        {
            "/projects/2/pipeline_schedules": [imported, {"id": 10}],
            "/projects/2/pipeline_schedules/9": imported,
        }
    )
    result = verify_schedules(source, dest, 1, 2, {1: 9})
    assert len(result["blockers"]) == 2
    assert verify_schedules(source, dest, 1, 2, {1: 9, 2: 9})["blockers"]
    assert not dest.writes
