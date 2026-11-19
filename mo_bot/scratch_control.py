"""Inspect and explicitly select service identities for scratch cleanup."""

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path

from . import proc_inspection as inspection


def _candidate(state):
    for name in ("jobs", "scratch-trash"):
        root = state / name
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", entry.name):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
                        return root, {entry.name}
        except FileNotFoundError:
            continue
    raise OSError("no owned task or scratch-trash directory is available to inspect")


def _entry_id(boot_id, entry):
    identity = {key: entry[key] for key in inspection.TRUST_FIELDS}
    encoded = json.dumps(
        {"boot_id": boot_id, "identity": identity},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def diagnose(state):
    root, ids = _candidate(state)
    report = inspection._privileged_scan(root, ids, diagnose=True)
    for item in report["blocked"]:
        if item["eligible"]:
            item["entry_id"] = _entry_id(report["boot_id"], item)
    return report


def _write_policy(state, boot_id, entries):
    # The helper accepts only an owner-controlled directory and regular 0600
    # file. Replace atomically so readers never see a partially written policy.
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    temporary = f".scratch-trust-{secrets.token_hex(16)}"
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise OSError("scratch trust requires an owner-controlled state directory")
        if boot_id != inspection._boot_id(Path("/proc")):
            raise OSError("host boot changed during trust selection")
        policy = {"version": 1, "boot_id": boot_id, "entries": entries}
        encoded = (json.dumps(policy, sort_keys=True, indent=2) + "\n").encode()
        if len(encoded) > inspection.MAX_TRUST_BYTES:
            raise OSError("scratch trust policy exceeds size limit")
        temp_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=fd,
        )
        try:
            with os.fdopen(temp_fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, inspection.TRUST_FILE, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(fd)


def set_trust(state, selected):
    if not selected:
        _write_policy(state, inspection._boot_id(Path("/proc")), [])
        return []
    if len(selected) > 4096 or len(set(selected)) != len(selected):
        raise ValueError("select each service identity at most once")
    if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in selected):
        raise ValueError("entry IDs must be copied exactly from a diagnostic")
    report = diagnose(state)
    available = {
        item["entry_id"]: item
        for item in report["blocked"]
        if item["eligible"]
    }
    if not set(selected) <= available.keys():
        raise ValueError("a selected service identity changed; diagnose and review it again")
    entries = [
        {key: available[item][key] for key in inspection.TRUST_FIELDS}
        for item in selected
    ]
    for entry in entries:
        inspection._trust_key(entry)
    _write_policy(state, report["boot_id"], entries)
    return entries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("diagnose", help="list blocking namespace identities")
    trust = commands.add_parser(
        "set-trust", help="replace the entire trust policy with selected identities"
    )
    selection = trust.add_mutually_exclusive_group(required=True)
    selection.add_argument("--entry", action="append", help="exact diagnostic entry ID")
    selection.add_argument("--clear", action="store_true", help="revoke all trust")
    args = parser.parse_args(argv)
    try:
        state = args.state_dir.resolve(strict=True)
        if args.command == "diagnose":
            result = diagnose(state)
        else:
            entries = set_trust(state, [] if args.clear else args.entry)
            result = {"trusted": entries, "policy": str(state / inspection.TRUST_FILE)}
        print(json.dumps(result, indent=2, sort_keys=True))
    except (OSError, ValueError) as error:
        print(f"scratch control: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
