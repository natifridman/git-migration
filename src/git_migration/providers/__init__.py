from git_migration.models import Endpoint, MigrationConfig
from git_migration.secrets import resolve_token

from .gitlab import GitLabProvider, ProviderError, not_found

__all__ = ["GitLabProvider", "ProviderError", "clients_for", "not_found"]


def clients_for(config: MigrationConfig) -> tuple[GitLabProvider, GitLabProvider]:
    def client(endpoint: Endpoint) -> GitLabProvider:
        return GitLabProvider(
            endpoint.url,
            resolve_token(endpoint),
            timeout=config.request_timeout,
            ca_bundle=endpoint.ca_bundle,
        )

    return client(config.source), client(config.destination)
