"""Exercise Git copy against real local repositories and a fake project API."""

import subprocess

import pytest
from test_preflight import FakeProvider, codes, config

from git_migration.compatibility import pilot_evidence
from git_migration.git import GitTransport
from git_migration.providers import ProviderError
from git_migration.steps.preflight import build_plan
from git_migration.strategies import AmbiguousOperation, GitCopyStrategy, TargetOccupied
from git_migration.strategies.repository import GitCopyBlocked


def git(*args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


class API:
    url = "https://git.example"
    token = "synthetic-token-never-persisted"

    def __init__(self, repository, target=False):
        self.repository, self.target = repository, target
        self.created = not target
        self.writes = []
        self.metadata = []
        self.merge_requests = []
        self.uncertain = False

    def group(self, path):
        return {"id": 7, "full_path": path}

    def project(self, path):
        if not self.created:
            raise ProviderError(404)
        return {
            "id": 20 if self.target else 10,
            "path_with_namespace": "new/repo" if self.target else "team/repo",
            "http_url_to_repo": str(self.repository),
            "builds_access_level": "disabled",
            "auto_devops_enabled": False,
        }

    def list(self, path, params=None):
        if path.endswith("/merge_requests"):
            return self.merge_requests
        return self.metadata

    def post(self, path, data):
        assert path == "/projects"
        self.writes.append((path, data))
        self.created = True
        if self.uncertain:
            raise ProviderError(503)
        return self.project(20)

    def put(self, path, data):
        self.writes.append((path, data))
        if data.get("default_branch"):
            git("symbolic-ref", "HEAD", "refs/heads/" + data["default_branch"], cwd=self.repository)
        return {}


@pytest.fixture
def copy(tmp_path):
    source_repo, target_repo = tmp_path / "source", tmp_path / "target.git"
    git("init", "-b", "development", str(source_repo))
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "--allow-empty",
        "-m",
        "initial",
        cwd=source_repo,
    )
    git("branch", "secondary", cwd=source_repo)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "tag",
        "-a",
        "release",
        "-m",
        "release",
        cwd=source_repo,
    )
    git("init", "--bare", str(target_repo))
    source, target = API(source_repo), API(target_repo, target=True)
    work = tmp_path / "work"
    work.mkdir()
    return GitCopyStrategy(source, target, work), source, target


def test_copy_preserves_branch_tag_objects_default_branch_and_resumes(copy):
    strategy, source, target = copy
    handle = strategy.start("team/repo", "new/repo")
    assert target.writes[0][1]["builds_access_level"] == "disabled"
    assert target.writes[0][1]["auto_devops_enabled"] is False
    assert not target.writes[0][1]["shared_runners_enabled"]
    progress = strategy.poll(handle)
    assert progress.status == "finished" and progress.destination_project_id == 20
    assert GitTransport().refs(str(source.repository)) == GitTransport().refs(
        str(target.repository)
    )
    assert strategy.recover("team/repo", "new/repo", "unused")["destination_project_id"] == 20
    assert strategy.poll(handle).status == "finished"
    assert len([p for p, _ in target.writes if p == "/projects"]) == 1
    assert "synthetic-token" not in strategy.journal.read_text()


def test_refuses_occupied_target_and_does_not_write(copy):
    strategy, _, target = copy
    target.created = True
    with pytest.raises(TargetOccupied):
        strategy.start("team/repo", "new/repo")
    assert not target.writes and not strategy.journal.exists()


@pytest.mark.parametrize("empty", [False, True])
def test_copy_without_tags_or_with_an_empty_repository(copy, empty):
    strategy, source, target = copy
    git("tag", "-d", "release", cwd=source.repository)
    if empty:
        git("update-ref", "-d", "refs/heads/secondary", cwd=source.repository)
        git("update-ref", "-d", "refs/heads/development", cwd=source.repository)
    handle = strategy.start("team/repo", "new/repo")
    assert strategy.poll(handle).status == "finished"
    assert GitTransport().refs(str(source.repository)) == GitTransport().refs(
        str(target.repository)
    )


def test_uncertain_create_is_not_adopted_or_replayed(copy):
    strategy, _, target = copy
    target.uncertain = True
    with pytest.raises(AmbiguousOperation):
        strategy.start("team/repo", "new/repo")
    with pytest.raises(AmbiguousOperation, match="uncertain"):
        strategy.recover("team/repo", "new/repo", "unused")
    assert len(target.writes) == 1


def test_unexpected_target_refs_are_not_removed_or_overwritten(copy):
    strategy, source, target = copy
    handle = strategy.start("team/repo", "new/repo")
    git("push", str(target.repository), "HEAD:refs/heads/unexpected", cwd=source.repository)
    before = GitTransport().refs(str(target.repository))
    with pytest.raises(GitCopyBlocked, match="unexpected"):
        strategy.poll(handle)
    assert GitTransport().refs(str(target.repository)) == before


def test_changed_source_and_new_metadata_block_copy(copy):
    strategy, source, target = copy
    handle = strategy.start("team/repo", "new/repo")
    git("branch", "later", cwd=source.repository)
    with pytest.raises(GitCopyBlocked, match="Source refs changed"):
        strategy.poll(handle)
    source.metadata = [{"id": 1}]
    with pytest.raises(GitCopyBlocked, match="issues or merge requests"):
        strategy.poll(handle)
    assert not GitTransport().refs(str(target.repository))["refs"]


def test_mr_history_requires_opt_in_and_copy_resumes_with_omission(copy):
    strategy, source, target = copy
    source.merge_requests = [{"iid": 1, "state": "merged"}]
    with pytest.raises(GitCopyBlocked, match="issues or merge requests"):
        strategy.start("team/repo", "new/repo")
    assert not target.writes and not strategy.journal.exists()
    git("update-ref", "refs/merge-requests/1/head", "HEAD", cwd=source.repository)
    opted = GitCopyStrategy(source, target, strategy.work_dir, skip_merge_request_history=True)
    handle = opted.start("team/repo", "new/repo")
    assert handle["skip_merge_request_history"] is True
    resumed = GitCopyStrategy(source, target, strategy.work_dir, skip_merge_request_history=True)
    recovered = resumed.recover("team/repo", "new/repo", "unused")
    assert resumed.poll(recovered).status == "finished"
    assert GitTransport().refs(str(source.repository)) == GitTransport().refs(
        str(target.repository)
    )
    assert "refs/merge-requests/" not in git("show-ref", cwd=target.repository)
    assert len([p for p, _ in target.writes if p == "/projects"]) == 1


@pytest.mark.parametrize("skip", [False, True])
def test_history_policy_cannot_change_on_resume(copy, skip):
    strategy, source, target = copy
    original = GitCopyStrategy(source, target, strategy.work_dir, skip_merge_request_history=skip)
    handle = original.start("team/repo", "new/repo")
    changed = GitCopyStrategy(
        source, target, strategy.work_dir, skip_merge_request_history=not skip
    )
    with pytest.raises(GitCopyBlocked, match="history policy changed"):
        changed.recover("team/repo", "new/repo", "unused")
    with pytest.raises(GitCopyBlocked, match="history policy changed"):
        changed.poll(handle)
    assert len(target.writes) == 1
    assert not GitTransport().refs(str(target.repository))["refs"]


def test_skip_mrs_still_blocks_issues_before_creation_and_on_resume(copy):
    strategy, source, target = copy
    opted = GitCopyStrategy(source, target, strategy.work_dir, skip_merge_request_history=True)
    source.metadata = [{"iid": 1}]
    with pytest.raises(GitCopyBlocked, match="Source has issues"):
        opted.start("team/repo", "new/repo")
    assert not target.writes
    source.metadata = []
    handle = opted.start("team/repo", "new/repo")
    source.metadata = [{"iid": 1}]
    with pytest.raises(GitCopyBlocked, match="Source has issues"):
        opted.poll(handle)
    assert not GitTransport().refs(str(target.repository))["refs"]


@pytest.mark.parametrize("skip", [False, True])
def test_plan_requires_explicit_mr_omission_and_still_blocks_issues(skip):
    source, target = FakeProvider(), FakeProvider(target=True)
    source.current_version, target.current_version = "19.6.0-pre", "19.4.3-ee"
    c = config()
    c.repositories[0].strategy = "git_copy"
    c.repositories[0].skip_merge_request_history = skip
    listing = source.list
    source.list = lambda path, params=None: (
        [{"iid": 1, "state": "closed"}]
        if path.endswith("/merge_requests")
        else listing(path, params)
    )
    plan = build_plan(c, source, target)
    assert plan.blocked is not skip
    assert ("git_copy_merge_requests_omitted" in codes(plan)) is skip
    assert plan.repositories[0].counts["merge_requests"] == 1
    source.list = lambda path, params=None: (
        [{"iid": 2}] if path.endswith("/issues") else listing(path, params)
    )
    assert "git_copy_metadata" in codes(build_plan(c, source, target))
    assert not target.writes


def test_skip_mrs_does_not_hide_failed_inventory():
    source, target = FakeProvider(), FakeProvider(target=True)
    c = config()
    c.repositories[0].strategy = "git_copy"
    c.repositories[0].skip_merge_request_history = True
    listing = source.list

    def unreadable(path, params=None):
        if path.endswith("/merge_requests"):
            raise ProviderError(403)
        return listing(path, params)

    source.list = unreadable
    assert "inventory_failed" in codes(build_plan(c, source, target))
    assert not target.writes


def test_git_copy_plans_across_version_gap_without_native_checks_or_pilot():
    source, target = FakeProvider(), FakeProvider(target=True)
    source.current_version, target.current_version = "19.6.0-pre", "19.2.7-ee"
    c = config()
    c.repositories[0].strategy = "git_copy"
    get = target.get
    target.get = lambda path, params=None: (
        pytest.fail("Native settings must not be checked")
        if path == "/application/settings"
        else get(path, params)
    )
    plan = build_plan(c, source, target)
    assert not plan.blocked
    assert "git_copy_scope" in codes(plan)
    assert not pilot_evidence(plan, None)
    assert not target.writes
    listing = source.list
    source.list = lambda path, params=None: (
        [{"id": 1}] if path.endswith("/issues") else listing(path, params)
    )
    assert "git_copy_metadata" in codes(build_plan(c, source, target))


def test_lfs_payload_is_uploaded_before_refs(copy, monkeypatch):
    strategy, source, target = copy
    handle = strategy.start("team/repo", "new/repo")
    calls = []
    monkeypatch.setattr(GitTransport, "lfs_oids", lambda self, directory: ["a" * 64])
    monkeypatch.setattr(
        GitTransport, "verify_lfs", lambda self, url, directory: calls.append("fetch-lfs")
    )
    run = GitTransport.run

    def record(self, args, *positional, **kwargs):
        if "lfs" in args and "push" in args:
            calls.append("push-lfs")
            return ""
        if args[0] == "push":
            calls.append("push-git")
            assert "--atomic" in args and "--force" not in args
        return run(self, args, *positional, **kwargs)

    monkeypatch.setattr(GitTransport, "run", record)
    assert strategy.poll(handle).status == "finished"
    assert calls == ["fetch-lfs", "push-lfs", "push-git"]
