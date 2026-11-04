"""Exercise durable checkpointing and actual cross-process writer exclusion."""

import copy
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from git_migration.compatibility import CompatibilityBlocked
from git_migration.models import MigrationConfig, MigrationPlan
from git_migration.state import (
    CIIncludeRevisionBlocked,
    RunnerMRAmendmentBlocked,
    RunnerTagRevisionBlocked,
    StateStore,
    atomic_json,
)


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
                        "jobs": [
                            {"job": "check", "tags": ["old-linux"]},
                            {"job": "untagged", "tags": []},
                        ],
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


@pytest.fixture
def amendment_run(tag_revision_run):
    store, run, saved = tag_revision_run
    request = store.step(run, "team/repo", "runner_mrs")["data"]["requests"][0]
    request.update(
        status="created",
        changed=True,
        iid=21,
        branch="codex/runner-migration-synthetic",
        source_branch="codex/runner-migration-synthetic",
        commit_sha="b" * 40,
    )
    store.save(run, "team/repo", "runner_mrs", "complete", {"requests": [request]})
    store.save(run, "team/repo", "preparation", "complete", {"awaiting_review": True})
    proposal = {
        "operation_id": "e" * 64,
        "project_id": 20,
        "prepared": request,
        "default_tags": ["approved"],
        "ci_path": ".gitlab-ci.yml",
        "root_before_sha256": "a" * 64,
        "root_after_sha256": "b" * 64,
        "root_last_commit_id": "b" * 40,
        "default_tag_metadata": {
            "inserted": True,
            "reason": "Added requested default tags",
            "affected_jobs": ["untagged"],
            "merged_ci_sha256": "c" * 64,
        },
        "jobs": [{"job": "untagged", "tags": ["approved"]}],
    }
    return store, run, saved, proposal


def amended_request(proposal):
    return {
        **proposal["prepared"],
        "status": "amended",
        "changed": True,
        "commit_sha": "c" * 40,
        "jobs": proposal["jobs"],
        "default_tags": proposal["default_tag_metadata"],
    }


def test_runner_mr_amendment_intent_and_finish_are_atomic_and_audited(amendment_run):
    store, run, saved, proposal = amendment_run
    original = store.report(run)
    context = store.validate_runner_mr_amendment(run, 21, ["approved"], saved.config_hash)
    assert context["status"] == "new" and store.report(run) == original
    intent = store.begin_runner_mr_amendment(run, 21, ["approved"], saved.config_hash, proposal)
    assert store.plan(run) == saved
    assert store.step(run, "team/repo", "runner_mrs") == context["steps"]["runner_mrs"]
    assert intent["status"] == "preparing"
    assert intent["data"]["previous_config_hash"] == saved.config_hash
    assert intent["data"]["previous_default_tags"] == []
    assert intent["data"]["original_request"] == proposal["prepared"]
    before_retry = store.report(run)
    assert (
        store.begin_runner_mr_amendment(run, 21, ["approved"], saved.config_hash, proposal)
        == intent
    )
    assert store.report(run) == before_retry
    with pytest.raises(RunnerMRAmendmentBlocked, match="--amend-runner-mr 21"):
        store.validate_runner_mr_amendment(run, 21, ["different"], saved.config_hash)
    updated = amended_request(proposal)
    revised = store.finish_runner_mr_amendment(run, intent["name"], updated, saved.config_hash)
    assert revised.config.runners.default_tags == ["approved"]
    assert revised.config_hash == revised.config.fingerprint() != saved.config_hash
    assert revised.endpoint_snapshot == saved.endpoint_snapshot
    assert store.step(run, "team/repo", "runner_mrs")["data"] == {"requests": [updated]}
    for name in ("snapshot", "import", "baseline_verified"):
        assert store.step(run, "team/repo", name) == context["steps"][name]
    for name in ("preparation", "verification"):
        assert store.step(run, "team/repo", name)["status"] == "superseded"
    audit = store.step(run, "team/repo", intent["name"])
    assert audit["status"] == "complete" and audit["data"]["updated_request"] == updated
    assert audit["data"]["superseded_steps"]["runner_mrs"] == context["steps"]["runner_mrs"]
    assert store.pending_runner_amendment(run) is None
    before_retry = store.report(run)
    assert (
        store.validate_runner_mr_amendment(run, 21, ["approved"], revised.config_hash)["status"]
        == "complete"
    )
    assert (
        store.finish_runner_mr_amendment(run, intent["name"], updated, revised.config_hash)
        == revised
    )
    assert store.report(run) == before_retry


def test_runner_mr_amendment_normalizes_custom_ci_path(amendment_run):
    store, run, saved, proposal = amendment_run
    snapshot = store.step(run, "team/repo", "snapshot")["data"]
    snapshot["ci_config_path"] = "/ci/root.yml"
    store.save(run, "team/repo", "snapshot", "complete", snapshot)
    proposal["ci_path"] = "ci/root.yml"
    assert (
        store.begin_runner_mr_amendment(run, 21, ["approved"], saved.config_hash, proposal)[
            "status"
        ]
        == "preparing"
    )


def test_runner_mr_amendment_recovery_flags_quote_literal_tags():
    import shlex

    message = StateStore.runner_amendment_recovery_message(
        {"data": {"iid": 21, "default_tags": ["linux;echo-danger", "linux`command`"]}}
    )
    assert "--default-runner-tag 'linux;echo-danger'" in message
    assert "--default-runner-tag 'linux`command`'" in message
    assert "linux;echo-danger" in shlex.split(message)


@pytest.mark.parametrize("stage", ["begin", "finish"])
def test_runner_mr_amendment_rolls_back_on_transaction_failure(amendment_run, stage):
    store, run, saved, proposal = amendment_run
    if stage == "finish":
        intent = store.begin_runner_mr_amendment(run, 21, ["approved"], saved.config_hash, proposal)
    before = store.report(run)
    with store.connection() as db:
        db.execute("""CREATE TRIGGER fail_amendment BEFORE UPDATE ON runs
                      BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        if stage == "begin":
            store.begin_runner_mr_amendment(run, 21, ["approved"], saved.config_hash, proposal)
        else:
            store.finish_runner_mr_amendment(
                run, intent["name"], amended_request(proposal), saved.config_hash
            )
    assert store.plan(run) == saved
    assert store.report(run) == before


@pytest.mark.parametrize(
    "fault",
    [
        "raw_content",
        "changed_proposal",
        "stale_hash",
        "wrong_iid",
        "no_tags",
        "active",
        "pilot",
        "native",
        "incomplete",
        "error",
        "smoke",
        "activation",
        "multiple_requests",
        "branch",
        "baseline",
        "commit",
        "smoke_journal",
    ],
)
def test_runner_mr_amendment_rejects_unsafe_intents_without_writes(amendment_run, fault):
    store, run, saved, proposal = amendment_run
    iid, tags, expected_hash = 21, ["approved"], saved.config_hash
    if fault == "raw_content":
        proposal["content"] = "synthetic-private-ci"
    elif fault == "changed_proposal":
        proposal["prepared"] = {**proposal["prepared"], "iid": 99}
    elif fault == "stale_hash":
        expected_hash = "stale"
    elif fault == "wrong_iid":
        iid = 99
    elif fault == "no_tags":
        tags = []
    elif fault == "active":
        store.set_status(run, "active")
    elif fault in {"pilot", "native"}:
        saved.pilot = fault == "pilot"
        if fault == "native":
            saved.config.repositories[0].strategy = "direct_transfer"
        saved.config_hash = expected_hash = saved.config.fingerprint()
        with store.connection() as db:
            db.execute("UPDATE runs SET plan=? WHERE id=?", (saved.model_dump_json(), run))
    elif fault == "incomplete":
        store.save(run, "team/repo", "preparation", "failed", {"error": "ambiguous write"})
    elif fault in {"smoke", "activation"}:
        store.save(run, "team/repo", fault, "failed", {"operation": "retain"})
    elif fault in {"multiple_requests", "branch", "baseline", "commit", "error"}:
        data = store.step(run, "team/repo", "runner_mrs")["data"]
        request = data["requests"][0]
        if fault == "multiple_requests":
            data["requests"].append(dict(request))
        elif fault == "branch":
            request["source_branch"] = "unowned-branch"
        elif fault == "baseline":
            request["source_sha"] = "f" * 40
        elif fault == "commit":
            request["commit_sha"] = "unknown"
        else:
            request["error"] = "ambiguous write"
        store.save(run, "team/repo", "runner_mrs", "complete", data)
    before, old_plan = store.report(run), store.plan(run)
    with pytest.raises((RunnerMRAmendmentBlocked, RunnerTagRevisionBlocked)):
        store.begin_runner_mr_amendment(
            run, iid, tags, expected_hash, proposal, smoke_attempted=fault == "smoke_journal"
        )
    assert store.report(run) == before and store.plan(run) == old_plan


@pytest.mark.parametrize(
    "fault",
    [
        "identity",
        "job_evidence",
        "import_evidence",
        "preparation_evidence",
        "verification_evidence",
        "runner_evidence",
        "smoke",
        "activation",
        "smoke_journal",
    ],
)
def test_pending_runner_mr_amendment_retains_intent_on_unattributed_finish(amendment_run, fault):
    store, run, saved, proposal = amendment_run
    intent = store.begin_runner_mr_amendment(run, 21, ["approved"], saved.config_hash, proposal)
    updated = amended_request(proposal)
    if fault == "identity":
        updated["iid"] = 99
    elif fault == "job_evidence":
        updated["jobs"] = []
    elif fault == "import_evidence":
        store.save(run, "team/repo", "import", "complete", {"destination_id": 99})
    elif fault in {"preparation_evidence", "verification_evidence", "runner_evidence"}:
        name = {
            "preparation_evidence": "preparation",
            "verification_evidence": "verification",
            "runner_evidence": "runner_mrs",
        }[fault]
        store.save(run, "team/repo", name, "failed", {"error": "changed evidence"})
    elif fault in {"smoke", "activation"}:
        store.save(run, "team/repo", fault, "failed", {"operation": "retain"})
    before = store.report(run)
    with pytest.raises(RunnerMRAmendmentBlocked):
        if fault == "smoke_journal":
            store.validate_runner_mr_amendment(
                run, 21, ["approved"], saved.config_hash, smoke_attempted=True
            )
        else:
            store.finish_runner_mr_amendment(run, intent["name"], updated, saved.config_hash)
    assert store.report(run) == before and store.plan(run) == saved


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
    assert audit["previous_default_tags"] == audit["default_tags"] == []
    assert audit["superseded_steps"] == {
        name: before[name] for name in ("runner_mrs", "preparation", "verification")
    }
    assert reopened.report(run)["status"] == "preparing"


@pytest.mark.parametrize("default_tags", [None, ["approved"]])
def test_same_correction_resumes_after_mr_attempt_but_different_one_is_refused(
    tag_revision_run, default_tags
):
    store, run, original = tag_revision_run
    updated = store.revise_runner_tags(
        run, {"old-linux": ["approved"]}, original.config_hash, default_tags=default_tags
    )
    # A crash during remote MR creation leaves the old checkpoint superseded.
    before = store.report(run)
    assert (
        store.revise_runner_tags(
            run, {"old-linux": ["approved"]}, updated.config_hash, default_tags=default_tags
        )
        == updated
    )
    assert store.report(run) == before
    with pytest.raises(RunnerTagRevisionBlocked):
        store.revise_runner_tags(run, {"old-linux": ["different"]}, updated.config_hash)
    # Once the same MR is recovered, its evidence still cannot be discarded.
    store.save(run, "team/repo", "runner_mrs", "complete", {"requests": [{"iid": 2}]})
    before = store.report(run)
    assert (
        store.revise_runner_tags(
            run, {"old-linux": ["approved"]}, updated.config_hash, default_tags=default_tags
        )
        == updated
    )
    with pytest.raises(RunnerTagRevisionBlocked):
        store.revise_runner_tags(run, {"old-linux": ["different"]}, updated.config_hash)
    assert store.report(run) == before


@pytest.mark.parametrize("changes", [{}, {"old-linux": ["mapped"]}])
def test_default_tag_correction_is_atomic_audited_and_retains_imports(tag_revision_run, changes):
    store, run, original = tag_revision_run
    before = {step["name"]: step for step in store.report(run)["steps"]}
    updated = store.revise_runner_tags(
        run, changes, original.config_hash, default_tags=["approved", "linux"]
    )
    assert updated.config.runners.default_tags == ["approved", "linux"]
    assert updated.config.runners.tag_map == changes
    assert updated.config.runners.allowed_runner_ids == original.config.runners.allowed_runner_ids
    assert updated.config_hash == updated.config.fingerprint() != original.config_hash
    assert StateStore(store.root).plan(run) == updated
    for name in ("snapshot", "import", "baseline_verified"):
        assert store.step(run, "team/repo", name) == before[name]
    audit = store.step(run, "team/repo", "runner_tag_revision_1")["data"]
    assert audit["previous_default_tags"] == []
    assert audit["default_tags"] == ["approved", "linux"]
    assert audit["previous_tag_map"] == {}
    assert audit["tag_map"] == changes
    assert audit["previous_config_hash"] == original.config_hash
    assert audit["config_hash"] == updated.config_hash
    assert audit["superseded_steps"] == {
        name: before[name] for name in ("runner_mrs", "preparation", "verification")
    }
    before_retry = store.report(run)
    assert (
        store.revise_runner_tags(
            run, changes, updated.config_hash, default_tags=["approved", "linux"]
        )
        == updated
    )
    assert store.report(run) == before_retry
    with pytest.raises(RunnerTagRevisionBlocked):
        store.revise_runner_tags(run, {}, updated.config_hash, default_tags=["different"])
    assert store.report(run) == before_retry


@pytest.mark.parametrize("default_tags", [None, ["replacement"]])
def test_default_tag_correction_replaces_and_omission_preserves_saved_defaults(
    tag_revision_run, default_tags
):
    store, run, saved = tag_revision_run
    saved.config.runners.default_tags = ["previous"]
    saved.config_hash = saved.config.fingerprint()
    with store.connection() as db:
        db.execute("UPDATE runs SET plan=? WHERE id=?", (saved.model_dump_json(), run))
    updated = store.revise_runner_tags(
        run, {"old-linux": ["mapped"]}, saved.config_hash, default_tags=default_tags
    )
    assert updated.config.runners.default_tags == (
        ["previous"] if default_tags is None else ["replacement"]
    )
    audit = store.step(run, "team/repo", "runner_tag_revision_1")["data"]
    assert audit["previous_default_tags"] == ["previous"]
    assert audit["default_tags"] == updated.config.runners.default_tags
    # Identical map retry preserves the configured defaults without a second audit.
    before = store.report(run)
    assert store.revise_runner_tags(run, {"old-linux": ["mapped"]}, updated.config_hash) == updated
    assert store.report(run) == before


@pytest.mark.parametrize(
    "jobs", [[{"job": "tagged", "tags": ["old-linux"]}], [{"job": "unknown"}], []]
)
def test_default_tag_correction_requires_a_saved_untagged_job(tag_revision_run, jobs):
    store, run, saved = tag_revision_run
    data = store.step(run, "team/repo", "runner_mrs")["data"]
    data["requests"][0]["jobs"] = jobs
    store.save(run, "team/repo", "runner_mrs", "complete", data)
    before = store.report(run)
    with pytest.raises(RunnerTagRevisionBlocked, match="untagged job"):
        store.revise_runner_tags(run, {}, saved.config_hash, default_tags=["approved"])
    assert store.plan(run) == saved
    assert store.report(run) == before


@pytest.mark.parametrize(
    "default_tags",
    [
        [""],
        ["$DYNAMIC"],
        ["two words"],
        ["same", "same"],
        [f"tag-{number}" for number in range(50)],
    ],
)
def test_default_tag_correction_rejects_invalid_values_atomically(tag_revision_run, default_tags):
    store, run, saved = tag_revision_run
    before = store.report(run)
    with pytest.raises(RunnerTagRevisionBlocked):
        store.revise_runner_tags(run, {}, saved.config_hash, default_tags=default_tags)
    assert store.plan(run) == saved
    assert store.report(run) == before


@pytest.mark.parametrize("default_tags", [None, ["approved"]])
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
        "missing_preparation",
        "pending_preparation",
        "failed_preparation",
        "preparation_error",
        "preparation_review",
        "pending_mr",
        "existing_mr",
        "changed_ci",
        "ci_branch",
        "ci_commit",
        "ci_error",
        "request_error",
        "unknown_change",
        "no_ci",
        "smoke",
        "smoke_recovery",
        "activation",
        "unknown_tag",
        "dynamic_tag",
        "empty_tag",
        "duplicate_tag",
    ],
)
def test_runner_tag_revision_refuses_unsafe_or_ambiguous_states(
    tag_revision_run, fault, default_tags
):
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
    elif fault == "missing_preparation":
        with store.connection() as db:
            db.execute(
                "DELETE FROM steps WHERE run_id=? AND resource=? AND name='preparation'",
                (run, "team/repo"),
            )
    elif fault in {
        "pending_preparation",
        "failed_preparation",
        "preparation_error",
        "preparation_review",
    }:
        store.save(
            run,
            "team/repo",
            "preparation",
            {"pending_preparation": "preparing", "failed_preparation": "failed"}.get(
                fault, "complete"
            ),
            {"awaiting_review": True}
            if fault == "preparation_review"
            else {"error": "Ambiguous CI write"},
        )
    elif fault in {
        "pending_mr",
        "existing_mr",
        "changed_ci",
        "no_ci",
        "ci_branch",
        "ci_commit",
        "ci_error",
        "request_error",
        "unknown_change",
    }:
        step = store.step(run, "team/repo", "runner_mrs")
        request = step["data"]["requests"][0]
        if fault == "existing_mr":
            request["iid"] = 2
        if fault == "changed_ci":
            request["changed"] = True
        if fault == "no_ci":
            request["status"] = "no_ci"
        if fault == "ci_branch":
            request["source_branch"] = "migration-write"
        if fault == "ci_commit":
            request["commit_sha"] = "b" * 40
        if fault == "ci_error":
            step["data"]["error"] = "Ambiguous write"
        if fault == "request_error":
            request["error"] = "Ambiguous write"
        if fault == "unknown_change":
            request.pop("changed")
        store.save(
            run,
            "team/repo",
            "runner_mrs",
            "preparing" if fault == "pending_mr" else "complete",
            step["data"],
        )
    elif fault in {"smoke", "smoke_recovery", "activation"}:
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
        store.revise_runner_tags(run, changes, saved.config_hash, default_tags=default_tags)
    assert store.report(run) == before
    assert store.plan(run) == before_plan


@pytest.mark.parametrize("changes", [{}, {"old-linux": ["approved"]}])
def test_runner_tag_revision_rolls_back_audit_and_invalidations_on_write_failure(
    tag_revision_run, changes
):
    store, run, saved = tag_revision_run
    before = store.report(run)
    with store.connection() as db:
        db.execute("""CREATE TRIGGER fail_revision BEFORE UPDATE OF plan ON runs
                      BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.revise_runner_tags(run, changes, saved.config_hash, default_tags=["approved"])
    assert StateStore(store.root).report(run) == before
    assert store.plan(run) == saved


@pytest.fixture
def include_revision_run(tag_revision_run):
    store, run, saved = tag_revision_run
    request = store.step(run, "team/repo", "runner_mrs")["data"]["requests"][0]
    request.update(
        status="blocked", blockers=["Project CI include requires an explicit destination mapping"]
    )
    store.save(run, "team/repo", "runner_mrs", "blocked", {"requests": [request]})
    return store, run, saved


def test_ci_omission_revision_preserves_import_and_audits_old_checkpoints(include_revision_run):
    store, run, saved = include_revision_run
    before = {step["name"]: step for step in store.report(run)["steps"]}
    updated = store.revise_ci_include_omissions(run, ["team/optional-review"], saved.config_hash)
    expected = saved.model_copy(deep=True)
    expected.config.repositories[0].omit_ci_project_includes = ["team/optional-review"]
    expected.config_hash = expected.config.fingerprint()
    assert StateStore(store.root).plan(run) == updated == expected
    for name in ("snapshot", "import", "baseline_verified"):
        assert store.step(run, "team/repo", name) == before[name]
    audit = store.step(run, "team/repo", "ci_include_revision_1")["data"]
    assert audit["previous_config_hash"] == saved.config_hash
    assert audit["config_hash"] == expected.config_hash != saved.config_hash
    assert audit["previous_omit_ci_project_includes"] == []
    assert audit["omit_ci_project_includes"] == ["team/optional-review"]
    assert audit["superseded_steps"] == {
        name: before[name] for name in ("runner_mrs", "preparation", "verification")
    }
    for name in audit["superseded_steps"]:
        assert store.step(run, "team/repo", name)["status"] == "superseded"
    before_retry = store.report(run)
    assert (
        store.revise_ci_include_omissions(run, ["team/optional-review"], expected.config_hash)
        == expected
    )
    assert store.report(run) == before_retry
    with pytest.raises(CIIncludeRevisionBlocked):
        store.revise_ci_include_omissions(run, ["team/another"], expected.config_hash)
    store.save(run, "team/repo", "runner_mrs", "complete", {"requests": [{"iid": 21}]})
    assert (
        store.revise_ci_include_omissions(run, ["team/optional-review"], expected.config_hash)
        == expected
    )
    with pytest.raises(CIIncludeRevisionBlocked):
        store.revise_ci_include_omissions(run, ["team/another"], expected.config_hash)


@pytest.mark.parametrize(
    "fault",
    [
        "native",
        "pilot",
        "batch",
        "active",
        "stale_hash",
        "bad_fingerprint",
        "import",
        "preparing",
        "superseded",
        "iid",
        "branch",
        "commit_sha",
        "changed",
        "source_sha",
        "other_blocker",
        "checkpoint_error",
        "smoke",
        "activation",
    ],
)
def test_ci_omission_refuses_ambiguous_or_unsafe_state(include_revision_run, fault):
    store, run, saved = include_revision_run
    if fault in {"native", "pilot", "batch", "bad_fingerprint"}:
        if fault == "native":
            saved.config.repositories[0].strategy = "direct_transfer"
        elif fault == "pilot":
            saved.pilot = True
        elif fault == "batch":
            saved.config.repositories.append(
                saved.config.repositories[0].model_copy(
                    update={"source_path": "team/second", "destination_path": "new/second"}
                )
            )
        saved.config_hash = "invalid" if fault == "bad_fingerprint" else saved.config.fingerprint()
        with store.connection() as db:
            db.execute("UPDATE runs SET plan=? WHERE id=?", (saved.model_dump_json(), run))
    elif fault == "active":
        store.set_status(run, "active")
    elif fault == "stale_hash":
        saved.config_hash = "stale"
    elif fault in {"import", "smoke", "activation"}:
        store.save(run, "team/repo", fault, "failed", {"operation": "retain"})
    else:
        step = store.step(run, "team/repo", "runner_mrs")
        request = step["data"]["requests"][0]
        if fault in {"iid", "branch", "commit_sha", "source_sha"}:
            request[fault] = "remote-evidence"
        elif fault == "changed":
            request["changed"] = True
        elif fault == "other_blocker":
            request["blockers"].append("Manual review required")
        elif fault == "checkpoint_error":
            step["data"]["error"] = "Uncertain remote write"
        store.save(
            run,
            "team/repo",
            "runner_mrs",
            fault if fault in {"preparing", "superseded"} else "blocked",
            step["data"],
        )
    before, original = store.report(run), store.plan(run)
    with pytest.raises(CIIncludeRevisionBlocked):
        store.revise_ci_include_omissions(run, ["team/optional-review"], saved.config_hash)
    assert store.report(run) == before
    assert store.plan(run) == original


def test_ci_omission_revision_transaction_rolls_back(include_revision_run):
    store, run, saved = include_revision_run
    before = store.report(run)
    with store.connection() as db:
        db.execute("""CREATE TRIGGER fail_revision BEFORE UPDATE OF plan ON runs
                      BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.revise_ci_include_omissions(run, ["team/optional-review"], saved.config_hash)
    assert StateStore(store.root).report(run) == before
    assert store.plan(run) == saved


@pytest.fixture
def endpoint_run(tag_revision_run):
    store, run, saved = tag_revision_run
    saved.source_version = saved.destination_version = "18.4.1"
    saved.endpoint_snapshot = {
        "source": {"url": saved.config.source.url, "version": "18.4.1", "revision": "a" * 40},
        "destination": {
            "url": saved.config.destination.url,
            "version": "18.4.1",
            "revision": "b" * 40,
        },
    }
    with store.connection() as db:
        db.execute("UPDATE runs SET plan=? WHERE id=?", (saved.model_dump_json(), run))
    store.save(run, "team/repo", "smoke", "complete", {"pipeline_id": 88, "jobs": [{"job_id": 99}]})
    current = copy.deepcopy(saved.endpoint_snapshot)
    current["source"]["revision"] = "c" * 40
    current["destination"]["version"] = "18.4.2"
    return store, run, saved, current


def test_endpoint_refresh_audits_drift_and_supersedes_evidence_atomically(endpoint_run):
    store, run, saved, current = endpoint_run
    before = {step["name"]: step for step in store.report(run)["steps"]}
    updated = store.refresh_endpoints(run, saved, current)
    assert store.plan(run) == updated
    assert updated.endpoint_snapshot == current
    assert updated.source_version == "18.4.1"
    assert updated.destination_version == "18.4.2"
    assert updated.config == saved.config
    assert updated.config_hash == saved.config_hash == updated.config.fingerprint()
    assert store.report(run)["status"] == "awaiting_verification"
    assert store.endpoint_generation(run) == 1
    for name in ("import", "snapshot", "baseline_verified", "runner_mrs", "preparation"):
        assert store.step(run, "team/repo", name) == before[name]
    audit = store.step(run, "run", "endpoint_revision_1")["data"]
    assert audit["previous_endpoint_snapshot"] == saved.endpoint_snapshot
    assert audit["endpoint_snapshot"] == current
    assert {step["name"]: step for step in audit["superseded_steps"]} == {
        name: before[name] for name in ("verification", "smoke")
    }
    for name in ("verification", "smoke"):
        assert store.step(run, "team/repo", name)["status"] == "superseded"
        assert store.step(run, "team/repo", name)["data"] == before[name]["data"]
    # Identical retries after a crash do not create another generation or reset new evidence.
    store.save(run, "team/repo", "verification", "failed", {"error": "Interrupted"})
    report = store.report(run)
    restarted = StateStore(store.root)
    assert restarted.refresh_endpoints(run, updated, current) == updated
    assert restarted.report(run) == report
    assert restarted.endpoint_generation(run) == 1
    # Returning to an older GitLab revision still requires a new smoke generation.
    assert (
        restarted.refresh_endpoints(run, updated, saved.endpoint_snapshot).endpoint_snapshot
        == saved.endpoint_snapshot
    )
    assert restarted.endpoint_generation(run) == 2


@pytest.mark.parametrize(
    "fault",
    [
        "active",
        "native",
        "mixed",
        "pilot",
        "incomplete_import",
        "incomplete_preparation",
        "activation",
        "missing_snapshot",
        "changed_url",
        "unknown_revision",
        "invalid_revision",
        "invalid_version",
        "stale_plan",
        "bad_hash",
    ],
)
def test_endpoint_refresh_refuses_unsafe_state_without_mutation(endpoint_run, fault):
    store, run, saved, current = endpoint_run
    if fault in {"native", "mixed", "pilot", "missing_snapshot", "bad_hash"}:
        if fault == "native":
            saved.config.repositories[0].strategy = "direct_transfer"
        elif fault == "mixed":
            saved.config.repositories.append(
                saved.config.repositories[0].model_copy(
                    update={
                        "source_path": "team/second",
                        "destination_path": "new/second",
                        "strategy": "direct_transfer",
                    }
                )
            )
        elif fault == "pilot":
            saved.pilot = True
        elif fault == "missing_snapshot":
            saved.endpoint_snapshot = {}
        saved.config_hash = "invalid" if fault == "bad_hash" else saved.config.fingerprint()
        with store.connection() as db:
            db.execute("UPDATE runs SET plan=? WHERE id=?", (saved.model_dump_json(), run))
    elif fault == "active":
        store.set_status(run, "active")
    elif fault in {"incomplete_import", "incomplete_preparation", "activation"}:
        name = {
            "incomplete_import": "import",
            "incomplete_preparation": "preparation",
            "activation": "activation",
        }[fault]
        store.save(run, "team/repo", name, "failed", {"identity": "retain"})
    elif fault == "changed_url":
        current["source"]["url"] = "https://different.example"
    elif fault == "unknown_revision":
        current["source"]["revision"] = ""
    elif fault == "invalid_revision":
        current["source"]["revision"] = "synthetic-sensitive-marker"
    elif fault == "invalid_version":
        current["source"]["version"] = "synthetic-sensitive-marker"
    elif fault == "stale_plan":
        saved.endpoint_snapshot["source"]["revision"] = "f" * 40
    before, before_plan = store.report(run), store.plan(run)
    with pytest.raises(CompatibilityBlocked) as error:
        store.refresh_endpoints(run, saved, current)
    assert "synthetic-sensitive-marker" not in str(error.value)
    assert store.report(run) == before
    assert store.plan(run) == before_plan
    assert store.endpoint_generation(run) == 0


def test_endpoint_refresh_rolls_back_audit_invalidation_and_plan_on_failure(endpoint_run):
    store, run, saved, current = endpoint_run
    before = store.report(run)
    with store.connection() as db:
        db.execute("""CREATE TRIGGER fail_refresh BEFORE UPDATE OF plan ON runs
                      BEGIN SELECT RAISE(ABORT, 'synthetic crash'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.refresh_endpoints(run, saved, current)
    assert StateStore(store.root).report(run) == before
    assert store.plan(run) == saved
    assert store.endpoint_generation(run) == 0
