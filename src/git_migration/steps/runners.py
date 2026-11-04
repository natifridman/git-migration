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
        blockers.append(
            "No approved runner can execute this job configuration "
            f"(required tags: {list(tags)}, protected ref: {bool(protected_ref)}); "
            "check job tag mapping, runner availability, paused state and protected access"
        )
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


def transform_ci_yaml(
    content: Any,
    tag_map: Any,
    project_include_map: Any = None,
    omit_project_includes: Any = None,
) -> Any:
    """Edit explicit tags/includes, preserving round-trip anchors and custom tags."""
    parser = _yaml()
    try:
        doc = parser.load(content)
    except Exception:
        return {"content": content, "changed": False, "blockers": ["CI YAML could not be parsed"]}
    if not isinstance(doc, MutableMapping):
        return {
            "content": content,
            "changed": False,
            "blockers": ["CI configuration must be a mapping"],
        }
    blockers, changed, seen = [], False, set()
    nodes: list[Mapping[Any, Any]] = [doc]
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
    retained = []
    omitted = set()
    for inc in includes:
        if isinstance(inc, MutableMapping) and "project" in inc:
            project = inc["project"]
            if isinstance(project, str) and project in (omit_project_includes or []):
                omitted.add(project)
                changed = True
                continue
            if isinstance(project, str) and project in (project_include_map or {}):
                if inc["project"] != project_include_map[project]:
                    inc["project"] = project_include_map[project]
                    changed = True
            else:
                blockers.append("Project CI include requires an explicit destination mapping")
        elif not (isinstance(inc, Mapping) and "local" in inc) and not (
            isinstance(inc, str) and inc.startswith("/")
        ):
            blockers.append("External, component or dynamic CI include requires manual review")
        retained.append(inc)
    if omitted:
        if retained:
            # Keep sequence comments and the original scalar/mapping shape when possible.
            if isinstance(doc["include"], list):
                doc["include"][:] = retained
            else:
                doc["include"] = retained[0]
        else:
            del doc["include"]
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


def _deep_merge(base: Mapping[Any, Any], override: Mapping[Any, Any]) -> dict[Any, Any]:
    """GitLab merges mappings recursively; later scalars and arrays replace earlier ones."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(result.get(key), Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _resolved_jobs(doc: Any) -> Any:
    """Resolve extends before applying defaults, retaining absent and explicit empty tags."""
    if not isinstance(doc, Mapping):
        return {}, ["CI configuration must be a mapping"]
    jobs = {k: v for k, v in doc.items() if k not in _RESERVED and isinstance(v, Mapping)}
    blockers: list[str] = []
    resolved: dict[str, Any] = {}

    def resolve(name: Any, stack: Any = ()) -> Any:
        if not isinstance(name, str) or name in stack or name not in jobs or len(stack) > 11:
            blockers.append("CI extends dependency could not be resolved locally")
            return {}
        if name in resolved:
            return resolved[name]
        job = jobs[name]
        inherited: dict[Any, Any] = {}
        parents = job.get("extends", [])
        parents = [parents] if isinstance(parents, str) else parents
        if not isinstance(parents, list) or str(getattr(parents, "tag", "")) == "!reference":
            blockers.append("Dynamic CI extends requires manual review")
            parents = []
        for parent in parents:
            inherited = _deep_merge(inherited, resolve(parent, (*stack, name)))
        inherited = _deep_merge(inherited, job)
        resolved[name] = inherited
        return inherited

    for name in jobs:
        if not isinstance(name, str):
            blockers.append("CI job names must be literal strings")
            continue
        if not name.startswith("."):
            resolve(name)
    return {name: resolved[name] for name in jobs if name in resolved}, blockers


def _inherits_default_tags(job: Mapping[Any, Any]) -> tuple[bool, list[str]]:
    inherit = job.get("inherit", {})
    if inherit is False:
        return False, []
    if not isinstance(inherit, Mapping) or str(getattr(inherit, "tag", "")) == "!reference":
        return False, ["CI default inheritance could not be resolved"]
    default = inherit.get("default", True)
    if isinstance(default, bool):
        return default, []
    if (
        isinstance(default, list)
        and str(getattr(default, "tag", "")) != "!reference"
        and all(isinstance(key, str) for key in default)
    ):
        return "tags" in default, []
    return False, ["CI default inheritance could not be resolved"]


def _job_tags(doc: Any) -> Any:
    """Resolve local extends/default/inherit semantics; return conservative blockers."""
    jobs, blockers = _resolved_jobs(doc)
    entries = []
    defaults = doc.get("default", {}) if isinstance(doc, Mapping) else {}
    if not isinstance(defaults, Mapping):
        blockers.append("CI defaults must be a mapping")
        defaults = {}
    for name, job in jobs.items():
        if name.startswith("."):
            continue
        if "trigger" in job and "script" not in job:
            blockers.append("Downstream pipeline dependency requires manual review")
            continue
        use_default, inherit_blockers = _inherits_default_tags(job)
        blockers.extend(inherit_blockers)
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


def _default_tag_jobs(doc: Any) -> tuple[list[str], list[str]]:
    jobs, blockers = _resolved_jobs(doc)
    if isinstance(doc, Mapping) and any(
        key not in _RESERVED and not isinstance(value, Mapping) for key, value in doc.items()
    ):
        blockers.append("CI job configuration could not be resolved")
    affected = []
    for name, job in jobs.items():
        if name.startswith("."):
            continue
        inherits, inherit_blockers = _inherits_default_tags(job)
        blockers.extend(inherit_blockers)
        if "trigger" in job:
            blockers.append("Downstream pipeline dependency requires manual review")
        elif ("script" in job or "run" in job) and "tags" not in job and inherits:
            affected.append(name)
    return sorted(affected), blockers


def _ci_fingerprint(doc: Mapping[Any, Any]) -> str:
    def normalize(value: Any) -> Any:
        tag = getattr(getattr(value, "tag", None), "value", None) or ""
        if isinstance(value, Mapping):
            return {
                "mapping": sorted(
                    [(str(key), normalize(item)) for key, item in value.items()],
                    key=lambda entry: entry[0],
                ),
                "tag": tag,
            }
        if isinstance(value, list):
            return {"sequence": [normalize(item) for item in value], "tag": tag}
        if value is None or isinstance(value, (bool, str, int, float)):
            return value
        return {"yaml_tag": str(getattr(value, "tag", "")), "value": str(value)}

    return hashlib.sha256(json.dumps(normalize(doc), sort_keys=True).encode()).hexdigest()


def _insert_default_tags(doc: MutableMapping[Any, Any], tags: list[str]) -> str:
    from ruamel.yaml.comments import CommentedMap
    from ruamel.yaml.mergevalue import MergeValue

    defaults = doc.get("default")
    if defaults is None:
        defaults = CommentedMap()
        doc["default"] = defaults
    elif getattr(getattr(defaults, "anchor", None), "value", None):
        # Extending an alias in place would also tag every job sharing that anchor.
        previous = defaults
        defaults = CommentedMap()
        merge = MergeValue()
        merge.append(previous)
        merge.merge_pos = 0
        defaults.add_yaml_merge(merge)
        doc["default"] = defaults
    defaults["tags"] = list(tags)
    output = io.StringIO()
    _yaml().dump(doc, output)
    return output.getvalue()


def _verify_branch(
    destination: Any,
    path: Any,
    source_sha: Any,
    branch: Any,
    actions: Any,
    ci_fingerprint: str | None = None,
) -> Any:
    commit = destination.get(path + "/repository/branches/" + quote(branch, safe=""))["commit"]
    commit_sha = commit["id"]
    if (
        ci_fingerprint
        and f"Migration CI evidence: {ci_fingerprint}"
        not in str(commit.get("message", "")).splitlines()
    ):
        raise RunnerBlocked(
            "Prepared default runner tags no longer match saved merged CI evidence; "
            "reconcile the existing migration branch"
        )
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


def _lint_configuration(
    destination: Any,
    path: Any,
    content: Any,
    ref: Any,
    jobs: Any,
    requires_merged: bool = False,
    ci_fingerprint: str | None = None,
) -> Any:
    lint = destination.post(
        path + "/ci/lint",
        {"content": content, "ref": ref, "dry_run": False, "include_merged_yaml": True},
    )
    if not lint.get("valid", False):
        return jobs, ["Destination CI Lint rejected the migration configuration"], None
    if requires_merged and not lint.get("merged_yaml"):
        return (
            jobs,
            ["Destination CI Lint did not return merged external include configuration"],
            None,
        )
    if lint.get("merged_yaml"):
        try:
            merged = _yaml().load(lint["merged_yaml"])
            if not isinstance(merged, Mapping):
                raise RunnerBlocked("Merged destination CI configuration is unreadable")
        except Exception:
            raise RunnerBlocked("Merged destination CI configuration is unreadable") from None
        jobs, blockers = _job_tags(merged)
        if ci_fingerprint and _ci_fingerprint(merged) != ci_fingerprint:
            blockers.append(
                "Merged CI configuration changed after default runner tags were prepared; "
                "reconcile the external includes"
            )
        return jobs, blockers, merged
    return jobs, [], None


def _lint_jobs(
    destination: Any,
    path: Any,
    content: Any,
    ref: Any,
    jobs: Any,
    requires_merged: bool = False,
    ci_fingerprint: str | None = None,
) -> Any:
    jobs, blockers, _ = _lint_configuration(
        destination, path, content, ref, jobs, requires_merged, ci_fingerprint
    )
    return jobs, blockers


def prepare_runner_merge_request(
    destination: Any,
    project_id: Any,
    ref: Any,
    tag_map: Any,
    ci_path: Any = ".gitlab-ci.yml",
    project_include_map: Any = None,
    omit_project_includes: Any = None,
    *,
    default_tags: Any = None,
) -> Any:
    """Prepare a deterministic branch/MR with builds disabled. Never run or merge source CI."""
    enabled_defaults = bool(default_tags)
    if enabled_defaults and (
        not isinstance(default_tags, list | tuple)
        or any(not isinstance(tag, str) or not tag.strip() or "$" in tag for tag in default_tags)
    ):
        raise RunnerBlocked("Default runner tags must be a literal tag list")
    default_evidence: dict[str, Any] = {
        "inserted": False,
        "reason": "Default runner tags could not be resolved safely",
        "affected_jobs": [],
    }
    preparation = {"default_tags": default_evidence} if enabled_defaults else {}
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
            default_evidence["reason"] = "Project has no CI configuration"
            return {
                "status": "no_ci",
                "source_sha": source_sha,
                "jobs": [],
                "blockers": [],
                "changed": False,
                **preparation,
            }
        raise
    originals, edited, documents, blockers = {}, {}, {}, []
    visiting = set()
    project_includes: list[Any] = []
    local_includes: dict[str, list[str]] = {}

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
        result = transform_ci_yaml(content, tag_map, project_include_map, omit_project_includes)
        originals[name], edited[name] = content, result["content"]
        blockers.extend(result["blockers"])
        try:
            doc = _yaml().load(result["content"])
        except Exception:
            blockers.append("CI YAML could not be parsed")
            visiting.remove(name)
            return
        documents[name] = doc
        local_includes[name] = []
        includes = doc.get("include", []) if isinstance(doc, Mapping) else []
        includes = includes if isinstance(includes, list) else [includes]
        for inc in includes:
            if (
                enabled_defaults
                and isinstance(inc, Mapping)
                and "local" in inc
                and (not isinstance(inc["local"], str) or not inc["local"].strip("/"))
            ):
                blockers.append("Dynamic local CI include requires manual review")
                continue
            if isinstance(inc, Mapping) and "project" in inc:
                project_includes.append(inc)
                if enabled_defaults:
                    files = inc.get("file")
                    files = files if isinstance(files, list) else [files]
                    literals = [inc.get("project"), *files]
                    if "ref" in inc:
                        literals.append(inc["ref"])
                    if (
                        set(inc) - {"project", "file", "ref"}
                        or not files
                        or any(
                            not isinstance(value, str) or not value or "$" in value
                            for value in literals
                        )
                    ):
                        blockers.append(
                            "Conditional, parameterized or dynamic project CI includes prevent "
                            "safe default runner tag resolution"
                        )
            local = (
                inc.get("local")
                if isinstance(inc, Mapping)
                else inc
                if isinstance(inc, str) and inc.startswith("/")
                else None
            )
            if local:
                if not isinstance(local, str):
                    blockers.append("Dynamic local CI include requires manual review")
                    continue
                if enabled_defaults and isinstance(inc, Mapping) and set(inc) != {"local"}:
                    blockers.append(
                        "Conditional or parameterized local CI includes prevent safe "
                        "default runner tag resolution"
                    )
                local_includes[name].append(local.lstrip("/"))
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

    def combine(name: str, stack: tuple[str, ...] = ()) -> dict[Any, Any]:
        if name in stack or name not in documents:
            return {}
        combined: dict[Any, Any] = {}
        for local in local_includes.get(name, []):
            combined = _deep_merge(combined, combine(local, (*stack, name)))
        doc = documents[name]
        return _deep_merge(combined, doc) if isinstance(doc, Mapping) else combined

    root_name = ci_path.lstrip("/")
    combined = combine(root_name)
    jobs, job_blockers = _job_tags(combined)
    if not project_includes:
        blockers.extend(job_blockers)
    default_branch: str | None = None
    ci_fingerprint: str | None = None
    if enabled_defaults:
        # Inference must not change recovery identity when an external include drifts.
        identity = json.dumps(
            {
                "ref": ref,
                "source_sha": source_sha,
                "files": edited,
                "default_tags": list(default_tags),
            },
            sort_keys=True,
        ).encode()
        default_branch = "codex/runner-migration-" + hashlib.sha256(identity).hexdigest()[:16]
        if project_includes and not blockers:
            if any(edited[name] != originals[name] for name in edited if name != root_name):
                blockers.append(
                    "Default runner tags cannot be resolved safely with project includes "
                    "and edited local CI files; prepare these CI changes manually"
                )
            else:
                jobs, lint_blockers, merged = _lint_configuration(
                    destination, path, edited[root_name], source_sha, jobs, True
                )
                blockers.extend(lint_blockers)
                if merged is not None:
                    combined = merged
                    if merged.get("include"):
                        blockers.append(
                            "Merged CI includes remain unresolved for default runner tags"
                        )
        candidates, candidate_blockers = _default_tag_jobs(combined)
        blockers.extend(candidate_blockers)
        # An explicit empty default tag list is still a declaration and must survive.
        defaults = combined.get("default", {})
        if not isinstance(defaults, Mapping):
            blockers.append("CI defaults must be a mapping")
        elif "tags" in defaults:
            tags = defaults["tags"]
            if (
                not isinstance(tags, list)
                or str(getattr(tags, "tag", "")) == "!reference"
                or any(not isinstance(tag, str) or "$" in tag for tag in tags)
            ):
                blockers.append("Default runner tags cannot be resolved to literal tags")
            default_evidence["reason"] = "Effective CI configuration already declares default.tags"
        elif not candidates:
            default_evidence["reason"] = "No executable jobs inherit an absent default.tags"
        elif not blockers:
            edited[root_name] = _insert_default_tags(documents[root_name], list(default_tags))
            combined = _deep_merge(combined, {"default": {"tags": list(default_tags)}})
            jobs, job_blockers = _job_tags(combined)
            blockers.extend(job_blockers)
            default_evidence.update(
                inserted=True,
                reason="Added default.tags for executable jobs without inherited or explicit tags",
                affected_jobs=candidates,
            )
        if not blockers and project_includes:
            ci_fingerprint = _ci_fingerprint(combined)
            default_evidence["merged_ci_sha256"] = ci_fingerprint
    if blockers:
        return {
            "status": "blocked",
            "changed": False,
            "source_sha": source_sha,
            "blockers": sorted(set(blockers)),
            "jobs": jobs,
            **preparation,
        }
    actions = [
        {"action": "update", "file_path": name, "content": edited[name]}
        for name in edited
        if edited[name] != originals[name]
    ]
    if not actions:
        if default_branch:
            previous_mrs = destination.list(
                path + "/merge_requests",
                params={
                    "scope": "all",
                    "source_branch": default_branch,
                    "target_branch": ref,
                    "state": "all",
                },
            )
            try:
                destination.get(path + "/repository/branches/" + quote(default_branch, safe=""))
            except Exception as error:
                if getattr(error, "status_code", None) != 404:
                    raise
                if previous_mrs:
                    raise RunnerBlocked(
                        "Previous default runner tag preparation requires reconciliation"
                    ) from None
            else:
                raise RunnerBlocked(
                    "Previous default runner tag preparation no longer matches resolved CI; "
                    "reconcile the existing migration branch"
                )
        jobs, lint_blockers = _lint_jobs(
            destination,
            path,
            edited[root_name],
            source_sha,
            jobs,
            bool(project_includes),
            ci_fingerprint,
        )
        return {
            "status": "blocked" if lint_blockers else "unchanged",
            "changed": False,
            "source_sha": source_sha,
            "jobs": jobs,
            "blockers": lint_blockers,
            **preparation,
        }
    identity = json.dumps({"ref": ref, "actions": actions}, sort_keys=True).encode()
    branch = default_branch or "codex/runner-migration-" + hashlib.sha256(identity).hexdigest()[:16]
    mrs = destination.list(
        path + "/merge_requests",
        params={"scope": "all", "source_branch": branch, "target_branch": ref, "state": "all"},
    )
    if mrs:
        mr = mrs[0]
        for action in actions:
            if _read_file(destination, path, action["file_path"], branch) != action["content"]:
                raise RunnerBlocked("Migration merge request branch contains conflicting edits")
        commit_sha = _verify_branch(destination, path, source_sha, branch, actions, ci_fingerprint)
        jobs, lint_blockers = _lint_jobs(
            destination,
            path,
            edited[root_name],
            commit_sha,
            jobs,
            bool(project_includes),
            ci_fingerprint,
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
            **preparation,
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
                "commit_message": "Prepare destination CI configuration [skip ci]"
                + (f"\n\nMigration CI evidence: {ci_fingerprint}" if ci_fingerprint else ""),
                "actions": branch_actions,
            },
        )
    commit_sha = _verify_branch(destination, path, source_sha, branch, actions, ci_fingerprint)
    jobs, lint_blockers = _lint_jobs(
        destination,
        path,
        edited[root_name],
        commit_sha,
        jobs,
        bool(project_includes),
        ci_fingerprint,
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
            **preparation,
        }
    mr = destination.post(
        path + "/merge_requests",
        {
            "source_branch": branch,
            "target_branch": ref,
            "title": "Draft: Prepare destination CI configuration",
            "description": "Review runner tags, requested default tags, include edits and explicitly requested include omissions before enabling CI. Merge only after explicit approval.",
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
        **preparation,
    }


def _amendment_tags(tags: Any) -> list[str]:
    if (
        not isinstance(tags, list)
        or not tags
        or len(tags) > 49
        or any(
            not isinstance(tag, str)
            or not tag.strip()
            or "$" in tag
            or any(char in tag for char in ("\n", "\r", "\x00"))
            for tag in tags
        )
        or len(tags) != len(set(tags))
    ):
        raise RunnerBlocked("Runner MR amendment requires a nonempty literal default tag list")
    return list(tags)


def _amendment_ci_path(ci_path: Any) -> str:
    if (
        not isinstance(ci_path, str)
        or not ci_path.lstrip("/")
        or any(char in ci_path for char in ("$", "*", "?", "@", "\n", "\r", "\x00"))
        or "://" in ci_path
        or any(part in {".", "..", ""} for part in ci_path.lstrip("/").split("/"))
    ):
        raise RunnerBlocked("Runner MR amendment requires a literal repository CI path")
    return ci_path.lstrip("/")


def _amendment_prepared(prepared: Any) -> dict[str, Any]:
    """Accept only the existing runner request's durable evidence, never raw configuration."""
    allowed = {
        "status",
        "commit_sha",
        "branch",
        "source_branch",
        "target_branch",
        "source_sha",
        "changed",
        "iid",
        "web_url",
        "jobs",
        "blockers",
        "default_tags",
        "omit_ci_project_includes",
    }
    if not isinstance(prepared, dict) or set(prepared) - allowed:
        raise RunnerBlocked("Saved runner MR amendment evidence is unsupported or unreadable")
    if (
        type(prepared.get("iid")) is not int
        or prepared["iid"] <= 0
        or prepared.get("changed") is not True
        or prepared.get("blockers", []) != []
        or prepared.get("status") not in {"created", "existing", "amended"}
        or any(
            not isinstance(prepared.get(key), str)
            or not re.fullmatch(r"[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?", prepared[key])
            for key in ("commit_sha", "source_sha")
        )
        or any(
            not isinstance(prepared.get(key), str) or not prepared[key] or "\n" in prepared[key]
            for key in ("source_branch", "target_branch")
        )
        or not prepared["source_branch"].startswith("codex/runner-migration-")
        or prepared.get("branch", prepared["source_branch"]) != prepared["source_branch"]
        or prepared["source_branch"] == prepared["target_branch"]
    ):
        raise RunnerBlocked("Saved runner MR amendment identity is incomplete or blocked")
    metadata = prepared.get("default_tags", {})
    if (
        not isinstance(metadata, Mapping)
        or set(metadata) - {"inserted", "reason", "affected_jobs", "merged_ci_sha256"}
        or "inserted" in metadata
        and not isinstance(metadata["inserted"], bool)
        or "reason" in metadata
        and (not isinstance(metadata["reason"], str) or "\n" in metadata["reason"])
        or "affected_jobs" in metadata
        and (
            not isinstance(metadata["affected_jobs"], list)
            or any(not isinstance(name, str) or "\n" in name for name in metadata["affected_jobs"])
        )
    ):
        raise RunnerBlocked("Saved default runner tag amendment evidence is unreadable")
    _, blockers = _saved_ci_fingerprint(prepared)
    if blockers:
        raise RunnerBlocked("; ".join(blockers))
    jobs = prepared.get("jobs", [])
    if not isinstance(jobs, list) or any(
        not isinstance(job, dict)
        or set(job) != {"job", "tags"}
        or not isinstance(job["job"], str)
        or "\n" in job["job"]
        or not isinstance(job["tags"], list)
        or any(not isinstance(tag, str) or "\n" in tag for tag in job["tags"])
        for job in jobs
    ):
        raise RunnerBlocked("Saved runner job amendment evidence is unreadable")
    omissions = prepared.get("omit_ci_project_includes", [])
    if not isinstance(omissions, list) or any(
        not isinstance(item, str) or "\n" in item for item in omissions
    ):
        raise RunnerBlocked("Saved CI include omission evidence is unreadable")
    if "web_url" in prepared and (
        not isinstance(prepared["web_url"], str | type(None))
        or isinstance(prepared["web_url"], str)
        and "\n" in prepared["web_url"]
    ):
        raise RunnerBlocked("Saved runner MR URL evidence is unreadable")
    return json.loads(json.dumps(prepared))


def _amendment_identity(
    destination: Any, project_id: int, prepared: Mapping[str, Any], ci_path: str
) -> tuple[dict[str, Any], str]:
    project = destination.project(project_id)
    if project.get("id") != project_id or project.get("builds_access_level") != "disabled":
        raise RunnerBlocked("Runner MR amendment requires the recorded project with CI disabled")
    blockers = _ci_setting_blockers(destination, project_id, ci_path)
    if blockers:
        raise RunnerBlocked("; ".join(blockers))
    path = _p(project_id)
    mr = destination.get(f"{path}/merge_requests/{prepared['iid']}")
    if (
        mr.get("iid") != prepared["iid"]
        or mr.get("state") != "opened"
        or mr.get("source_project_id") != project_id
        or mr.get("target_project_id") != project_id
        or mr.get("source_branch") != prepared["source_branch"]
        or mr.get("target_branch") != prepared["target_branch"]
    ):
        raise RunnerBlocked("Runner MR amendment requires the same owned, open, same-project MR")
    target = destination.get(
        f"{path}/repository/branches/{quote(prepared['target_branch'], safe='')}"
    )
    if (target.get("commit") or {}).get("id") != prepared["source_sha"]:
        raise RunnerBlocked("Runner MR amendment target branch changed from the saved baseline")
    source = destination.get(
        f"{path}/repository/branches/{quote(prepared['source_branch'], safe='')}"
    )
    head = (source.get("commit") or {}).get("id")
    if not isinstance(head, str) or mr.get("sha") != head:
        raise RunnerBlocked("Runner MR amendment source branch and MR head could not be verified")
    return mr, head


def _amendment_file(destination: Any, path: str, name: str, ref: str) -> tuple[str, str]:
    result = destination.get(f"{path}/repository/files/{quote(name, safe='')}", params={"ref": ref})
    last_commit = result.get("last_commit_id")
    if (
        result.get("encoding") != "base64"
        or result.get("commit_id") != ref
        or not isinstance(last_commit, str)
        or not re.fullmatch(r"[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?", last_commit)
    ):
        raise RunnerBlocked("Immutable CI file commit evidence is unavailable for MR amendment")
    try:
        content = base64.b64decode(result["content"], validate=True).decode("utf-8")
    except (ValueError, UnicodeError, KeyError):
        raise RunnerBlocked("Repository CI file is unreadable") from None
    return content, last_commit


def _amendment_delta(
    destination: Any, project_id: int, prepared: dict[str, Any], tags: list[str], ci_path: str
) -> dict[str, Any]:
    """Regenerate private content in memory from the immutable original MR commit."""
    path, ref = _p(project_id), prepared["commit_sha"]
    original, last_commit = _amendment_file(destination, path, ci_path, ref)
    documents: dict[str, Any] = {}
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if (
            not name
            or any(char in name for char in ("$", "*", "?", "@"))
            or "://" in name
            or ".." in name.split("/")
            or name in visiting
        ):
            raise RunnerBlocked("Unresolved or cyclic local CI include prevents MR amendment")
        if name in documents:
            return
        visiting.add(name)
        content = original if name == ci_path else _read_file(destination, path, name, ref)
        try:
            doc = _yaml().load(content)
        except Exception:
            raise RunnerBlocked("CI YAML could not be parsed for runner MR amendment") from None
        if not isinstance(doc, MutableMapping):
            raise RunnerBlocked("CI configuration must be a mapping for runner MR amendment")
        documents[name] = doc
        includes = doc.get("include", [])
        includes = includes if isinstance(includes, list) else [includes]
        identity_map = {}
        for inc in includes:
            if isinstance(inc, Mapping) and "project" in inc:
                files = inc.get("file")
                files = files if isinstance(files, list) else [files]
                values = [inc["project"], *files]
                if "ref" in inc:
                    values.append(inc["ref"])
                if (
                    set(inc) - {"project", "file", "ref"}
                    or not files
                    or any(
                        not isinstance(value, str) or not value or "$" in value for value in values
                    )
                ):
                    raise RunnerBlocked("Dynamic project CI includes prevent runner MR amendment")
                identity_map[inc["project"]] = inc["project"]
            elif isinstance(inc, Mapping) and set(inc) == {"local"}:
                if not isinstance(inc["local"], str):
                    raise RunnerBlocked("Dynamic local CI includes prevent runner MR amendment")
                visit(inc["local"].lstrip("/"))
            elif isinstance(inc, str) and inc.startswith("/"):
                visit(inc.lstrip("/"))
            else:
                raise RunnerBlocked(
                    "Unresolved or conditional CI includes prevent runner MR amendment"
                )
        checked = transform_ci_yaml(content, {}, identity_map)
        if checked["blockers"] or checked["changed"]:
            raise RunnerBlocked(
                "; ".join(checked["blockers"]) or "CI transformation was unexpected"
            )
        visiting.remove(name)

    visit(ci_path)
    previous_fingerprint, blockers = _saved_ci_fingerprint(prepared)
    if blockers:
        raise RunnerBlocked("; ".join(blockers))
    _, blockers, merged = _lint_configuration(
        destination, path, original, ref, [], True, previous_fingerprint
    )
    if blockers:
        raise RunnerBlocked("; ".join(blockers))
    if not isinstance(merged, Mapping) or merged.get("include"):
        raise RunnerBlocked("CI Lint did not resolve runner MR amendment configuration")
    candidates, blockers = _default_tag_jobs(merged)
    if blockers:
        raise RunnerBlocked("; ".join(sorted(set(blockers))))
    defaults = merged.get("default", {})
    if not isinstance(defaults, Mapping):
        raise RunnerBlocked("CI defaults must be a mapping for runner MR amendment")
    if "tags" in defaults:
        raise RunnerBlocked(
            "Runner MR amendment refused: effective CI already declares default.tags"
        )
    if not candidates:
        raise RunnerBlocked(
            "Runner MR amendment refused: no executable job would inherit default.tags"
        )
    content = _insert_default_tags(documents[ci_path], tags)
    expected = _deep_merge(merged, {"default": {"tags": tags}})
    fingerprint = _ci_fingerprint(expected)
    jobs, blockers, _ = _lint_configuration(destination, path, content, ref, [], True, fingerprint)
    if blockers:
        raise RunnerBlocked("; ".join(blockers))
    return {
        "content": content,
        "root_before_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "root_after_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "root_last_commit_id": last_commit,
        "default_tag_metadata": {
            "inserted": True,
            "reason": "Added default.tags to the existing runner MR for jobs without explicit or inherited tags",
            "affected_jobs": candidates,
            "merged_ci_sha256": fingerprint,
        },
        "jobs": sorted(jobs, key=lambda job: job["job"]),
    }


def _amendment_proposal(
    project_id: int,
    prepared: dict[str, Any],
    tags: list[str],
    ci_path: str,
    delta: dict[str, Any],
) -> dict[str, Any]:
    proposal = {
        "project_id": project_id,
        "prepared": prepared,
        "default_tags": tags,
        "ci_path": ci_path,
        **{key: value for key, value in delta.items() if key != "content"},
    }
    proposal["operation_id"] = hashlib.sha256(
        json.dumps(proposal, sort_keys=True).encode()
    ).hexdigest()
    return proposal


def plan_runner_default_amendment(
    destination: Any,
    project_id: int,
    prepared: dict[str, Any],
    default_tags: list[str],
    *,
    ci_path: str = ".gitlab-ci.yml",
) -> dict[str, Any]:
    """Read-only planning; return only hashes and durable MR/job evidence, never CI content."""
    prepared = _amendment_prepared(prepared)
    tags = _amendment_tags(default_tags)
    if type(project_id) is not int or project_id <= 0:
        raise RunnerBlocked("Runner MR amendment requires a recorded project and CI path")
    ci_path = _amendment_ci_path(ci_path)
    _, head = _amendment_identity(destination, project_id, prepared, ci_path)
    if head != prepared["commit_sha"]:
        raise RunnerBlocked("Runner MR amendment source head changed from the saved commit")
    delta = _amendment_delta(destination, project_id, prepared, tags, ci_path)
    _, head = _amendment_identity(destination, project_id, prepared, ci_path)
    if head != prepared["commit_sha"]:
        raise RunnerBlocked("Runner MR amendment source head changed during planning")
    return _amendment_proposal(project_id, prepared, tags, ci_path, delta)


def _amendment_message(operation_id: str) -> str:
    return (
        "Draft: Add destination default runner tags [skip ci]\n\n"
        f"Migration runner default amendment: {operation_id}"
    )


def _verify_amendment_commit(
    destination: Any, project_id: int, proposal: Mapping[str, Any], sha: str
) -> None:
    path = _p(project_id)
    commit = destination.get(f"{path}/repository/commits/{quote(sha, safe='')}")
    if (
        commit.get("id") != sha
        or commit.get("parent_ids") != [proposal["prepared"]["commit_sha"]]
        or str(commit.get("message", "")).rstrip("\n")
        != _amendment_message(proposal["operation_id"])
    ):
        raise RunnerBlocked("Runner MR amendment head is not attributable to the saved operation")
    comparison = destination.get(
        path + "/repository/compare",
        params={"from": proposal["prepared"]["commit_sha"], "to": sha, "straight": True},
    )
    diffs = comparison.get("diffs")
    if (
        comparison.get("compare_timeout")
        or comparison.get("overflow")
        or not isinstance(diffs, list)
        or len(diffs) != 1
        or diffs[0].get("new_path") != proposal["ci_path"]
        or diffs[0].get("old_path") != proposal["ci_path"]
        or diffs[0].get("a_mode") != diffs[0].get("b_mode")
        or any(
            diffs[0].get(key)
            for key in ("deleted_file", "renamed_file", "new_file", "too_large", "collapsed")
        )
    ):
        raise RunnerBlocked(
            "Runner MR amendment commit changes more than the expected root CI file"
        )
    content, last_commit = _amendment_file(destination, path, proposal["ci_path"], sha)
    if (
        last_commit != sha
        or hashlib.sha256(content.encode()).hexdigest() != proposal["root_after_sha256"]
    ):
        raise RunnerBlocked(
            "Runner MR amendment root CI content does not match the saved operation"
        )


def apply_runner_default_amendment(
    destination: Any, project_id: int, proposal: dict[str, Any]
) -> dict[str, Any]:
    """Apply or recover one journaled addition to an existing unmerged migration MR."""
    keys = {
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
    if (
        not isinstance(proposal, dict)
        or set(proposal) != keys
        or proposal.get("project_id") != project_id
    ):
        raise RunnerBlocked("Runner MR amendment proposal identity is unreadable")
    prepared = _amendment_prepared(proposal["prepared"])
    tags = _amendment_tags(proposal["default_tags"])
    ci_path = _amendment_ci_path(proposal["ci_path"])
    if (
        type(project_id) is not int
        or project_id <= 0
        or type(proposal["project_id"]) is not int
        or proposal["ci_path"] != ci_path
        or any(
            not isinstance(proposal.get(key), str)
            or not re.fullmatch(r"[a-f0-9]{64}", proposal[key])
            for key in ("operation_id", "root_before_sha256", "root_after_sha256")
        )
    ):
        raise RunnerBlocked("Runner MR amendment proposal identity is unreadable")
    mr, head = _amendment_identity(destination, project_id, prepared, ci_path)
    if head != prepared["commit_sha"]:
        _verify_amendment_commit(destination, project_id, proposal, head)
    delta = _amendment_delta(destination, project_id, prepared, tags, ci_path)
    expected = _amendment_proposal(project_id, prepared, tags, ci_path, delta)
    if proposal != expected:
        raise RunnerBlocked("Runner MR amendment no longer matches its saved planning evidence")
    mr, head = _amendment_identity(destination, project_id, prepared, ci_path)
    if head == prepared["commit_sha"]:
        if not mr.get("draft", mr.get("work_in_progress", False)):
            title = mr.get("title")
            if not isinstance(title, str) or not title:
                raise RunnerBlocked("Runner MR title is unavailable for Draft amendment")
            destination.put(
                f"{_p(project_id)}/merge_requests/{prepared['iid']}", {"title": "Draft: " + title}
            )
        mr, head = _amendment_identity(destination, project_id, prepared, ci_path)
        if (
            head != prepared["commit_sha"]
            or mr.get("draft", mr.get("work_in_progress")) is not True
        ):
            raise RunnerBlocked("Runner MR changed or could not be marked Draft before amendment")
        destination.post(
            _p(project_id) + "/repository/commits",
            {
                "branch": prepared["source_branch"],
                "commit_message": _amendment_message(proposal["operation_id"]),
                "actions": [
                    {
                        "action": "update",
                        "file_path": ci_path,
                        "content": delta["content"],
                        "last_commit_id": proposal["root_last_commit_id"],
                    }
                ],
            },
        )
        mr, head = _amendment_identity(destination, project_id, prepared, ci_path)
    if head == prepared["commit_sha"] or mr.get("draft", mr.get("work_in_progress")) is not True:
        raise RunnerBlocked("Runner MR amendment did not produce an attributable Draft MR head")
    _verify_amendment_commit(destination, project_id, proposal, head)
    jobs, blockers = _lint_jobs(
        destination,
        _p(project_id),
        delta["content"],
        head,
        [],
        True,
        proposal["default_tag_metadata"]["merged_ci_sha256"],
    )
    if blockers:
        raise RunnerBlocked("; ".join(blockers))
    final_mr, final_head = _amendment_identity(destination, project_id, prepared, ci_path)
    if final_head != head or final_mr.get("draft", final_mr.get("work_in_progress")) is not True:
        raise RunnerBlocked("Runner MR amendment head changed during final verification")
    return {
        **prepared,
        "status": "amended",
        "changed": True,
        "commit_sha": head,
        "jobs": sorted(jobs, key=lambda job: job["job"]),
        "blockers": [],
        "default_tags": proposal["default_tag_metadata"],
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


def _saved_ci_fingerprint(prepared: Mapping[str, Any]) -> tuple[str | None, list[str]]:
    metadata = prepared.get("default_tags", {})
    if not isinstance(metadata, Mapping):
        return None, ["Prepared default runner tag evidence is unreadable"]
    if "merged_ci_sha256" not in metadata:
        return None, []
    fingerprint = metadata["merged_ci_sha256"]
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        return None, ["Prepared default runner tag merged CI evidence is unreadable"]
    return fingerprint, []


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
    ci_fingerprint, evidence_blockers = _saved_ci_fingerprint(prepared)
    blockers.extend(evidence_blockers)
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
    jobs, lint_blockers = _lint_jobs(
        destination,
        path,
        content,
        lint_ref,
        [],
        requires_merged=True,
        ci_fingerprint=ci_fingerprint,
    )
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
    ci_fingerprint, evidence_blockers = _saved_ci_fingerprint(prepared)
    blockers.extend(evidence_blockers)
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
        destination,
        path,
        content,
        prepared["target_branch"],
        [],
        requires_merged=True,
        ci_fingerprint=ci_fingerprint,
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
