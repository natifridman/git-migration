"""Load manifests without resolving or serializing secrets."""

from __future__ import annotations

import csv
from pathlib import Path

from ruamel.yaml import YAML

from git_migration.models import MigrationConfig, MigrationPlan


def load_config(path: Path) -> MigrationConfig:
    data = YAML(typ="safe").load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Manifest must be a YAML mapping")
    if data.get("repositories_csv"):
        if data.get("repositories"):
            raise ValueError("Choose repositories or repositories_csv, not both")
        csv_path = (path.parent / data["repositories_csv"]).resolve()
        with csv_path.open(newline="") as stream:
            reader = csv.DictReader(stream)
            if set(reader.fieldnames or []) != {"source_path", "destination_path"}:
                raise ValueError("CSV requires exactly source_path,destination_path columns")
            data["repositories"] = list(reader)
        data["repositories_csv"] = None
    config = MigrationConfig.model_validate(data)
    if not config.repositories:
        raise ValueError("Repository list must not be empty")
    return config


def load_plan(path: Path) -> MigrationPlan:
    plan = MigrationPlan.model_validate_json(path.read_text())
    if plan.config_hash != plan.config.fingerprint():
        raise ValueError("Plan configuration changed; generate a fresh plan")
    return plan
