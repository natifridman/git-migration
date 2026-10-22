"""Scope-preserving CI variable reconciliation. Secret material never leaves this call."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import quote

from git_migration.models import CIVariable, MigrationConfig, MigrationPlan
from git_migration.secrets import SafeError
from git_migration.state import atomic_json


class VariableConflict(SafeError):
    """A safe error which deliberately contains no API payload or secret."""


_DEFAULTS = {
    "environment_scope": "*",
    "variable_type": "env_var",
    "protected": False,
    "masked": False,
    "hidden": False,
    "raw": False,
    "description": None,
}


def _normalise(variable: Any, overrides: Any) -> Any:
    key = variable.get("key")
    if not isinstance(key, str) or not key:
        raise VariableConflict("Variable has an unreadable key")
    scope = variable.get("environment_scope", "*")
    result = {**_DEFAULTS, **{k: variable[k] for k in _DEFAULTS if k in variable}, "key": key}
    if not isinstance(scope, str) or not scope:
        raise VariableConflict("Variable has an unsupported environment scope")
    reference = overrides.get(f"{key}@{scope}", overrides.get((key, scope), overrides.get(key)))
    if reference is not None:
        if not isinstance(reference, str) or not reference:
            raise VariableConflict("Variable replacement must reference an environment variable")
        name = reference[4:] if reference.startswith("env:") else reference
        if name not in os.environ:
            raise VariableConflict("Variable replacement environment reference is unavailable")
        result["value"] = os.environ[name]
    else:
        result["value"] = variable.get("value")
    if not isinstance(result["value"], str):
        raise VariableConflict(
            "Variable value is unreadable; supply an explicit environment reference"
        )
    if result["variable_type"] not in ("env_var", "file"):
        raise VariableConflict("Unsupported variable type")
    if any(not isinstance(result[k], bool) for k in ("protected", "masked", "hidden", "raw")):
        raise VariableConflict("Unsupported variable attributes")
    if result["hidden"] and not result["masked"]:
        raise VariableConflict("Hidden variable must also be masked")
    if result["masked"] and not result["value"]:
        raise VariableConflict("Masked variable replacement is empty; supply its environment value")
    return result


def _reconcile_variables(
    source: Any,
    destination: Any,
    source_kind: Any,
    source_id: Any,
    dest_id: Any,
    overrides: Any = None,
    write: bool = True,
) -> Any:
    """Fetch afresh; create absent variables and fail closed on any existing mismatch.

    Overrides map KEY or KEY@scope to an environment variable name.
    The caller invokes this once per explicitly mapped group, never per child project.
    """
    if source_kind not in ("project", "group"):
        raise VariableConflict("Variable scope must be project or group")
    prefix = source_kind + "s"
    # Resolve the requested scope before touching variables. Never fall back from group to project.
    resolver = "group" if source_kind == "group" else "project"
    for provider, identifier in ((source, source_id), (destination, dest_id)):
        resolved = getattr(provider, resolver)(identifier)
        if str(resolved.get("id")) != str(identifier) and resolved.get(
            "full_path", resolved.get("path_with_namespace")
        ) != str(identifier):
            raise VariableConflict("Resolved variable scope does not match the requested scope")
    path = f"/{prefix}/{quote(str(dest_id), safe='')}/variables"
    source_path = f"/{prefix}/{quote(str(source_id), safe='')}/variables"
    wanted = [_normalise(v, overrides or {}) for v in source.list(source_path)]
    return _reconcile_wanted(destination, path, wanted, source_kind, write)


def _reconcile_wanted(
    destination: Any,
    path: str,
    wanted: list[dict],
    source_kind: str,
    write: bool,
    receipt_path: Path | None = None,
) -> dict:
    # GitLab may redact hidden values even from the API. A private receipt proves
    # what this run successfully submitted; it cannot detect later hidden-value
    # changes made on the server. Never adopt an unreceipted opaque variable.
    receipts = (
        json.loads(receipt_path.read_text()) if receipt_path and receipt_path.exists() else None
    )

    def receipt_identity(variable: dict) -> str:
        return json.dumps([variable["key"], variable["environment_scope"]])

    def digest(variable: dict) -> str:
        assert receipts is not None
        payload = json.dumps(
            [getattr(destination, "url", ""), path, variable], sort_keys=True
        ).encode()
        return hmac.new(bytes.fromhex(receipts["key"]), payload, hashlib.sha256).hexdigest()

    wanted_index = {(v["key"], v["environment_scope"]): v for v in wanted}
    current = []
    opaque: set[tuple[str, str]] = set()
    for item in destination.list(path):
        key = (item.get("key"), item.get("environment_scope", "*"))
        if item.get("hidden") and not isinstance(item.get("value"), str):
            expected = wanted_index.get(key)
            receipt = (
                receipts["variables"].get(receipt_identity(expected))
                if receipts and expected
                else None
            )
            if (
                not expected
                or not receipt
                or receipt.get("status") != "complete"
                or not hmac.compare_digest(receipt.get("digest", ""), digest(expected))
            ):
                raise VariableConflict(
                    "Hidden destination value cannot be read or matched to this run's creation receipt; reconcile it explicitly"
                )
            item = {**item, "value": expected["value"]}
            opaque.add(key)
        current.append(_normalise(item, {}))
    indexed = {(v["key"], v["environment_scope"]): v for v in current}
    if len(indexed) != len(current) or len(
        {(v["key"], v["environment_scope"]) for v in wanted}
    ) != len(wanted):
        raise VariableConflict("Duplicate variable key and environment scope")
    wanted_identities = {(v["key"], v["environment_scope"]) for v in wanted}
    if set(indexed) - wanted_identities:
        raise VariableConflict(
            "Destination contains variables outside the source key and environment scopes"
        )
    # Preflight every conflict before mutating the destination.
    for variable in wanted:
        existing = indexed.get((variable["key"], variable["environment_scope"]))
        if existing is not None and existing != variable:
            raise VariableConflict("Destination variable conflicts with source configuration")
    results = []
    for variable in wanted:
        identity = (variable["key"], variable["environment_scope"])
        status = "identical" if identity in indexed else "created"
        if status == "created" and not write:
            raise VariableConflict("Destination variable is missing")
        if status == "created":
            if variable["hidden"] and receipt_path:
                if receipts is None:
                    receipts = {"key": secrets.token_hex(32), "variables": {}}
                receipts["variables"][receipt_identity(variable)] = {
                    "digest": digest(variable),
                    "status": "pending",
                }
                atomic_json(receipt_path, receipts)
            payload = {k: v for k, v in variable.items() if k != "hidden"}
            if variable["hidden"]:
                payload["masked_and_hidden"] = True
            destination.post(path, payload)
            if variable["hidden"] and receipt_path:
                assert receipts is not None
                receipts["variables"][receipt_identity(variable)]["status"] = "complete"
                atomic_json(receipt_path, receipts)
        result = {
            "key": variable["key"],
            "environment_scope": variable["environment_scope"],
            "status": status,
        }
        if (variable["key"], variable["environment_scope"]) in opaque:
            result["verification"] = "creation_receipt"
        results.append(result)
    return {"scope": source_kind, "variables": results}


def explicit_values(definitions: list[CIVariable]) -> list[dict]:
    """Resolve only declared values; callers must never persist the returned list."""
    result = []
    for definition in definitions:
        data = definition.model_dump(exclude={"value_env"})
        result.append(_normalise(data, {definition.key: "env:" + definition.value_env}))
    return result


def reconcile_explicit_variables(
    destination: Any,
    project_id: int,
    definitions: list[CIVariable],
    *,
    write: bool = True,
    receipt_path: Path | None = None,
) -> dict:
    if destination.project(project_id).get("id") != project_id:
        raise VariableConflict("Destination project variable identity changed")
    return _reconcile_wanted(
        destination,
        f"/projects/{project_id}/variables",
        explicit_values(definitions),
        "project",
        write,
        receipt_path,
    )


def destination_inheritance(config: MigrationConfig, destination: Any) -> dict[str, list[dict]]:
    """Inventory metadata only, including every ancestor of the target namespaces."""
    paths = {
        "/".join(parts[:depth])
        for repo in config.repositories
        for parts in [repo.destination_path.split("/")[:-1]]
        for depth in range(1, len(parts) + 1)
    }
    result: dict[str, list[dict]] = {}
    fields = ("environment_scope", "variable_type", "protected", "masked", "hidden", "raw")
    for path in sorted(paths):
        try:
            group = destination.group(path)
        except Exception as error:
            if (
                getattr(error, "status_code", None) == 404
                and config.create_missing_groups
                and any(
                    path == root or path.startswith(root + "/")
                    for root in config.group_mapping.values()
                )
            ):
                result[path] = []
                continue
            raise VariableConflict(
                "Cannot inventory destination ancestor variables; check group access"
            ) from None
        values = destination.list(f"/groups/{group['id']}/variables")
        metadata = [
            {"key": v["key"], **{key: v.get(key, _DEFAULTS[key]) for key in fields}} for v in values
        ]
        result[path] = sorted(metadata, key=lambda v: (v["key"], v["environment_scope"]))
    return result


def verify_destination_inheritance(plan: MigrationPlan, destination: Any) -> None:
    current = destination_inheritance(plan.config, destination)
    if current != plan.destination_variable_inventory:
        raise VariableConflict(
            "Destination inherited variable inventory changed; review it and generate a fresh plan"
        )
    if any(current.values()) and not plan.config.variables.accept_destination_inheritance:
        raise VariableConflict("Destination inherited variables have not been explicitly accepted")


def reconcile_variables(
    source: Any,
    destination: Any,
    source_kind: Any,
    source_id: Any,
    dest_id: Any,
    overrides: Any = None,
) -> Any:
    """Reconcile mapped variables; values remain in memory only."""
    return _reconcile_variables(
        source, destination, source_kind, source_id, dest_id, overrides, write=True
    )


def verify_variables(
    source: Any,
    destination: Any,
    source_kind: Any,
    source_id: Any,
    dest_id: Any,
    overrides: Any = None,
) -> Any:
    """Read-only comparison of secret values and metadata, with the same safe errors."""
    return _reconcile_variables(
        source, destination, source_kind, source_id, dest_id, overrides, write=False
    )
