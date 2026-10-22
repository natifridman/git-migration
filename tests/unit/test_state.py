"""Exercise durable checkpointing and actual cross-process writer exclusion."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from git_migration.models import MigrationConfig, MigrationPlan
from git_migration.state import StateStore, atomic_json


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
