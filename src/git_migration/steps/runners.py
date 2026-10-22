"""Runner eligibility, conservative CI editing, and review-only migration branches."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
from collections.abc import Mapping, MutableMapping
from typing import Any
from urllib.parse import quote

from git_migration.models import RunnerProfile
from git_migration.secrets import SafeError


class RunnerBlocked(SafeError):
    pass


def _p(project_id: Any) -> Any:
    return f"/projects/{quote(str(project_id), safe='')}"


def _eligible(runner: Any, tags: Any, protected_ref: Any) -> Any:
    if runner.get("paused", False):
        return False
    if runner.get("access_level") == "ref_protected" and not protected_ref:
        return False
    return (
        set(tags).issubset(runner.get("tag_list", []))
        if tags
        else bool(runner.get("run_untagged", False))
    )


def discover_runner_ids(destination: Any, namespace: str, profile: RunnerProfile) -> list[int]:
    """Resolve all configured tags against runners inherited by this namespace."""
    path = namespace
    while path:
        try:
            group = destination.group(path)
            break
        except Exception as error:
            if getattr(error, "status_code", None) != 404:
                raise
            path = path.rpartition("/")[0]
    else:
        raise RunnerBlocked(
            "Runner tag discovery requires an existing destination group or ancestor"
        )
    summaries = destination.list(
        f"/groups/{group['id']}/runners", params={"tag_list": ",".join(profile.tags)}
    )
    allowed_scopes = set()
    if profile.group_enabled:
        allowed_scopes.add("group_type")
    if profile.shared_enabled:
        allowed_scopes.add("instance_type")
    identifiers = set()
    for summary in summaries:
        runner = destination.get(f"/runners/{summary['id']}")
        if runner.get("id") != summary["id"]:
            raise RunnerBlocked("Destination runner identity could not be verified")
        if runner.get("runner_type") in allowed_scopes and set(profile.tags).issubset(
            runner.get("tag_list", [])
        ):
            identifiers.add(int(runner["id"]))
    if not identifiers:
        raise RunnerBlocked(
            "No destination group or instance runner matches all configured tags in an enabled "
            "scope; check runners.tags, group_enabled, and shared_enabled"
        )
    return sorted(identifiers)


def inspect_runners(
    destination: Any,
    project_id: Any,
    allowed_runner_ids: Any,
    required_tags: Any = (),
    untagged: Any = False,
    protected_ref: Any = False,
    strict: bool = False,
) -> Any:
    """All requested tags must match; report any eligible runner outside the allowlist."""
    allowed = set(allowed_runner_ids)
    runners = destination.list(_p(project_id) + "/runners")
    # The list endpoint omits tag/access details. Never guess eligibility from it.
    details = [destination.get(f"/runners/{r['id']}") for r in runners]
    if strict and any(
        runner.get("id") != summary.get("id")
        or not isinstance(runner.get("tag_list"), list)
        or any(not isinstance(tag, str) for tag in runner.get("tag_list", []))
        or not isinstance(runner.get("paused"), bool)
        or not isinstance(runner.get("run_untagged"), bool)
        or runner.get("access_level") not in {"not_protected", "ref_protected"}
        or runner.get("status") not in {"online", "offline", "stale", "never_contacted"}
        for summary, runner in zip(runners, details, strict=True)
    ):
        return {
            "eligible_runner_ids": [],
            "blockers": [
                "Runner eligibility details are incomplete; check runner read permissions"
            ],
        }
    tags = () if untagged else required_tags
    eligible = [r["id"] for r in details if _eligible(r, tags, protected_ref)]
    online = {r["id"] for r in details if r.get("status") == "online"}
    blockers = []
    if not set(eligible) & allowed:
        blockers.append("No approved runner can execute this job configuration")
    if set(eligible) & allowed and not set(eligible) & allowed & online:
        blockers.append("Approved matching runners are currently offline")
    if set(eligible) - allowed:
        blockers.append(
            "Eligible inherited or assigned runners exist outside the approved allowlist"
        )
    return {"eligible_runner_ids": eligible, "blockers": blockers}


def configure_runners(
    destination: Any,
    project_id: Any,
    approved_runner_ids: Any,
    shared_enabled: Any = False,
    group_enabled: Any = False,
) -> Any:
    """Only project flags and project-runner assignments; never modify a runner fleet."""
    path = _p(project_id)
    current = {r["id"] for r in destination.list(path + "/runners")}
    approved = [destination.get(f"/runners/{i}") for i in approved_runner_ids]
    for runner in approved:
        kind = runner.get("runner_type")
        if kind not in ("project_type", "group_type", "instance_type"):
            raise RunnerBlocked("Runner scope cannot be determined")
        if (
            kind == "group_type"
            and not group_enabled
            or kind == "instance_type"
            and not shared_enabled
        ):
            raise RunnerBlocked("Approved runner requires an explicitly enabled runner scope")
    destination.put(
        path, {"shared_runners_enabled": shared_enabled, "group_runners_enabled": group_enabled}
    )
    for runner in approved:
        if runner.get("runner_type") == "project_type" and runner["id"] not in current:
            destination.post(path + "/runners", {"runner_id": runner["id"]})
    return {
        "approved_runner_ids": list(approved_runner_ids),
        "shared_enabled": shared_enabled,
        "group_enabled": group_enabled,
    }


def _yaml() -> Any:
    from ruamel.yaml import YAML

    parser = YAML(typ="rt")
    parser.preserve_quotes = True
    return parser


_RESERVED = {
    "include",
    "default",
    "stages",
    "variables",
    "workflow",
    "image",
    "services",
    "before_script",
    "after_script",
    "cache",
    "spec",
    "types",
}


def transform_ci_yaml(content: Any, tag_map: Any, project_include_map: Any = None) -> Any:
    """Edit explicit tag nodes only. Preserve round-trip anchors and custom !reference tags."""
    parser = _yaml()
    try:
        doc = parser.load(content)
    except Exception:
        return {"content": content, "changed": False, "blockers": ["CI YAML could not be parsed"]}
    if not isinstance(doc, Mapping):
        return {
            "content": content,
            "changed": False,
            "blockers": ["CI configuration must be a mapping"],
        }
    blockers, changed, seen = [], False, set()
    nodes = [doc]
    if isinstance(doc.get("default"), Mapping):
        nodes.append(doc["default"])
    nodes.extend(v for k, v in doc.items() if k not in _RESERVED and isinstance(v, Mapping))
    for node in nodes:
        if "tags" not in node:
            continue
        tags = node["tags"]
        if id(tags) in seen:
            continue
        seen.add(id(tags))
        if (
            not isinstance(tags, list)
            or str(getattr(tags, "tag", "")) == "!reference"
            or any(not isinstance(t, str) or "$" in t for t in tags)
        ):
            blockers.append("Dynamic or referenced runner tags require explicit resolution")
            continue
        replacement = []
        for tag in tags:
            values = tag_map.get(tag, [tag])
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list) or any(
                not isinstance(t, str) or "$" in t for t in values
            ):
                blockers.append("Runner tag mapping must contain literal tag lists")
                values = [tag]
            replacement.extend(values)
        if replacement != list(tags):
            tags[:] = replacement
            changed = True
    includes = doc.get("include", [])
    includes = includes if isinstance(includes, list) else [includes]
    for inc in includes:
        if isinstance(inc, MutableMapping) and "project" in inc:
            project = inc["project"]
            if project in (project_include_map or {}):
                if inc["project"] != project_include_map[project]:
                    inc["project"] = project_include_map[project]
                    changed = True
            else:
                blockers.append("Project CI include requires an explicit destination mapping")
        elif not (isinstance(inc, Mapping) and "local" in inc) and not (
            isinstance(inc, str) and inc.startswith("/")
        ):
            blockers.append("External, component or dynamic CI include requires manual review")
    output = io.StringIO()
    parser.dump(doc, output)
    return {
        "content": output.getvalue() if changed else content,
        "changed": changed,
        "blockers": sorted(set(blockers)),
    }


def _read_file(destination: Any, path: Any, name: Any, ref: Any) -> Any:
    result = destination.get(f"{path}/repository/files/{quote(name, safe='')}", params={"ref": ref})
    if result.get("encoding") != "base64":
        raise RunnerBlocked("Repository file encoding is unsupported")
    try:
        return base64.b64decode(result["content"], validate=False).decode("utf-8")
    except (ValueError, UnicodeError):
        raise RunnerBlocked("Repository CI file is unreadable") from None


def _job_tags(doc: Any) -> Any:
    """Resolve local extends/default/inherit semantics; return conservative blockers."""
    jobs = {k: v for k, v in doc.items() if k not in _RESERVED and isinstance(v, Mapping)}
    blockers: list[str] = []
    resolved: dict[str, Any] = {}

    def resolve(name: Any, stack: Any = ()) -> Any:
        if name in stack or name not in jobs:
            blockers.append("CI extends dependency could not be resolved locally")
            return {}
        if name in resolved:
            return resolved[name]
        job, inherited = jobs[name], {}
        parents = job.get("extends", [])
        parents = [parents] if isinstance(parents, str) else parents
        if not isinstance(parents, list):
            blockers.append("Dynamic CI extends requires manual review")
            parents = []
        for parent in parents:
            inherited.update(resolve(parent, (*stack, name)))
        inherited.update(job)
        resolved[name] = inherited
        return inherited

    entries = []
    for name in jobs:
        if name.startswith("."):
            continue
        job = resolve(name)
        if "trigger" in job and "script" not in job:
            blockers.append("Downstream pipeline dependency requires manual review")
            continue
        defaults = doc.get("default", {})
        if not isinstance(defaults, Mapping):
            blockers.append("CI defaults must be a mapping")
            defaults = {}
        inherit = job.get("inherit", {})
        use_default = inherit is not False and (
            not isinstance(inherit, Mapping) or inherit.get("default", True) is not False
        )
        if isinstance(inherit, Mapping) and isinstance(inherit.get("default"), list):
            use_default = "tags" in inherit["default"]
        tags = job.get("tags", defaults.get("tags", doc.get("tags", [])) if use_default else [])
        if (
            not isinstance(tags, list)
            or str(getattr(tags, "tag", "")) == "!reference"
            or any(not isinstance(t, str) or "$" in t for t in tags)
        ):
            blockers.append("Job runner tags cannot be resolved to literal tags")
        else:
            entries.append({"job": name, "tags": list(tags)})
    return entries, blockers


def _verify_branch(destination: Any, path: Any, source_sha: Any, branch: Any, actions: Any) -> Any:
    commit_sha = destination.get(path + "/repository/branches/" + quote(branch, safe=""))["commit"][
        "id"
    ]
    comparison = destination.get(
        path + "/repository/compare",
        params={"from": source_sha, "to": commit_sha, "straight": True},
    )
    diffs = comparison.get("diffs")
    if comparison.get("compare_timeout") or not isinstance(diffs, list):
        raise RunnerBlocked("Migration branch diff could not be verified")
    expected = {a["file_path"] for a in actions}
    actual = {d.get("new_path") for d in diffs}
    if actual != expected or any(
        d.get("deleted_file") or d.get("renamed_file") or d.get("new_file") for d in diffs
    ):
        raise RunnerBlocked("Migration branch contains changes outside the prepared CI edits")
    for action in actions:
        if _read_file(destination, path, action["file_path"], commit_sha) != action["content"]:
            raise RunnerBlocked("Migration branch content does not match the prepared CI edits")
    return commit_sha


def _lint_jobs(
    destination: Any, path: Any, content: Any, ref: Any, jobs: Any, requires_merged: bool = False
) -> Any:
    lint = destination.post(
        path + "/ci/lint",
        {"content": content, "ref": ref, "dry_run": False, "include_merged_yaml": True},
    )
    if not lint.get("valid", False):
        return jobs, ["Destination CI Lint rejected the migration configuration"]
    if requires_merged and not lint.get("merged_yaml"):
        return jobs, ["Destination CI Lint did not return merged external include configuration"]
    if lint.get("merged_yaml"):
        try:
            merged = _yaml().load(lint["merged_yaml"])
            if not isinstance(merged, Mapping):
                raise RunnerBlocked("Merged destination CI configuration is unreadable")
        except Exception:
            raise RunnerBlocked("Merged destination CI configuration is unreadable") from None
        return _job_tags(merged)
    return jobs, []


def prepare_runner_merge_request(
    destination: Any,
    project_id: Any,
    ref: Any,
    tag_map: Any,
    ci_path: Any = ".gitlab-ci.yml",
    project_include_map: Any = None,
) -> Any:
    """Prepare a deterministic branch/MR with builds disabled. Never run or merge source CI."""
    path = _p(project_id)
    project = destination.project(project_id)
    if project.get("builds_access_level") != "disabled":
        raise RunnerBlocked(
            "Destination CI must be disabled while preparing migration merge requests"
        )
    if not ci_path or "$" in ci_path or "@" in ci_path or "://" in ci_path:
        raise RunnerBlocked("External or dynamic CI configuration requires manual review")
    base_branch = destination.get(path + "/repository/branches/" + quote(ref, safe=""))
    source_sha = base_branch["commit"]["id"]
    try:
        _read_file(destination, path, ci_path.lstrip("/"), source_sha)
    except Exception as error:
        if (
            getattr(error, "status_code", None) == 404
            and ci_path == ".gitlab-ci.yml"
            and not project.get("ci_config_path")
            and project.get("auto_devops_enabled") is False
        ):
            return {
                "status": "no_ci",
                "source_sha": source_sha,
                "jobs": [],
                "blockers": [],
                "changed": False,
            }
        raise
    originals, edited, documents, blockers = {}, {}, {}, []
    visiting = set()
    project_includes: list[Any] = []

    def visit(name: Any) -> Any:
        name = name.lstrip("/")
        if name in visiting:
            blockers.append("Cyclic local CI include requires manual review")
            return
        if name in originals:
            return
        if any(c in name for c in ("$", "*", "?")) or ".." in name.split("/"):
            blockers.append("Dynamic local CI include requires manual review")
            return
        visiting.add(name)
        content = _read_file(destination, path, name, source_sha)
        result = transform_ci_yaml(content, tag_map, project_include_map)
        originals[name], edited[name] = content, result["content"]
        blockers.extend(result["blockers"])
        try:
            doc = _yaml().load(result["content"])
        except Exception:
            blockers.append("CI YAML could not be parsed")
            visiting.remove(name)
            return
        documents[name] = doc
        includes = doc.get("include", []) if isinstance(doc, Mapping) else []
        includes = includes if isinstance(includes, list) else [includes]
        for inc in includes:
            if isinstance(inc, Mapping) and "project" in inc:
                project_includes.append(inc)
            local = (
                inc.get("local")
                if isinstance(inc, Mapping)
                else inc
                if isinstance(inc, str) and inc.startswith("/")
                else None
            )
            if local:
                visit(local)
        visiting.remove(name)

    visit(ci_path)
    try:
        if _read_file(destination, path, ".gitmodules", source_sha).strip():
            blockers.append(
                "Submodule URLs require explicit dependency review before CI activation"
            )
    except Exception as exc:
        if getattr(exc, "status_code", None) != 404:
            raise
    combined = {}
    # GitLab merges included configs before the root file.
    for _name, doc in reversed(list(documents.items())):
        if isinstance(doc, Mapping):
            for key, value in doc.items():
                if key in combined and key not in _RESERVED:
                    blockers.append(
                        "CI job defined across multiple files requires merged configuration review"
                    )
                combined[key] = value
    jobs, job_blockers = _job_tags(combined)
    if not project_includes:
        blockers.extend(job_blockers)
    if blockers:
        return {
            "status": "blocked",
            "changed": False,
            "source_sha": source_sha,
            "blockers": sorted(set(blockers)),
            "jobs": jobs,
        }
    actions = [
        {"action": "update", "file_path": name, "content": edited[name]}
        for name in edited
        if edited[name] != originals[name]
    ]
    if not actions:
        jobs, lint_blockers = _lint_jobs(
            destination, path, edited[ci_path.lstrip("/")], source_sha, jobs, bool(project_includes)
        )
        return {
            "status": "blocked" if lint_blockers else "unchanged",
            "changed": False,
            "source_sha": source_sha,
            "jobs": jobs,
            "blockers": lint_blockers,
        }
    identity = json.dumps({"ref": ref, "actions": actions}, sort_keys=True).encode()
    branch = "codex/runner-migration-" + hashlib.sha256(identity).hexdigest()[:16]
    mrs = destination.list(
        path + "/merge_requests",
        params={"scope": "all", "source_branch": branch, "target_branch": ref, "state": "all"},
    )
    if mrs:
        mr = mrs[0]
        for action in actions:
            if _read_file(destination, path, action["file_path"], branch) != action["content"]:
                raise RunnerBlocked("Migration merge request branch contains conflicting edits")
        commit_sha = _verify_branch(destination, path, source_sha, branch, actions)
        jobs, lint_blockers = _lint_jobs(
            destination, path, edited[ci_path.lstrip("/")], commit_sha, jobs, bool(project_includes)
        )
        return {
            "status": "blocked" if lint_blockers else "existing",
            "commit_sha": commit_sha,
            "branch": branch,
            "source_branch": branch,
            "target_branch": ref,
            "source_sha": source_sha,
            "changed": True,
            "iid": mr["iid"],
            "web_url": mr.get("web_url"),
            "jobs": jobs,
            "blockers": lint_blockers,
        }
    try:
        destination.get(path + "/repository/branches/" + quote(branch, safe=""))
    except Exception as exc:
        if getattr(exc, "status_code", None) != 404:
            raise
        destination.post(path + "/repository/branches", {"branch": branch, "ref": source_sha})
    branch_actions = []
    for action in actions:
        actual = _read_file(destination, path, action["file_path"], branch)
        if actual == action["content"]:
            continue
        if actual != originals[action["file_path"]]:
            raise RunnerBlocked("Migration branch contains conflicting edits")
        branch_actions.append(action)
    if branch_actions:
        destination.post(
            path + "/repository/commits",
            {
                "branch": branch,
                "commit_message": "Prepare destination runner tags [skip ci]",
                "actions": branch_actions,
            },
        )
    commit_sha = _verify_branch(destination, path, source_sha, branch, actions)
    jobs, lint_blockers = _lint_jobs(
        destination, path, edited[ci_path.lstrip("/")], commit_sha, jobs, bool(project_includes)
    )
    if lint_blockers:
        return {
            "status": "blocked",
            "branch": branch,
            "commit_sha": commit_sha,
            "source_sha": source_sha,
            "changed": True,
            "jobs": jobs,
            "blockers": lint_blockers,
        }
    mr = destination.post(
        path + "/merge_requests",
        {
            "source_branch": branch,
            "target_branch": ref,
            "title": "Draft: Prepare destination CI runner tags",
            "description": "Review runner eligibility and dependencies before enabling CI. Merge only after explicit approval.",
            "remove_source_branch": False,
        },
    )
    return {
        "status": "created",
        "commit_sha": commit_sha,
        "branch": branch,
        "source_branch": branch,
        "target_branch": ref,
        "source_sha": source_sha,
        "changed": True,
        "iid": mr["iid"],
        "web_url": mr.get("web_url"),
        "jobs": jobs,
        "blockers": [],
    }


def smoke_job_yaml(marker: Any = "migration-runner-smoke", tags: Any = ()) -> Any:
    """Standalone config: no checkout, includes, source scripts, environments, or secret output."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", marker) or any(
        not isinstance(t, str) or "$" in t for t in tags
    ):
        raise RunnerBlocked("Smoke marker and tags must be literal")
    doc = {
        "workflow": {"rules": [{"if": '$CI_PIPELINE_SOURCE == "api"'}, {"when": "never"}]},
        "variables": {"GIT_STRATEGY": "none", "CI_DEBUG_TRACE": "false"},
        marker: {
            "tags": list(tags),
            "inherit": {"default": False, "variables": False},
            "variables": {"GIT_STRATEGY": "none", "CI_DEBUG_TRACE": "false"},
            "script": ["printf 'runner-smoke-ok\\n'"],
            "timeout": "5 minutes",
        },
    }
    output = io.StringIO()
    _yaml().dump(doc, output)
    return output.getvalue()


def verify_prepared_merge_request(
    destination: Any,
    project_id: Any,
    prepared: dict[str, Any],
    ci_path: str = ".gitlab-ci.yml",
    require_merged: bool = True,
) -> dict[str, Any]:
    """Verify reviewed content against its eventual merge tree, without mutating it."""
    result: dict[str, Any] = {"merged_sha": None, "jobs": [], "blockers": []}
    blockers = result["blockers"]
    path = _p(project_id)
    blockers.extend(_ci_setting_blockers(destination, project_id, ci_path))
    if blockers:
        return result
    required = ("iid", "source_branch", "target_branch", "commit_sha")
    if any(not prepared.get(key) for key in required):
        blockers.append("Prepared merge request evidence is incomplete")
        return result
    mr = destination.get(f"{path}/merge_requests/{prepared['iid']}")
    if (
        mr.get("source_branch") != prepared["source_branch"]
        or mr.get("target_branch") != prepared["target_branch"]
        or mr.get("sha") != prepared["commit_sha"]
    ):
        blockers.append("Migration merge request identity or content changed after preparation")
        return result
    state = mr.get("state")
    if (require_merged and state != "merged") or state not in ("opened", "merged"):
        blockers.append(
            "Migration merge request has not been reviewed and merged"
            if require_merged
            else "Migration merge request is no longer open or merged"
        )
        return result
    try:
        branch = destination.get(
            path + "/repository/branches/" + quote(prepared["source_branch"], safe="")
        )
    except Exception as exc:
        if getattr(exc, "status_code", None) != 404:
            raise
        if state != "merged":
            blockers.append("Prepared migration source branch is missing")
            return result
    else:
        if (branch.get("commit") or {}).get("id") != prepared["commit_sha"]:
            blockers.append("Prepared migration source branch changed after review")
            return result
    if state == "merged":
        # A merge commit includes the target tree; the squash commit alone may not.
        merged_sha = (
            mr.get("merge_commit_sha") or mr.get("squash_commit_sha") or prepared["commit_sha"]
        )
        comparison = destination.get(
            path + "/repository/compare",
            params={"from": prepared["commit_sha"], "to": merged_sha, "straight": True},
        )
        if (
            comparison.get("compare_timeout")
            or comparison.get("overflow")
            or not isinstance(comparison.get("diffs"), list)
            or comparison["diffs"]
        ):
            blockers.append(
                "Merged migration tree differs from the prepared CI changes or could not be verified"
            )
            return result
        target = destination.get(
            path + "/repository/branches/" + quote(prepared["target_branch"], safe="")
        )
        if (target.get("commit") or {}).get("id") != merged_sha:
            blockers.append("Destination target branch moved after the reviewed migration merge")
            return result
        result["merged_sha"] = merged_sha
        lint_ref = prepared["target_branch"]
    else:
        lint_ref = prepared["source_branch"]
    if not ci_path or "$" in ci_path or "@" in ci_path or "://" in ci_path:
        blockers.append("External or dynamic CI configuration requires manual review")
        return result
    content = _read_file(destination, path, ci_path.lstrip("/"), lint_ref)
    jobs, lint_blockers = _lint_jobs(destination, path, content, lint_ref, [], requires_merged=True)
    result["jobs"] = jobs
    blockers.extend(lint_blockers)
    return result


def verify_runner_configuration(
    destination: Any,
    project_id: Any,
    prepared: dict[str, Any],
    ci_path: str = ".gitlab-ci.yml",
) -> dict[str, Any]:
    """Freshly resolve CI jobs for unchanged configurations at their pinned baseline."""
    if prepared.get("iid"):
        return verify_prepared_merge_request(destination, project_id, prepared, ci_path)
    result: dict[str, Any] = {"merged_sha": None, "jobs": [], "blockers": []}
    blockers = result["blockers"]
    if (
        prepared.get("changed")
        or prepared.get("blockers")
        or prepared.get("status") not in ("unchanged", "no_ci")
    ):
        blockers.append("Prepared runner configuration did not complete successfully")
        return result
    if not prepared.get("target_branch") or not prepared.get("source_sha"):
        blockers.append("Prepared runner configuration baseline is incomplete")
        return result
    blockers.extend(_ci_setting_blockers(destination, project_id, ci_path))
    if blockers:
        return result
    path = _p(project_id)
    branch = destination.get(
        path + "/repository/branches/" + quote(prepared["target_branch"], safe="")
    )
    if (branch.get("commit") or {}).get("id") != prepared["source_sha"]:
        blockers.append("Destination CI branch changed after preparation")
        return result
    try:
        content = _read_file(destination, path, ci_path.lstrip("/"), prepared["target_branch"])
    except Exception as exc:
        if getattr(exc, "status_code", None) != 404:
            raise
        if prepared["status"] != "no_ci":
            blockers.append("Prepared CI configuration is missing")
        elif (
            ci_path != ".gitlab-ci.yml"
            or destination.project(project_id).get("ci_config_path")
            or destination.project(project_id).get("auto_devops_enabled") is not False
        ):
            blockers.append(
                "No-CI evidence requires an absent default configuration, no custom path, and explicitly disabled Auto DevOps"
            )
        return result
    if prepared["status"] == "no_ci":
        blockers.append("CI configuration appeared after preparation")
        return result
    jobs, lint_blockers = _lint_jobs(
        destination, path, content, prepared["target_branch"], [], requires_merged=True
    )
    result["jobs"] = jobs
    blockers.extend(lint_blockers)
    return result


def _ci_setting_blockers(destination: Any, project_id: Any, ci_path: str) -> list[str]:
    if not ci_path or "$" in ci_path or "@" in ci_path or "://" in ci_path:
        return ["External or dynamic CI configuration requires manual review"]
    configured = destination.project(project_id).get("ci_config_path") or ".gitlab-ci.yml"
    if configured.lstrip("/") != ci_path.lstrip("/"):
        return ["Destination project CI configuration path changed after preparation"]
    return []


def verify_runner_access(
    destination: Any,
    project_id: Any,
    allowed_runner_ids: Any,
) -> dict[str, Any]:
    """Reject every executable runner outside the allowlist, regardless of current tags.

    Offline runners remain a potential execution path when they reconnect. Paused
    runners cannot execute jobs and are excluded; this call never changes a runner.
    """
    allowed = {str(identifier) for identifier in allowed_runner_ids}
    result: dict[str, Any] = {
        "blockers": [],
        "unpaused_runner_ids": [],
        "unapproved_runner_ids": [],
    }
    for summary in destination.list(_p(project_id) + "/runners"):
        identifier = summary.get("id")
        if identifier is None:
            result["blockers"].append("Accessible project runner identity could not be verified")
            continue
        try:
            runner = destination.get(f"/runners/{identifier}")
        except Exception as exc:
            if getattr(exc, "status_code", None) in (403, 404):
                result["blockers"].append("Accessible project runner details could not be verified")
                continue
            raise
        if str(runner.get("id")) != str(identifier):
            result["blockers"].append(
                "Accessible project runner identity changed during verification"
            )
            continue
        if runner.get("paused") is True:
            continue
        result["unpaused_runner_ids"].append(identifier)
        if str(identifier) not in allowed:
            result["unapproved_runner_ids"].append(identifier)
    if result["unapproved_runner_ids"]:
        result["blockers"].append(
            "Project has accessible unpaused runners outside the approved allowlist"
        )
    result["blockers"] = sorted(set(result["blockers"]))
    return result
