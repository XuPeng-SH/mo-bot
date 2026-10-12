"""Transport routing only. Jobs, retries and leases still belong to Store/Service."""

import time

from .remote import RemoteBackend
from .rpc import Backend


class Nodes:
    def __init__(self, config, events):
        self.config, self.events = config, events
        self.closed = False
        self.local = Backend(config, events)
        self.backends = {"local": self.local}
        self.definitions = {n["name"]: n for n in config.nodes}
        self.retry_at, self.errors, self.catalogs = {}, {}, {}
        self.threads = {}

    @property
    def dead(self):
        return self.closed

    def available(self, slot):
        name = dict(slot).get("node", "local")
        backend = self.backends.get(name)
        return bool(
            backend and not backend.dead and not getattr(backend, "cleanup_error", None)
        )

    def connect(self, slot):
        name = dict(slot).get("node", "local")
        if name != "local" and name not in self.definitions:
            raise ValueError("configured node missing for leased session")
        backend = self.backends.get(name)
        if backend and getattr(backend, "cleanup_error", None):
            raise RuntimeError("node fenced: " + backend.cleanup_error)
        if not backend or backend.dead:
            if time.monotonic() < self.retry_at.get(name, 0):
                raise RuntimeError(self.errors.get(name, "node reconnect backoff"))
            self.retry_at[name] = time.monotonic() + 60
            try:
                if backend:
                    backend.close()
                backend = (
                    Backend(self.config, self.events)
                    if name == "local"
                    else RemoteBackend(self.config, self.events, self.definitions[name])
                )
                catalog, cursor = [], None
                while True:
                    page = backend.call("model/list", {"cursor": cursor})
                    catalog.extend(page.get("data", []))
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
                self.backends[name] = backend
                if name == "local":
                    self.local = backend
                self.catalogs[name] = catalog
                self.errors.pop(name, None)
            except Exception as error:
                if backend:
                    backend.close()
                self.errors[name] = str(error)
                raise
        return backend

    def session(self, slot, workspace):
        backend = self.connect(slot)
        response, replaced = backend.session(slot, workspace)
        previous = slot["session_id"]
        if previous and previous != response["thread"]["id"]:
            self.threads.pop(previous, None)
        self.threads[response["thread"]["id"]] = backend
        return response, replaced

    def start(self, slot, directory, workspace, prompt, schema=None):
        backend = self.connect(slot)
        return backend.start(slot, directory, workspace, prompt, schema)

    def call(self, method, params=None):
        params = params or {}
        thread = params.get("threadId")
        if thread and thread not in self.threads:
            raise RuntimeError("native thread is not bound to a connected session node")
        backend = self.threads[thread] if thread else self.local
        return backend.call(method, params)

    def models_for(self, slot, default):
        name = dict(slot).get("node", "local")
        return self.catalogs.get(name, default if name == "local" else [])

    def close_slot(self, slot):
        backend = self.backends.get(dict(slot).get("node", "local"))
        if backend:
            backend.close()

    def status(self, local_models=None):
        return [
            {
                "name": name,
                "transport": "local" if name == "local" else "ssh",
                "ssh": self.definitions.get(name, {}).get("ssh"),
                "state": "fenced"
                if getattr(backend, "cleanup_error", None)
                else "connected"
                if backend and not backend.dead
                else "recovering",
                "error": getattr(backend, "cleanup_error", None)
                or self.errors.get(name),
                "models": self.models_for({"node": name}, local_models or []),
            }
            for name in ["local", *self.definitions]
            for backend in [self.backends.get(name)]
        ]

    def close(self):
        self.closed = True
        errors = []
        for backend in self.backends.values():
            try:
                backend.close()
            except Exception as error:
                errors.append(error)
        if errors:
            raise RuntimeError("node cleanup failed: " + str(errors[0]))
