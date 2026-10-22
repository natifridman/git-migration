"""Independent verification of imported repositories and merge-request evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..git import GitError, snapshot_repository


def _result(blockers: list[str], warnings: list[str], **evidence: Any) -> dict[str, Any]:
    return {
        "status": "blocked" if blockers else "passed",
        "blockers": blockers,
        "warnings": warnings,
        **evidence,
    }


def _mr_key(mr: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(mr.get(key) for key in ("title", "source_branch", "target_branch"))


def _discussion_signature(discussion: dict[str, Any], project: dict[str, Any]) -> tuple[Any, ...]:
    def normalized(body: str) -> str:
        for url in (project.get("web_url"), project.get("http_url_to_repo")):
            if url:
                body = body.replace(str(url).removesuffix(".git"), "<PROJECT>")
        return body

    return tuple(
        (
            normalized(note.get("body") or ""),
            note.get("resolvable", False),
            note.get("resolved", False),
            note.get("type"),
            note.get("created_at"),
            note.get("internal", False),
            note.get("confidential", False),
            tuple(
                (note.get("position") or {}).get(key)
                for key in ("old_path", "new_path", "old_line", "new_line")
            ),
        )
        for note in discussion.get("notes", [])
        if not note.get("system")
    )


def verify_merge_requests(
    source: Any,
    destination: Any,
    source_project: dict[str, Any],
    target_project: dict[str, Any],
    ignored_mr_iids: set[int] | None = None,
    user_mapping: dict[int, int] | None = None,
) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    mapping: dict[str, int] = {}
    evidence: dict[str, Any] = {
        "source_count": None,
        "target_count": None,
        "iid_mapping": mapping,
        "discussions_verified": 0,
        "identities": [],
        "coverage": [],
    }
    try:
        source_mrs = source.list(
            f"/projects/{source_project['id']}/merge_requests", params={"state": "all"}
        )
        target_mrs = destination.list(
            f"/projects/{target_project['id']}/merge_requests", params={"state": "all"}
        )
    except Exception:
        return _result(["Merge requests could not be read from both providers"], [], **evidence)
    target_mrs = [mr for mr in target_mrs if mr.get("iid") not in (ignored_mr_iids or set())]
    evidence.update(source_count=len(source_mrs), target_count=len(target_mrs))
    if len(source_mrs) != len(target_mrs):
        blockers.append("Merge request counts differ")
    unused = list(target_mrs)
    coverage: set[str] = set()
    for original in source_mrs:
        if original.get("state") in {"opened", "closed", "merged"}:
            coverage.add("mr_" + original["state"])
        candidates = [
            mr
            for mr in unused
            if mr.get("iid") == original.get("iid") and _mr_key(mr) == _mr_key(original)
        ]
        if not candidates:
            candidates = [mr for mr in unused if _mr_key(mr) == _mr_key(original)]
        if len(candidates) != 1:
            blockers.append(f"Merge request !{original.get('iid')} has no unique matching target")
            continue
        imported = candidates[0]
        unused.remove(imported)
        mapping[str(original["iid"])] = imported["iid"]
        if any(
            original.get(field) != imported.get(field)
            for field in (
                "state",
                "description",
                "draft",
                "work_in_progress",
                "discussion_locked",
                "created_at",
                "merged_at",
                "closed_at",
            )
        ):
            blockers.append(f"Merge request !{original['iid']} metadata or state differs")
        if original.get("source_project_id") not in (None, source_project["id"]):
            warnings.append(
                f"Merge request !{original['iid']} depends on an external fork; dependency mapping needs review"
            )
            if not imported.get("source_project_id"):
                blockers.append(f"Merge request !{original['iid']} is missing its source fork")
        author = imported.get("author") or {}
        evidence["identities"].append(
            author_identity(original.get("author"), author, user_mapping, f"mr:{original['iid']}")
        )
        if author.get("state") == "blocked" or "placeholder" in author.get("username", "").lower():
            warnings.append(
                f"Merge request !{original['iid']} has an unresolved placeholder author"
            )
        if (original.get("author") or {}).get("username") != author.get("username"):
            warnings.append(
                f"Merge request !{original['iid']} author identity mapping needs review"
            )
        try:
            old = source.list(
                f"/projects/{source_project['id']}/merge_requests/{original['iid']}/discussions"
            )
            new = destination.list(
                f"/projects/{target_project['id']}/merge_requests/{imported['iid']}/discussions"
            )
            # Compare thread contents, resolution and order within each thread, independent of API IDs.
            remaining = [
                (signature, thread)
                for thread in new
                if (signature := _discussion_signature(thread, target_project))
            ]
            old_system = sum(
                bool(note.get("system")) for thread in old for note in thread.get("notes", [])
            )
            new_system = sum(
                bool(note.get("system")) for thread in new for note in thread.get("notes", [])
            )
            if old_system != new_system:
                warnings.append(
                    f"Merge request !{original['iid']} system note counts differ after import"
                )
            for thread in old:
                signature = _discussion_signature(thread, source_project)
                if not signature:
                    continue
                matches = [pair for pair in remaining if pair[0] == signature]
                if matches:
                    matched = matches[0]
                    remaining.remove(matched)
                    coverage.add("discussions")
                    old_notes = [n for n in thread.get("notes", []) if not n.get("system")]
                    new_notes = [n for n in matched[1].get("notes", []) if not n.get("system")]
                    for old_note, new_note in zip(old_notes, new_notes, strict=True):
                        if old_note.get("resolved"):
                            coverage.add("resolved_discussions")
                        evidence["identities"].append(
                            author_identity(
                                old_note.get("author"),
                                new_note.get("author"),
                                user_mapping,
                                f"mr:{original['iid']}:note:{old_note.get('id')}",
                            )
                        )
                else:
                    blockers.append(f"Merge request !{original['iid']} discussions differ")
                    break
            else:
                if remaining:
                    blockers.append(f"Merge request !{original['iid']} discussions differ")
                else:
                    evidence["discussions_verified"] += 1
            if any(
                (note.get("author") or {}).get("username", "").lower().find("placeholder") >= 0
                for thread in new
                for note in thread.get("notes", [])
            ):
                warnings.append(
                    f"Merge request !{original['iid']} discussion authors include unresolved placeholders"
                )
        except Exception:
            blockers.append(f"Merge request !{original['iid']} discussions could not be verified")
    if unused:
        blockers.append("Target contains unmatched merge requests")
    warnings.append(
        "Author mappings and external fork dependencies require explicit reconciliation; counts alone do not establish fidelity"
    )
    evidence["coverage"] = sorted(coverage)
    return _result(blockers, warnings, **evidence)


def author_identity(source: Any, target: Any, mapping: dict[int, int] | None, entity: str) -> dict:
    source, target = source or {}, target or {}
    source_id, target_id = source.get("id"), target.get("id")
    placeholder = "placeholder" in str(target.get("username", "")).lower()
    status = "placeholder" if placeholder else "unresolved"
    if (
        not placeholder
        and source_id is not None
        and target_id is not None
        and (mapping or {}).get(source_id) == target_id
    ):
        status = "mapped"
    return {
        "entity": entity,
        "source_user_id": source_id,
        "destination_user_id": target_id,
        "status": status,
    }


def verify_issues(
    source: Any,
    destination: Any,
    source_project: dict,
    target_project: dict,
    user_mapping: dict[int, int] | None = None,
) -> dict:
    blockers: list[str] = []
    evidence: dict[str, Any] = {
        "iid_mapping": {},
        "identities": [],
        "coverage": [],
        "comments_verified": 0,
    }
    coverage: set[str] = set()
    try:
        old = source.list(
            f"/projects/{source_project['id']}/issues", params={"state": "all", "scope": "all"}
        )
        new = destination.list(
            f"/projects/{target_project['id']}/issues", params={"state": "all", "scope": "all"}
        )
        evidence.update(source_count=len(old), target_count=len(new))
        if len(old) != len(new):
            blockers.append("Issue counts differ")
        remaining = list(new)
        for issue in old:
            candidates = [
                item
                for item in remaining
                if item.get("iid") == issue.get("iid") and item.get("title") == issue.get("title")
            ]
            if len(candidates) != 1:
                blockers.append(f"Issue #{issue['iid']} has no unique matching target")
                continue
            imported = candidates[0]
            remaining.remove(imported)
            evidence["iid_mapping"][str(issue["iid"])] = imported["iid"]
            coverage.add("issues")
            if any(
                issue.get(k) != imported.get(k)
                for k in (
                    "title",
                    "description",
                    "state",
                    "confidential",
                    "created_at",
                    "closed_at",
                    "due_date",
                    "labels",
                )
            ):
                blockers.append(f"Issue #{issue['iid']} metadata or state differs")
            evidence["identities"].append(
                author_identity(
                    issue.get("author"),
                    imported.get("author"),
                    user_mapping,
                    f"issue:{issue['iid']}",
                )
            )
            old_notes = [
                note
                for note in source.list(
                    f"/projects/{source_project['id']}/issues/{issue['iid']}/notes"
                )
                if not note.get("system")
            ]
            new_notes = [
                note
                for note in destination.list(
                    f"/projects/{target_project['id']}/issues/{imported['iid']}/notes"
                )
                if not note.get("system")
            ]
            # Compare as a multiset; API IDs and pagination order differ after import.
            for note in old_notes:
                signature = _discussion_signature({"notes": [note]}, source_project)
                matches = [
                    n
                    for n in new_notes
                    if _discussion_signature({"notes": [n]}, target_project) == signature
                    and n.get("internal", False) == note.get("internal", False)
                ]
                if not matches:
                    blockers.append(f"Issue #{issue['iid']} comments differ")
                    continue
                match = matches[0]
                new_notes.remove(match)
                coverage.add("issue_comments")
                evidence["comments_verified"] += 1
                evidence["identities"].append(
                    author_identity(
                        note.get("author"),
                        match.get("author"),
                        user_mapping,
                        f"issue:{issue['iid']}:note:{note.get('id')}",
                    )
                )
            if new_notes:
                blockers.append(f"Issue #{issue['iid']} has extra destination comments")
        if remaining:
            blockers.append("Target contains unmatched issues")
    except Exception:
        blockers.append("Issues or comments could not be verified; check access on both projects")
    evidence["coverage"] = sorted(coverage)
    return _result(blockers, [], **evidence)


def verify_repository(
    source: Any,
    destination: Any,
    source_project: dict[str, Any],
    target_project: dict[str, Any],
    work_dir: Path,
    expected_refs: dict[str, Any] | None = None,
    ignored_refs: set[str] | None = None,
    ignored_mr_iids: set[int] | None = None,
    user_mapping: dict[int, int] | None = None,
    require_identity_mapping: bool = False,
) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    for label, project in (("Source", source_project), ("Target", target_project)):
        if project.get("import_error"):
            blockers.append(f"{label} project reports an import error")
    if target_project.get("import_status") not in (None, "none", "finished"):
        blockers.append("Target project import has not completed successfully")
    refs_evidence = {}
    try:
        baseline = (
            expected_refs
            if expected_refs is not None
            else snapshot_repository(source, source_project, work_dir)
        )
        actual = snapshot_repository(destination, target_project, work_dir)
        expected = baseline.get("refs", baseline)
        wanted = {
            key: value
            for key, value in expected.items()
            if key.startswith(("refs/heads/", "refs/tags/")) and key not in (ignored_refs or set())
        }
        actual["refs"] = {
            key: value
            for key, value in actual["refs"].items()
            if key not in (ignored_refs or set())
        }
        missing = sorted(set(wanted) - set(actual["refs"]))
        extra = sorted(set(actual["refs"]) - set(wanted))
        changed = sorted(
            key for key in set(wanted) & set(actual["refs"]) if wanted[key] != actual["refs"][key]
        )
        refs_evidence = {
            "missing": missing,
            "extra": extra,
            "changed": changed,
            "source_ref_count": len(wanted),
            "target_ref_count": len(actual["refs"]),
        }
        if missing or extra or changed:
            blockers.append("Branch or tag names/object identifiers differ")
        default_branch = baseline.get("default_branch", source_project.get("default_branch"))
        if default_branch != actual.get("default_branch") or source_project.get(
            "default_branch"
        ) != target_project.get("default_branch"):
            blockers.append("Default branch differs")
        if "lfs_oids" not in baseline:
            # A bare ref map is insufficient to prove transfer of LFS payloads.
            baseline = snapshot_repository(source, source_project, work_dir)
        if baseline.get("lfs_oids", []) != actual.get("lfs_oids", []):
            blockers.append("LFS object identifiers differ")
        refs_evidence["lfs_object_count"] = len(actual.get("lfs_oids", []))
    except GitError as error:
        blockers.append(str(error))
    except Exception:
        blockers.append("Repository verification could not be completed")
    merge_requests = verify_merge_requests(
        source, destination, source_project, target_project, ignored_mr_iids, user_mapping
    )
    blockers.extend(merge_requests["blockers"])
    warnings.extend(merge_requests["warnings"])
    issues = verify_issues(source, destination, source_project, target_project, user_mapping)
    blockers.extend(issues["blockers"])
    identities = merge_requests["identities"] + issues["identities"]
    unresolved = [item for item in identities if item["status"] != "mapped"]
    if unresolved:
        message = "Contribution identities need explicit user_mapping or native placeholder reassignment; inspect identity evidence"
        (blockers if require_identity_mapping else warnings).append(message)
    coverage = set(merge_requests["coverage"]) | set(issues["coverage"])
    if refs_evidence.get("source_ref_count"):
        coverage.add("git")
    if refs_evidence.get("lfs_object_count"):
        coverage.add("lfs")
    if (
        len({item["source_user_id"] for item in identities if item["source_user_id"] is not None})
        >= 2
    ):
        coverage.add("multiple_authors")
    warnings.append(
        "Imported pipeline statuses are historical; a new smoke job on an approved runner is required"
    )
    return _result(
        blockers,
        warnings,
        refs=refs_evidence,
        merge_requests=merge_requests,
        issues=issues,
        identities=identities,
        coverage=sorted(coverage),
        evidence_version=2,
        smoke_required=True,
    )


def verify_runner_smoke(
    job: dict[str, Any], allowed_runner_ids: list[int] | set[int]
) -> dict[str, Any]:
    blockers = []
    if job.get("status") != "success" or not job.get("finished_at"):
        blockers.append("Smoke job has not finished successfully")
    runner_id = (job.get("runner") or {}).get("id")
    if runner_id is None or str(runner_id) not in {str(item) for item in allowed_runner_ids}:
        blockers.append("Smoke job did not run on an approved runner")
    return _result(blockers, [], job_id=job.get("id"), runner_id=runner_id)
