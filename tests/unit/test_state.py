"""Exercise durable checkpointing and actual cross-process writer exclusion."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from git_migration.models import MigrationConfig, MigrationPlan
from git_migration.state import RunnerTagRevisionBlocked, StateStore, atomic_json


def plan() -> MigrationPlan:
    config = MigrationConfig.model_validate(
        {
            "source": {"url": "https://source.example", "token_env": "SOURCE_TOKEN"},
            "destination": {"url": "https://target.example", "token_env": "TARGET_TOKEN"},
            "repositories": [{"source_path": "team/repo", "destination_path": "new/repo"}],
            "group_mapping": {"team": "new"},
            "runners": {"allowed_runner_ids": [7]},
        }
    )
    return MigrationPlan(
        created_at="2026-10-21T00:00:00Z",
        config=config,
        config_hash=config.fingerprint(),
        source_version="18.4",
        destination_version="18.5",
    )


def test_committed_transitions_survive_new_store_and_unknown_outcomes(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    run = store.create_run(plan())
    store.save(run, "team/repo", "import", "writing", {"operation": "opaque-123"})
    store.set_status(run, "interrupted")
    restarted = StateStore(tmp_path)
    assert restarted.plan(run).config_hash == plan().config_hash
    assert restarted.step(run, "team/repo", "import")["data"] == {"operation": "opaque-123"}
    assert restarted.report(run)["status"] == "interrupted"
    restarted.save(run, "team/repo", "import", "done", {"project_id": 4})
    report = StateStore(tmp_path).report(run)
    assert len(report["steps"]) == 1
    assert report["steps"][0]["status"] == "done"
    assert (tmp_path / "state.sqlite3").stat().st_mode & 0o777 == 0o600


def test_failed_transaction_rolls_back_without_losing_prior_checkpoint(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    run = store.create_run(plan())
    store.save(run, "team/repo", "import", "pending", {})
    with pytest.raises(RuntimeError), store.connection() as db:
        db.execute("UPDATE steps SET status=? WHERE run_id=?", ("done", run))
        raise RuntimeError("synthetic crash before commit")
    assert StateStore(tmp_path).step(run, "team/repo", "import")["status"] == "pending"


def test_abrupt_process_exit_does_not_commit_partial_transition(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    run = store.create_run(plan())
    store.save(run, "team/repo", "import", "pending", {"operation": "opaque-123"})
    script = """
import os
from pathlib import Path
import sys
from git_migration.state import StateStore
store = StateStore(Path(sys.argv[1]))
with store.connection() as db:
    db.execute('UPDATE steps SET status=? WHERE run_id=?', ('done', sys.argv[2]))
    os._exit(23)
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), run],
        env=env,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert child.returncode == 23
    assert StateStore(tmp_path).step(run, "team/repo", "import")["status"] == "pending"


def test_one_writer_lock_excludes_an_independent_process(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    script = """
from pathlib import Path
import sys
from git_migration.state import StateStore
try:
    with StateStore(Path(sys.argv[1])).lock():
        print('acquired')
except RuntimeError:
    print('locked')
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    with store.lock():
        child = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        assert child.stdout.strip() == "locked"
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert child.stdout.strip() == "acquired"


def test_atomic_json_is_private_and_replaces_complete_document(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    atomic_json(path, {"state": "first"})
    atomic_json(path, {"state": "second"})
    assert "second" in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.fixture
def tag_revision_run(tmp_path):
    saved = plan()
    saved.config.repositories[0].strategy = "git_copy"
    saved.config_hash = saved.config.fingerprint()
    store = StateStore(tmp_path)
    run = store.create_run(saved)
    for name, data in (
        ("snapshot", {"snapshot": {"refs": {"refs/heads/main": "a" * 40}}}),
        ("import", {"destination_id": 20, "handle": {"journal": "synthetic"}}),
        ("baseline_verified", {"blockers": []}),
        (
            "runner_mrs",
            {
                "requests": [
                    {
                        "status": "unchanged",
                        "changed": False,
                        "source_sha": "a" * 40,
                        "target_branch": "main",
                        "jobs": [{"job": "check", "tags": ["old-linux"]}],
                        "blockers": [],
                    }
                ]
            },
        ),
        ("preparation", {"awaiting_review": False}),
    ):
        store.save(run, "team/repo", name, "complete", data)
    store.save(run, "team/repo", "verification", "blocked", {"blockers": ["No runner"]})
    store.set_status(run, "blocked")
    return store, run, saved


def test_runner_tag_revision_is_audited_atomic_and_keeps_import_identity(tag_revision_run):
    store, run, original = tag_revision_run
    before = {step["name"]: step for step in store.report(run)["steps"]}
    updated = store.revise_runner_tags(run, {"old-linux": ["approved"]}, original.config_hash)
    reopened = StateStore(store.root)
    assert reopened.plan(run) == updated
    assert updated.config_hash == updated.config.fingerprint() != original.config_hash
    expected = original.model_copy(deep=True)
    expected.config.runners.tag_map = {"old-linux": ["approved"]}
    expected.config_hash = expected.config.fingerprint()
    assert updated == expected  # No other policy, runner IDs or repository identity changed.
    for name in ("snapshot", "import", "baseline_verified"):
        assert reopened.step(run, "team/repo", name) == before[name]
    for name in ("runner_mrs", "preparation", "verification"):
        assert reopened.step(run, "team/repo", name)["status"] == "superseded"
        assert reopened.step(run, "team/repo", name)["data"] == before[name]["data"]
    audit = reopened.step(run, "team/repo", "runner_tag_revision_1")["data"]
    assert audit["previous_config_hash"] == original.config_hash
    assert audit["config_hash"] == updated.config_hash
    assert audit["previous_tag_map"] == {}
    assert audit["tag_map"] == {"old-linux": ["approved"]}
    assert audit["superseded_steps"] == {
        name: before[name] for name in ("runner_mrs", "preparation", "verification")
    }
    assert reopened.report(run)["status"] == "preparing"


def test_same_correction_resumes_after_mr_attempt_but_different_one_is_refused(tag_revision_run):
    store, run, original = tag_revision_run
    updated = store.revise_runner_tags(run, {"old-linux": ["approved"]}, original.config_hash)
    # A crash during remote MR creation leaves the old checkpoint superseded.
    before = store.report(run)
    assert (
        store.revise_runner_tags(run, {"old-linux": ["approved"]}, updated.config_hash) == updated
    )
    assert store.report(run) == before
    with pytest.raises(RunnerTagRevisionBlocked):
        store.revise_runner_tags(run, {"old-linux": ["different"]}, updated.config_hash)
    # Once the same MR is recovered, its evidence still cannot be discarded.
    store.save(run, "team/repo", "runner_mrs", "complete", {"requests": [{"iid": 2}]})
    before = store.report(run)
    assert (
        store.revise_runner_tags(run, {"old-linux": ["approved"]}, updated.config_hash) == updated
    )
    with pytest.raises(RunnerTagRevisionBlocked):
        store.revise_runner_tags(run, {"old-linux": ["different"]}, updated.config_hash)
    assert store.report(run) == before


@pytest.mark.parametrize(
    "fault",
    [
        "active",
        "native",
        "pilot",
        "batch",
        "stale_hash",
        "bad_fingerprint",
        "incomplete_import",
        "pending_mr",
        "existing_mr",
        "changed_ci",
        "no_ci",
        "smoke",
        "activation",
        "unknown_tag",
        "dynamic_tag",
        "empty_tag",
        "duplicate_tag",
    ],
)
def test_runner_tag_revision_refuses_unsafe_or_ambiguous_states(tag_revision_run, fault):
    store, run, saved = tag_revision_run
    changes = {"old-linux": ["approved"]}
    if fault == "active":
        store.set_status(run, "active")
    elif fault in {"native", "pilot", "batch", "bad_fingerprint"}:
        if fault == "native":
            saved.config.repositories[0].strategy = "direct_transfer"
        elif fault == "pilot":
            saved.pilot = True
        elif fault == "batch":
            extra = saved.config.repositories[0].model_copy(
                update={
                    "source_path": "team/second",
                    "destination_path": "new/second",
                }
            )
            saved.config.repositories.append(extra)
        saved.config_hash = "invalid" if fault == "bad_fingerprint" else saved.config.fingerprint()
        with store.connection() as db:
            db.execute("UPDATE runs SET plan=? WHERE id=?", (saved.model_dump_json(), run))
    elif fault == "stale_hash":
        saved.config_hash = "stale"
    elif fault == "incomplete_import":
        store.save(run, "team/repo", "import", "writing", {"handle": "keep-me"})
    elif fault in {"pending_mr", "existing_mr", "changed_ci", "no_ci"}:
        step = store.step(run, "team/repo", "runner_mrs")
        request = step["data"]["requests"][0]
        if fault == "existing_mr":
            request["iid"] = 2
        if fault == "changed_ci":
            request["changed"] = True
        if fault == "no_ci":
            request["status"] = "no_ci"
        store.save(
            run,
            "team/repo",
            "runner_mrs",
            "preparing" if fault == "pending_mr" else "complete",
            step["data"],
        )
    elif fault in {"smoke", "activation"}:
        store.save(run, "team/repo", fault, "failed", {"operation": "keep-me"})
    elif fault == "unknown_tag":
        changes = {"unknown": ["approved"]}
    elif fault == "dynamic_tag":
        changes = {"old-linux": ["$RUNNER"]}
    elif fault == "empty_tag":
        changes = {"old-linux": []}
    elif fault == "duplicate_tag":
        changes = {"old-linux": ["approved", "approved"]}
    before, before_plan = store.report(run), store.plan(run)
    with pytest.raises(RunnerTagRevisionBlocked):
        store.revise_runner_tags(run, changes, saved.config_hash)
    assert store.report(run) == before
    assert store.plan(run) == before_plan


def test_runner_tag_revision_rolls_back_audit_and_invalidations_on_write_failure(tag_revision_run):
    store, run, saved = tag_revision_run
    before = store.report(run)
    with store.connection() as db:
        db.execute("""CREATE TRIGGER fail_revision BEFORE UPDATE OF plan ON runs
                      BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.revise_runner_tags(run, {"old-linux": ["approved"]}, saved.config_hash)
    assert StateStore(store.root).report(run) == before
    assert store.plan(run) == saved
