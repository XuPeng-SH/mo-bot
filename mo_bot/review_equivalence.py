"""Prove that a reviewed head changed only by clean default-branch merges."""

import logging
import re
import subprocess
import time


LOG = logging.getLogger("mo-bot")
SHA = re.compile(r"[0-9a-f]{40}\Z")


class MergeOnlyReviews:
    def __init__(self, config):
        self.config = config
        # Discovery itself is bounded. Leave time for its other intake work.
        self.deadline = time.monotonic() + config.command_timeout_seconds / 2

    def git(self, *args, check=True):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("merge-only proof budget exhausted")
        result = subprocess.run(
            ["git", "-C", str(self.config.source_dir), *args],
            text=True,
            capture_output=True,
            timeout=remaining,
        )
        if check and result.returncode:
            raise RuntimeError(result.stderr.strip()[:500])
        return result

    def matches(self, pr, reviewed_head):
        head = pr.get("headRefOid")
        default = pr.get("_defaultBranch") or {}
        base = default.get("oid")
        if (
            not default.get("name")
            or pr.get("baseRefName") != default["name"]
            or not all(
                isinstance(s, str) and SHA.fullmatch(s)
                for s in (head, base, reviewed_head)
            )
            or head == reviewed_head
        ):
            return False
        try:
            for sha in (head, base, reviewed_head):
                if self.git(
                    "cat-file", "-e", sha + "^{commit}", check=False
                ).returncode:
                    # Only objects are fetched: no checkout, branch, or live slot
                    # ref is changed. Pin the exact discovery/review revisions.
                    self.git(
                        "fetch",
                        "--no-tags",
                        "--no-write-fetch-head",
                        "https://github.com/" + self.config.repository + ".git",
                        sha,
                    )
            current = head
            for _ in range(64):
                if current == reviewed_head:
                    return True
                fields = self.git(
                    "show", "-s", "--format=%T %P", current
                ).stdout.split()
                if len(fields) != 3:
                    return False  # An ordinary commit, root, or octopus merge.
                tree, first, second = fields
                if self.git(
                    "merge-base", "--is-ancestor", second, base, check=False
                ).returncode:
                    return False  # Merging another feature branch is new work.
                merged = self.git(
                    "merge-tree", "--write-tree", first, second, check=False
                )
                if merged.returncode or merged.stdout.splitlines()[0] != tree:
                    return False  # Conflicts or edits made inside the merge.
                current = first
            return False
        except (OSError, RuntimeError, subprocess.SubprocessError, IndexError) as error:
            LOG.warning(
                "cannot prove merge-only review PR=%s: %s", pr.get("number"), error
            )
            return False
