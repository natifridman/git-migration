"""Reconcile schedule configuration without running pipelines or taking ownership."""

from typing import Any
from urllib.parse import quote

from git_migration.secrets import SafeError


class ScheduleConflict(SafeError):
    pass


def _config(item: Any) -> Any:
    return {
        "description": item["description"],
        "cron": item["cron"],
        "cron_timezone": item.get("cron_timezone", "UTC"),
        "ref": item["ref"],
        "inputs": sorted(
            [{"name": i["name"], "value": i["value"]} for i in item.get("inputs", [])],
            key=lambda i: i["name"],
        ),
    }


def _variables(item: Any) -> Any:
    result = {}
    for v in item.get("variables", []):
        if not isinstance(v.get("value"), str):
            raise ScheduleConflict("Schedule variable value is unreadable")
        if v.get("raw", False):
            raise ScheduleConflict("Schedule variable raw behavior cannot be preserved by this API")
        result[v["key"]] = {
            "key": v["key"],
            "value": v["value"],
            "variable_type": v.get("variable_type", "env_var"),
        }
    return result


def reconcile_schedules(
    source: Any, destination: Any, source_id: Any, dest_id: Any, activate: Any = False
) -> Any:
    sp = f"/projects/{quote(str(source_id), safe='')}/pipeline_schedules"
    dp = f"/projects/{quote(str(dest_id), safe='')}/pipeline_schedules"
    wanted = [source.get(f"{sp}/{s['id']}") for s in source.list(sp)]
    current = [destination.get(f"{dp}/{s['id']}") for s in destination.list(dp)]
    results = []
    for item in wanted:
        config, variables = _config(item), _variables(item)
        matches = [
            s
            for s in current
            if s["description"] == item["description"] and s["ref"] == item["ref"]
        ]
        if len(matches) > 1:
            raise ScheduleConflict("Ambiguous destination schedules")
        if matches:
            target = matches[0]
            if _config(target) != config:
                raise ScheduleConflict("Destination schedule conflicts with source configuration")
            existing = _variables(target)
            if any(k in existing and existing[k] != v for k, v in variables.items()) or set(
                existing
            ) - set(variables):
                raise ScheduleConflict("Destination schedule variable configuration conflicts")
            if target.get("active", False) and (
                not activate or not item.get("active", False) or set(variables) - set(existing)
            ):
                destination.put(f"{dp}/{target['id']}", {"active": False})
            status = "identical"
        else:
            target = destination.post(dp, {**config, "active": False})
            existing, status = {}, "created"
            current.append({**config, **target, "variables": list(variables.values())})
        for key, variable in variables.items():
            if key not in existing:
                destination.post(f"{dp}/{target['id']}/variables", variable)
        if (
            activate
            and item.get("active", False)
            and (
                status == "created"
                or not target.get("active", False)
                or set(variables) - set(existing)
            )
        ):
            destination.put(f"{dp}/{target['id']}", {"active": True})
        results.append(
            {
                "source_id": item["id"],
                "destination_id": target["id"],
                "status": status,
                "active": bool(activate and item.get("active", False)),
                "destination_owner_id": target.get("owner", {}).get("id"),
                "owner_review_required": True,
                "owner_action": "Destination owner must review permissions and take ownership if required",
            }
        )
    return {"schedules": results}


def verify_schedules(
    source: Any,
    destination: Any,
    source_id: Any,
    dest_id: Any,
    mapping: dict[Any, Any],
) -> dict[str, Any]:
    """Read-only comparison of restored schedules; never return input or variable values."""
    sp = f"/projects/{quote(str(source_id), safe='')}/pipeline_schedules"
    dp = f"/projects/{quote(str(dest_id), safe='')}/pipeline_schedules"
    result: dict[str, Any] = {"blockers": [], "schedules": []}
    blockers = result["blockers"]
    source_summaries = source.list(sp)
    destination_summaries = destination.list(dp)
    normal_mapping = {str(key): value for key, value in mapping.items()}
    if len(set(normal_mapping.values())) != len(normal_mapping):
        blockers.append("Schedule mapping aliases multiple source schedules to one destination")
        return result
    if {str(item["id"]) for item in source_summaries} != set(normal_mapping):
        blockers.append(
            "Restored schedule mapping differs from the current source schedule inventory"
        )
    if {str(item["id"]) for item in destination_summaries} != {
        str(value) for value in normal_mapping.values()
    }:
        blockers.append("Destination schedule inventory differs from the restored schedule mapping")
    if any(item.get("active", False) for item in destination_summaries):
        blockers.append("Destination schedules must remain inactive until explicit activation")
    for summary in source_summaries:
        source_schedule_id = summary["id"]
        destination_schedule_id = normal_mapping.get(str(source_schedule_id))
        if destination_schedule_id is None:
            continue
        try:
            wanted = source.get(f"{sp}/{source_schedule_id}")
            actual = destination.get(f"{dp}/{destination_schedule_id}")
            if wanted.get("active", False):
                blockers.append("Source schedules must remain inactive during migration")
            if actual.get("active", False):
                blockers.append(
                    "Destination schedules must remain inactive until explicit activation"
                )
            if _config(wanted) != _config(actual) or _variables(wanted) != _variables(actual):
                blockers.append("Restored schedule configuration differs from its source")
            result["schedules"].append(
                {
                    "source_id": source_schedule_id,
                    "destination_id": destination_schedule_id,
                    "destination_owner_id": (actual.get("owner") or {}).get("id"),
                    "owner_review_required": True,
                }
            )
        except ScheduleConflict as exc:
            blockers.append(str(exc))
        except Exception as exc:
            if getattr(exc, "status_code", None) in (403, 404):
                blockers.append("Restored schedule is missing or inaccessible")
            else:
                raise
    result["blockers"] = sorted(set(blockers))
    return result
