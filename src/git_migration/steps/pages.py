"""Pages settings only: deployment, domains, DNS and certificates stay operator actions."""

from typing import Any
from urllib.parse import quote


def reconcile_pages(source: Any, destination: Any, source_id: Any, dest_id: Any) -> Any:
    sp = f"/projects/{quote(str(source_id), safe='')}/pages"
    dp = f"/projects/{quote(str(dest_id), safe='')}/pages"
    try:
        wanted, existing = source.get(sp), destination.get(dp)
    except Exception as exc:
        if getattr(exc, "status_code", None) in (403, 404, 405):
            return {
                "status": "blocked",
                "blockers": ["Pages settings API is unavailable or inaccessible"],
            }
        raise
    names = {
        "is_unique_domain_enabled": "pages_unique_domain_enabled",
        "force_https": "pages_https_only",
    }
    payload = {
        dest: wanted[key]
        for key, dest in names.items()
        if key in wanted and wanted[key] != existing.get(key)
    }
    if payload:
        destination.patch(dp, payload)
    actions = [
        "Review and redeploy Pages explicitly after CI migration",
        "Configure and verify destination DNS and TLS certificates",
    ]
    if wanted.get("primary_domain"):
        actions.append(
            "Assign and verify the custom domain before selecting it as the destination primary domain"
        )
    return {"status": "configured" if payload else "identical", "manual_actions": actions}
