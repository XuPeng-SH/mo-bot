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
import json,sys,time
for line in sys.stdin:
 r=json.loads(line);method=r.get('method');p=r.get('params',{})
 if 'id' not in r: continue
 result={}
 if method in ('thread/start','thread/resume'): result={'thread':{'id':p.get('threadId','persistent-thread')},'model':'fake','reasoningEffort':'xhigh'}
 if method=='turn/start':
  tid='turn-'+str(r['id'])
  # Late previous-turn completion precedes the new start response.
  print(json.dumps({'method':'item/completed','params':{'threadId':p['threadId'],'turnId':'old-turn','item':{'type':'agentMessage','text':'{"ready":false}','phase':'final_answer'}}}),flush=True)
  print(json.dumps({'method':'turn/completed','params':{'threadId':p['threadId'],'turn':{'id':'old-turn','status':'completed'}}}),flush=True)
  print(json.dumps({'id':r['id'],'result':{'turn':{'id':tid}}}),flush=True)
  time.sleep(.05)
  for e in [{'method':'item/completed','params':{'threadId':p['threadId'],'turnId':tid,'item':{'type':'agentMessage','text':'{"ready":true}','phase':'final_answer'}}},{'method':'turn/completed','params':{'threadId':p['threadId'],'turn':{'id':tid,'status':'completed'}}}]:print(json.dumps(e),flush=True)
  continue
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

    def test_stalled_stdin_write_is_bounded_and_process_reaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "codex"
            exe.write_text("""#!/usr/bin/python3
import json,sys,time
r=json.loads(sys.stdin.readline())
print(json.dumps({'id':r['id'],'result':{}}),flush=True)
time.sleep(10)
""")
            exe.chmod(0o755)
            cfg = Config(
                "test",
                root,
                "o/r",
                "me",
                root,
                root / "q",
                codex=str(exe),
                command_timeout_seconds=0.1,
            )
            backend = Backend(cfg, Events())
            started = time.monotonic()
            try:
                with self.assertRaises(TimeoutError):
                    backend.call("turn/start", {"input": "x" * 1000000})
                self.assertLess(time.monotonic() - started, 1)
            finally:
                backend.close()
            self.assertIsNotNone(backend.process.poll())
            self.assertFalse(backend.reader.is_alive())

    def test_malformed_envelope_resolves_pending_request_and_fences_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "codex"
            exe.write_text("""#!/usr/bin/python3
import json,sys
for line in sys.stdin:
 r=json.loads(line)
 if r.get('method')=='initialize':print(json.dumps({'id':r['id'],'result':{}}),flush=True)
 elif r.get('method')=='model/list':print('[]',flush=True)
""")
            exe.chmod(0o755)
            cfg = Config(
                "test",
                root,
                "o/r",
                "me",
                root,
                root / "q",
                codex=str(exe),
                command_timeout_seconds=1,
            )
            backend = Backend(cfg, Events())
            try:
                with self.assertRaisesRegex(RuntimeError, "disconnected"):
                    backend.call("model/list")
                self.assertTrue(backend.dead)
                self.assertFalse(backend.pending)
            finally:
                backend.close()

    def test_spawn_failure_closes_backend_log(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config("test", root, "o/r", "me", root, root / "q")
            stream = (root / "tracked.log").open("w")
            with (
                patch(
                    "mo_bot.rpc.subprocess.Popen", side_effect=OSError("spawn failed")
                ),
                patch("pathlib.Path.open", return_value=stream),
            ):
                with self.assertRaises(OSError):
                    Backend(cfg, Events())
            self.assertTrue(stream.closed)

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


class TestEventStream(unittest.TestCase):
    def test_oversize_marker_retains_session_routing(self):
        events = Events()
        cursor = events.sequence
        events.emit(
            {
                "method": "item/completed",
                "params": {"threadId": "session-1", "text": "x" * 70000},
            }
        )
        items, _ = events.since(cursor, 0)
        self.assertEqual(items[0]["method"], "event/oversize")
        self.assertEqual(items[0]["params"]["threadId"], "session-1")
        self.assertLess(len(json.dumps(items[0])), 1024)


class TestHTTP(unittest.TestCase):
    def test_pool_resize_authenticated_http_routes_to_scheduler(self):
        from concurrent.futures import Future

        with tempfile.TemporaryDirectory() as tmp:
            service = Mock()
            service.config = Mock(state_dir=Path(tmp), api_host="127.0.0.1", api_port=0)
            service.status_lock = threading.Lock()
            service.status = {"pools": {"review": {"sessions": 2}}}

            def request(action, args):
                future = Future()
                future.set_result({"action": action, **args})
                return future

            service.request.side_effect = request
            api = API(service)
            api.start()
            try:
                c = HTTPConnection("127.0.0.1", api.server.server_port, timeout=3)
                c.request("POST", "/api/pools/review", '{"sessions":3}')
                r = c.getresponse()
                self.assertEqual(r.status, 401)
                r.read()
                headers = {"Authorization": "Bearer " + api.token}
                c.request(
                    "POST",
                    "/api/pools/worker",
                    '{"sessions":4,"role":"review"}',
                    headers,
                )
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                self.assertEqual(
                    json.loads(r.read()),
                    {"action": "resize_pool", "role": "worker", "sessions": 4},
                )
                service.request.assert_called_once_with(
                    "resize_pool", {"role": "worker", "sessions": 4}
                )
                c.request("GET", "/api/pools", headers=headers)
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                self.assertEqual(json.loads(r.read())["review"]["sessions"], 2)
                c.close()
            finally:
                api.close()

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
