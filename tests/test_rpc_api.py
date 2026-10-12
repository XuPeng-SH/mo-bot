from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from mo_bot.api import API
from mo_bot.config import Config
from mo_bot.rpc import Backend, Events


class TestRPC(unittest.TestCase):
    def test_stdio_responses_events_reuse_and_disconnect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "fake-codex"
            executable.write_text("""#!/usr/bin/python3
import json,sys
for line in sys.stdin:
 r=json.loads(line);method=r.get('method');p=r.get('params',{})
 if 'id' not in r: continue
 result={}
 if method in ('thread/start','thread/resume'): result={'thread':{'id':p.get('threadId','persistent-thread')},'model':'fake','reasoningEffort':'xhigh'}
 if method=='turn/start':
  result={'turn':{'id':'turn-'+str(r['id'])}}
  tid=result['turn']['id']
  # Intentionally emit notifications BEFORE the start response.
  for e in [{'method':'item/completed','params':{'threadId':p['threadId'],'turnId':tid,'item':{'type':'agentMessage','text':'{"ready":true}','phase':'final_answer'}}},{'method':'turn/completed','params':{'threadId':p['threadId'],'turn':{'id':tid,'status':'completed'}}}]:print(json.dumps(e),flush=True)
 print(json.dumps({'id':r['id'],'result':result}),flush=True)
""")
            executable.chmod(0o755)
            cfg = Config(
                "test",
                root,
                "o/r",
                "me",
                root,
                root / "q",
                codex=str(executable),
                command_timeout_seconds=2,
            )
            backend = Backend(cfg, Events())
            try:
                response, _ = backend.session({"session_id": None}, root)
                slot = {
                    "session_id": response["thread"]["id"],
                    "model": "fake",
                    "effort": "xhigh",
                }
                for attempt in range(2):
                    turn = backend.start(
                        slot, root / str(attempt), root, "ready", {"type": "object"}
                    )
                    deadline = time.monotonic() + 2
                    while not turn.completed and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(turn.result(), {"ready": True})
                    self.assertEqual(turn.session_id, "persistent-thread")
                    turn.release()
                backend.process.kill()
                backend.reader.join(2)
                self.assertTrue(backend.dead)
                with self.assertRaises(RuntimeError):
                    backend.call("model/list")
            finally:
                backend.close()

    def test_missing_session_is_replaced_but_provider_failure_is_not(self):
        backend = object.__new__(Backend)
        backend.config = Mock(model="model", reasoning_effort="xhigh")
        backend.call = Mock(
            side_effect=[RuntimeError("thread not found"), {"thread": {"id": "new"}}]
        )
        _, replaced = backend.session({"session_id": "old"}, Path("/tmp/source"))
        self.assertTrue(replaced)
        self.assertEqual(backend.call.call_args.args[0], "thread/start")
        backend.call = Mock(side_effect=RuntimeError("unauthorized provider"))
        with self.assertRaises(RuntimeError):
            backend.session({"session_id": "old"}, Path("/tmp/source"))
        self.assertEqual(backend.call.call_count, 1)


class TestPagination(unittest.TestCase):
    def test_old_gh_concatenated_page_arrays(self):
        from mo_bot.github import GitHub
        from unittest.mock import patch

        gh = object.__new__(GitHub)
        gh.config = Mock(gh="gh", command_timeout_seconds=3)
        with patch(
            "mo_bot.github.subprocess.run",
            return_value=Mock(
                returncode=0, stdout='[{"id":1}]\n[{"id":2}]\n[]', stderr=""
            ),
        ) as run:
            self.assertEqual(gh.api("comments", pages=True), [{"id": 1}, {"id": 2}])
            self.assertNotIn("--slurp", run.call_args.args[0])


class TestHTTP(unittest.TestCase):
    def test_auth_status_and_force_enqueue_real_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            from concurrent.futures import Future

            service = Mock()
            service.config = Mock(state_dir=Path(tmp), api_host="127.0.0.1", api_port=0)
            service.status_lock = threading.Lock()
            service.status = {
                "sessions": [{"id": 0, "model": "gpt-6.1-sol"}],
                "jobs": [],
            }

            def request(action, args):
                future = Future()
                future.set_result({"enqueued": True, "pr": args["pr"]})
                return future

            service.request = request
            api = API(service)
            api.start()
            port = api.server.server_port
            try:
                c = HTTPConnection("127.0.0.1", port, timeout=3)
                c.request("GET", "/api/status")
                r = c.getresponse()
                self.assertEqual(r.status, 401)
                r.read()
                headers = {"Authorization": "Bearer " + api.token}
                c.request("GET", "/api/sessions", headers=headers)
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                self.assertEqual(json.loads(r.read())[0]["model"], "gpt-6.1-sol")
                c.request(
                    "POST", "/api/jobs", json.dumps({"pr": 1, "force": True}), headers
                )
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                self.assertTrue(json.loads(r.read())["enqueued"])
                c.request("POST", "/api/jobs", "[]", headers)
                r = c.getresponse()
                self.assertEqual(r.status, 400)
                r.read()
                c.close()
                self.assertEqual(
                    (Path(tmp) / "api-token").stat().st_mode & 0o777, 0o600
                )
            finally:
                api.close()


if __name__ == "__main__":
    unittest.main()
