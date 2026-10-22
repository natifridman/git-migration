from unittest.mock import MagicMock

import pytest

from git_migration.providers import ProviderError
from git_migration.strategies import (
    AmbiguousOperation,
    DirectTransferStrategy,
    FileExportStrategy,
    TargetOccupied,
)


@pytest.fixture
def providers():
    source, dest = MagicMock(), MagicMock()
    source.url, source.token, dest.url = "https://source.test", "secret", "https://dest.test"
    source.project.return_value = {"id": 10, "path_with_namespace": "old/repo"}
    dest.group.return_value = {"id": 20, "full_path": "new"}
    dest.project.side_effect = ProviderError(404)
    return source, dest


def test_direct_start_disables_memberships_and_keeps_secret_out_of_handle(providers, tmp_path):
    source, dest = providers
    dest.post.return_value = {"id": 3}
    handle = DirectTransferStrategy(source, dest, tmp_path).start("old/repo", "new/repo")
    data = dest.post.call_args.args[1]
    assert data["entities"][0]["migrate_memberships"] is False
    assert data["entities"][0]["destination_namespace"] == "new"
    assert data["configuration"]["access_token"] == "secret"
    assert "secret" not in str(handle)


def test_preexisting_target_never_overwritten(providers, tmp_path):
    source, dest = providers
    dest.project.side_effect = None
    dest.project.return_value = {"id": 99}
    with pytest.raises(TargetOccupied):
        DirectTransferStrategy(source, dest, tmp_path).start("old/repo", "new/repo")
    dest.post.assert_not_called()


def _entity(**kwargs):
    return {
        "id": 5,
        "entity_type": "project",
        "source_full_path": "old/repo",
        "destination_full_path": "new/repo",
        "destination_namespace": "new",
        "destination_slug": "repo",
        "status": "finished",
        "project_id": 50,
        **kwargs,
    }


def test_direct_partial_failure_is_failed_even_when_overall_finished(providers, tmp_path):
    source, dest = providers
    dest.get.return_value = {"status": "finished"}
    dest.list.side_effect = [[_entity()], [{"exception_message": "secret server response"}]]
    progress = DirectTransferStrategy(source, dest, tmp_path).poll(
        {"bulk_import_id": 3, "source_path": "old/repo", "destination_path": "new/repo"}
    )
    assert progress.status == "failed"
    assert "secret" not in str(progress.failures)


def test_direct_preserves_both_documented_failure_formats_without_remote_text(providers, tmp_path):
    source, dest = providers
    dest.get.return_value = {"status": "finished"}
    dest.list.side_effect = [
        [
            _entity(
                failures=[
                    {
                        "relation": "merge_requests",
                        "pipeline_step": "extractor",
                        "exception_message": "secret pipeline payload",
                    }
                ]
            )
        ],
        [{"relation": "issues", "exception_message": "secret issue payload"}],
    ]
    progress = DirectTransferStrategy(source, dest, tmp_path).poll(
        {"bulk_import_id": 3, "source_path": "old/repo", "destination_path": "new/repo"}
    )
    assert progress.status == "failed"
    assert progress.handle["native_evidence"]["failures"] == [
        {"step": "extractor", "relation": "merge_requests"},
        {"step": "unknown", "relation": "issues"},
    ]
    assert "secret" not in str(progress)


def test_direct_finished_verifies_project_id(providers, tmp_path):
    source, dest = providers
    dest.get.return_value = {"status": "finished"}
    dest.list.side_effect = [[_entity()], []]
    dest.project.side_effect = None
    dest.project.return_value = {"id": 51, "path_with_namespace": "new/repo"}
    with pytest.raises(AmbiguousOperation):
        DirectTransferStrategy(source, dest, tmp_path).poll(
            {"bulk_import_id": 3, "source_path": "old/repo", "destination_path": "new/repo"}
        )


@pytest.mark.parametrize(
    "stats", [{"source": 3, "imported": 2}, {"status": "skipped"}, {"status": "failed"}]
)
def test_direct_incomplete_required_relation_blocks_finished_import(providers, tmp_path, stats):
    source, dest = providers
    dest.get.return_value = {"status": "finished"}
    dest.list.side_effect = [[_entity(stats={"merge_requests": stats})], []]
    progress = DirectTransferStrategy(source, dest, tmp_path).poll(
        {"bulk_import_id": 3, "source_path": "old/repo", "destination_path": "new/repo"}
    )
    assert progress.status == "failed"
    assert "merge_requests" in str(progress.failures)
    assert progress.handle["native_evidence"]["relations"]["merge_requests"] == stats


def test_native_failure_details_only_retain_safe_structural_fields(providers, tmp_path):
    source, dest = providers
    dest.get.return_value = {"status": "finished"}
    dest.list.side_effect = [
        [_entity()],
        [
            {
                "id": 9,
                "pipeline_class": "BulkImports::Projects::Pipelines::NotesPipeline",
                "pipeline_step": "load",
                "exception_message": "token-secret",
                "exception_class": "token-secret",
            }
        ],
    ]
    progress = DirectTransferStrategy(source, dest, tmp_path).poll(
        {"bulk_import_id": 3, "source_path": "old/repo", "destination_path": "new/repo"}
    )
    assert "token-secret" not in str(progress)
    assert progress.handle["native_evidence"]["failures"] == [
        {"id": 9, "step": "load", "relation": "Notes"}
    ]


def test_direct_recovery_correlates_source_instance_and_entity(providers, tmp_path):
    source, dest = providers
    dest.list.side_effect = [
        [{"id": 3, "source_url": source.url, "created_at": "2026-01-02T00:00:00Z"}],
        [_entity()],
    ]
    handle = DirectTransferStrategy(source, dest, tmp_path).recover(
        "old/repo", "new/repo", "2026-01-01T00:00:00Z"
    )
    assert handle["bulk_import_id"] == 3
    dest.post.assert_not_called()


def test_direct_unknown_post_does_not_retry(providers, tmp_path):
    source, dest = providers
    dest.post.side_effect = ProviderError()
    with pytest.raises(AmbiguousOperation):
        DirectTransferStrategy(source, dest, tmp_path).start("old/repo", "new/repo")
    assert dest.post.call_count == 1


def test_file_export_import_stages_and_private_journal(providers, tmp_path):
    source, dest = providers
    strategy = FileExportStrategy(source, dest, tmp_path)
    handle = strategy.start("old/repo", "new/repo")
    source.get.return_value = {"export_status": "finished"}
    source.download.side_effect = lambda path, target: target.write_bytes(b"opaque")
    dest.upload.return_value = {"id": 50, "path_with_namespace": "new/repo"}
    progress = strategy.poll(handle)
    assert progress.status == "pending"
    assert progress.handle["stage"] == "importing"
    assert dest.upload.call_args.args[2] == {"path": "repo", "namespace": 20, "overwrite": False}
    assert not list(tmp_path.glob("*.tar.gz"))
    journal = next(tmp_path.glob("*.json"))
    assert journal.stat().st_mode & 0o777 == 0o600
    assert "secret" not in journal.read_text()
    dest.get.return_value = {"import_status": "finished", "failed_relations": []}
    dest.project.side_effect = None
    dest.project.return_value = {"id": 50, "path_with_namespace": "new/repo"}
    assert strategy.poll(handle).status == "finished"  # stale parent state uses journal


def test_file_upload_crash_never_resubmits(providers, tmp_path):
    source, dest = providers
    strategy = FileExportStrategy(source, dest, tmp_path)
    handle = strategy.start("old/repo", "new/repo")
    source.get.return_value = {"export_status": "finished"}
    source.download.side_effect = lambda path, target: target.write_bytes(b"opaque")
    dest.upload.side_effect = ProviderError()
    with pytest.raises(AmbiguousOperation):
        strategy.poll(handle)
    with pytest.raises(AmbiguousOperation):
        strategy.poll(handle)
    assert dest.upload.call_count == 1
    assert not list(tmp_path.glob("*.tar.gz"))


def test_file_missing_journal_refuses_unrelated_target_adoption(providers, tmp_path):
    source, dest = providers
    with pytest.raises(AmbiguousOperation):
        FileExportStrategy(source, dest, tmp_path).recover(
            "old/repo", "new/repo", "2026-01-01T00:00:00Z"
        )
    dest.project.assert_not_called()
