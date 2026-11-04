import base64
import io
import json
from urllib.parse import unquote

import pytest

from git_migration.steps.runners import (
    RunnerBlocked,
    _job_tags,
    _yaml,
    apply_runner_default_amendment,
    configure_runners,
    inspect_runners,
    plan_runner_default_amendment,
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


@pytest.mark.parametrize(
    "include",
    [
        "include:\n  project: team/optional-review\n  file: ci/review.yml\n",
        "include:\n  - project: team/optional-review\n    file: ci/review.yml\n    rules:\n      - if: '$CI_MERGE_REQUEST_LABELS =~ /ai-review/'\n",
    ],
)
def test_optional_project_include_is_removed_only_with_explicit_opt_in(include):
    content = include + "job:\n  tags: [old]\n  script: echo ok\n"
    assert transform_ci_yaml(content, {})["blockers"]
    result = transform_ci_yaml(content, {}, omit_project_includes=["team/optional-review"])
    assert result["changed"] and not result["blockers"]
    assert _yaml().load(result["content"]) == _yaml().load(content[content.index("job:") :])
    assert not transform_ci_yaml(
        result["content"], {}, omit_project_includes=["team/optional-review"]
    )["changed"]


def test_omission_retains_other_includes_and_unmapped_dependency_gate():
    content = "include:\n  - project: team/optional-review\n    file: ci/review.yml\n  - local: /ci/local.yml\n  - project: team/required\n    file: ci/build.yml\n"
    result = transform_ci_yaml(content, {}, omit_project_includes=["team/optional-review"])
    assert result["blockers"] == ["Project CI include requires an explicit destination mapping"]
    assert _yaml().load(result["content"])["include"] == [
        {"local": "/ci/local.yml"},
        {"project": "team/required", "file": "ci/build.yml"},
    ]


def test_omission_creates_review_mr_without_altering_source_branch():
    api = CIAPI()
    original = "include:\n  - project: team/optional-review\n    file: ci/review.yml\njob:\n  tags: [old]\n  script: echo ok\n"
    api.files["main"] = original
    blocked = prepare_runner_merge_request(api, 1, "main", {})
    assert blocked["status"] == "blocked" and not api.mrs and api.commits == 0
    result = prepare_runner_merge_request(
        api, 1, "main", {}, omit_project_includes=["team/optional-review"]
    )
    assert result["status"] == "created"
    assert api.files["main"] == original
    assert "include" not in _yaml().load(api.files[result["branch"]])
    assert result["jobs"] == [{"job": "job", "tags": ["old"]}]


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


class DefaultTagsAPI:
    """An offline repository with pinned trees and recoverable remote writes."""

    def __init__(self, files, ci_path=None):
        self.trees = {"source-sha": dict(files)}
        self.branches = {"main": "source-sha"}
        self.messages = {}
        self.mrs = []
        self.commits = []
        self.lints = []
        self.ci_path = ci_path
        self.lint_response = lambda data: {"valid": True}
        self.lose_commit = False
        self.lose_mr = False
        self.lose_branch = False

    def project(self, project_id):
        return {
            "builds_access_level": "disabled",
            "auto_devops_enabled": False,
            "ci_config_path": self.ci_path,
        }

    def get(self, path, params=None):
        if "/repository/branches/" in path:
            ref = unquote(path.split("/repository/branches/")[-1])
            if ref not in self.branches:
                raise Missing()
            sha = self.branches[ref]
            return {"commit": {"id": sha, "message": self.messages.get(sha, "")}}
        if "/repository/files/" in path:
            name = unquote(path.split("/repository/files/")[-1])
            ref = params["ref"]
            tree = self.trees[self.branches.get(ref, ref)]
            if name not in tree:
                raise Missing()
            return {
                "encoding": "base64",
                "content": base64.b64encode(tree[name].encode()).decode(),
            }
        if path.endswith("/repository/compare"):
            before = self.trees[params["from"]]
            after = self.trees[params["to"]]
            return {
                "diffs": [
                    {"new_path": name}
                    for name in sorted(before.keys() | after.keys())
                    if before.get(name) != after.get(name)
                ]
            }
        if "/merge_requests/" in path:
            return self.mrs[int(path.rsplit("/", 1)[-1]) - 1]
        raise AssertionError(path)

    def list(self, path, params=None):
        assert path.endswith("/merge_requests")
        return [mr for mr in self.mrs if mr["source_branch"] == params["source_branch"]]

    def post(self, path, data):
        if path.endswith("/lint"):
            self.lints.append(data)
            return self.lint_response(data)
        if path.endswith("/branches"):
            self.branches[data["branch"]] = self.branches.get(data["ref"], data["ref"])
            if self.lose_branch:
                self.lose_branch = False
                raise TimeoutError("response lost")
            return {}
        if path.endswith("/commits"):
            self.commits.append(data)
            tree = dict(self.trees[self.branches[data["branch"]]])
            for action in data["actions"]:
                assert action["action"] == "update" and action["file_path"] in tree
                tree[action["file_path"]] = action["content"]
            sha = f"migration-sha-{len(self.commits)}"
            self.trees[sha] = tree
            self.messages[sha] = data["commit_message"]
            self.branches[data["branch"]] = sha
            if self.lose_commit:
                self.lose_commit = False
                raise TimeoutError("response lost")
            return {"id": sha}
        if path.endswith("/merge_requests"):
            mr = {
                **data,
                "iid": len(self.mrs) + 1,
                "web_url": "https://example.test/review",
                "sha": self.branches[data["source_branch"]],
                "state": "opened",
            }
            self.mrs.append(mr)
            if self.lose_mr:
                self.lose_mr = False
                raise TimeoutError("response lost")
            return mr
        raise AssertionError(path)

    def prepared_files(self, result):
        return self.trees[result["commit_sha"]]

    def use_project_ci(self, content):
        self.project_ci = content

        def lint(data):
            # These fixtures only overlap the default mapping, preserving included jobs.
            merged = _yaml().load(self.project_ci)
            root = _yaml().load(data["content"])
            for key, value in root.items():
                if key == "include":
                    continue
                if key == "default" and key in merged:
                    merged[key].update(value)
                else:
                    merged[key] = value
            output = io.StringIO()
            _yaml().dump(merged, output)
            return {"valid": True, "merged_yaml": output.getvalue()}

        self.lint_response = lint


def test_default_tags_apply_after_mapping_only_to_jobs_without_tags():
    original = (
        "# Keep this comment\n"
        "default:\n  image: alpine # Keep this default\n"
        ".template:\n  tags: [old]\n"
        "inherited:\n  extends: .template\n  script: echo ok\n"
        "explicit-empty:\n  tags: []\n  script: echo ok\n"
        "ordinary:\n  script: echo ok\n"
        "steps-job:\n  run:\n    - name: hello\n      script: echo ok\n"
    )
    api = DefaultTagsAPI({".gitlab-ci.yml": original})
    result = prepare_runner_merge_request(
        api,
        1,
        "main",
        {"old": ["mapped"], "destination": ["wrong"]},
        default_tags=["destination", "linux"],
    )
    assert result["status"] == "created"
    content = api.prepared_files(result)[".gitlab-ci.yml"]
    doc = _yaml().load(content)
    assert doc["default"] == {"image": "alpine", "tags": ["destination", "linux"]}
    assert doc[".template"]["tags"] == ["mapped"]
    assert doc["explicit-empty"]["tags"] == []
    assert "Keep this comment" in content and "Keep this default" in content
    assert result["default_tags"]["inserted"] is True
    assert result["default_tags"]["affected_jobs"] == ["ordinary", "steps-job"]
    assert result["jobs"] == [
        {"job": "inherited", "tags": ["mapped"]},
        {"job": "explicit-empty", "tags": []},
        {"job": "ordinary", "tags": ["destination", "linux"]},
        {"job": "steps-job", "tags": ["destination", "linux"]},
    ]
    assert api.trees["source-sha"][".gitlab-ci.yml"] == original


@pytest.mark.parametrize("tags", ["[]", "[old]"])
def test_default_tags_preserve_existing_declaration_even_when_empty(tags):
    api = DefaultTagsAPI({".gitlab-ci.yml": f"default:\n  tags: {tags}\njob:\n  script: true\n"})
    result = prepare_runner_merge_request(api, 1, "main", {}, default_tags=["destination"])
    assert result["status"] == "unchanged"
    assert result["default_tags"]["inserted"] is False
    assert "already declares default.tags" in result["default_tags"]["reason"]
    assert not api.commits and not api.mrs


def test_default_tags_honor_inherited_optouts_and_empty_tags():
    api = DefaultTagsAPI(
        {
            ".gitlab-ci.yml": (
                ".optout:\n  inherit:\n    default: false\n"
                ".variables:\n  inherit:\n    variables: false\n"
                ".empty:\n  tags: []\n"
                "optout:\n  extends: [.optout, .variables]\n  script: true\n"
                "allowlist:\n  inherit:\n    default: [image]\n  script: true\n"
                "empty:\n  extends: .empty\n  script: true\n"
                "included:\n  inherit:\n    default: [tags]\n  script: true\n"
            )
        }
    )
    result = prepare_runner_merge_request(api, 1, "main", {}, default_tags=["destination"])
    assert result["default_tags"]["affected_jobs"] == ["included"]
    assert result["jobs"] == [
        {"job": "optout", "tags": []},
        {"job": "allowlist", "tags": []},
        {"job": "empty", "tags": []},
        {"job": "included", "tags": ["destination"]},
    ]


def test_default_tags_do_not_modify_jobs_sharing_the_default_anchor():
    api = DefaultTagsAPI(
        {
            ".gitlab-ci.yml": (
                "default: &settings\n  image: alpine # preserved\n"
                "optout:\n  <<: *settings\n  inherit:\n    default: false\n  script: true\n"
                "ordinary:\n  script: true\n"
            )
        }
    )
    result = prepare_runner_merge_request(api, 1, "main", {}, default_tags=["destination"])
    content = api.prepared_files(result)[".gitlab-ci.yml"]
    doc = _yaml().load(content)
    assert "&settings" in content and "*settings" in content and "preserved" in content
    assert doc["default"]["tags"] == ["destination"]
    assert "tags" not in doc["optout"]
    assert result["jobs"][0] == {"job": "optout", "tags": []}


@pytest.mark.parametrize("default_tags", [None, []])
def test_disabled_default_tags_keep_legacy_return_and_no_edits(default_tags):
    api = DefaultTagsAPI({".gitlab-ci.yml": "job:\n  script: true\n"})
    result = prepare_runner_merge_request(api, 1, "main", {}, default_tags=default_tags)
    assert "default_tags" not in result
    assert result["status"] == "unchanged" and not api.commits


@pytest.mark.parametrize(
    "content",
    [
        ".hidden:\n  script: true\n",
        "job:\n  tags: []\n  script: true\n",
        "job:\n  inherit:\n    default: false\n  script: true\n",
    ],
)
def test_default_tags_skip_when_no_executable_job_would_inherit(content):
    api = DefaultTagsAPI({".gitlab-ci.yml": content})
    result = prepare_runner_merge_request(api, 1, "main", {}, default_tags=["destination"])
    assert result["status"] == "unchanged" and not api.commits
    assert result["default_tags"]["affected_jobs"] == []
    assert "No executable jobs" in result["default_tags"]["reason"]


def test_default_tags_use_ordered_nested_repeated_local_deep_merges():
    api = DefaultTagsAPI(
        {
            ".gitlab-ci.yml": (
                "include:\n  - local: ci/a.yml\n  - local: ci/b.yml\n  - local: ci/a.yml\n"
                "default:\n  image: alpine\njob:\n  extends: .base\n"
            ),
            "ci/a.yml": "include:\n  local: ci/nested.yml\ndefault:\n  tags: []\n",
            "ci/b.yml": "default:\n  tags: [other]\n.base:\n  script: false\n",
            "ci/nested.yml": ".base:\n  script: true\n",
        }
    )
    result = prepare_runner_merge_request(api, 1, "main", {}, default_tags=["destination"])
    assert result["status"] == "unchanged" and not api.commits
    assert result["jobs"] == [{"job": "job", "tags": []}]
    assert "already declares" in result["default_tags"]["reason"]


def test_default_tags_only_edit_custom_root_after_local_tag_mapping():
    root = "include:\n  local: ci/jobs.yml\ndefault:\n  image: alpine\n"
    local = "mapped:\n  tags: [old]\n  script: true\nuntagged:\n  script: true\n"
    api = DefaultTagsAPI({"ci/root.yml": root, "ci/jobs.yml": local}, ci_path="ci/root.yml")
    result = prepare_runner_merge_request(
        api,
        1,
        "main",
        {"old": ["approved"]},
        ci_path="/ci/root.yml",
        default_tags=["destination"],
    )
    assert result["status"] == "created"
    files = api.prepared_files(result)
    assert _yaml().load(files["ci/root.yml"])["default"]["tags"] == ["destination"]
    assert "default" not in _yaml().load(files["ci/jobs.yml"])
    assert _yaml().load(files["ci/jobs.yml"])["mapped"]["tags"] == ["approved"]
    assert result["default_tags"]["affected_jobs"] == ["untagged"]
    assert set(files) == {"ci/root.yml", "ci/jobs.yml"}
    assert api.trees["source-sha"] == {"ci/root.yml": root, "ci/jobs.yml": local}


@pytest.mark.parametrize(
    "content",
    [
        "default: []\njob:\n  script: true\n",
        "default:\n  tags: null\njob:\n  script: true\n",
        "default:\n  tags: [$DYNAMIC]\njob:\n  script: true\n",
        "job:\n  extends: .missing\n  script: true\n",
        "job:\n  extends: [[invalid]]\n  script: true\n",
        "job:\n  inherit:\n    default: unresolved\n  script: true\n",
        "job:\n  inherit:\n    default: !reference [tags]\n  script: true\n",
        "job:\n  tags: !reference [.missing, tags]\n  script: true\n",
        "include:\n  local: []\njob:\n  script: true\n",
        "include:\n  local: ci/*.yml\njob:\n  script: true\n",
        "job: [invalid]\n",
        "job: [unterminated\n",
        "job:\n  trigger: other/project\n",
    ],
)
def test_default_tags_block_unresolved_or_malformed_configuration_without_writes(content):
    api = DefaultTagsAPI({".gitlab-ci.yml": content})
    result = prepare_runner_merge_request(api, 1, "main", {}, default_tags=["destination"])
    assert result["status"] == "blocked" and result["blockers"]
    assert not api.commits and not api.mrs
    assert api.branches == {"main": "source-sha"}


def test_default_tags_block_conditional_local_include_and_cycles():
    for include in [
        "include:\n  local: ci/jobs.yml\n  rules:\n    - if: '$CONDITION'\n",
        "include:\n  local: .gitlab-ci.yml\n",
    ]:
        api = DefaultTagsAPI(
            {".gitlab-ci.yml": include + "job:\n  script: true\n", "ci/jobs.yml": ".base: {}\n"}
        )
        result = prepare_runner_merge_request(api, 1, "main", {}, default_tags=["destination"])
        assert result["status"] == "blocked" and not api.commits


def test_default_tags_do_not_create_ci_or_replace_missing_custom_configuration():
    api = DefaultTagsAPI({})
    result = prepare_runner_merge_request(api, 1, "main", {}, default_tags=["destination"])
    assert result["status"] == "no_ci" and not api.commits and not api.mrs
    assert result["default_tags"]["inserted"] is False
    assert api.trees["source-sha"] == {}
    api.ci_path = "ci/missing.yml"
    with pytest.raises(Missing):
        prepare_runner_merge_request(
            api, 1, "main", {}, ci_path=api.ci_path, default_tags=["destination"]
        )


def _project_default_tags_api():
    api = DefaultTagsAPI(
        {
            ".gitlab-ci.yml": (
                "include:\n  project: source/templates\n  file: ci/jobs.yml\n"
                "root-job:\n  script: true\n"
            )
        }
    )
    api.use_project_ci("included-job:\n  script: true\n")
    return api


def _prepare_project_defaults(api, **kwargs):
    return prepare_runner_merge_request(
        api,
        1,
        "main",
        {},
        project_include_map={"source/templates": "destination/templates"},
        default_tags=["destination"],
        **kwargs,
    )


@pytest.mark.parametrize("tags", ["[]", "[external]"])
def test_default_tags_use_merged_project_default_including_explicit_empty(tags):
    api = _project_default_tags_api()
    # Include mapping still needs a review MR even though defaults are not inserted.
    api.use_project_ci(f"default:\n  tags: {tags}\nincluded-job:\n  script: true\n")
    result = _prepare_project_defaults(api)
    assert result["status"] == "created"
    assert result["default_tags"]["inserted"] is False
    assert "already declares" in result["default_tags"]["reason"]
    assert "default" not in _yaml().load(api.prepared_files(result)[".gitlab-ci.yml"])
    assert len(result["default_tags"]["merged_ci_sha256"]) == 64
    assert "Migration CI evidence:" in api.commits[0]["commit_message"]
    assert all("destination/templates" in call["content"] for call in api.lints)


def test_default_tags_use_project_job_inheritance_and_add_root_defaults_only():
    api = _project_default_tags_api()
    api.use_project_ci(
        ".tagged:\n  tags: []\n"
        ".optout:\n  inherit:\n    default: false\n"
        "included-empty:\n  extends: .tagged\n  script: true\n"
        "included-optout:\n  extends: .optout\n  script: true\n"
        "included-ordinary:\n  script: true\n"
    )
    result = _prepare_project_defaults(api)
    assert result["status"] == "created"
    assert result["default_tags"]["affected_jobs"] == ["included-ordinary", "root-job"]
    assert result["jobs"] == [
        {"job": "included-empty", "tags": []},
        {"job": "included-optout", "tags": []},
        {"job": "included-ordinary", "tags": ["destination"]},
        {"job": "root-job", "tags": ["destination"]},
    ]
    assert [action["file_path"] for action in api.commits[0]["actions"]] == [".gitlab-ci.yml"]


@pytest.mark.parametrize(
    "response",
    [
        {"valid": True},
        {"valid": False, "merged_yaml": "job:\n  script: true\n"},
        {"valid": True, "merged_yaml": "job:\n  extends: .unresolved\n  script: true\n"},
        {
            "valid": True,
            "merged_yaml": "include:\n  local: ci/unresolved.yml\njob:\n  script: true\n",
        },
        {"valid": True, "merged_yaml": "job:\n  tags: [$DYNAMIC]\n  script: true\n"},
    ],
)
def test_default_tags_require_reliably_resolved_project_lint_before_any_write(response):
    api = _project_default_tags_api()
    api.lint_response = lambda data: response
    result = _prepare_project_defaults(api)
    assert result["status"] == "blocked" and result["blockers"]
    assert not api.commits and not api.mrs and api.branches == {"main": "source-sha"}


def test_default_tags_block_unreadable_merged_project_yaml_without_writes():
    api = _project_default_tags_api()
    api.lint_response = lambda data: {"valid": True, "merged_yaml": "[not-a-mapping]"}
    with pytest.raises(RunnerBlocked, match="unreadable"):
        _prepare_project_defaults(api)
    assert not api.commits and not api.mrs


def test_default_tags_block_conditional_project_includes_without_weakening_legacy_mode():
    api = _project_default_tags_api()
    api.trees["source-sha"][".gitlab-ci.yml"] = api.trees["source-sha"][".gitlab-ci.yml"].replace(
        "  file: ci/jobs.yml\n", "  file: ci/jobs.yml\n  rules:\n    - if: '$CONDITION'\n"
    )
    result = _prepare_project_defaults(api)
    assert result["status"] == "blocked" and not api.commits
    legacy = prepare_runner_merge_request(
        api, 1, "main", {}, project_include_map={"source/templates": "destination/templates"}
    )
    assert legacy["status"] == "created"


def test_default_tags_block_project_includes_with_transformed_local_files():
    api = DefaultTagsAPI(
        {
            ".gitlab-ci.yml": "include:\n  local: ci/jobs.yml\nroot-job:\n  script: true\n",
            "ci/jobs.yml": (
                "include:\n  project: source/templates\n  file: ci/jobs.yml\n"
                "local-job:\n  tags: [old]\n  script: true\n"
            ),
        }
    )
    api.use_project_ci("included-job:\n  script: true\n")
    result = prepare_runner_merge_request(
        api,
        1,
        "main",
        {"old": ["mapped"]},
        project_include_map={"source/templates": "destination/templates"},
        default_tags=["destination"],
    )
    assert result["status"] == "blocked"
    assert any("edited local CI files" in reason for reason in result["blockers"])
    assert not api.commits and not api.mrs


@pytest.mark.parametrize("lost_response", ["lose_branch", "lose_commit", "lose_mr"])
@pytest.mark.parametrize("project_include", [False, True])
def test_default_tags_recover_lost_responses_with_one_commit_and_one_mr(
    lost_response, project_include
):
    api = (
        _project_default_tags_api()
        if project_include
        else DefaultTagsAPI({".gitlab-ci.yml": "job:\n  script: true\n"})
    )
    prepare = (
        _prepare_project_defaults
        if project_include
        else lambda provider: prepare_runner_merge_request(
            provider, 1, "main", {}, default_tags=["destination"]
        )
    )
    setattr(api, lost_response, True)
    with pytest.raises(TimeoutError):
        prepare(api)
    result = prepare(api)
    again = prepare(api)
    assert result["iid"] == again["iid"] == 1
    assert len(api.commits) == len(api.mrs) == 1
    assert result["default_tags"]["inserted"] is True
    if not project_include:
        assert api.trees["source-sha"][".gitlab-ci.yml"] == "job:\n  script: true\n"


def test_default_tags_retry_reconciles_prior_branch_when_external_default_appears():
    api = _project_default_tags_api()
    # No include remapping, so a newly appearing external default would mean no edits.
    api.trees["source-sha"][".gitlab-ci.yml"] = api.trees["source-sha"][".gitlab-ci.yml"].replace(
        "source/templates", "destination/templates"
    )

    def prepare():
        return prepare_runner_merge_request(
            api,
            1,
            "main",
            {},
            project_include_map={"destination/templates": "destination/templates"},
            default_tags=["destination"],
        )

    api.lose_commit = True
    with pytest.raises(TimeoutError):
        prepare()
    api.use_project_ci("default:\n  tags: []\nincluded-job:\n  script: true\n")
    with pytest.raises(RunnerBlocked, match="reconcile the existing migration branch"):
        prepare()
    assert len(api.commits) == 1 and not api.mrs


def test_default_tags_retry_refuses_drifted_project_evidence_after_lost_commit():
    api = _project_default_tags_api()
    api.lose_commit = True
    with pytest.raises(TimeoutError):
        _prepare_project_defaults(api)
    api.use_project_ci("included-job:\n  script: echo changed\n")
    with pytest.raises(RunnerBlocked, match="saved merged CI evidence"):
        _prepare_project_defaults(api)
    assert len(api.commits) == 1 and not api.mrs


def test_default_tags_final_lint_refuses_project_drift_before_mr_creation():
    api = _project_default_tags_api()
    stable = api.lint_response

    def lint(data):
        if data["ref"] != "source-sha":
            api.project_ci = "included-job:\n  script: echo changed\n"
        return stable(data)

    api.lint_response = lint
    result = _prepare_project_defaults(api)
    assert result["status"] == "blocked" and result["changed"] is True
    assert any("changed after default runner tags" in reason for reason in result["blockers"])
    assert len(api.commits) == 1 and not api.mrs


@pytest.mark.parametrize("with_mr", [False, True])
def test_default_tags_verification_preserves_external_ci_evidence(with_mr):
    from git_migration.steps.runners import (
        verify_prepared_merge_request,
        verify_runner_configuration,
    )

    api = _project_default_tags_api()
    if with_mr:
        prepared = _prepare_project_defaults(api)

        def verify():
            return verify_prepared_merge_request(api, 1, prepared, require_merged=False)
    else:
        api.trees["source-sha"][".gitlab-ci.yml"] = api.trees["source-sha"][
            ".gitlab-ci.yml"
        ].replace("source/templates", "destination/templates")
        api.use_project_ci("default:\n  tags: []\nincluded-job:\n  script: true\n")
        prepared = prepare_runner_merge_request(
            api,
            1,
            "main",
            {},
            project_include_map={"destination/templates": "destination/templates"},
            default_tags=["destination"],
        )
        assert prepared["status"] == "unchanged"
        prepared["target_branch"] = "main"

        def verify():
            return verify_runner_configuration(api, 1, prepared)

    assert not verify()["blockers"]
    api.use_project_ci("included-job:\n  script: echo changed\n")
    assert any("changed after default runner tags" in reason for reason in verify()["blockers"])


def test_default_tags_compiled_fingerprint_preserves_collection_yaml_tags():
    from git_migration.steps.runners import _ci_fingerprint

    plain = _yaml().load("job:\n  script: [.base, script]\n")
    reference = _yaml().load("job:\n  script: !reference [.base, script]\n")
    assert _ci_fingerprint(plain) != _ci_fingerprint(reference)


class AmendmentAPI(DefaultTagsAPI):
    baseline_sha = "a" * 40
    original_sha = "b" * 40
    source_branch = "codex/runner-migration-synthetic"

    def __init__(self, root=None, local=None, ci_path=".gitlab-ci.yml"):
        root = (
            root
            if root is not None
            else (
                "# Keep prior include omission\n"
                "include:\n  local: ci/jobs.yml\n"
                "default:\n  image: alpine # Keep existing defaults\n"
                "root-job:\n  script: echo synthetic-private-script\n"
            )
        )
        local = (
            local
            if local is not None
            else (
                ".tagged:\n  tags: [already-mapped]\n"
                ".optout:\n  inherit:\n    default: false\n"
                "tagged:\n  extends: .tagged\n  script: true\n"
                "empty:\n  tags: []\n  script: true\n"
                "optout:\n  extends: .optout\n  script: true\n"
                "untagged:\n  script: true\n"
            )
        )
        old = {ci_path: root, "ci/jobs.yml": local}
        super().__init__({}, ci_path=ci_path if ci_path != ".gitlab-ci.yml" else None)
        self.root_path = ci_path
        baseline = dict(old)
        baseline[ci_path] = "include:\n  project: source/omitted\n  file: optional.yml\n" + root
        self.trees = {self.baseline_sha: baseline, self.original_sha: old}
        self.branches = {"main": self.baseline_sha, self.source_branch: self.original_sha}
        self.mrs = [
            {
                "iid": 1,
                "source_project_id": 1,
                "target_project_id": 1,
                "source_branch": self.source_branch,
                "target_branch": "main",
                "sha": self.original_sha,
                "state": "opened",
                "title": "Prepare destination CI",
                "draft": False,
                "web_url": "https://example.test/review/1",
            }
        ]
        self.prepared = {
            "status": "created",
            "changed": True,
            "iid": 1,
            "branch": self.source_branch,
            "source_branch": self.source_branch,
            "target_branch": "main",
            "commit_sha": self.original_sha,
            "source_sha": self.baseline_sha,
            "web_url": self.mrs[0]["web_url"],
            "jobs": [{"job": "root-job", "tags": []}],
            "blockers": [],
            "omit_ci_project_includes": ["source/omitted"],
        }
        self.commit_records = {}
        self.file_last_commits = {}
        self.writes = []
        self.lose_draft = False
        self.lose_final_lint = False
        self.external_ci = "included:\n  script: true\n"
        self.lint_response = self.compile_ci

    def project(self, project_id):
        return {**super().project(project_id), "id": 1}

    def get(self, path, params=None):
        if "/repository/commits/" in path:
            sha = path.split("/repository/commits/")[-1]
            if sha not in self.commit_records:
                raise Missing()
            return dict(self.commit_records[sha])
        response = super().get(path, params)
        if "/repository/files/" in path:
            name = unquote(path.split("/repository/files/")[-1])
            sha = self.branches.get(params["ref"], params["ref"])
            response.update(
                commit_id=sha,
                last_commit_id=self.file_last_commits.get((sha, name), sha),
            )
        if path.endswith("/repository/compare"):
            for diff in response["diffs"]:
                diff.update(old_path=diff["new_path"], a_mode="100644", b_mode="100644")
        return dict(response)

    def put(self, path, data):
        assert path == "/projects/1/merge_requests/1" and set(data) == {"title"}
        self.writes.append(("draft", data))
        self.mrs[0]["title"] = data["title"]
        self.mrs[0]["draft"] = True
        if self.lose_draft:
            self.lose_draft = False
            raise TimeoutError("Draft response lost")
        return dict(self.mrs[0])

    def post(self, path, data):
        if path.endswith("/commits"):
            assert data["branch"] == self.source_branch
            before = self.branches[data["branch"]]
            tree = dict(self.trees[before])
            self.writes.append(("commit", data))
            for action in data["actions"]:
                actual_last = self.file_last_commits.get((before, action["file_path"]), before)
                assert action["last_commit_id"] == actual_last
                tree[action["file_path"]] = action["content"]
            self.commits.append(data)
            sha = f"{len(self.commits) + 11:040x}"
            self.trees[sha] = tree
            self.branches[data["branch"]] = sha
            self.mrs[0]["sha"] = sha
            self.messages[sha] = data["commit_message"]
            self.commit_records[sha] = {
                "id": sha,
                "parent_ids": [before],
                "message": data["commit_message"] + "\n",
            }
            if self.lose_commit:
                self.lose_commit = False
                raise TimeoutError("Commit response lost")
            return dict(self.commit_records[sha])
        assert path.endswith("/lint"), "Amendment must not create another MR or branch"
        if self.lose_final_lint and data["ref"] != self.original_sha:
            self.lose_final_lint = False
            raise TimeoutError("Final lint response lost")
        return super().post(path, data)

    def compile_ci(self, data):
        sha = self.branches.get(data["ref"], data["ref"])
        tree = self.trees[sha]

        def compile_document(doc):
            combined = {}
            includes = doc.get("include", [])
            includes = includes if isinstance(includes, list) else [includes]
            for inc in includes:
                if isinstance(inc, dict) and "project" in inc:
                    combined.update(_yaml().load(self.external_ci))
                else:
                    local_path = inc["local"] if isinstance(inc, dict) else inc
                    combined.update(compile_document(_yaml().load(tree[local_path.lstrip("/")])))
            for key, value in doc.items():
                if key == "include":
                    continue
                if key == "default" and key in combined:
                    combined[key].update(value)
                else:
                    combined[key] = value
            return combined

        output = io.StringIO()
        _yaml().dump(compile_document(_yaml().load(data["content"])), output)
        return {"valid": True, "merged_yaml": output.getvalue()}

    def plan_amendment(self):
        return plan_runner_default_amendment(
            self, 1, self.prepared, ["destination"], ci_path=self.root_path
        )


@pytest.mark.parametrize("ci_path", [".gitlab-ci.yml", "ci/custom-root.yml"])
def test_existing_runner_mr_amendment_is_readonly_planned_and_root_only(ci_path):
    api = AmendmentAPI(ci_path=ci_path)
    proposal = api.plan_amendment()
    assert not api.writes and not api.commits
    assert api.branches[api.source_branch] == api.original_sha
    serialized = json.dumps(proposal)
    assert "synthetic-private-script" not in serialized and '"content"' not in serialized
    assert set(proposal) == {
        "operation_id",
        "project_id",
        "prepared",
        "default_tags",
        "ci_path",
        "root_before_sha256",
        "root_after_sha256",
        "root_last_commit_id",
        "default_tag_metadata",
        "jobs",
    }
    assert api.plan_amendment() == proposal
    updated = apply_runner_default_amendment(api, 1, proposal)
    assert updated["status"] == "amended" and updated["iid"] == api.prepared["iid"]
    assert updated["source_branch"] == api.source_branch and len(api.mrs) == 1
    assert updated["source_sha"] == api.baseline_sha
    assert updated["omit_ci_project_includes"] == ["source/omitted"]
    assert updated["default_tags"]["affected_jobs"] == ["root-job", "untagged"]
    before, after = api.trees[api.original_sha], api.prepared_files(updated)
    assert before["ci/jobs.yml"] == after["ci/jobs.yml"]
    assert "source/omitted" not in after[ci_path]
    assert "Keep prior include omission" in after[ci_path]
    assert "Keep existing defaults" in after[ci_path]
    assert _yaml().load(after[ci_path])["default"]["tags"] == ["destination"]
    assert _yaml().load(after["ci/jobs.yml"])[".tagged"]["tags"] == ["already-mapped"]
    assert [action["file_path"] for action in api.commits[0]["actions"]] == [ci_path]
    assert api.commits[0]["actions"][0]["last_commit_id"] == proposal["root_last_commit_id"]
    assert "[skip ci]" in api.commits[0]["commit_message"]
    assert proposal["operation_id"] in api.commits[0]["commit_message"]
    assert [kind for kind, _ in api.writes] == ["draft", "commit"]
    assert api.mrs[0]["draft"] is True
    assert all("content" not in job for job in updated["jobs"])
    assert "synthetic-private-script" not in json.dumps(updated)


@pytest.mark.parametrize("loss", ["lose_draft", "lose_commit", "lose_final_lint"])
def test_existing_runner_mr_amendment_recovers_lost_replies_without_duplicate_commit(loss):
    api = AmendmentAPI()
    proposal = api.plan_amendment()
    setattr(api, loss, True)
    with pytest.raises(TimeoutError):
        apply_runner_default_amendment(api, 1, proposal)
    recovered = apply_runner_default_amendment(api, 1, proposal)
    again = apply_runner_default_amendment(api, 1, proposal)
    assert recovered == again
    assert len(api.commits) == len(api.mrs) == 1
    assert [kind for kind, _ in api.writes] == ["draft", "commit"]


@pytest.mark.parametrize("default_tags", [[], ["$DYNAMIC"], ["destination", "destination"]])
def test_existing_runner_mr_amendment_rejects_invalid_tags(default_tags):
    api = AmendmentAPI()
    with pytest.raises(RunnerBlocked, match="literal default tag list"):
        plan_runner_default_amendment(api, 1, api.prepared, default_tags)
    assert not api.writes


@pytest.mark.parametrize(
    "root,local,message",
    [
        ("default:\n  tags: []\njob:\n  script: true\n", "", "already declares"),
        ("default:\n  tags: [already]\njob:\n  script: true\n", "", "already declares"),
        ("job:\n  tags: []\n  script: true\n", "", "no executable job"),
        ("job:\n  inherit:\n    default: false\n  script: true\n", "", "no executable job"),
        ("job:\n  inherit:\n    default: [image]\n  script: true\n", "", "no executable job"),
        (
            "include:\n  local: ci/jobs.yml\njob:\n  script: true\n",
            "default:\n  tags: []\n",
            "already declares",
        ),
        (
            "include:\n  project: destination/template\n  file: ci.yml\njob:\n  script: true\n",
            "",
            "already declares",
        ),
    ],
)
def test_existing_runner_mr_amendment_refuses_no_applicable_insertion(root, local, message):
    api = AmendmentAPI(root=root, local=local)
    api.external_ci = "default:\n  tags: []\nincluded:\n  script: true\n"
    with pytest.raises(RunnerBlocked, match=message):
        api.plan_amendment()
    assert not api.writes and not api.commits


@pytest.mark.parametrize(
    "fault",
    [
        "enabled",
        "wrong_project",
        "closed",
        "merged",
        "missing",
        "fork",
        "target_project",
        "target_branch",
        "source_branch",
        "mr_head",
        "branch_head",
        "target_drift",
        "custom_path",
    ],
)
def test_existing_runner_mr_amendment_refuses_identity_or_quarantine_drift(fault):
    api = AmendmentAPI()
    if fault == "enabled":
        api.project = lambda _: {"id": 1, "builds_access_level": "enabled"}
    elif fault == "wrong_project":
        api.project = lambda _: {"id": 2, "builds_access_level": "disabled"}
    elif fault in {"closed", "merged"}:
        api.mrs[0]["state"] = fault
    elif fault == "missing":
        api.branches.pop(api.source_branch)
    elif fault == "fork":
        api.mrs[0]["source_project_id"] = 2
    elif fault == "target_project":
        api.mrs[0]["target_project_id"] = 2
    elif fault in {"target_branch", "source_branch"}:
        api.mrs[0][fault] = "unexpected"
    elif fault == "mr_head":
        api.mrs[0]["sha"] = "d" * 40
    elif fault == "branch_head":
        api.branches[api.source_branch] = "d" * 40
        api.mrs[0]["sha"] = "d" * 40
    elif fault == "target_drift":
        api.branches["main"] = "d" * 40
    else:
        api.ci_path = "different.yml"
    with pytest.raises((RunnerBlocked, Missing)):
        api.plan_amendment()
    assert not api.writes and not api.commits


@pytest.mark.parametrize("field", ["content", "merged_yaml", "actions"])
def test_existing_runner_mr_amendment_proposal_rejects_raw_saved_configuration(field):
    api = AmendmentAPI()
    api.prepared[field] = "synthetic-private-script"
    with pytest.raises(RunnerBlocked, match="unsupported or unreadable"):
        api.plan_amendment()
    assert not api.writes


def test_existing_runner_mr_amendment_uses_immutable_file_last_commit_id():
    api = AmendmentAPI()
    api.file_last_commits[(api.original_sha, api.root_path)] = api.baseline_sha
    proposal = api.plan_amendment()
    assert proposal["root_last_commit_id"] == api.baseline_sha
    apply_runner_default_amendment(api, 1, proposal)
    assert api.commits[0]["actions"][0]["last_commit_id"] == api.baseline_sha


def test_existing_runner_mr_amendment_requires_merged_lint_and_prior_fingerprint():
    from git_migration.steps.runners import _ci_fingerprint

    api = AmendmentAPI()
    api.lint_response = lambda data: {"valid": True}
    with pytest.raises(RunnerBlocked, match="did not return merged"):
        api.plan_amendment()
    api.lint_response = api.compile_ci
    api.prepared["default_tags"] = {
        "inserted": False,
        "reason": "No defaults",
        "affected_jobs": [],
        "merged_ci_sha256": _ci_fingerprint(_yaml().load("different:\n  script: true\n")),
    }
    with pytest.raises(RunnerBlocked, match="changed after default runner tags"):
        api.plan_amendment()
    assert not api.writes


@pytest.mark.parametrize(
    "fault", ["content", "parent", "marker", "extra_file", "mode", "closed", "merged"]
)
def test_existing_runner_mr_amendment_recovery_refuses_unattributable_or_closed_head(fault):
    api = AmendmentAPI()
    proposal = api.plan_amendment()
    api.lose_commit = True
    with pytest.raises(TimeoutError):
        apply_runner_default_amendment(api, 1, proposal)
    sha = api.branches[api.source_branch]
    if fault == "content":
        api.trees[sha][api.root_path] += "tampered:\n  script: true\n"
    elif fault == "parent":
        api.commit_records[sha]["parent_ids"] = [api.original_sha, api.baseline_sha]
    elif fault == "marker":
        api.commit_records[sha]["message"] += "Unrelated marker\n"
    elif fault == "extra_file":
        api.trees[sha]["application.py"] = "unexpected"
    elif fault == "mode":
        original_get = api.get

        def get(path, params=None):
            result = original_get(path, params)
            if path.endswith("/repository/compare"):
                result["diffs"][0]["b_mode"] = "100755"
            return result

        api.get = get
    else:
        api.mrs[0]["state"] = fault
    with pytest.raises(RunnerBlocked):
        apply_runner_default_amendment(api, 1, proposal)
    assert len(api.commits) == 1


def test_existing_runner_mr_amendment_refuses_new_source_head_before_any_write():
    api = AmendmentAPI()
    proposal = api.plan_amendment()
    unrelated = "d" * 40
    api.branches[api.source_branch] = unrelated
    api.mrs[0]["sha"] = unrelated
    api.commit_records[unrelated] = {
        "id": unrelated,
        "parent_ids": [api.original_sha],
        "message": "unrelated",
    }
    with pytest.raises(RunnerBlocked, match="not attributable"):
        apply_runner_default_amendment(api, 1, proposal)
    assert not api.writes


def test_existing_runner_mr_amendment_rechecks_after_marking_draft():
    api = AmendmentAPI()
    proposal = api.plan_amendment()
    original_put = api.put

    def put(path, data):
        result = original_put(path, data)
        api.branches["main"] = "d" * 40
        return result

    api.put = put
    with pytest.raises(RunnerBlocked, match="target branch changed"):
        apply_runner_default_amendment(api, 1, proposal)
    assert [kind for kind, _ in api.writes] == ["draft"] and not api.commits


def test_existing_runner_mr_amendment_refuses_compiled_drift_from_saved_proposal():
    api = AmendmentAPI(
        root="include:\n  project: destination/template\n  file: ci.yml\njob:\n  script: true\n"
    )
    proposal = api.plan_amendment()
    api.external_ci = "included:\n  script: echo changed\n"
    with pytest.raises(RunnerBlocked, match="saved planning evidence"):
        apply_runner_default_amendment(api, 1, proposal)
    assert not api.writes


def test_existing_runner_mr_amendment_verification_rejects_old_sha_and_accepts_new_evidence():
    from git_migration.steps.runners import verify_prepared_merge_request

    api = AmendmentAPI()
    proposal = api.plan_amendment()
    updated = apply_runner_default_amendment(api, 1, proposal)
    assert verify_prepared_merge_request(api, 1, api.prepared, require_merged=False)["blockers"]
    assert not verify_prepared_merge_request(api, 1, updated, require_merged=False)["blockers"]
    assert verify_prepared_merge_request(api, 1, updated)["blockers"]
    sha = updated["commit_sha"]
    api.mrs[0].update(state="merged", merge_commit_sha=sha)
    api.branches["main"] = sha
    assert not verify_prepared_merge_request(api, 1, updated)["blockers"]


def test_existing_runner_mr_amendment_preserves_shared_anchor_and_inherited_optout():
    api = AmendmentAPI(
        root=(
            "default: &settings\n  image: alpine # preserved\n"
            "optout:\n  <<: *settings\n  inherit:\n    default: false\n  script: true\n"
            "ordinary:\n  script: true\n"
        )
    )
    proposal = api.plan_amendment()
    updated = apply_runner_default_amendment(api, 1, proposal)
    content = api.prepared_files(updated)[api.root_path]
    doc = _yaml().load(content)
    assert "&settings" in content and "*settings" in content and "preserved" in content
    assert doc["default"]["tags"] == ["destination"] and "tags" not in doc["optout"]
    assert updated["default_tags"]["affected_jobs"] == ["ordinary"]
    assert updated["jobs"] == [
        {"job": "optout", "tags": []},
        {"job": "ordinary", "tags": ["destination"]},
    ]


@pytest.mark.parametrize(
    "root",
    [
        "include:\n  local: ci/jobs.yml\n  rules:\n    - if: '$DYNAMIC'\njob:\n  script: true\n",
        "include:\n  project: destination/template\n  file: ci.yml\n  rules:\n    - if: '$DYNAMIC'\njob:\n  script: true\n",
        "include:\n  local: ci/*.yml\njob:\n  script: true\n",
        "job:\n  tags: !reference [.base, tags]\n  script: true\n",
        "job:\n  trigger: destination/child\n",
    ],
)
def test_existing_runner_mr_amendment_blocks_unresolved_or_unsupported_config(root):
    api = AmendmentAPI(root=root)
    with pytest.raises(RunnerBlocked):
        api.plan_amendment()
    assert not api.writes and not api.commits


def test_existing_runner_mr_amendment_checks_final_lint_before_returning_updated_evidence():
    api = AmendmentAPI(
        root="include:\n  project: destination/template\n  file: ci.yml\njob:\n  script: true\n"
    )
    proposal = api.plan_amendment()
    stable = api.lint_response

    def lint(data):
        if data["ref"] != api.original_sha:
            api.external_ci = "included:\n  script: echo changed\n"
        return stable(data)

    api.lint_response = lint
    with pytest.raises(RunnerBlocked, match="changed after default runner tags"):
        apply_runner_default_amendment(api, 1, proposal)
    assert len(api.commits) == 1 and api.prepared["commit_sha"] == api.original_sha
    # Reconciliation cannot accept that commit against a newly compiled proposal.
    with pytest.raises(RunnerBlocked, match="saved planning evidence"):
        apply_runner_default_amendment(api, 1, proposal)
    assert len(api.commits) == 1
