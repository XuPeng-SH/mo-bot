"""Review admission policy adopted from the existing mo-pr-review-queue skill."""

from __future__ import annotations
import datetime as dt
import time
from typing import Any

QUERY = """
query Queue($owner: String!, $repo: String!, $endCursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequests(first: 25, states: OPEN, orderBy: {field: UPDATED_AT, direction: DESC}, after: $endCursor) {
      nodes {
        number title url isDraft updatedAt headRefOid mergeable reviewDecision
        author { login }
        labels(first: 100) { nodes { name } }
        reviews(last: 100) { nodes { author { login } state commit { oid } submittedAt } }
        statusCheckRollup {
          contexts(first: 100) {
            nodes {
              __typename
              ... on CheckRun { name status conclusion startedAt completedAt }
              ... on StatusContext { context state updatedAt }
            }
          }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

PASSING_CONCLUSIONS = {"SUCCESS", "SKIPPED", "NEUTRAL"}
NON_EXECUTION_CHECKS = {"Summary", "Mergify Merge Protections", "Mergify Merge Queue"}
DECISIVE_REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED"}


class OwnerWip(ValueError):
    """Owner's WIP label is an admission wait, not a task failure."""


def owner_wip(pr, reviewer):
    author = pr.get("author") or pr.get("user") or {}
    if (author.get("login") or "").casefold() != reviewer.casefold():
        return False
    labels = pr.get("labels") or []
    if isinstance(labels, dict):
        labels = labels.get("nodes") or []
    return any(
        (label.get("name", "") if isinstance(label, dict) else label).casefold()
        == "do-not-merge/wip"
        for label in labels
    )


def stabilize_mergeability(
    prs: list[dict[str, Any]],
    owner: str,
    repo: str,
    retries: int,
    retry_delay: float,
    fetcher: Any = None,
    sleeper: Any = None,
) -> None:
    """Resolve transient UNKNOWN states without silently trusting or dropping them."""
    if fetcher is None:
        raise ValueError("mergeability refresh requires a GitHub fetcher")
    sleeper = sleeper or time.sleep
    retries = max(1, retries)
    retry_delay = max(0.0, retry_delay)

    pending = [pr for pr in prs if pr.get("mergeable") == "UNKNOWN"]
    for attempt in range(retries):
        next_pending: list[dict[str, Any]] = []
        for pr in pending:
            refreshed = fetcher(owner, repo, pr["number"])
            refreshed_head = (refreshed.get("head") or {}).get("sha")
            if refreshed_head and refreshed_head != pr.get("headRefOid"):
                pr["_mergeability_detail"] = (
                    "head changed during refresh; rerun required"
                )
                next_pending.append(pr)
                continue

            mergeable = refreshed.get("mergeable")
            if mergeable is True:
                pr["mergeable"] = "MERGEABLE"
                pr["_mergeability_detail"] = (
                    f"resolved after {attempt + 1} refresh attempt(s)"
                )
            elif mergeable is False:
                pr["mergeable"] = "CONFLICTING"
                pr["_mergeability_detail"] = (
                    f"resolved after {attempt + 1} refresh attempt(s)"
                )
            else:
                pr["_mergeability_detail"] = (
                    f"still UNKNOWN after {attempt + 1} refresh attempt(s)"
                )
                next_pending.append(pr)
        pending = next_pending
        if not pending:
            return
        if attempt + 1 < retries and retry_delay:
            sleeper(retry_delay)


def latest_decisive_review(pr: dict[str, Any], reviewer: str) -> dict[str, Any] | None:
    """Return the latest review that can close a review-queue item.

    COMMENTED reviews carry no approve/request-changes decision, so they must
    neither close an item nor hide an earlier CHANGES_REQUESTED review.
    """
    mine = [
        review
        for review in pr["reviews"]["nodes"]
        if review.get("author")
        and review["author"]["login"] == reviewer
        and review.get("state") in DECISIVE_REVIEW_STATES
    ]
    return max(mine, key=lambda r: r.get("submittedAt") or "") if mine else None


def latest_ci_contexts(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse superseded CI attempts while retaining a newly started rerun."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for context in contexts:
        typename = context.get("__typename", "")
        if typename == "CheckRun":
            key = (typename, context.get("name") or "")
            attempt_time = context.get("startedAt") or context.get("completedAt") or ""
        elif typename == "StatusContext":
            key = (typename, context.get("context") or "")
            attempt_time = context.get("updatedAt") or ""
        else:
            continue

        previous = latest.get(key)
        if previous is None:
            latest[key] = context
            continue
        if typename == "CheckRun":
            previous_time = (
                previous.get("startedAt") or previous.get("completedAt") or ""
            )
        else:
            previous_time = previous.get("updatedAt") or ""
        if attempt_time >= previous_time:
            latest[key] = context
    return list(latest.values())


def ci_state(pr: dict[str, Any], stale_days: int) -> tuple[bool, str]:
    rollup = pr.get("statusCheckRollup") or {}
    contexts = (rollup.get("contexts") or {}).get("nodes") or []
    if not contexts:
        return False, "missing"

    substantive_times: list[dt.datetime] = []
    for context in latest_ci_contexts(contexts):
        if context["__typename"] == "CheckRun":
            if (
                context.get("status") != "COMPLETED"
                or context.get("conclusion") not in PASSING_CONCLUSIONS
            ):
                return False, "not-green"
            completed = context.get("completedAt")
            if completed and context.get("name") not in NON_EXECUTION_CHECKS:
                substantive_times.append(
                    dt.datetime.fromisoformat(completed.replace("Z", "+00:00"))
                )
        elif (
            context["__typename"] == "StatusContext"
            and context.get("state") != "SUCCESS"
        ):
            return False, "not-green"

    if not substantive_times:
        return False, "missing"
    latest = max(substantive_times)
    age = (dt.datetime.now(dt.timezone.utc) - latest).days
    return (
        True,
        f"{'stale' if age > stale_days else 'fresh'} ({age}d; latest substantive CI {latest.date().isoformat()})",
    )


def classify(
    pr: dict[str, Any],
    reviewer: str,
    exclusions: set[int],
    stale_days: int,
) -> tuple[str, str, dict[str, Any] | None]:
    if pr["number"] in exclusions:
        return "excluded", "explicitly-excluded", None
    if owner_wip(pr, reviewer):
        return "excluded", "owner-wip", None
    if pr["isDraft"]:
        return "excluded", "draft", None
    if (pr.get("author") or {}).get("login") == reviewer:
        return "excluded", "self-authored", None
    green, ci = ci_state(pr, stale_days)
    if not green:
        return "excluded", ci, None
    mine = latest_decisive_review(pr, reviewer)
    needs_review = mine is None or (
        mine["state"] == "CHANGES_REQUESTED"
        and (mine.get("commit") or {}).get("oid") != pr["headRefOid"]
    )
    if not needs_review:
        return "excluded", "already-reviewed", mine
    if pr.get("mergeable") == "UNKNOWN":
        detail = pr.get("_mergeability_detail") or "mergeability UNKNOWN"
        return "indeterminate", f"{ci}; {detail}", mine
    if pr.get("mergeable") != "MERGEABLE":
        return "excluded", f"mergeability {pr.get('mergeable')}", mine
    return "candidate", ci, mine


class Queue:
    """Bundled queue policy; GitHub owns bounded authenticated requests."""

    classify = staticmethod(classify)
    ci_state = staticmethod(ci_state)

    def __init__(self, github):
        self.github = github

    def all_prs(self, owner, repo):
        result, cursor, seen = [], None, set()
        for _ in range(100):
            payload = self.github.api(
                "graphql",
                method="POST",
                body={
                    "query": QUERY,
                    "variables": {"owner": owner, "repo": repo, "endCursor": cursor},
                },
            )
            if payload.get("errors"):
                raise RuntimeError("GitHub PR discovery returned GraphQL errors")
            page = payload["data"]["repository"]["pullRequests"]
            result.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return result
            cursor = page["pageInfo"]["endCursor"]
            if not cursor or cursor in seen:
                raise RuntimeError("GitHub PR discovery pagination did not advance")
            seen.add(cursor)
        raise RuntimeError("GitHub PR discovery pagination limit reached")

    def stabilize_mergeability(self, prs, owner, repo, retries, retry_delay):
        stabilize_mergeability(
            prs,
            owner,
            repo,
            retries,
            retry_delay,
            fetcher=lambda o, r, n: self.github.api(f"repos/{o}/{r}/pulls/{n}"),
        )
