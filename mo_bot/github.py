import importlib.util
import json
import subprocess
import sys
import time
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed
from .review_queue import Queue, owner_wip
from .review_equivalence import MergeOnlyReviews
from .review_context import (
    MARKER,
    inventory,
    digest,
    new_since,
    relevant,
    issue_references,
    linked_issue_digest,
)


def current_bot_reviews(pr, reviewer, *, current_only=True):
    """Recognize published bot reviews, including earlier service identities."""
    reviews = (pr.get("reviews") or {}).get("nodes")
    if not isinstance(reviews, list):
        return None
    found = []
    for review in reviews:
        if (
            ((review.get("author") or {}).get("login") or "").casefold()
            != reviewer.casefold()
            or review.get("state") not in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED")
            or (
                current_only
                and (review.get("commit") or {}).get("oid") != pr["headRefOid"]
            )
        ):
            continue
        match = MARKER.search(review.get("body") or "")
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        if (
            isinstance(data, dict)
            and isinstance(data.get("service"), str)
            and data["service"]
            and isinstance(data.get("job"), str)
            and data["job"]
            and data.get("head") == (review.get("commit") or {}).get("oid")
            and isinstance(data.get("head"), str)
            and data.get("state") == "review"
        ):
            found.append(review)
    return found


def load_queue(path):
    spec = importlib.util.spec_from_file_location("mo_review_queue", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover(config):
    """Run in a bounded subprocess; reuse the existing authoritative predicate."""
    queue = (
        load_queue(config.queue_script)
        if config.queue_script
        else Queue(GitHub(config))
    )
    owner, repo = config.repository.split("/")
    prs = queue.all_prs(owner, repo)
    queue.stabilize_mergeability(prs, owner, repo, 3, 1)
    candidates, unknown, admission = [], [], {}
    merge_only = None
    pending_discussions = []
    for pr in prs:
        bot_reviews = current_bot_reviews(pr, config.reviewer)
        # The shared human queue may consider ordinary reviews decisive. Bot
        # admission only closes a head on an authenticated marked bot review.
        policy_pr = {**pr, "reviews": {"nodes": bot_reviews or []}}
        state, ci, prior = queue.classify(
            policy_pr, config.reviewer, set(config.exclude), config.stale_ci_days
        )
        if owner_wip(pr, config.reviewer):
            state, ci = "excluded", "owner-wip"
        # Own PRs need a current-head published review, independently of CI.
        # Other authors continue through the authoritative green-CI predicate.
        own_unreviewed = False
        if state == "excluded" and ci == "self-authored":
            reviews = (pr.get("reviews") or {}).get("nodes")
            if not isinstance(reviews, list):
                state, ci = "indeterminate", "incomplete review evidence"
            elif bot_reviews:
                state, ci = "excluded", "already-reviewed"
            elif len(reviews) >= 100:
                # The shared query uses last:100; absence in a truncated window
                # cannot prove this revision has never been reviewed.
                state, ci = "indeterminate", "incomplete review evidence"
            elif pr.get("mergeable") == "UNKNOWN":
                state, ci = "indeterminate", "mergeability unknown"
            elif pr.get("mergeable") == "MERGEABLE":
                _, status = queue.ci_state(pr, config.stale_ci_days)
                state, ci, own_unreviewed = (
                    "candidate",
                    f"own-unreviewed; CI {status}",
                    True,
                )
        if (
            state == "excluded"
            and ci == "already-reviewed"
            and pr.get("mergeable") == "MERGEABLE"
        ):
            latest_review = max(
                (r.get("submittedAt") or "" for r in bot_reviews or []), default=""
            )
            if latest_review:
                pending_discussions.append((pr, latest_review, prior))
        reviewed_head = None
        if state == "candidate" and not bot_reviews:
            previous = (
                current_bot_reviews(pr, config.reviewer, current_only=False) or []
            )
            if previous and pr.get("_defaultBranch"):
                prior_bot = max(previous, key=lambda r: r.get("submittedAt") or "")
                reviewed_head = prior_bot["commit"]["oid"]
                if merge_only is None:
                    merge_only = MergeOnlyReviews(config)
                if merge_only.matches(pr, reviewed_head):
                    state, ci = "excluded", "merge-only-reviewed"
        if state == "candidate" and (own_unreviewed or ci.startswith("fresh")):
            candidates.append(
                {
                    **pr,
                    "repo": config.repository,
                    "ci": ci,
                    "prior_review": prior,
                }
            )
        elif state == "indeterminate":
            unknown.append(pr["number"])
        admission[str(pr["number"])] = {
            "head": pr["headRefOid"],
            "state": state,
            "reason": ci,
        }
        if ci == "merge-only-reviewed":
            admission[str(pr["number"])]["reviewed_head"] = reviewed_head
    # Bounded concurrent read-only calls keep full discussion polling inside
    # the existing discovery subprocess deadline on repositories with many PRs.
    if pending_discussions:
        gh = GitHub(config)
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = {
                pool.submit(
                    gh.discussion, {"repo": config.repository, "pr": pr["number"]}
                ): (pr, cutoff, prior)
                for pr, cutoff, prior in pending_discussions
            }
            for future in as_completed(pending):
                pr, cutoff, prior = pending[future]
                try:
                    rows = inventory(future.result(), config.reviewer)
                    if new_since(rows, cutoff):
                        fingerprint = digest(rows)
                        candidates.append(
                            {
                                **pr,
                                "repo": config.repository,
                                "ci": "discussion-updated",
                                "prior_review": prior,
                                "context_review": True,
                                "discussion_digest": fingerprint,
                                "trigger_key": "discussion:" + fingerprint,
                            }
                        )
                        admission[str(pr["number"])].update(
                            state="candidate", reason="discussion-updated"
                        )
                except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                    unknown.append(pr["number"])
                    admission[str(pr["number"])].update(
                        state="indeterminate", reason="incomplete discussion evidence"
                    )
    candidates.sort(key=lambda p: p["updatedAt"])
    assigned_issues, assigned_issue_error = [], None
    try:
        gh = GitHub(config)
        if gh.author.casefold() != config.reviewer.casefold():
            raise RuntimeError(
                "GitHub authentication does not match configured issue assignee"
            )
        query = urlencode(
            {
                "state": "open",
                "assignee": config.reviewer,
                "labels": "bot-ready",
                "sort": "created",
                "direction": "asc",
                "per_page": 100,
            }
        )
        issues = gh.api(f"repos/{config.repository}/issues?{query}", pages=True)
        for issue in issues:
            if issue.get("pull_request") or issue.get("state") != "open":
                continue
            assignees = {
                (value.get("login") or "").casefold()
                for value in issue.get("assignees", [])
            }
            labels = {
                (value.get("name") or "").casefold()
                for value in issue.get("labels", [])
            }
            if (
                config.reviewer.casefold() in assignees
                and "bot-ready" in labels
                and type(issue.get("number")) is int
            ):
                assigned_issues.append(issue["number"])
        assigned_issues.sort()
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        # Issue intake is independent from PR review discovery. A GitHub issue
        # outage must not prevent the existing review queue from progressing.
        assigned_issue_error = str(error)[:2000]
    return {
        "complete": True,
        "candidates": candidates,
        "heads": {str(p["number"]): p["headRefOid"] for p in prs},
        "indeterminate": unknown,
        "admission": admission,
        "own_prs": [
            p
            for p in prs
            if (p.get("author") or {}).get("login", "").casefold()
            == config.reviewer.casefold()
            and not p.get("isDraft")
            and p["number"] not in config.exclude
            and not owner_wip(p, config.reviewer)
        ],
        "assigned_issues": assigned_issues,
        "assigned_issue_error": assigned_issue_error,
    }


def marker(service, job, state):
    payload = {
        "service": service,
        "job": job["id"],
        "head": job["head"],
        "state": state,
    }
    return (
        "<!-- mo-bot-review:v1 " + json.dumps(payload, separators=(",", ":")) + " -->"
    )


class GitHub:
    def __init__(self, config):
        self.config = config
        self.actor = self.api("user")
        self.author = self.actor["login"]

    def api(self, endpoint, *, method="GET", body=None, pages=False, timeout=None):
        args = [self.config.gh, "api", endpoint, "--method", method]
        if pages:
            args += ["--paginate"]
        if body is not None:
            args += ["--input", "-"]
        try:
            result = subprocess.run(
                args,
                input=None if body is None else json.dumps(body),
                text=True,
                capture_output=True,
                timeout=(
                    self.config.command_timeout_seconds
                    if timeout is None
                    else min(timeout, self.config.command_timeout_seconds)
                ),
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"GitHub {method} {endpoint} timed out; reconcile any attempted write"
            ) from error
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[:2000])
        if not pages:
            return json.loads(result.stdout) if result.stdout.strip() else None
        # gh 2.46 emits concatenated page arrays; --slurp is not available there.
        decoder = json.JSONDecoder()
        rest, items = result.stdout.strip(), []
        while rest:
            page, end = decoder.raw_decode(rest)
            if not isinstance(page, list):
                raise ValueError("expected an array page")
            items.extend(page)
            rest = rest[end:].lstrip()
        return items

    def pr(self, job):
        return self.api(f"repos/{job['repo']}/pulls/{job['pr']}")

    def comments(self, job):
        return self.api(
            f"repos/{job['repo']}/issues/{dict(job).get('target_number') or job['pr']}/comments",
            pages=True,
        )

    def owned_marker(self, comment, job):
        if (comment.get("user") or {}).get("login") != self.author:
            return None
        for match in MARKER.finditer(comment.get("body") or ""):
            try:
                data = json.loads(match.group(1))
            except ValueError:
                continue
            if (
                data.get("service") == self.config.service_id
                and data.get("job") == job["id"]
                and data.get("head") == job["head"]
            ):
                return data
        return None

    def find_comment(self, job):
        found = [c for c in self.comments(job) if self.owned_marker(c, job)]
        if len(found) > 1:
            raise RuntimeError(
                "multiple matching remote claims; manual reconciliation required"
            )
        return found[0] if found else None

    def ensure_comment(self, job):
        existing = self.find_comment(job)
        if existing:
            return existing
        body = self.render(job, "reviewing", "Review in progress.")
        # An uncertain write is never retried by this call. The coordinator
        # reconciles its marker before any later attempt to start the model.
        return self.api(
            f"repos/{job['repo']}/issues/{dict(job).get('target_number') or job['pr']}/comments",
            method="POST",
            body={"body": body},
        )

    def render(self, job, state, text):
        return (
            f"**mo-bot {dict(job).get('kind', 'review')} · `{self.config.service_id}` · {state}**\n\n"
            f"Commit: `{job['head']}`\n\n{text}\n\n"
            + marker(self.config.service_id, job, state)
        )

    def patch_comment(self, job, state, text):
        endpoint = f"repos/{job['repo']}/issues/comments/{job['comment_id']}"
        existing = self.api(endpoint)
        if not self.owned_marker(existing, job):
            raise RuntimeError(
                "review comment identity changed; refusing another publication"
            )
        desired = self.render(job, state, text)
        if existing["body"] != desired:
            self.api(endpoint, method="PATCH", body={"body": desired})

    def find_review(self, job):
        reviews = self.api(f"repos/{job['repo']}/pulls/{job['pr']}/reviews", pages=True)
        found = [r for r in reviews if self.owned_marker(r, job)]
        if len(found) > 1:
            raise RuntimeError(
                "multiple matching reviews; manual reconciliation required"
            )
        return found[0] if found else None

    def validate_review(self, review, job, event):
        expected = {
            "APPROVE": "APPROVED",
            "REQUEST_CHANGES": "CHANGES_REQUESTED",
            "COMMENT": "COMMENTED",
        }[event]
        owned = self.owned_marker(review, job)
        if (
            not owned
            or owned.get("state") != "review"
            or review.get("commit_id") != job["head"]
            or review.get("state") != expected
        ):
            raise RuntimeError(
                "review identity/state mismatch; manual reconciliation required"
            )

    def submit_review(self, job, event, text):
        pr = self.pr(job)
        if pr["state"] != "open" or pr["head"]["sha"] != job["head"]:
            raise RuntimeError("PR changed before review submission")
        review = self.api(
            f"repos/{job['repo']}/pulls/{job['pr']}/reviews",
            method="POST",
            body={
                "commit_id": job["head"],
                "event": event,
                "body": self.render(job, "review", text),
            },
        )
        self.validate_review(review, job, event)
        return review

    def discussion(self, job):
        prefix = f"repos/{job['repo']}"
        return {
            "issue_comments": self.comments(job),
            "review_comments": self.api(
                f"{prefix}/pulls/{job['pr']}/comments", pages=True
            ),
            "reviews": self.api(f"{prefix}/pulls/{job['pr']}/reviews", pages=True),
        }

    def closing_issue_references(self, job, fetch):
        owner, name = job["repo"].split("/")
        query = """query($owner:String!,$name:String!,$number:Int!,$cursor:String){
          repository(owner:$owner,name:$name){pullRequest(number:$number){
            closingIssuesReferences(first:100,after:$cursor){
              nodes{number repository{nameWithOwner}}
              pageInfo{hasNextPage endCursor}
            }
          }}
        }"""
        found, cursor = set(), None
        try:
            for _ in range(100):
                payload = fetch(
                    "graphql",
                    method="POST",
                    body={
                        "query": query,
                        "variables": {
                            "owner": owner,
                            "name": name,
                            "number": job["pr"],
                            "cursor": cursor,
                        },
                    },
                )
                connection = payload["data"]["repository"]["pullRequest"][
                    "closingIssuesReferences"
                ]
                for node in connection["nodes"]:
                    found.add(
                        (node["repository"]["nameWithOwner"].casefold(), node["number"])
                    )
                if payload.get("errors"):
                    raise RuntimeError("GitHub linked issue discovery returned errors")
                page = connection["pageInfo"]
                if not page["hasNextPage"]:
                    return found, None
                next_cursor = page["endCursor"]
                if not next_cursor or next_cursor == cursor:
                    raise RuntimeError("GitHub linked issue pagination did not advance")
                cursor = next_cursor
            raise RuntimeError("GitHub linked issue pagination limit reached")
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            return found, str(error)[:300]

    def linked_issues(self, job, pr, discussion=None):
        # The entire optional context fetch has a bounded cost on the
        # coordinator. The first query gets official links; the second batches
        # all body/official references, including cross-repository references.
        deadline = time.monotonic() + 30

        def fetch(endpoint, **kwargs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("linked issue evidence deadline expired")
            return self.api(endpoint, timeout=min(10, remaining), **kwargs)

        refs = issue_references(pr.get("body"), job["repo"])
        if discussion is not None:
            for row in relevant(inventory(discussion, self.author)):
                refs.update(issue_references(row["body"], job["repo"]))
        official, discovery_error = self.closing_issue_references(job, fetch)
        complete, link_discovery = True, "complete"
        errors = []
        if discovery_error is not None:
            complete = False
            link_discovery = "partial" if official else "unavailable"
            errors.append({"source": "github_links", "error": discovery_error})
        # Preserve the most authoritative references when many are mentioned.
        ordered = sorted(official) + sorted(refs - official)
        selected, omitted = ordered[:32], ordered[32:]
        if omitted:
            complete = False

        issues, non_issues = [], []
        if selected:
            fields = []
            for index, (repo, number) in enumerate(selected):
                owner, name = repo.split("/")
                fields.append(
                    f"r{index}:repository(owner:{json.dumps(owner)},name:{json.dumps(name)})"
                    f"{{target:issueOrPullRequest(number:{number}){{__typename "
                    "... on PullRequest{number} ... on Issue{databaseId number title body "
                    "state url updatedAt comments(first:100){nodes{databaseId body url "
                    "createdAt updatedAt author{login __typename}} "
                    "pageInfo{hasNextPage endCursor}}}}}"
                )
            try:
                payload = fetch(
                    "graphql",
                    method="POST",
                    body={"query": "query{" + " ".join(fields) + "}"},
                )
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("data") or {}, dict
                ):
                    raise ValueError("invalid GitHub issue batch response")
                data = payload.get("data") or {}
                if payload.get("errors"):
                    complete = False
                    errors.append(
                        {"source": "issue_batch", "error": "GraphQL returned errors"}
                    )
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                data = {}
                errors.append({"source": "issue_batch", "error": str(error)[:300]})

            for index, (repo, number) in enumerate(selected):
                entry = {"repo": repo, "number": number, "status": "ok"}
                try:
                    repository = data.get(f"r{index}")
                    value = (
                        repository.get("target")
                        if isinstance(repository, dict)
                        else None
                    )
                    if not isinstance(value, dict) or value.get("number") != number:
                        raise ValueError("referenced issue unavailable")
                    if value.get("__typename") == "PullRequest":
                        non_issues.append({"repo": repo, "number": number})
                        continue
                    if value.get("__typename") != "Issue":
                        raise ValueError("unknown referenced object")
                    entry["issue"] = {
                        "id": value["databaseId"],
                        "number": value["number"],
                        "title": value["title"],
                        "body": value["body"],
                        "state": value["state"],
                        "html_url": value["url"],
                        "updated_at": value["updatedAt"],
                    }
                    connection = value["comments"]
                    entry["comments"] = [
                        {
                            "id": node["databaseId"],
                            "body": node.get("body"),
                            "html_url": node.get("url"),
                            "created_at": node.get("createdAt"),
                            "updated_at": node.get("updatedAt"),
                            "user": {
                                "login": (node.get("author") or {}).get("login") or "",
                                "type": (node.get("author") or {}).get("__typename")
                                or "",
                            },
                        }
                        for node in connection["nodes"]
                    ]
                    if connection["pageInfo"]["hasNextPage"]:
                        # Reuse the established REST paginator for long threads.
                        entry["comments"] = fetch(
                            f"repos/{repo}/issues/{number}/comments", pages=True
                        )
                except (
                    OSError,
                    RuntimeError,
                    ValueError,
                    KeyError,
                    TypeError,
                ) as error:
                    complete = False
                    entry["status"] = "partial" if "issue" in entry else "unavailable"
                    entry["error"] = str(error)[:300]
                issues.append(entry)
        return {
            "complete": complete,
            "link_discovery": link_discovery,
            "issues": issues,
            "non_issues": non_issues,
            "omitted": [{"repo": repo, "number": number} for repo, number in omitted],
            "errors": errors,
        }

    def evidence(self, job, pr):
        discussion = self.discussion(job)
        rows = inventory(discussion, self.author)
        issues = self.linked_issues(job, pr, discussion)
        return {
            "pr": pr,
            **discussion,
            "discussion_index": rows,
            "discussion_digest": digest(rows),
            "linked_issues": issues,
            "linked_issue_digest": linked_issue_digest(pr, issues, self.author),
            "eligibility_snapshot": json.loads(job["metadata"]),
        }


if __name__ == "__main__":
    from pathlib import Path
    from .config import Config

    print(json.dumps(discover(Config.load(Path(sys.argv[1])))))
