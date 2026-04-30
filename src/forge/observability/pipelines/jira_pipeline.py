"""dlt pipeline: JIRA API → bronze.jira_tickets + bronze.human_interactions."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import dlt
import httpx
from dlt.sources import DltResource

_FORGE_LABELS = {
    "forge:managed",
    "forge:prd-pending",
    "forge:spec-pending",
    "forge:plan-pending",
    "forge:task-pending",
    "forge:blocked",
    "forge:retry",
}

_APPROVAL_LABELS = {
    "forge:prd-approved",
    "forge:spec-approved",
    "forge:plan-approved",
    "forge:task-approved",
}
_REJECTION_LABELS = {
    "forge:prd-pending",
    "forge:spec-pending",
    "forge:plan-pending",
    "forge:task-pending",
}


def _classify_interaction(changelog_items: list[dict], comment_body: str) -> str | None:
    """Derive an interaction type from a JIRA changelog item or comment."""
    for item in changelog_items:
        field = item.get("field", "")
        to_val = (item.get("toString") or "").lower()
        if field == "labels":
            if any(lbl.lower() in to_val for lbl in _APPROVAL_LABELS):
                return "approval"
            if "forge:blocked" in to_val:
                return "rejection"
    if comment_body.strip():
        body_lower = comment_body.lower()
        if any(w in body_lower for w in ("lgtm", "approve", "approved", "looks good")):
            return "approval"
        if any(w in body_lower for w in ("?", "clarify", "what", "why", "how")):
            return "question"
        return "rejection"
    return None


@dlt.source(name="jira")
def jira_source(
    base_url: str,
    user_email: str,
    api_token: str,
    project_keys: list[str] | None = None,
    lookback_days: int = 30,
) -> tuple[DltResource, DltResource]:
    """Extract tickets and human interactions from JIRA."""

    auth = (user_email, api_token)
    since = (datetime.now(tz=UTC) - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M")

    project_clause = ""
    if project_keys:
        keys = ", ".join(f'"{k}"' for k in project_keys)
        project_clause = f" AND project IN ({keys})"

    jql = f'labels = "forge:managed"{project_clause} AND updated >= "{since}" ORDER BY updated DESC'

    @dlt.resource(
        table_name="jira_tickets",
        write_disposition="merge",
        primary_key="ticket_key",
    )
    def jira_tickets(
        _updated_at=dlt.sources.incremental("updated_at", initial_value=since),  # noqa: B008
    ) -> Iterator[dict]:
        api_base = f"{base_url}/rest/api/3"

        with httpx.Client(timeout=30.0) as client:
            next_page_token: str | None = None
            while True:
                body: dict = {
                    "jql": jql,
                    "maxResults": 50,
                    "fields": [
                        "summary",
                        "issuetype",
                        "status",
                        "labels",
                        "assignee",
                        "priority",
                        "created",
                        "updated",
                        "resolutiondate",
                    ],
                }
                if next_page_token:
                    body["nextPageToken"] = next_page_token

                resp = client.post(
                    f"{api_base}/search/jql",
                    auth=auth,
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()
                issues = data.get("issues", [])
                if not issues:
                    break

                for issue in issues:
                    fields = issue.get("fields", {})
                    updated = fields.get("updated", "")

                    yield {
                        "ticket_key": issue["key"],
                        "ticket_type": (fields.get("issuetype") or {}).get("name", ""),
                        "summary": fields.get("summary", ""),
                        "status": (fields.get("status") or {}).get("name", ""),
                        "labels": ",".join(fields.get("labels") or []),
                        "forge_labels": ",".join(
                            lbl for lbl in (fields.get("labels") or []) if lbl.startswith("forge:")
                        ),
                        "assignee": (fields.get("assignee") or {}).get("emailAddress", ""),
                        "priority": (fields.get("priority") or {}).get("name", ""),
                        "created_at": fields.get("created"),
                        "updated_at": updated,
                        "resolved_at": fields.get("resolutiondate"),
                    }

                if data.get("isLast", True):
                    break
                next_page_token = data.get("nextPageToken")

    @dlt.resource(
        table_name="human_interactions",
        write_disposition="append",
    )
    def human_interactions() -> Iterator[dict]:
        api_base = f"{base_url}/rest/api/3"

        with httpx.Client(timeout=30.0) as client:
            next_page_token: str | None = None
            while True:
                body: dict = {"jql": jql, "maxResults": 50, "fields": ["summary"]}
                if next_page_token:
                    body["nextPageToken"] = next_page_token

                resp = client.post(
                    f"{api_base}/search/jql",
                    auth=auth,
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()
                issues = data.get("issues", [])
                if not issues:
                    break

                for issue in issues:
                    ticket_key = issue["key"]
                    # Fetch changelog separately (not via expand to avoid 410)
                    cl_resp = client.get(
                        f"{api_base}/issue/{ticket_key}/changelog",
                        auth=auth,
                        params={"maxResults": 50},
                    )
                    changelog = (
                        cl_resp.json().get("values", []) if cl_resp.status_code == 200 else []
                    )

                    for history in changelog:
                        author = (history.get("author") or {}).get("emailAddress", "")
                        created = history.get("created", "")
                        items = history.get("items", []) or history.get("items", [])
                        interaction = _classify_interaction(items, "")
                        if interaction:
                            # Determine stage from label changes
                            stage = ""
                            for item in items:
                                if item.get("field") == "labels":
                                    to_val = item.get("toString", "").lower()
                                    for lbl in ("prd", "spec", "plan", "task"):
                                        if lbl in to_val:
                                            stage = lbl
                                            break

                            yield {
                                "ticket_key": ticket_key,
                                "interaction_type": interaction,
                                "user": author,
                                "workflow_stage": stage,
                                "source": "jira_changelog",
                                "interacted_at": created,
                            }

                    # Also fetch recent comments
                    comments_resp = client.get(
                        f"{api_base}/issue/{ticket_key}/comment",
                        auth=auth,
                        params={"maxResults": 20, "orderBy": "-created"},
                    )
                    if comments_resp.status_code != 200:
                        continue

                    for comment in comments_resp.json().get("comments", []):
                        body = comment.get("body", "")
                        if isinstance(body, dict):
                            # ADF format — extract plain text
                            body = " ".join(
                                node.get("text", "")
                                for block in body.get("content", [])
                                for node in block.get("content", [])
                                if node.get("type") == "text"
                            )

                        interaction = _classify_interaction([], body)
                        if interaction:
                            author = (comment.get("author") or {}).get("emailAddress", "")
                            yield {
                                "ticket_key": ticket_key,
                                "interaction_type": interaction,
                                "user": author,
                                "workflow_stage": "",
                                "source": "jira_comment",
                                "interacted_at": comment.get("created"),
                            }

                if data.get("isLast", True):
                    break
                next_page_token = data.get("nextPageToken")

    return jira_tickets, human_interactions
