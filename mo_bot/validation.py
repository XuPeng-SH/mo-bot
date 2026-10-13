"""Foreground checks and private test runtime for coordinator-owned tasks."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time


def prepare(directory):
    directory = Path(directory).resolve()
    runtime = directory / "tmp" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    base = [sys.executable, str(Path(__file__).resolve()), "--runtime", str(runtime)]
    for name, args in (
        ("check-run", []),
        ("test-exec", ["--isolated", "--"]),
    ):
        launcher = directory / name
        launcher.write_text("#!/bin/sh\nexec " + shlex.join(base + args) + ' "$@"\n')
        launcher.chmod(0o700)
    return (
        f"Shared validation executors: {directory}/check-run and {directory}/test-exec. "
        f"Before compiling tests requiring private /tmp or network isolation, run "
        f"{shlex.quote(str(directory / 'check-run'))} --preflight. "
        f"Then run Go tests with -exec={shlex.quote(str(directory / 'test-exec'))}; "
        "each test binary gets a dedicated runtime child, private /tmp and loopback. "
        "Use check-run --timeout SECONDS -- COMMAND for bounded foreground checks. "
        f"Each check-run saves a command/source/exit receipt under {directory}/checks; "
        "final review must match claimed checks to these receipts and retained logs. "
        "Never bind the whole task tmp directory over /tmp or invent another execution "
        "wrapper. Preserve stdout/stderr and terminal receipts outside tmp. "
        "Preflight failure is an environment diagnostic: keep the candidate and "
        "matching build/evidence, continue independent review, and resume only missing "
        "validation after recovery; it is neither a product-test failure nor a pass.\n"
    )


def run(command, timeout):
    """Own a process group so timeout/cancellation also stops child tools."""
    process = subprocess.Popen(command, start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"check timeout after {timeout}s", file=sys.stderr)
        return 124
    finally:
        # Even a completed parent can leave descendants running. They belong to
        # this invocation, never to another task or the host's global processes.
        try:
            os.killpg(process.pid, signal.SIGTERM)
            if process.poll() is None:
                process.wait(timeout=5)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            pass
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def inner(runtime, command):
    identity = runtime.stat()
    subprocess.run(["mount", "--make-rprivate", "/"], check=True)
    subprocess.run(["mount", "--bind", str(runtime), "/tmp"], check=True)
    mounted = Path("/tmp").stat()
    if (identity.st_dev, identity.st_ino) != (mounted.st_dev, mounted.st_ino):
        raise RuntimeError("private /tmp does not match assigned runtime")
    with tempfile.TemporaryFile(dir="/tmp") as probe:
        probe.write(b"preflight")
        probe.flush()
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    os.environ["TMPDIR"] = "/tmp"
    if command:
        os.execvpe(command[0], command, os.environ)
    print("private /tmp and loopback preflight passed", flush=True)
    return 0


def source_state():
    """Fingerprint the actual checkout, including newly added regression tests."""
    try:

        def git(*args):
            return subprocess.check_output(
                ["git", *args], stderr=subprocess.DEVNULL, timeout=15
            )

        delta = git("diff", "HEAD", "--binary")
        remaining = 8 * 1024 * 1024 - len(delta)
        if remaining < 0:
            raise OSError(
                "source fingerprint exceeds 8 MiB; retain a scoped evidence ledger"
            )
        fingerprint = hashlib.sha256(delta)
        root = Path(os.fsdecode(git("rev-parse", "--show-toplevel")).strip())
        for name in git("ls-files", "--others", "--exclude-standard", "-z").split(
            b"\0"
        ):
            if not name:
                continue
            path = root / os.fsdecode(name)
            fingerprint.update(name + b"\0")
            if path.is_symlink():
                fingerprint.update(os.fsencode(os.readlink(path)))
            else:
                if path.stat().st_size > remaining:
                    raise OSError(
                        "untracked source exceeds fingerprint budget; retain a scoped evidence ledger"
                    )
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise OSError(
                                "untracked source grew beyond fingerprint budget"
                            )
                        fingerprint.update(chunk)
        return {
            "head": git("rev-parse", "HEAD").decode().strip(),
            "delta_sha256": fingerprint.hexdigest(),
        }
    except (OSError, subprocess.SubprocessError) as error:
        return {"unavailable": str(error)}


def execute(args, command):
    if args.inner:
        return inner(args.runtime, command)
    if not (args.isolated or args.preflight):
        return run(command, args.timeout)
    missing = [name for name in ("unshare", "mount", "ip") if not shutil.which(name)]
    if missing:
        raise RuntimeError("isolation tools unavailable: " + ", ".join(missing))
    args.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Each binary owns a separate leaf; compilation caches remain outside it.
    with tempfile.TemporaryDirectory(prefix="run-", dir=args.runtime) as runtime:
        invocation = [
            "unshare",
            "--user",
            "--map-root-user",
            "--mount",
            "--net",
            sys.executable,
            str(Path(__file__).resolve()),
            "--runtime",
            runtime,
            "--inner",
        ]
        invocation += ["--preflight"] if args.preflight else ["--", *command]
        return run(
            invocation, min(args.timeout, 15) if args.preflight else args.timeout
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--isolated", action="store_true")
    parser.add_argument("--inner", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not 1 <= args.timeout <= 86400:
        parser.error("timeout must be 1..86400 seconds")
    if not command and not args.preflight:
        parser.error("a command or --preflight is required")
    if args.preflight and command:
        parser.error("preflight does not execute a command; run the check separately")

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    receipt = {
        "command": command,
        "cwd": str(Path.cwd()),
        "kind": "preflight" if args.preflight else "check",
        "started_at": time.time(),
        "timeout": args.timeout,
    }
    try:
        if not (args.inner or args.isolated):
            receipt["source"] = source_state()
        code = execute(args, command)
    except KeyboardInterrupt:
        code = 130
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"check environment/setup error: {error}", file=sys.stderr)
        code = 125
        receipt["setup_error"] = str(error)
    if not (args.inner or args.isolated):
        receipt.update(exit_code=code, finished_at=time.time())
        directory = args.runtime.parent.parent / "checks"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (str(time.time_ns()) + "-" + str(os.getpid()))
            path.with_suffix(".tmp").write_text(json.dumps(receipt, indent=2))
            path.with_suffix(".tmp").rename(path.with_suffix(".json"))
        except OSError as error:
            print(f"validation receipt unavailable: {error}", file=sys.stderr)
            return 125
    return code


if __name__ == "__main__":
    sys.exit(main())
