import importlib.util
import json
import re
import subprocess
import sys
from urllib.parse import urlencode
from .review_queue import Queue


MARKER = re.compile(r"<!-- mo-bot-review:v1 (\{[^\n]+\}) -->\s*\Z")


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
    for pr in prs:
        state, ci, prior = queue.classify(
            pr, config.reviewer, set(config.exclude), config.stale_ci_days
        )
        # Own PRs need a current-head published review, independently of CI.
        # Other authors continue through the authoritative green-CI predicate.
        own_unreviewed = False
        if state == "excluded" and ci == "self-authored":
            reviews = (pr.get("reviews") or {}).get("nodes")
            if not isinstance(reviews, list):
                state, ci = "indeterminate", "incomplete review evidence"
            elif any(
                r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED")
                and (r.get("commit") or {}).get("oid") == pr["headRefOid"]
                for r in reviews
            ):
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
        if state == "candidate" and (own_unreviewed or ci.startswith("fresh")):
            candidates.append(
                {**pr, "repo": config.repository, "ci": ci, "prior_review": prior}
            )
        elif state == "indeterminate":
            unknown.append(pr["number"])
        admission[str(pr["number"])] = {
            "head": pr["headRefOid"],
            "state": state,
            "reason": ci,
        }
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

    def api(self, endpoint, *, method="GET", body=None, pages=False):
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
                timeout=self.config.command_timeout_seconds,
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

    def evidence(self, job, pr):
        prefix = f"repos/{job['repo']}"
        return {
            "pr": pr,
            "issue_comments": self.comments(job),
            "review_comments": self.api(
                f"{prefix}/pulls/{job['pr']}/comments", pages=True
            ),
            "reviews": self.api(f"{prefix}/pulls/{job['pr']}/reviews", pages=True),
            "eligibility_snapshot": json.loads(job["metadata"]),
        }


if __name__ == "__main__":
    from pathlib import Path
    from .config import Config

    print(json.dumps(discover(Config.load(Path(sys.argv[1])))))
