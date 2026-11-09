"""The session service: `python3 -m app.server` (listens on 127.0.0.1:8080).

GET  /healthz
GET  /stats                     session count and user id types
GET  /session?token=T           one session
POST /login?user=U-000123       create a session for a user, returns {"token": ...}
POST /admin/migrate  {"name"}   run migrations/<name>.py's migrate(STORE) inside this process
POST /admin/reload   {"module"} re-import a module (e.g. "app.sessions") so code changes on disk
                                take effect without a restart
"""

import importlib
import importlib.util
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from app import sessions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED = "/var/lib/sessions/seed.json"
LOCK = threading.RLock()


def load_seed() -> None:
    """One-shot import of the sessions exported from the old cluster (which is gone)."""
    if os.path.exists(SEED):
        with open(SEED) as f:
            sessions.STORE.update(json.load(f))
        os.remove(SEED)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("content-length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        with LOCK:
            store = sessions.STORE
            if u.path == "/healthz":
                return self._send(200, {"ok": True})
            if u.path == "/stats":
                types: dict = {}
                for s in store.values():
                    t = type(s["user"]).__name__
                    types[t] = types.get(t, 0) + 1
                return self._send(200, {"sessions": len(store), "user_id_types": types})
            if u.path == "/session":
                s = sessions.get(q.get("token", [""])[0])
                return self._send(200, s) if s else self._send(404, {"error": "no such session"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            with LOCK:
                if u.path == "/login":
                    return self._send(200, {"token": sessions.new_session(q["user"][0])})
                if u.path == "/admin/migrate":
                    name = self._body()["name"]
                    path = os.path.join(ROOT, "migrations", f"{name}.py")
                    spec = importlib.util.spec_from_file_location(f"migration_{name}", path)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    return self._send(200, {"migration": name, "result": mod.migrate(sessions.STORE)})
                if u.path == "/admin/reload":
                    name = self._body()["module"]
                    importlib.reload(sys.modules[name])
                    return self._send(200, {"reloaded": name})
        except Exception:
            return self._send(500, {"error": traceback.format_exc()})
        self._send(404, {"error": "not found"})


def main() -> None:
    load_seed()
    ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
