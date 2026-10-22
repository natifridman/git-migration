from unittest.mock import MagicMock

import gitlab
import pytest
import requests

from git_migration.models import MigrationConfig
from git_migration.providers import GitLabProvider, ProviderError, clients_for, not_found
from git_migration.secrets import safe_error


@pytest.fixture
def client(monkeypatch):
    sdk = MagicMock()
    factory = MagicMock(return_value=sdk)
    monkeypatch.setattr(gitlab, "Gitlab", factory)
    return sdk, factory


def test_clients_use_each_endpoints_credentials_ca_and_configured_timeout(client, monkeypatch):
    _, factory = client
    monkeypatch.setenv("SOURCE_TOKEN", "source-secret")
    monkeypatch.setenv("DESTINATION_TOKEN", "destination-secret")
    config = MigrationConfig.model_validate(
        {
            "source": {"url": "https://source.test", "token_env": "SOURCE_TOKEN"},
            "destination": {
                "url": "https://destination.test",
                "token_env": "DESTINATION_TOKEN",
                "ca_bundle": "/private/destination-ca.pem",
            },
            "group_mapping": {"team": "new"},
            "runners": {"allowed_runner_ids": [7]},
            "request_timeout": 42,
        }
    )
    source, destination = clients_for(config)
    assert source is not destination
    source_call, destination_call = factory.call_args_list
    assert source_call.args == ("https://source.test",)
    assert destination_call.args == ("https://destination.test",)
    assert source_call.kwargs["private_token"] == "source-secret"
    assert destination_call.kwargs["private_token"] == "destination-secret"
    assert source_call.kwargs["ssl_verify"] is True
    assert destination_call.kwargs["ssl_verify"] == "/private/destination-ca.pem"
    assert source_call.kwargs["timeout"] == destination_call.kwargs["timeout"] == 42


def test_tls_pagination_encoding_and_secret_repr(client):
    sdk, factory = client
    provider = GitLabProvider("https://example.test/", "top-secret", ca_bundle="/private/ca.pem")
    sdk.http_list.return_value = [{"id": 1}, {"id": 2}]
    assert provider.list("/projects", {"membership": True}) == [{"id": 1}, {"id": 2}]
    assert sdk.http_list.call_args.kwargs["get_all"] is True
    sdk.http_get.return_value = {"id": 3}
    provider.project("group/sub/project")
    assert sdk.http_get.call_args.args == ("/projects/group%2Fsub%2Fproject",)
    assert factory.call_args.kwargs["ssl_verify"] == "/private/ca.pem"
    assert "top-secret" not in repr(provider)


def test_write_never_retries_and_errors_are_sanitized(client):
    sdk, _ = client
    provider = GitLabProvider("https://example.test", "secret")
    sdk.http_post.side_effect = gitlab.exceptions.GitlabHttpError("echo secret", response_code=503)
    with pytest.raises(ProviderError) as error:
        provider.post("/bulk_imports", {"access_token": "secret"})
    assert "secret" not in str(error.value)
    assert sdk.http_post.call_count == 1
    assert sdk.http_post.call_args.kwargs["obey_rate_limit"] is False
    assert sdk.http_post.call_args.kwargs["max_retries"] == 0


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (
            requests.exceptions.SSLError("secret-certificate-detail"),
            "TLS certificate verification failed",
        ),
        (requests.exceptions.Timeout("secret-timeout-detail"), "request timed out"),
        (requests.exceptions.ProxyError("secret-proxy-password"), "proxy connection failed"),
        (requests.exceptions.ConnectionError("secret-connection-detail"), "could not connect"),
        (gitlab.exceptions.GitlabParsingError("secret-response-body"), "valid GitLab JSON"),
        (
            gitlab.exceptions.GitlabAuthenticationError("secret-response-body", response_code=401),
            "HTTP 401",
        ),
    ],
)
def test_failure_diagnostics_identify_server_and_cause_without_secrets(
    client, monkeypatch, failure, message
):
    sdk, _ = client
    monkeypatch.setattr("git_migration.providers.gitlab.time.sleep", lambda _: None)
    sdk.http_get.side_effect = failure
    provider = GitLabProvider("https://destination.example", "secret-token")
    with pytest.raises(ProviderError) as error:
        provider.version()
    rendered = safe_error(error.value)
    assert "version check on destination.example" in rendered
    assert message in rendered
    assert "secret" not in rendered
    assert "checkpoint" not in rendered


def test_invalid_api_response_is_described_without_echoing_it(client):
    sdk, _ = client
    sdk.http_get.return_value = "secret HTML login page"
    with pytest.raises(ProviderError) as error:
        GitLabProvider("https://destination.example", "secret-token").get("/user")
    rendered = safe_error(error.value)
    assert "authentication check on destination.example" in rendered
    assert "valid GitLab JSON" in rendered
    assert "secret" not in rendered


def test_reads_retry_bounded_and_not_found(client, monkeypatch):
    sdk, _ = client
    monkeypatch.setattr("git_migration.providers.gitlab.time.sleep", lambda _: None)
    sdk.http_get.side_effect = [
        gitlab.exceptions.GitlabHttpError("rate", response_code=429),
        {"id": 1},
    ]
    assert GitLabProvider("https://example.test", "secret").get("/user") == {"id": 1}
    assert sdk.http_get.call_count == 2
    assert not_found(ProviderError(404))
    assert not not_found(ProviderError(403))


def test_default_tls_and_reject_foreign_url(client):
    _, factory = client
    provider = GitLabProvider("https://example.test", "secret")
    assert factory.call_args.kwargs["ssl_verify"] is True
    with pytest.raises(ValueError):
        provider.get("https://other.test/api/v4/projects")


def test_download_is_private_exclusive_and_streamed(client, tmp_path):
    sdk, _ = client
    sdk.http_get.return_value.iter_content.return_value = [b"opaque", b"archive"]
    target = tmp_path / "archive.tar.gz"
    GitLabProvider("https://example.test", "secret").download("/projects/1/export/download", target)
    assert target.read_bytes() == b"opaquearchive"
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        GitLabProvider("https://example.test", "secret").download(
            "/projects/1/export/download", target
        )


def test_real_sdk_exhausts_pages_without_network():
    """Exercise installed python-gitlab, including its pagination URL handling."""
    import json

    import requests

    class Adapter(requests.adapters.BaseAdapter):
        def __init__(self):
            self.seen = []

        def send(self, request, **kwargs):
            self.seen.append(request)
            response = requests.Response()
            response.status_code = 200
            response.request = request
            response.headers["Content-Type"] = "application/json"
            if "page=2" not in request.url:
                response.headers["Link"] = (
                    '<https://misconfigured.test/api/v4/projects?page=2>; rel="next"'
                )
                response._content = json.dumps([{"id": 1}]).encode()
            else:
                response._content = json.dumps([{"id": 2}]).encode()
            return response

        def close(self):
            pass

    provider = GitLabProvider("https://example.test", "secret")
    adapter = Adapter()
    provider._client.session.mount("https://", adapter)
    assert provider.list("/projects") == [{"id": 1}, {"id": 2}]
    assert len(adapter.seen) == 2
    assert all(request.url.startswith("https://example.test/") for request in adapter.seen)
