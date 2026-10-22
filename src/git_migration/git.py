"""Native Git verification with ephemeral, environment-only HTTPS credentials."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class GitError(RuntimeError):
    """A deliberately sanitized Git failure, safe for reports and checkpoints."""


def _safe_url(url: str) -> str:
    if url.startswith("-"):
        raise GitError("Invalid repository URL")
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise GitError("Repository URL must not contain credentials, a query, or a fragment")
    if parsed.scheme not in ("https", "http", "file", ""):
        raise GitError("Unsupported repository transport")
    if parsed.scheme == "http":
        raise GitError("Authenticated repository transport requires HTTPS")
    return url


class GitTransport:
    def __init__(
        self,
        token: str = "",
        ca_bundle: str | Path | None = None,
        timeout: int = 120,
    ) -> None:
        self.token = token
        self.ca_bundle = ca_bundle
        self.timeout = timeout

    @contextmanager
    def _environment(self) -> Iterator[dict[str, str]]:
        env = os.environ.copy()
        env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
        if self.ca_bundle:
            env["GIT_SSL_CAINFO"] = str(self.ca_bundle)
        with tempfile.TemporaryDirectory(prefix="git-auth-") as folder:
            helper = Path(folder) / "askpass"
            helper.write_text(
                '#!/bin/sh\ncase "$1" in\n*Username*) printf "%s\\n" "oauth2" ;;\n*) printf "%s\\n" "$GIT_MIGRATION_TOKEN" ;;\nesac\n'
            )
            helper.chmod(0o700)
            env["GIT_ASKPASS"] = str(helper)
            env["GIT_MIGRATION_TOKEN"] = self.token
            yield env

    def run(self, args: list[str], cwd: Path | None = None, input_text: str | None = None) -> str:
        return self._run_bytes(args, cwd, input_text).decode("utf-8", errors="replace")

    def _run_bytes(
        self, args: list[str], cwd: Path | None = None, input_text: str | None = None
    ) -> bytes:
        with self._environment() as env:
            try:
                result = subprocess.run(
                    [
                        "git",
                        "-c",
                        "credential.helper=",
                        "-c",
                        # Permit our askpass helper; GIT_TERMINAL_PROMPT=0 blocks terminal prompts.
                        "credential.interactive=true",
                        *args,
                    ],
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    input=input_text.encode() if input_text is not None else None,
                    timeout=self.timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise GitError("Git operation timed out") from None
            except OSError:
                raise GitError("Git executable is unavailable or could not be started") from None
        if result.returncode:
            raise GitError(f"Git operation failed (exit code {result.returncode})")
        return result.stdout

    def refs(self, project_url: str) -> dict[str, Any]:
        output = self.run(
            ["ls-remote", "--symref", _safe_url(project_url), "HEAD", "refs/heads/*", "refs/tags/*"]
        )
        refs = {}
        default_branch = None
        for line in output.splitlines():
            value, separator, name = line.partition("\t")
            if not separator:
                continue
            if value.startswith("ref: ") and name == "HEAD":
                default_branch = value[5:].removeprefix("refs/heads/")
            elif name.startswith(("refs/heads/", "refs/tags/")):
                if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
                    raise GitError("Git returned an invalid object identifier")
                refs[name] = value.lower()
        return {"refs": refs, "default_branch": default_branch}

    def fetch(self, project_url: str, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=False)
        self.run(["init", "--bare", str(directory)])
        self.run(
            [
                "fetch",
                "--force",
                "--no-tags",
                _safe_url(project_url),
                "+refs/heads/*:refs/heads/*",
                "+refs/tags/*:refs/tags/*",
            ],
            cwd=directory,
        )
        self.run(["fsck", "--full", "--no-reflogs"], cwd=directory)

    def lfs_oids(self, directory: Path) -> list[str]:
        objects = self.run(["rev-list", "--objects", "--all"], cwd=directory)
        oids = set()
        # Batch metadata avoids spawning a Git process for every historical object.
        object_ids = [line.split(" ", 1)[0] for line in objects.splitlines()]
        metadata = self.run(
            ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
            cwd=directory,
            input_text="".join(oid + "\n" for oid in object_ids),
        )
        small_blobs = []
        for line in metadata.splitlines():
            oid, kind, size = line.split()
            if kind == "blob" and int(size) <= 1024:
                small_blobs.append(oid)
        for offset in range(0, len(small_blobs), 512):
            payload = self._run_bytes(
                ["cat-file", "--batch"],
                cwd=directory,
                input_text="".join(oid + "\n" for oid in small_blobs[offset : offset + 512]),
            )
            position = 0
            while position < len(payload):
                header_end = payload.index(b"\n", position)
                header = payload[position:header_end].split()
                blob_size = int(header[2])
                blob = payload[header_end + 1 : header_end + 1 + blob_size]
                position = header_end + 2 + blob_size
                if blob.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
                    match = re.search(rb"^oid sha256:([0-9a-f]{64})$", blob, re.MULTILINE)
                    if not match:
                        raise GitError("Repository contains a malformed LFS pointer")
                    oids.add(match.group(1).decode("ascii"))
        return sorted(oids)

    def verify_lfs(self, project_url: str, directory: Path) -> None:
        self.run(["lfs", "version"])
        # A named remote is needed by git-lfs. Only the credential-free URL persists.
        self.run(["remote", "add", "origin", _safe_url(project_url)], cwd=directory)
        self.run(
            [
                "-c",
                "lfs.url=" + project_url.rstrip("/") + "/info/lfs",
                "lfs",
                "fetch",
                "--all",
                "origin",
            ],
            cwd=directory,
        )
        refs = self.run(
            ["for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags"], cwd=directory
        ).splitlines()
        for ref in refs:
            self.run(["lfs", "fsck", "--objects", ref], cwd=directory)


def transport_for(provider: Any) -> GitTransport:
    return GitTransport(
        token=getattr(provider, "token", ""),
        ca_bundle=getattr(provider, "ca_bundle", None),
    )


def repository_url(provider: Any, project: dict[str, Any]) -> str:
    return _safe_url(
        project.get("http_url_to_repo")
        or (provider.url.rstrip("/") + "/" + project["path_with_namespace"] + ".git")
    )


def snapshot_repository(provider: Any, project: dict[str, Any], work_dir: Path) -> dict[str, Any]:
    transport = transport_for(provider)
    url = repository_url(provider, project)
    snapshot = transport.refs(url)
    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="git-verify-", dir=work_dir) as temporary:
        directory = Path(temporary) / "repository.git"
        transport.fetch(url, directory)
        snapshot["lfs_oids"] = transport.lfs_oids(directory)
        if snapshot["lfs_oids"]:
            transport.verify_lfs(url, directory)
    return snapshot
