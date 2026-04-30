"""dlt pipeline: GitHub API → bronze.pull_requests + bronze.ci_checks."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import dlt
import httpx
from dlt.sources import DltResource


def _extract_ticket_key(text: str) -> str:
    """Extract a JIRA-style ticket key (e.g. FOR-123) from PR title or branch."""
    import re

    match = re.search(r"[A-Z]{2,10}-\d+", text or "")
    return match.group(0) if match else ""


@dlt.source(name="github")
def github_source(
    token: str,
    repos: list[str],
    lookback_days: int = 30,
) -> tuple[DltResource, DltResource]:
    """Extract pull requests and CI check runs from GitHub."""

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    since = (datetime.now(tz=UTC) - timedelta(days=lookback_days)).isoformat()

    @dlt.resource(
        table_name="pull_requests",
        write_disposition="merge",
        primary_key=["repo", "pr_number"],
    )
    def pull_requests(
        updated_at=dlt.sources.incremental("updated_at", initial_value=since),  # noqa: B008
    ) -> Iterator[dict]:
        with httpx.Client(
            base_url="https://api.github.com",
            headers=headers,
            timeout=30.0,
        ) as client:
            for repo in repos:
                page = 1
                while True:
                    resp = client.get(
                        f"/repos/{repo}/pulls",
                        params={
                            "state": "all",
                            "sort": "updated",
                            "direction": "desc",
                            "per_page": 50,
                            "page": page,
                        },
                    )
                    if resp.status_code == 404:
                        break
                    resp.raise_for_status()
                    prs = resp.json()
                    if not prs:
                        break

                    for pr in prs:
                        pr_updated = pr.get("updated_at", "")
                        if pr_updated < updated_at.last_value:
                            return  # sorted desc by updated_at; stop here

                        merged_at = pr.get("merged_at")
                        closed_at = pr.get("closed_at")
                        created_at = pr.get("created_at")

                        yield {
                            "repo": repo,
                            "pr_number": pr["number"],
                            "ticket_key": _extract_ticket_key(
                                pr.get("title", "") + " " + pr.get("head", {}).get("ref", "")
                            ),
                            "title": pr.get("title", ""),
                            "state": pr.get("state", ""),
                            "merged": merged_at is not None,
                            "draft": pr.get("draft", False),
                            "author": pr.get("user", {}).get("login", ""),
                            "base_branch": pr.get("base", {}).get("ref", ""),
                            "head_branch": pr.get("head", {}).get("ref", ""),
                            "lines_added": pr.get("additions", 0),
                            "lines_deleted": pr.get("deletions", 0),
                            "files_changed": pr.get("changed_files", 0),
                            "commits_count": pr.get("commits", 0),
                            "review_comments": pr.get("review_comments", 0),
                            "created_at": created_at,
                            "updated_at": pr_updated,
                            "merged_at": merged_at,
                            "closed_at": closed_at,
                        }

                    page += 1

    @dlt.resource(
        table_name="ci_checks",
        write_disposition="merge",
        primary_key="check_id",
    )
    def ci_checks() -> Iterator[dict]:
        with httpx.Client(
            base_url="https://api.github.com",
            headers=headers,
            timeout=30.0,
        ) as client:
            for repo in repos:
                # Fetch recent commits and their check runs
                resp = client.get(
                    f"/repos/{repo}/commits",
                    params={"since": since, "per_page": 30},
                )
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()

                for commit in resp.json():
                    sha = commit.get("sha", "")
                    checks_resp = client.get(
                        f"/repos/{repo}/commits/{sha}/check-runs",
                        params={"per_page": 50},
                    )
                    if checks_resp.status_code != 200:
                        continue

                    for check in checks_resp.json().get("check_runs", []):
                        started = check.get("started_at")
                        completed = check.get("completed_at")
                        duration = None
                        if started and completed:
                            try:
                                s = datetime.fromisoformat(started.replace("Z", "+00:00"))
                                c = datetime.fromisoformat(completed.replace("Z", "+00:00"))
                                duration = int((c - s).total_seconds())
                            except (ValueError, TypeError):
                                pass

                        yield {
                            "check_id": check["id"],
                            "repo": repo,
                            "commit_sha": sha,
                            "check_name": check.get("name", ""),
                            "status": check.get("status", ""),
                            "conclusion": check.get("conclusion") or "",
                            "duration_seconds": duration,
                            "started_at": started,
                            "completed_at": completed,
                        }

    return pull_requests, ci_checks
