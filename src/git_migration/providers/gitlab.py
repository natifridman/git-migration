"""Small, secret-safe boundary around python-gitlab's public HTTP API."""

from __future__ import annotations

import copy
import os
import time
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote, urlsplit

import gitlab
import requests

from git_migration.secrets import SafeError


class ProviderError(SafeError):
    def __init__(
        self,
        status_code: int | None = None,
        *,
        server: str | None = None,
        operation: str = "request",
        reason: Literal["tls", "timeout", "proxy", "connection", "invalid_response"] | None = None,
    ) -> None:
        self.status_code = status_code
        context = f"GitLab {operation}" + (f" on {server}" if server else "")
        messages = {
            "tls": "TLS certificate verification failed; configure ca_bundle with a trusted CA bundle",
            "timeout": "request timed out; check server connectivity, VPN, and request_timeout",
            "proxy": "proxy connection failed; check proxy settings and VPN connectivity",
            "connection": "could not connect; check DNS, VPN, proxy settings, and server connectivity",
            "invalid_response": "server did not return valid GitLab JSON; check the endpoint URL and any login proxy",
        }
        if reason:
            detail = messages[reason]
        elif status_code:
            detail = f"HTTP {status_code}"
        else:
            detail = "request failed without an HTTP status; remote outcome may be unknown"
        super().__init__(f"{context} failed: {detail}.")


def not_found(error: Exception) -> bool:
    return isinstance(error, ProviderError) and error.status_code == 404


class GitLabProvider:
    def __init__(
        self, url: str, token: str, *, timeout: int = 30, ca_bundle: str | None = None
    ) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "GitLab URL must be an HTTP(S) URL without credentials, query, or fragment."
            )
        self.url = url.rstrip("/")
        self.token = token
        self.ca_bundle = ca_bundle
        self._client = gitlab.Gitlab(
            self.url,
            private_token=token,
            timeout=timeout,
            ssl_verify=ca_bundle or True,
            retry_transient_errors=False,
            keep_base_url=True,
        )

    def __repr__(self) -> str:
        return "GitLabProvider()"

    def _error(self, path: str, error: Exception | None = None) -> ProviderError:
        # Use only local labels and exception types; never echo server responses,
        # request parameters, tokens, or arbitrary exception messages.
        reason: Literal["tls", "timeout", "proxy", "connection", "invalid_response"] | None
        if isinstance(error, requests.exceptions.SSLError):
            reason = "tls"
        elif isinstance(error, requests.exceptions.Timeout):
            reason = "timeout"
        elif isinstance(error, requests.exceptions.ProxyError):
            reason = "proxy"
        elif isinstance(
            error, (requests.exceptions.ConnectionError, gitlab.exceptions.GitlabConnectionError)
        ):
            reason = "connection"
        elif error is None or isinstance(error, gitlab.exceptions.GitlabParsingError):
            reason = "invalid_response"
        else:
            reason = None
        operation = {
            "/version": "version check",
            "/user": "authentication check",
            "/application/settings": "application settings check",
        }.get(path, "API request")
        return ProviderError(
            getattr(error, "response_code", None),
            server=urlsplit(self.url).netloc,
            operation=operation,
            reason=reason,
        )

    def _call(self, method: str, path: str, *, read: bool = False, **kwargs: Any) -> Any:
        if not path.startswith("/") or path.startswith("//") or "://" in path:
            raise ValueError("API path must be relative to the GitLab API.")
        # Disable SDK backoff, including unbounded Retry-After sleeps, and write retries.
        for attempt in range(3 if read else 1):
            try:
                result = getattr(self._client, method)(
                    path,
                    obey_rate_limit=False,
                    retry_transient_errors=False,
                    max_retries=0,
                    **kwargs,
                )
                if not isinstance(result, (dict, list)):
                    raise self._error(path)
                return copy.deepcopy(result)
            except (gitlab.exceptions.GitlabError, requests.RequestException) as exc:
                status = getattr(exc, "response_code", None)
                if read and attempt < 2 and (status in {429, 500, 502, 503, 504} or status is None):
                    # python-gitlab exceptions do not consistently expose response headers.
                    # Fixed bounded backoff deliberately avoids trusting Retry-After.
                    time.sleep(min(2**attempt, 4))
                    continue
                raise self._error(path, exc) from None

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._call("http_get", path, read=True, query_data=params or {})

    def list(self, path: str, params: dict | None = None) -> list[dict]:
        return self._call("http_list", path, read=True, query_data=params or {}, get_all=True)

    def post(self, path: str, data: dict | None = None) -> dict:
        return self._call("http_post", path, post_data=data or {})

    def put(self, path: str, data: dict | None = None) -> dict:
        return self._call("http_put", path, post_data=data or {})

    def patch(self, path: str, data: dict | None = None) -> dict:
        return self._call("http_patch", path, post_data=data or {})

    def project(self, id_or_path: str | int) -> dict:
        return self.get(f"/projects/{quote(str(id_or_path), safe='')}")

    def group(self, id_or_path: str | int) -> dict:
        return self.get(f"/groups/{quote(str(id_or_path), safe='')}")

    def version(self) -> dict:
        return self.get("/version")

    def download(self, path: str, destination: Path) -> None:
        destination = Path(destination)
        # A private exclusive file prevents symlink following and partial archive reuse.
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        response = None
        try:
            with os.fdopen(fd, "wb") as stream:
                # A streamed response cannot be deep-copied or replayed safely.
                if not path.startswith("/") or path.startswith("//") or "://" in path:
                    raise ValueError("API path must be relative to the GitLab API.")
                response = cast(
                    requests.Response,
                    self._client.http_get(
                        path,
                        streamed=True,
                        raw=True,
                        obey_rate_limit=False,
                        retry_transient_errors=False,
                        max_retries=0,
                    ),
                )
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        stream.write(chunk)
        except (gitlab.exceptions.GitlabError, requests.RequestException) as exc:
            destination.unlink(missing_ok=True)
            raise self._error(path, exc) from None
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        finally:
            if response is not None:
                response.close()

    def upload(self, path: str, archive: Path, data: dict | None = None) -> dict:
        with Path(archive).open("rb") as stream:
            return self._call(
                "http_post",
                path,
                post_data=data or {},
                files={"file": ("project-export.tar.gz", stream, "application/gzip")},
            )
