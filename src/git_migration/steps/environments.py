"""Environment definitions and explicitly mapped protection identities, never deployments."""

from typing import Any
from urllib.parse import quote

from git_migration.secrets import SafeError


class EnvironmentConflict(SafeError):
    pass


def _rules(rules: Any, user_map: Any, group_map: Any) -> Any:
    mapped = []
    for rule in rules:
        result = {}
        for field, mapping in (("user_id", user_map), ("group_id", group_map)):
            if rule.get(field) is not None:
                value = mapping.get(str(rule[field]), mapping.get(rule[field]))
                if value is None:
                    raise EnvironmentConflict(
                        "Environment protection identity requires an explicit destination mapping"
                    )
                result[field] = value
        if not result and rule.get("access_level") is not None:
            result["access_level"] = rule["access_level"]
        if not result:
            raise EnvironmentConflict("Unsupported environment protection access rule")
        result["group_inheritance_type"] = rule.get("group_inheritance_type", 0)
        if "required_approvals" in rule:
            result["required_approvals"] = rule["required_approvals"]
        mapped.append(result)
    return sorted(mapped, key=lambda r: str(sorted(r.items())))


def _protection(item: Any, user_map: Any, group_map: Any) -> Any:
    if item.get("required_approval_count", 0) and not item.get("approval_rules"):
        raise EnvironmentConflict(
            "Legacy environment approval configuration requires manual migration"
        )
    return {
        "name": item["name"],
        "deploy_access_levels": _rules(item.get("deploy_access_levels", []), user_map, group_map),
        "approval_rules": _rules(item.get("approval_rules", []), user_map, group_map),
    }


def reconcile_environments(
    source: Any,
    destination: Any,
    source_id: Any,
    dest_id: Any,
    user_map: Any = None,
    group_map: Any = None,
) -> Any:
    sp, dp = (f"/projects/{quote(str(i), safe='')}" for i in (source_id, dest_id))
    wanted = source.list(sp + "/environments")
    current = {e["name"]: e for e in destination.list(dp + "/environments")}
    # Protection preflight happens before creating any environment definition.
    try:
        protections = source.list(sp + "/protected_environments")
        existing_protections = destination.list(dp + "/protected_environments")
    except Exception as exc:
        if getattr(exc, "status_code", None) in (403, 404, 405):
            return {
                "status": "blocked",
                "blockers": ["Environment protections API unavailable or inaccessible"],
            }
        raise
    try:
        mapped = [_protection(p, user_map or {}, group_map or {}) for p in protections]
    except EnvironmentConflict as exc:
        return {"status": "blocked", "blockers": [str(exc)]}
    existing = {
        p["name"]: _protection(p, {}, {})
        for p in existing_protections
        if not any(
            r.get("user_id") or r.get("group_id")
            for r in p.get("deploy_access_levels", []) + p.get("approval_rules", [])
        )
    }
    # Normalize destination identities using identity maps, not source IDs.
    for p in existing_protections:
        ids = [r for r in p.get("deploy_access_levels", []) + p.get("approval_rules", [])]
        existing[p["name"]] = _protection(
            p,
            {r["user_id"]: r["user_id"] for r in ids if r.get("user_id")},
            {r["group_id"]: r["group_id"] for r in ids if r.get("group_id")},
        )
    for p in mapped:
        if p["name"] in existing and existing[p["name"]] != p:
            raise EnvironmentConflict("Destination environment protection conflicts")
    results = []
    for env in wanted:
        payload = {k: env[k] for k in ("name", "external_url", "tier") if k in env}
        old = current.get(env["name"])
        if old and any(old.get(k) != v for k, v in payload.items()):
            raise EnvironmentConflict("Destination environment configuration conflicts")
        if not old:
            destination.post(dp + "/environments", payload)
        results.append({"name": env["name"], "status": "identical" if old else "created"})
    for p in mapped:
        if p["name"] not in existing:
            destination.post(dp + "/protected_environments", p)
    return {
        "status": "configured",
        "environments": results,
        "manual_actions": ["Verify deployment infrastructure and permissions separately"],
    }
