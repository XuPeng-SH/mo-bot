"""Loopback REST + SSE. Scheduler alone mutates workflow/session ownership."""

from concurrent.futures import TimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import queue
import secrets
import threading
from urllib.parse import parse_qs, urlsplit


class API:
    def __init__(self, service):
        self.service = service
        token_path = service.config.state_dir / "api-token"
        if not token_path.exists():
            token_path.touch(mode=0o600)
            token_path.write_text(secrets.token_urlsafe(32))
        token_path.chmod(0o600)
        self.token = token_path.read_text().strip()
        if not self.token:
            raise ValueError("empty API token")
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass  # Avoid logging credentials or chat contents.

            def json(self, status, body):
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

            def authenticated(self):
                self.connection.settimeout(30)
                return hmac.compare_digest(
                    self.headers.get("Authorization", ""), "Bearer " + api.token
                )

            def do_GET(self):
                if not self.authenticated():
                    self.json(401, {"error": "bearer token required"})
                    return
                parsed = urlsplit(self.path)
                if parsed.path == "/api/events":
                    self.stream(parsed.query)
                    return
                with service.status_lock:
                    status = service.status.copy()
                routes = {
                    "/api/status": status,
                    "/api/jobs": status.get("jobs", []),
                    "/api/sessions": status.get("sessions", []),
                    "/api/pools": status.get("pools", {}),
                    "/api/models": status.get("models", []),
                    "/api/nodes": status.get("nodes", []),
                    "/api/alerts": status.get("alerts", []),
                }
                if parsed.path in routes:
                    self.json(200, routes[parsed.path])
                    return
                parts = parsed.path.strip("/").split("/")
                if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                    self.dispatch("job", {"job_id": parts[2]})
                    return
                if (
                    len(parts) == 4
                    and parts[:2] == ["api", "sessions"]
                    and parts[3] == "history"
                ):
                    try:
                        self.dispatch(
                            "history",
                            {
                                "slot": int(parts[2]),
                                "cursor": parse_qs(parsed.query).get("cursor", [None])[
                                    0
                                ],
                            },
                        )
                    except ValueError as error:
                        self.json(400, {"error": str(error)})
                    return
                self.json(404, {"error": "unknown endpoint"})

            def do_POST(self):
                if not self.authenticated():
                    self.json(401, {"error": "bearer token required"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 65536:
                        raise ValueError("request body must be 1..65536 bytes")
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError("JSON object required")
                    if self.path == "/api/jobs":
                        self.dispatch(
                            "work" if body.get("kind") == "work" else "enqueue", body
                        )
                        return
                    parts = self.path.strip("/").split("/")
                    if len(parts) == 3 and parts[:2] == ["api", "pools"]:
                        self.dispatch("resize_pool", {**body, "role": parts[2]})
                        return
                    if (
                        parts[:2] == ["api", "service"]
                        and len(parts) == 3
                        and parts[2] in ("drain", "resume")
                    ):
                        self.dispatch(
                            "drain" if parts[2] == "drain" else "resume_dispatch", body
                        )
                        return
                    if (
                        parts[:2] == ["api", "jobs"]
                        and len(parts) == 4
                        and parts[3] in ("pause", "resume", "retry", "cleanup", "rerun")
                    ):
                        self.dispatch(parts[3] + "_job", {**body, "job_id": parts[2]})
                        return
                    if (
                        len(parts) == 4
                        and parts[:2] == ["api", "jobs"]
                        and parts[3] == "reconcile"
                    ):
                        self.dispatch("reconcile", {"job_id": parts[2]})
                        return
                    if (
                        len(parts) == 4
                        and parts[:2] == ["api", "sessions"]
                        and parts[3] in ("messages", "interrupt", "model")
                    ):
                        self.dispatch(
                            "message" if parts[3] == "messages" else parts[3],
                            {**body, "slot": int(parts[2])},
                        )
                        return
                    self.json(404, {"error": "unknown endpoint"})
                except (ValueError, KeyError) as error:
                    self.json(400, {"error": str(error)})

            def dispatch(self, action, args):
                try:
                    future = service.request(action, args)
                    result = future.result(timeout=90)
                    self.json(200, result)
                except queue.Full:
                    self.json(503, {"error": "command queue full"})
                except TimeoutError:
                    canceled = future.cancel()
                    self.json(
                        504,
                        {
                            "error": "scheduler timeout",
                            "canceled_before_execution": canceled,
                            "reconcile": "GET /api/status before retrying; task admission is idempotent",
                        },
                    )
                except ValueError as error:
                    self.json(400, {"error": str(error)})
                except Exception as error:
                    self.json(503, {"error": str(error)[:2000]})

            def stream(self, query):
                try:
                    args = parse_qs(query)
                    cursor = int(
                        args.get("after", [self.headers.get("Last-Event-ID", "0")])[0]
                    )
                    thread = args.get("thread", [None])[0]
                except ValueError:
                    self.json(400, {"error": "invalid event cursor"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    while not service.stopping:
                        events, gap = service.events.since(cursor)
                        if gap:
                            self.wfile.write(
                                b'event: gap\ndata: {"action":"reload session history; events buffer is bounded"}\n\n'
                            )
                        for event in events:
                            cursor = event["sequence"]
                            if (
                                thread
                                and event.get("params", {}).get("threadId") != thread
                            ):
                                continue
                            self.wfile.write(
                                f"id: {cursor}\nevent: codex\ndata: {json.dumps(event)}\n\n".encode()
                            )
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                except (OSError, TimeoutError):
                    pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            # Slow/SSE clients cannot create unbounded threads.
            capacity = threading.BoundedSemaphore(32)

            def process_request(self, request, address):
                if not self.capacity.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, address)
                except Exception:
                    self.capacity.release()
                    raise

            def process_request_thread(self, request, address):
                try:
                    super().process_request_thread(request, address)
                finally:
                    self.capacity.release()

        self.server = Server(
            (service.config.api_host, service.config.api_port), Handler
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True, name="rest-api"
        )

    def start(self):
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
