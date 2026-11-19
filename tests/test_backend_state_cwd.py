import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TestBackendStateCwd(unittest.TestCase):
    def test_backend_started_from_state_dir_uses_neutral_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            binary_dir = state / "bin"
            binary_dir.mkdir(parents=True)
            external = Path(tmp) / "external"
            (external / "target").mkdir(parents=True)
            (state / "link").symlink_to(external / "target", target_is_directory=True)
            binary = binary_dir / "fake-codex"
            binary.write_text(
                """#!/usr/bin/python3
import json, os, sys
from pathlib import Path
Path(os.environ['MO_BOT_TEST_MARKER']).write_text(json.dumps({'cwd': os.getcwd(), 'path': os.environ['PATH'], 'pwd': os.environ['PWD'], 'codex_home': os.path.realpath(os.environ['CODEX_HOME']), 'library_path': os.environ['LD_LIBRARY_PATH']}))
for line in sys.stdin:
    request = json.loads(line)
    if 'id' in request:
        print(json.dumps({'id': request['id'], 'result': {}}), flush=True)
"""
            )
            binary.chmod(0o755)
            script = """
import sys
from pathlib import Path
from mo_bot.config import Config
from mo_bot.rpc import Backend, Events
state = Path(sys.argv[1])
nested = sys.argv[2] == 'nested'
outside = sys.argv[2] == 'outside'
custom = sys.argv[2] == 'custom'
backend_state = state / 'nodes' / 'remote' if nested else state
backend_state.mkdir(parents=True, exist_ok=True)
executable = str(state / 'bin' / 'fake-codex') if outside else 'fake-codex'
config = Config('test', backend_state, 'o/r', 'me', state, codex=executable, command_timeout_seconds=2)
kwargs = {}
if custom:
    kwargs['command'] = [sys.executable, 'bin/fake-codex']
elif nested:
    kwargs.update(command=['fake-codex'], owner_state_dir=state)
backend = Backend(config, Events(), **kwargs)
try:
    assert backend.process.poll() is None
finally:
    backend.close()
"""
            outside = Path(tmp) / "outside"
            outside.mkdir()
            for mode in ("local", "nested", "outside", "custom"):
                marker = state / f"{mode}.json"
                env = os.environ.copy()
                env["PATH"] = os.pathsep.join(
                    ("bin", "link/../tools", env.get("PATH", os.defpath))
                )
                env["PYTHONPATH"] = os.pathsep.join(
                    filter(
                        None,
                        (
                            str(Path(__file__).resolve().parent.parent),
                            env.get("PYTHONPATH"),
                        ),
                    )
                )
                env["MO_BOT_TEST_MARKER"] = str(marker)
                env["PWD"] = str(outside if mode == "outside" else state)
                env["CODEX_HOME"] = "link/../codex-local"
                env["LD_LIBRARY_PATH"] = "$ORIGIN/../lib:relative-lib"
                subprocess.run(
                    [sys.executable, "-c", script, str(state), mode],
                    cwd=outside if mode == "outside" else state,
                    env=env,
                    check=True,
                    timeout=10,
                )
                result = json.loads(marker.read_text())
                expected_cwd = (
                    outside
                    if mode == "outside"
                    else state
                    if mode == "custom"
                    else state.parent
                )
                self.assertEqual(result["cwd"], str(expected_cwd))
                self.assertEqual(result["pwd"], str(expected_cwd))
                self.assertEqual(
                    result["path"].split(os.pathsep)[0],
                    "bin" if mode in ("outside", "custom") else str(binary_dir),
                )
                self.assertEqual(
                    result["path"].split(os.pathsep)[1],
                    "link/../tools"
                    if mode in ("outside", "custom")
                    else str(state / "link/../tools"),
                )
                self.assertEqual(
                    result["codex_home"],
                    str((outside if mode == "outside" else external) / "codex-local"),
                )
                self.assertEqual(
                    result["library_path"],
                    "$ORIGIN/../lib:"
                    + (
                        "relative-lib"
                        if mode in ("outside", "custom")
                        else str(state / "relative-lib")
                    ),
                )


if __name__ == "__main__":
    unittest.main()
