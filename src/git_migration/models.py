"""Validated configuration and durable, secret-free plan models."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def project_path(value: str) -> str:
    value = value.strip().strip("/")
    if len(value.split("/")) < 2 or any(
        part in {"", ".", ".."} or not re.fullmatch(r"[\w.-]+", part) for part in value.split("/")
    ):
        raise ValueError("Use a namespace/project path, without a hostname or credentials")
    return value


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Endpoint(Model):
    url: str
    token_env: str
    ca_bundle: str | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Endpoint must be an HTTPS URL without credentials, query or fragment")
        return value.rstrip("/")

    @field_validator("token_env")
    @classmethod
    def validate_env(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("token_env must name an environment variable")
        return value


class CIVariable(Model):
    key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    environment_scope: str = Field(default="*", min_length=1)
    variable_type: Literal["env_var", "file"] = "env_var"
    protected: bool = False
    masked: bool = False
    hidden: bool = False
    raw: bool = True
    description: str | None = None

    @model_validator(mode="after")
    def validate_hidden(self) -> CIVariable:
        if self.hidden and not self.masked:
            raise ValueError("Hidden variables must also be masked")
        return self


class CompatibilityPolicy(Model):
    mode: Literal["strict", "experimental_direct_transfer"] = "strict"
    pilot_run_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{12}$")


class VariablePolicy(Model):
    mode: Literal["preserve_scopes", "project_explicit"] = "preserve_scopes"
    accept_destination_inheritance: bool = False


class Repository(Model):
    source_path: str
    destination_path: str
    strategy: Literal["direct_transfer", "file_export", "git_copy"] = "direct_transfer"
    skip_merge_request_history: bool = False
    ci_branches: list[str] = Field(default_factory=list)
    variable_overrides: dict[str, str] = Field(default_factory=dict)
    ci_variables: list[CIVariable] = Field(default_factory=list)
    schedule_activation_ids: list[int] = Field(default_factory=list)

    _paths = field_validator("source_path", "destination_path")(project_path)

    @model_validator(mode="after")
    def validate_history_policy(self) -> Repository:
        if self.skip_merge_request_history and self.strategy != "git_copy":
            raise ValueError("skip_merge_request_history requires git_copy strategy")
        return self

    @field_validator("ci_variables")
    @classmethod
    def unique_variables(cls, value: list[CIVariable]) -> list[CIVariable]:
        if len({(v.key, v.environment_scope) for v in value}) != len(value):
            raise ValueError("Duplicate CI variable key and environment scope")
        return value

    @field_validator("variable_overrides")
    @classmethod
    def validate_overrides(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not re.fullmatch(r"(?:env:)?[A-Za-z_][A-Za-z0-9_]*", ref) for ref in value.values()):
            raise ValueError("Variable overrides must reference environment variable names")
        return value

    @field_validator("schedule_activation_ids")
    @classmethod
    def validate_schedule_ids(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value) or len(value) != len(set(value)):
            raise ValueError("Schedule IDs must be positive and unique")
        return value


class RunnerProfile(Model):
    allowed_runner_ids: list[int] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    tag_map: dict[str, list[str]] = Field(default_factory=dict)
    shared_enabled: bool = False
    group_enabled: bool = True
    enforcement: Literal["exclusive_access", "job_eligibility"] = "exclusive_access"

    @field_validator("allowed_runner_ids")
    @classmethod
    def validate_ids(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value) or len(value) != len(set(value)):
            raise ValueError("Runner IDs must be positive and unique")
        return value

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        if any(not tag or tag != tag.strip() or "$" in tag or "," in tag for tag in value):
            raise ValueError("Runner tags must be nonempty literal tags without commas")
        if len(value) != len(set(value)):
            raise ValueError("Runner tags must be unique")
        return value

    @model_validator(mode="after")
    def validate_selection(self) -> RunnerProfile:
        if bool(self.allowed_runner_ids) == bool(self.tags):
            raise ValueError("Choose either runner tags or allowed_runner_ids, not both")
        return self

    @field_validator("tag_map", mode="before")
    @classmethod
    def normalize_tags(cls, value: dict[str, Any]) -> dict[str, Any]:
        return {key: [tags] if isinstance(tags, str) else tags for key, tags in value.items()}


class Features(Model):
    schedules: bool = False
    environments: bool = False
    pages: bool = False


class MigrationConfig(Model):
    schema_version: Literal[1] = 1
    source: Endpoint
    destination: Endpoint
    repositories: list[Repository] = Field(default_factory=list)
    repositories_csv: str | None = None
    group_mapping: dict[str, str]
    runners: RunnerProfile
    compatibility: CompatibilityPolicy = Field(default_factory=CompatibilityPolicy)
    variables: VariablePolicy = Field(default_factory=VariablePolicy)
    features: Features = Field(default_factory=Features)
    user_mapping: dict[int, int] = Field(default_factory=dict)
    create_missing_groups: bool = False
    accept_file_import_memberships: bool = False
    concurrency: int = Field(default=2, ge=1, le=8)
    request_timeout: int = Field(default=30, ge=1, le=300)
    import_timeout: int = Field(default=3600, ge=10)
    poll_interval: float = Field(default=5, ge=0.1, le=60)

    @field_validator("group_mapping")
    @classmethod
    def normalize_group_paths(cls, value: dict[str, str]) -> dict[str, str]:
        normalized = {
            project_path(source.strip().strip("/") + "/placeholder").rsplit("/", 1)[
                0
            ]: project_path(destination.strip().strip("/") + "/placeholder").rsplit("/", 1)[0]
            for source, destination in value.items()
        }
        if len(normalized) != len(value):
            raise ValueError("Duplicate normalized source group mapping")
        return normalized

    @field_validator("user_mapping")
    @classmethod
    def validate_user_mapping(cls, value: dict[int, int]) -> dict[int, int]:
        if any(source <= 0 or destination <= 0 for source, destination in value.items()):
            raise ValueError("User mapping IDs must be positive")
        return value

    @model_validator(mode="after")
    def validate_mapping(self) -> MigrationConfig:
        if self.compatibility.mode == "strict" and self.compatibility.pilot_run_id:
            raise ValueError("Pilot evidence requires experimental_direct_transfer mode")
        if self.compatibility.mode == "experimental_direct_transfer" and any(
            repo.strategy != "direct_transfer" for repo in self.repositories
        ):
            raise ValueError("Experimental compatibility is only available for direct transfer")
        for repo in self.repositories:
            if self.variables.mode == "project_explicit" and repo.variable_overrides:
                raise ValueError(
                    "Use ci_variables instead of variable_overrides in project_explicit mode"
                )
            if self.variables.mode == "preserve_scopes" and repo.ci_variables:
                raise ValueError("ci_variables requires project_explicit mode")
        if self.source.url == self.destination.url:
            raise ValueError("Source and destination must be different GitLab instances")
        for attribute in ("source_path", "destination_path"):
            values = [getattr(repo, attribute) for repo in self.repositories]
            if len(values) != len(set(values)):
                raise ValueError(f"Duplicate {attribute} in repository list")
        if not self.group_mapping:
            raise ValueError(
                "At least one explicit source-to-destination group mapping is required"
            )
        for source, destination in self.group_mapping.items():
            project_path(source + "/placeholder")
            project_path(destination + "/placeholder")
        if len(set(self.group_mapping.values())) != len(self.group_mapping):
            raise ValueError("Group consolidation is not supported; map each group distinctly")
        for source, destination in self.group_mapping.items():
            for ancestor, target in self.group_mapping.items():
                if (
                    source.startswith(ancestor + "/")
                    and destination != target + source[len(ancestor) :]
                ):
                    raise ValueError("Group mappings must preserve the source subgroup hierarchy")
        for repo in self.repositories:
            if (
                self.map_group(repo.source_path.rsplit("/", 1)[0])
                != repo.destination_path.rsplit("/", 1)[0]
            ):
                raise ValueError("Repository destination must match its preserved group mapping")
        return self

    def map_group(self, source: str) -> str:
        matches = [
            key for key in self.group_mapping if source == key or source.startswith(key + "/")
        ]
        if not matches:
            raise ValueError(f"Missing mapping for source group {source}")
        key = max(matches, key=len)
        return self.group_mapping[key] + source[len(key) :]

    def fingerprint(self) -> str:
        data = self.model_dump(mode="json")
        # Preserve fingerprints of existing manifests and saved plans that use IDs.
        if not self.runners.tags:
            data["runners"].pop("tags")
        if self.runners.enforcement == "exclusive_access":
            data["runners"].pop("enforcement")
        if self.compatibility == CompatibilityPolicy():
            data.pop("compatibility")
        if self.variables == VariablePolicy():
            data.pop("variables")
        for repository in data["repositories"]:
            if not repository["ci_variables"]:
                repository.pop("ci_variables")
            if not repository["skip_merge_request_history"]:
                repository.pop("skip_merge_request_history")
        payload = json.dumps(data, sort_keys=True).encode()
        return hashlib.sha256(payload).hexdigest()


class Finding(Model):
    severity: Literal["blocker", "warning", "info"]
    code: str
    message: str
    repository: str | None = None


class PlannedRepository(Model):
    source_path: str
    destination_path: str
    source_id: int
    default_branch: str | None = None
    source_updated_at: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)


class MigrationPlan(Model):
    schema_version: Literal[1] = 1
    created_at: str
    config: MigrationConfig
    config_hash: str
    source_version: str
    destination_version: str
    pilot: bool = False
    endpoint_snapshot: dict[str, dict[str, str]] = Field(default_factory=dict)
    destination_variable_inventory: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    repositories: list[PlannedRepository] = Field(default_factory=list)
    groups: dict[str, str] = Field(default_factory=dict)
    resolved_runner_ids: dict[str, list[int]] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(finding.severity == "blocker" for finding in self.findings)
