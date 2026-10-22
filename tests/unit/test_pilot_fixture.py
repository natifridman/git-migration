from pathlib import Path

import pytest
from test_profiles import experimental

from git_migration.git import GitTransport
from git_migration.models import Repository
from git_migration.pilot_fixture import create_source, prepare_local
from git_migration.secrets import SafeError


def test_local_fixture_has_real_lfs_and_ci_without_network(tmp_path: Path):
    directory = tmp_path / "fixture"
    prepare_local(directory)
    git = GitTransport()
    assert (directory / "asset.bin").stat().st_size == 4096
    assert len(git.lfs_oids(directory)) == 1
    assert git.run(["show", "main:asset.bin"], cwd=directory).startswith(
        "version https://git-lfs.github.com/spec/v1"
    )
    assert git.run(["tag", "--list"], cwd=directory).strip() == "fixture-v1"
    assert not git.run(["remote"], cwd=directory).strip()
    assert "action: verify" in (directory / ".gitlab-ci.yml").read_text()
    with pytest.raises(SafeError, match="already exists"):
        prepare_local(directory)


def test_fixture_seeder_refuses_missing_secondary_identity_before_any_api_call(
    tmp_path, monkeypatch
):
    from git_migration import pilot_fixture

    config = experimental()
    config.repositories = [
        Repository(
            source_path="team/git-migration-fixture",
            destination_path="new/git-migration-fixture-pilot",
        )
    ]
    (tmp_path / ".git").mkdir()
    monkeypatch.delenv("SECONDARY_TEST_TOKEN", raising=False)
    monkeypatch.setattr(
        pilot_fixture,
        "GitLabProvider",
        lambda *args, **kwargs: pytest.fail("No API call without both identities"),
    )
    with pytest.raises(SafeError, match="secondary source token"):
        create_source(config, tmp_path, "SECONDARY_TEST_TOKEN")


def test_fixture_seeder_never_replays_an_existing_journal(tmp_path):
    config = experimental()
    config.repositories = [
        Repository(
            source_path="team/git-migration-fixture",
            destination_path="new/git-migration-fixture-pilot",
        )
    ]
    directory = tmp_path / "fixture"
    directory.mkdir()
    (tmp_path / "fixture-source.json").write_text('{"status":"write_pending"}')
    with pytest.raises(SafeError, match="journal exists"):
        create_source(config, directory, "SECONDARY_TEST_TOKEN")
