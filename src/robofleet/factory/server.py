"""HTTP API for the factory (standard library only).

    GET    /                                 Operations dashboard
    GET    /api/state                        machines, robots, jobs, tasks, documents, events

    Work orders
    POST   /api/requests                     {"text", "document_id"?, "deliver_to"?, "priority"?} -> job or task
    POST   /api/jobs/<id>/confirm            a person approves the machine start
    POST   /api/jobs/<id>/cancel             cancel a job awaiting approval

    Robots
    POST   /api/robots                       register robot(s): {"id"?, "name", "kind", "model", "home_zone",
                                             "payload_kg", "capabilities", "connection", "battery", "count"}
    DELETE /api/robots/<id>
    POST   /api/robots/<id>/heartbeat        robot state in; next task + instructions out
           {"zone", "battery", "status": idle|busy|charging|fault, ...}
    POST   /api/tasks/<id>/complete          {"robot_id", "ok": true|false, "note"}

    Machines and layout
    POST   /api/machines                     {"id", "name", "type", "driver", "zone", ...driver fields}
    DELETE /api/machines/<id>
    POST   /api/zones                        {"name", "x", "y"}

    Documents (reference PDFs for work orders)
    POST   /api/documents                    raw PDF body, header X-Filename
    GET    /api/documents/<id>               the PDF
    DELETE /api/documents/<id>

If a token is set, every /api call needs the header ``Authorization: Bearer <token>``
(the dashboard reads it from the page address: http://host:8080/#token=<token>;
document links may pass it as ``?token=``).
"""

from __future__ import annotations

import hmac
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from .runtime import MAX_DOCUMENT_BYTES, Factory

ID = r"([\w.-]+)"
ROUTES = [
    ("GET", re.compile(r"^/api/state$"), "state"),
    ("POST", re.compile(r"^/api/requests$"), "request"),
    ("POST", re.compile(rf"^/api/jobs/{ID}/confirm$"), "confirm"),
    ("POST", re.compile(rf"^/api/jobs/{ID}/cancel$"), "cancel"),
    ("POST", re.compile(r"^/api/robots$"), "add_robots"),
    ("DELETE", re.compile(rf"^/api/robots/{ID}$"), "remove_robot"),
    ("POST", re.compile(rf"^/api/robots/{ID}/heartbeat$"), "heartbeat"),
    ("POST", re.compile(rf"^/api/tasks/{ID}/complete$"), "complete"),
    ("POST", re.compile(r"^/api/machines$"), "add_machine"),
    ("DELETE", re.compile(rf"^/api/machines/{ID}$"), "remove_machine"),
    ("POST", re.compile(r"^/api/zones$"), "add_zone"),
    ("POST", re.compile(r"^/api/documents$"), "add_document"),
    ("GET", re.compile(rf"^/api/documents/{ID}$"), "get_document"),
    ("DELETE", re.compile(rf"^/api/documents/{ID}$"), "remove_document"),
]


def dashboard_html() -> str:
    return resources.files(__package__).joinpath("dashboard.html").read_text(encoding="utf-8")


def make_server(factory: Factory, host: str = "127.0.0.1", port: int = 8080, token: str | None = None) -> ThreadingHTTPServer:
    page = dashboard_html()

    class Handler(BaseHTTPRequestHandler):
        server_version = "robofleet"

        def log_message(self, *args):
            pass

        def _send(self, code: int, body, content_type: str = "application/json", extra: dict | None = None) -> None:
            if isinstance(body, bytes):
                data = body
            elif isinstance(body, str):
                data = body.encode()
            else:
                data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self, query: dict) -> bool:
            if not token:
                return True
            given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not given and query.get("token"):
                given = query["token"][0]
            return hmac.compare_digest(given.encode(), token.encode())

        def _raw(self, limit: int) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length > limit:
                raise ValueError(f"body larger than {limit // (1024 * 1024)} MB")
            return self.rfile.read(length)

        def _body(self) -> dict:
            data = json.loads(self._raw(1_000_000) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            return data

        def _route(self, method: str) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
            if method == "GET" and path in {"/", "/index.html"}:
                return self._send(200, page, "text/html; charset=utf-8")
            for m, pattern, name in ROUTES:
                match = pattern.match(path)
                if m == method and match:
                    break
            else:
                return self._send(404, {"error": "not found"})
            if not self._authorized(query):
                return self._send(401, {"error": "missing or wrong token"})
            arg = match.group(1) if match.groups() else None
            try:
                if name == "add_document":
                    filename = urllib.parse.unquote(self.headers.get("X-Filename", "document.pdf"))
                    return self._send(200, factory.add_document(filename, self._raw(MAX_DOCUMENT_BYTES + 1)))
                if name == "get_document":
                    doc = factory.documents.get(arg)
                    if doc is None:
                        raise KeyError(arg)
                    disposition = f"inline; filename*=UTF-8''{urllib.parse.quote(doc['name'])}"
                    return self._send(200, factory.document_path(arg).read_bytes(), "application/pdf",
                                      {"Content-Disposition": disposition})
                body = self._body() if method == "POST" else {}
                if name == "state":
                    return self._send(200, factory.state())
                if name == "request":
                    text = str(body.get("text", "")).strip()
                    if not text:
                        return self._send(400, {"error": "text is required"})
                    return self._send(200, factory.submit_request(
                        text, document_id=body.get("document_id") or None, deliver_to=body.get("deliver_to") or None,
                        priority=body.get("priority") or "normal"))
                handlers = {
                    "confirm": lambda: factory.confirm_job(arg),
                    "cancel": lambda: factory.cancel_job(arg),
                    "add_robots": lambda: {"robots": factory.add_robots(body)},
                    "remove_robot": lambda: factory.remove_robot(arg),
                    "heartbeat": lambda: factory.heartbeat(arg, body),
                    "complete": lambda: factory.complete_task(arg, str(body.get("robot_id", "")),
                                                              bool(body.get("ok", True)), str(body.get("note", ""))),
                    "add_machine": lambda: factory.add_machine(body),
                    "remove_machine": lambda: factory.remove_machine(arg),
                    "add_zone": lambda: factory.add_zone(body.get("name"), body.get("x"), body.get("y")),
                    "remove_document": lambda: factory.remove_document(arg),
                }
                return self._send(200, handlers[name]())
            except KeyError as e:
                return self._send(404, {"error": f"unknown: {e}"})
            except (ValueError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_DELETE(self):
            self._route("DELETE")

    return ThreadingHTTPServer((host, port), Handler)


def serve(factory: Factory, host: str = "127.0.0.1", port: int = 8080, token: str | None = None,
          tick_s: float = 2.0) -> None:
    """Runs the control loop and the HTTP API until Ctrl+C."""
    stop = threading.Event()
    threading.Thread(target=factory.run_forever, args=(tick_s, stop), daemon=True).start()
    httpd = make_server(factory, host, port, token)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        factory.stop_simulated_robots()
        httpd.server_close()
