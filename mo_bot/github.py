import importlib.util
import json
import re
import subprocess
import sys


MARKER = re.compile(r"<!-- mo-bot-review:v1 (\{[^\n]+\}) -->\s*\Z")


def load_queue(path):
    spec = importlib.util.spec_from_file_location("mo_review_queue", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover(config):
    """Run in a bounded subprocess; reuse the existing authoritative predicate."""
    queue = load_queue(config.queue_script)
    owner, repo = config.repository.split("/")
    prs = queue.all_prs(owner, repo)
    queue.stabilize_mergeability(prs, owner, repo, 3, 1)
    candidates, unknown = [], []
    for pr in prs:
        state, ci, prior = queue.classify(
            pr, config.reviewer, set(config.exclude), config.stale_ci_days
        )
        if state == "candidate" and ci.startswith("fresh"):
            candidates.append(
                {**pr, "repo": config.repository, "ci": ci, "prior_review": prior}
            )
        elif state == "indeterminate":
            unknown.append(pr["number"])
    candidates.sort(key=lambda p: p["updatedAt"])
    return {
        "candidates": candidates,
        "heads": {str(p["number"]): p["headRefOid"] for p in prs},
        "indeterminate": unknown,
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
        self.author = self.api("user")["login"]

    def api(self, endpoint, *, method="GET", body=None, pages=False):
        args = [self.config.gh, "api", endpoint, "--method", method]
        if pages:
            args += ["--paginate"]
        if body is not None:
            args += ["--input", "-"]
        result = subprocess.run(
            args,
            input=None if body is None else json.dumps(body),
            text=True,
            capture_output=True,
            timeout=self.config.command_timeout_seconds,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[:2000])
        if not pages:
            return json.loads(result.stdout)
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
        return self.api(f"repos/{job['repo']}/issues/{job['pr']}/comments", pages=True)

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
            f"repos/{job['repo']}/issues/{job['pr']}/comments",
            method="POST",
            body={"body": body},
        )

    def render(self, job, state, text):
        return (
            f"**mo-bot review · `{self.config.service_id}` · {state}**\n\n"
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
