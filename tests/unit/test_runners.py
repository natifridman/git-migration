import base64

import pytest

from git_migration.steps.runners import (
    RunnerBlocked,
    _job_tags,
    _yaml,
    configure_runners,
    inspect_runners,
    prepare_runner_merge_request,
    smoke_job_yaml,
    transform_ci_yaml,
)


def test_roundtrip_anchors_reference_and_only_tags():
    source = ".tags: &runner\n  tags: [old, linux]\njob:\n  <<: *runner\n  script: echo old\n  before_script: !reference [.template, before_script]\n"
    result = transform_ci_yaml(source, {"old": ["new", "docker"]})
    assert not result["blockers"]
    assert "&runner" in result["content"] and "*runner" in result["content"]
    assert "!reference" in result["content"]
    assert "echo old" in result["content"]
    assert _yaml().load(result["content"])["job"]["tags"] == ["new", "docker", "linux"]


def test_inherited_default_untagged_and_dynamic_tags():
    doc = _yaml().load(
        "default:\n  tags: [linux]\n.base:\n  tags: [docker, linux]\na:\n  extends: .base\n  script: true\nb:\n  inherit:\n    default: false\n  script: true\nc:\n  script: true\n"
    )
    jobs, blockers = _job_tags(doc)
    assert not blockers
    assert [j["tags"] for j in jobs] == [["docker", "linux"], [], ["linux"]]
    assert transform_ci_yaml("job:\n  tags: [$RUNNER]\n", {})["blockers"]
    assert transform_ci_yaml("job:\n  tags: !reference [.base, tags]\n", {})["blockers"]


class RunnerAPI:
    def __init__(self):
        self.writes = []
        self.runners = [
            {
                "id": 1,
                "tag_list": ["linux", "docker"],
                "run_untagged": True,
                "status": "online",
                "runner_type": "project_type",
            },
            {
                "id": 2,
                "tag_list": ["linux"],
                "run_untagged": False,
                "status": "online",
                "access_level": "ref_protected",
                "runner_type": "group_type",
            },
        ]

    def list(self, path, params=None):
        return self.runners

    def get(self, path, params=None):
        return next(r for r in self.runners if str(r["id"]) == path.split("/")[-1])

    def put(self, path, data):
        self.writes.append((path, data))

    post = put


def test_all_tags_protected_and_unapproved_inherited():
    api = RunnerAPI()
    assert inspect_runners(api, 10, [1], ["linux", "docker"])["eligible_runner_ids"] == [1]
    assert not inspect_runners(api, 10, [1], ["linux"])["blockers"]
    assert inspect_runners(api, 10, [1], ["linux"], protected_ref=True)["blockers"]
    assert inspect_runners(api, 10, [1], untagged=True)["eligible_runner_ids"] == [1]
    configure_runners(api, 10, [1])
    assert all("/runners/" not in path for path, _ in api.writes)


class Missing(Exception):
    status_code = 404


class CIAPI:
    def __init__(self):
        self.files = {"main": "job:\n  tags: [old]\n  script: echo ok\n"}
        self.mrs = []
        self.commits = 0
        self.heads = {"sha": "main"}

    def project(self, identifier):
        return {"builds_access_level": "disabled"}

    def get(self, path, params=None):
        if path.endswith("/repository/compare"):
            return {"diffs": [{"new_path": ".gitlab-ci.yml"}], "compare_timeout": False}
        if "/repository/branches/" in path:
            ref = path.split("/branches/")[-1].replace("%2F", "/")
            if ref not in self.files:
                raise Missing()
            return {"commit": {"id": "sha" if ref == "main" else "migration-sha"}}
        if ".gitmodules" in path:
            raise Missing()
        return {
            "encoding": "base64",
            "content": base64.b64encode(
                self.files[self.heads.get(params["ref"], params["ref"])].encode()
            ).decode(),
        }

    def list(self, path, params=None):
        return self.mrs

    def post(self, path, data):
        if path.endswith("/branches"):
            self.files[data["branch"]] = self.files[self.heads.get(data["ref"], data["ref"])]
        elif path.endswith("/commits"):
            self.commits += 1
            self.heads["migration-sha"] = data["branch"]
            self.files[data["branch"]] = data["actions"][0]["content"]
        elif path.endswith("/lint"):
            return {"valid": True}
        elif path.endswith("/merge_requests"):
            self.mrs.append({"iid": 1, "web_url": "https://example/mr/1"})
            return self.mrs[0]
        return {}


def test_retry_has_no_duplicate_mr_or_commit():
    api = CIAPI()
    first = prepare_runner_merge_request(api, 1, "main", {"old": ["new"]})
    second = prepare_runner_merge_request(api, 1, "main", {"old": ["new"]})
    assert first["iid"] == second["iid"]
    assert api.commits == 1 and len(api.mrs) == 1
    assert api.files["main"].startswith("job:\n  tags: [old]")


def test_preparation_requires_disabled_ci():
    api = CIAPI()
    api.project = lambda _: {"builds_access_level": "enabled"}
    with pytest.raises(RunnerBlocked, match="disabled"):
        prepare_runner_merge_request(api, 1, "main", {})


def test_smoke_config_has_no_source_or_secret_output():
    content = smoke_job_yaml(tags=["approved"])
    doc = _yaml().load(content)
    assert "include" not in doc
    assert doc["migration-runner-smoke"]["inherit"]["variables"] is False
    assert doc["migration-runner-smoke"]["variables"]["GIT_STRATEGY"] == "none"
    assert "$" not in doc["migration-runner-smoke"]["script"][0]


def test_offline_unapproved_runner_is_a_potential_allowlist_violation():
    api = RunnerAPI()
    api.runners[1].update(status="offline", access_level="not_protected")
    assert inspect_runners(api, 10, [1], ["linux"])["blockers"]
    api.runners = [api.runners[0]]
    api.runners[0]["status"] = "offline"
    assert "offline" in inspect_runners(api, 10, [1], ["linux"])["blockers"][0]


def test_retry_refuses_tampered_migration_branch():
    api = CIAPI()
    result = prepare_runner_merge_request(api, 1, "main", {"old": ["new"]})
    assert result["commit_sha"] == "migration-sha"
    api.files[result["source_branch"]] += "tampered:\n  script: echo changed\n"
    with pytest.raises(RunnerBlocked, match="conflicting edits"):
        prepare_runner_merge_request(api, 1, "main", {"old": ["new"]})
    assert len(api.mrs) == 1


def test_retry_refuses_unrelated_branch_changes():
    api = CIAPI()
    prepare_runner_merge_request(api, 1, "main", {"old": ["new"]})
    original_get = api.get

    def get(path, params=None):
        if path.endswith("/repository/compare"):
            return {"diffs": [{"new_path": ".gitlab-ci.yml"}, {"new_path": "application.py"}]}
        return original_get(path, params)

    api.get = get
    with pytest.raises(RunnerBlocked, match="outside"):
        prepare_runner_merge_request(api, 1, "main", {"old": ["new"]})


def test_unchanged_configuration_is_linted_without_committing():
    api = CIAPI()
    calls = []
    original_post = api.post

    def post(path, data):
        calls.append(path)
        return original_post(path, data)

    api.post = post
    result = prepare_runner_merge_request(api, 1, "main", {})
    assert result["status"] == "unchanged" and result["changed"] is False
    assert result["jobs"] == [{"job": "job", "tags": ["old"]}]
    assert "/projects/1/ci/lint" in calls
    assert api.commits == 0 and not api.mrs


def test_mapped_project_includes_require_and_use_merged_lint_jobs():
    api = CIAPI()
    api.files["main"] = (
        "include:\n  - project: destination/templates\n    file: /template.yml\njob:\n  extends: .template\n  script: echo ok\n"
    )
    first = prepare_runner_merge_request(
        api, 1, "main", {}, project_include_map={"destination/templates": "destination/templates"}
    )
    assert first["status"] == "blocked"
    original_post = api.post

    def post(path, data):
        if path.endswith("/lint"):
            return {
                "valid": True,
                "merged_yaml": ".template:\n  tags: [approved]\njob:\n  extends: .template\n  script: echo ok\n",
            }
        return original_post(path, data)

    api.post = post
    result = prepare_runner_merge_request(
        api, 1, "main", {}, project_include_map={"destination/templates": "destination/templates"}
    )
    assert result["status"] == "unchanged"
    assert result["jobs"] == [{"job": "job", "tags": ["approved"]}]


def test_local_include_is_read_for_inherited_job_tags():
    from urllib.parse import unquote

    api = CIAPI()
    api.files["main"] = (
        "include:\n  - local: /ci/templates.yml\njob:\n  extends: .template\n  script: echo ok\n"
    )
    original_get = api.get
    reads = []

    def get(path, params=None):
        if "/repository/files/" in path:
            name = unquote(path.split("/repository/files/")[-1])
            reads.append(name)
            if name == "ci/templates.yml":
                return {
                    "encoding": "base64",
                    "content": base64.b64encode(b".template:\n  tags: [local-runner]\n").decode(),
                }
        return original_get(path, params)

    api.get = get
    result = prepare_runner_merge_request(api, 1, "main", {})
    assert result["status"] == "unchanged"
    assert result["jobs"] == [{"job": "job", "tags": ["local-runner"]}]
    assert "ci/templates.yml" in reads


class ReviewedMRAPI:
    def project(self, project_id):
        return {"ci_config_path": None, "auto_devops_enabled": False}

    def __init__(self):
        self.mr = {
            "iid": 1,
            "source_branch": "codex/migration",
            "target_branch": "main",
            "sha": "prepared-sha",
            "state": "merged",
            "merge_commit_sha": "merge-sha",
            "squash_commit_sha": "squash-sha",
        }
        self.branches = {"codex/migration": "prepared-sha", "main": "merge-sha"}
        self.comparison = {"diffs": [], "compare_timeout": False}
        self.compares = []
        self.lints = []

    def get(self, path, params=None):
        from urllib.parse import unquote

        if "/merge_requests/" in path:
            return self.mr
        if "/repository/branches/" in path:
            name = unquote(path.split("/repository/branches/")[-1])
            if name not in self.branches:
                raise Missing()
            return {"commit": {"id": self.branches[name]}}
        if path.endswith("/repository/compare"):
            self.compares.append(params)
            return self.comparison
        return {
            "encoding": "base64",
            "content": base64.b64encode(b"job:\n  tags: [approved]\n  script: echo ok\n").decode(),
        }

    def post(self, path, data):
        assert path.endswith("/ci/lint")
        self.lints.append(data)
        return {"valid": True, "merged_yaml": "job:\n  tags: [approved]\n  script: echo ok\n"}


def _prepared():
    return {
        "iid": 1,
        "source_branch": "codex/migration",
        "target_branch": "main",
        "commit_sha": "prepared-sha",
    }


def test_reviewed_merge_verifies_merge_tree_before_squash_and_lints_target():
    from git_migration.steps.runners import verify_prepared_merge_request

    api = ReviewedMRAPI()
    result = verify_prepared_merge_request(api, 1, _prepared())
    assert not result["blockers"]
    assert result["merged_sha"] == "merge-sha"
    assert api.compares[0] == {"from": "prepared-sha", "to": "merge-sha", "straight": True}
    assert api.lints[0]["ref"] == "main"
    assert result["jobs"] == [{"job": "job", "tags": ["approved"]}]


@pytest.mark.parametrize(
    "comparison",
    [
        {"diffs": [{"new_path": "application.py"}]},
        {"diffs": [], "compare_timeout": True},
        {"diffs": [], "overflow": True},
        {},
    ],
)
def test_reviewed_merge_rejects_unexpected_or_unverifiable_tree(comparison):
    from git_migration.steps.runners import verify_prepared_merge_request

    api = ReviewedMRAPI()
    api.comparison = comparison
    result = verify_prepared_merge_request(api, 1, _prepared())
    assert result["blockers"] and not api.lints


def test_reviewed_merge_allows_deleted_source_but_not_tampered_retained_source():
    from git_migration.steps.runners import verify_prepared_merge_request

    api = ReviewedMRAPI()
    del api.branches["codex/migration"]
    assert not verify_prepared_merge_request(api, 1, _prepared())["blockers"]
    api.branches["codex/migration"] = "tampered-sha"
    assert verify_prepared_merge_request(api, 1, _prepared())["blockers"]


def test_saved_open_mr_revalidates_and_lints_source_on_resume():
    from git_migration.steps.runners import verify_prepared_merge_request

    api = ReviewedMRAPI()
    api.mr["state"] = "opened"
    assert verify_prepared_merge_request(api, 1, _prepared())["blockers"]
    result = verify_prepared_merge_request(api, 1, _prepared(), require_merged=False)
    assert not result["blockers"] and result["merged_sha"] is None
    assert api.lints[0]["ref"] == "codex/migration"
    api.mr["sha"] = "changed-sha"
    assert verify_prepared_merge_request(api, 1, _prepared(), require_merged=False)["blockers"]


def test_reviewed_merge_rejects_target_advance():
    from git_migration.steps.runners import verify_prepared_merge_request

    api = ReviewedMRAPI()
    api.branches["main"] = "later-sha"
    assert verify_prepared_merge_request(api, 1, _prepared())["blockers"]
    assert not api.lints


def test_unchanged_runner_verification_freshly_lints_baseline_target():
    from git_migration.steps.runners import verify_runner_configuration

    api = ReviewedMRAPI()
    prepared = {
        "status": "unchanged",
        "changed": False,
        "source_sha": "merge-sha",
        "target_branch": "main",
    }
    result = verify_runner_configuration(api, 1, prepared)
    assert not result["blockers"]
    assert result["jobs"] == [{"job": "job", "tags": ["approved"]}]
    assert api.lints[0]["ref"] == "main"
    api.branches["main"] = "drift-sha"
    assert verify_runner_configuration(api, 1, prepared)["blockers"]


def test_runner_verification_rejects_changed_project_ci_path_for_any_mr_state():
    from git_migration.steps.runners import (
        verify_prepared_merge_request,
        verify_runner_configuration,
    )

    api = ReviewedMRAPI()
    api.project = lambda _: {"ci_config_path": "different.yml"}
    prepared = {
        "status": "unchanged",
        "changed": False,
        "source_sha": "merge-sha",
        "target_branch": "main",
    }
    assert verify_runner_configuration(api, 1, prepared)["blockers"]
    assert verify_prepared_merge_request(api, 1, _prepared())["blockers"]
    assert not api.lints


def test_no_ci_verification_requires_root_absence_and_explicit_auto_devops_disabled():
    from git_migration.steps.runners import verify_runner_configuration

    api = ReviewedMRAPI()
    prepared = {
        "status": "no_ci",
        "changed": False,
        "source_sha": "merge-sha",
        "target_branch": "main",
    }
    assert verify_runner_configuration(api, 1, prepared)["blockers"]
    original_get = api.get

    def get(path, params=None):
        if "/repository/files/" in path:
            raise Missing()
        return original_get(path, params)

    api.get = get
    assert not verify_runner_configuration(api, 1, prepared)["blockers"]
    api.project = lambda _: {"auto_devops_enabled": True}
    assert verify_runner_configuration(api, 1, prepared)["blockers"]
    api.project = lambda _: {"auto_devops_enabled": None}
    assert verify_runner_configuration(api, 1, prepared)["blockers"]
    assert not api.lints


def test_no_mr_runner_verification_rejects_incomplete_or_blocked_preparation():
    from git_migration.steps.runners import verify_runner_configuration

    api = ReviewedMRAPI()
    for prepared in (
        {"status": "unchanged"},
        {"status": "blocked"},
        {"status": "no_ci", "changed": True},
    ):
        assert verify_runner_configuration(api, 1, prepared)["blockers"]
    assert not api.lints


def test_unchanged_runner_verification_requires_merged_lint_configuration():
    from git_migration.steps.runners import verify_runner_configuration

    api = ReviewedMRAPI()
    api.post = lambda path, data: {"valid": True}
    prepared = {
        "status": "unchanged",
        "changed": False,
        "source_sha": "merge-sha",
        "target_branch": "main",
    }
    assert verify_runner_configuration(api, 1, prepared)["blockers"]


def test_runner_access_rejects_unapproved_even_when_tags_differ_and_offline():
    from git_migration.steps.runners import verify_runner_access

    api = RunnerAPI()
    api.runners[1].update(tag_list=["unrelated-old-branch"], status="offline")
    result = verify_runner_access(api, 10, [1])
    assert result["blockers"]
    assert result["unapproved_runner_ids"] == [2]
    assert not api.writes


def test_runner_access_allows_only_approved_or_explicitly_paused_runners():
    from git_migration.steps.runners import verify_runner_access

    api = RunnerAPI()
    api.runners[1]["paused"] = True
    result = verify_runner_access(api, 10, [1])
    assert not result["blockers"]
    assert result["unpaused_runner_ids"] == [1]
    assert not api.writes
    api.runners[1]["paused"] = False
    assert not verify_runner_access(api, 10, [1, 2])["blockers"]


def test_runner_access_fails_closed_on_unreadable_runner_details():
    from git_migration.steps.runners import verify_runner_access

    api = RunnerAPI()

    def missing(path, params=None):
        raise Missing()

    api.get = missing
    assert verify_runner_access(api, 10, [1, 2])["blockers"]
    assert not api.writes
