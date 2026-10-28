"""Complete discussion inventory and stable, bot-loop-free review input identity."""

import hashlib
import json
import re
from datetime import datetime, timezone


MARKER = re.compile(r"<!-- mo-bot-review:v1 (\{[^\n]+\}) -->\s*\Z")


KINDS = ("issue_comments", "review_comments", "reviews")


def inventory(discussion, reviewer):
    rows = []
    for kind in KINDS:
        values = discussion.get(kind)
        if not isinstance(values, list):
            raise ValueError(f"incomplete PR discussion: {kind}")
        for value in values:
            author = (value.get("user") or value.get("author") or {}).get("login", "")
            body = value.get("body") or ""
            # Only an authenticated bot's own marked output is excluded from
            # admission identity. It remains in the review inventory in full.
            marked = author.casefold() == reviewer.casefold() and bool(
                MARKER.search(body)
            )
            rows.append(
                {
                    "key": f"{kind}:{value['id']}",
                    "kind": kind,
                    "author": author,
                    "body": body,
                    "url": value.get("html_url") or value.get("url"),
                    "created_at": value.get("created_at") or value.get("submitted_at"),
                    "updated_at": value.get("updated_at")
                    or value.get("submitted_at")
                    or value.get("created_at"),
                    "state": value.get("state"),
                    "commit_id": value.get("commit_id"),
                    "in_reply_to_id": value.get("in_reply_to_id"),
                    "path": value.get("path"),
                    "line": value.get("line"),
                    "original_line": value.get("original_line"),
                    "service_generated": marked,
                    "bot_author": (value.get("user") or {}).get("type") == "Bot"
                    or author.endswith("[bot]"),
                }
            )
    return sorted(rows, key=lambda row: (row["created_at"] or "", row["key"]))


def relevant(rows):
    # Empty approvals and automated notices are still visible, but do not
    # continuously consume reviewer capacity or trigger feedback loops.
    return [
        r
        for r in rows
        if r["body"].strip() and not r["service_generated"] and not r["bot_author"]
    ]


def digest(rows):
    return hashlib.sha256(
        json.dumps(relevant(rows), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


# GitHub autolinks bare #numbers and owner/repo#numbers in prose. Ignore
# examples in code or HTML comments; referenced PRs are resolved separately.
ISSUE_REFS = re.compile(
    r"(?<![\w.])(?:https?://)?github\.com/(?P<url_owner>[\w.-]+)/(?P<url_repo>[\w.-]+)/issues/(?P<url_num>[1-9]\d{0,9})(?!\d)"
    r"|(?<![\w/])(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)#(?P<num>[1-9]\d{0,9})(?!\d)"
    r"|(?<![\\\w/#])#(?P<local>[1-9]\d{0,9})(?!\d)",
    re.IGNORECASE,
)


def issue_references(body, repo):
    """Return explicit GitHub issue/PR references in PR prose."""
    body = re.sub(r"<!--.*?-->", "", body or "", flags=re.DOTALL)
    result, fence = set(), None
    for line in body.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence:
            continue
        line = re.sub(r"`+[^`]*`+", "", line)
        for match in ISSUE_REFS.finditer(line):
            if match["url_num"]:
                target = f"{match['url_owner']}/{match['url_repo']}"
                number = match["url_num"]
            elif match["num"]:
                target = f"{match['owner']}/{match['repo']}"
                number = match["num"]
            else:
                target, number = repo, match["local"]
            number = int(number)
            if number <= 2_147_483_647:  # GitHub GraphQL Int
                result.add((target.casefold(), number))
    return result


def linked_issue_digest(pr, context, reviewer):
    """Fingerprint review inputs, excluding automated chatter and timestamps."""
    issues = []
    for entry in context["issues"]:
        issue = entry.get("issue") or {}
        comments = entry.get("comments") or []
        rows = inventory(
            {"issue_comments": comments, "review_comments": [], "reviews": []},
            reviewer,
        )
        issues.append(
            {
                "repo": entry["repo"],
                "number": entry["number"],
                "status": entry["status"],
                "issue": {
                    key: issue.get(key)
                    for key in ("id", "number", "title", "body", "state")
                },
                "comments": [
                    {key: row.get(key) for key in ("key", "author", "body")}
                    for row in relevant(rows)
                ],
            }
        )
    value = {
        "body": pr.get("body") or "",
        "complete": context["complete"],
        "link_discovery": context["link_discovery"],
        "issues": issues,
        "non_issues": context["non_issues"],
        "omitted": context["omitted"],
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def new_since(rows, timestamp):
    if not timestamp:
        return False

    def parse(value):
        if isinstance(value, (float, int)):
            return datetime.fromtimestamp(value, timezone.utc)
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    cutoff = parse(timestamp)
    return any(
        r["updated_at"] and parse(r["updated_at"]) > cutoff for r in relevant(rows)
    )


def validate_ledger(rows, ledger):
    """Require traceable handling, not agreement with either party's opinion."""
    known = {r["key"] for r in rows}
    required = {r["key"] for r in rows if r["body"].strip()}
    if not isinstance(ledger, list):
        raise ValueError("discussion-assessment.json must be a list")
    seen = set()
    dispositions = {
        "supported",
        "resolved",
        "accepted-design",
        "superseded",
        "disputed",
        "informational",
    }
    for entry in ledger:
        if not isinstance(entry, dict):
            raise ValueError("invalid discussion assessment entry")
        keys = entry.get("keys")
        if (
            not isinstance(keys, list)
            or not keys
            or any(not isinstance(k, str) for k in keys)
        ):
            raise ValueError("discussion assessment requires source keys")
        if len(set(keys)) != len(keys) or seen.intersection(keys) or set(keys) - known:
            raise ValueError("duplicate or unknown discussion source keys")
        seen.update(keys)
        if (
            entry.get("disposition") not in dispositions
            or not isinstance(entry.get("rationale"), str)
            or not entry["rationale"].strip()
        ):
            raise ValueError("discussion assessment requires disposition and rationale")
        evidence = entry.get("evidence")
        if not isinstance(evidence, list) or any(
            not isinstance(v, str) or not v.strip() for v in evidence
        ):
            raise ValueError("discussion assessment requires an evidence list")
        if entry["disposition"] != "informational" and not evidence:
            raise ValueError("substantive discussion disposition requires evidence")
    if required - seen:
        raise ValueError(
            "discussion assessment omits frozen comments: "
            + ", ".join(sorted(required - seen))
        )
