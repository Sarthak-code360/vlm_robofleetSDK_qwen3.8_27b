"""HTTP API for the factory (standard library only).

    GET    /                                 Operations dashboard
    GET    /api/state                        machines, robots, jobs, tasks, documents, events
    GET    /api/showcase                     the 3D model shown on the Overview (factory file "showcase")
    GET    /api/machines/<id>/camera.jpg     newest camera picture of a printer (Bambu A1/P1: port 6000)
    GET    /static/...                       bundled browser files (3D viewer)

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
    GET    /api/machines/<id>/files          job files on the machine (name, size, modified, deletable)
    POST   /api/machines/<id>/files          raw *.gcode.3mf body, header X-Filename: upload from a laptop
    DELETE /api/machines/<id>/files/<name>   remove an uploaded file
    POST   /api/machines/<id>/print          {"file", "robot_id"?}: print it now; a robot collects it
    POST   /api/zones                        {"name", "x", "y"}

    AI worker chat
    POST   /api/chat                         {"text", "machine_id"?, "robot_id"?, "document_id"?} -> new messages

    Production runs (make N units autonomously)
    POST   /api/runs                         {"name", "target", "parts": {file: qty}, "description",
                                             "document_id", "deliver_to", "approve_each_job"}
    POST   /api/runs/<id>/pause|resume|stop

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
import tempfile
import threading
from pathlib import Path
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from .runtime import MAX_DOCUMENT_BYTES, Factory

MAX_PRINT_FILE_BYTES = 500 * 1024 * 1024
STATIC_DIR = Path(__file__).parent / "static"
STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json",
                ".png": "image/png", ".svg": "image/svg+xml", ".glb": "model/gltf-binary"}

ID = r"([\w.-]+)"
ROUTES = [
    ("GET", re.compile(r"^/api/state$"), "state"),
    ("GET", re.compile(r"^/api/showcase$"), "showcase"),
    ("POST", re.compile(r"^/api/requests$"), "request"),
    ("POST", re.compile(rf"^/api/jobs/{ID}/confirm$"), "confirm"),
    ("POST", re.compile(rf"^/api/jobs/{ID}/cancel$"), "cancel"),
    ("POST", re.compile(r"^/api/robots$"), "add_robots"),
    ("DELETE", re.compile(rf"^/api/robots/{ID}$"), "remove_robot"),
    ("POST", re.compile(rf"^/api/robots/{ID}/heartbeat$"), "heartbeat"),
    ("POST", re.compile(rf"^/api/tasks/{ID}/complete$"), "complete"),
    ("POST", re.compile(r"^/api/machines$"), "add_machine"),
    ("DELETE", re.compile(rf"^/api/machines/{ID}$"), "remove_machine"),
    ("GET", re.compile(rf"^/api/machines/{ID}/files$"), "machine_files"),
    ("POST", re.compile(rf"^/api/machines/{ID}/files$"), "add_machine_file"),
    ("DELETE", re.compile(rf"^/api/machines/{ID}/files/([^/]+)$"), "remove_machine_file"),
    ("POST", re.compile(rf"^/api/machines/{ID}/print$"), "print_file"),
    ("POST", re.compile(r"^/api/machines/ensure$"), "ensure_machine"),
    ("GET", re.compile(rf"^/api/machines/{ID}/camera\.jpg$"), "camera"),
    ("POST", re.compile(r"^/api/zones$"), "add_zone"),
    ("POST", re.compile(r"^/api/chat$"), "chat"),
    ("POST", re.compile(r"^/api/runs$"), "start_run"),
    ("POST", re.compile(rf"^/api/runs/{ID}/(pause|resume|stop)$"), "run_action"),
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

        def _send(self, code: int, body, content_type: str = "application/json", extra: dict | None = None,
                  cache: str = "no-store") -> None:
            if isinstance(body, bytes):
                data = body
            elif isinstance(body, str):
                data = body.encode()
            else:
                data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
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
            if method == "GET" and path.startswith("/static/"):
                return self._static(path[len("/static/"):])
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
                if name == "add_machine_file":
                    return self._send(200, self._receive_print_file(arg))
                if name == "camera":
                    result = factory.camera_frame(arg)
                    if result is None:
                        return self._send(404, {"error": "this machine has no camera"})
                    jpeg, age, error = result
                    if jpeg is None or (age is not None and age > 20):
                        return self._send(503, {"error": error or "connecting to the camera…"})
                    return self._send(200, jpeg, "image/jpeg", {"X-Frame-Age": f"{age:.1f}"})
                if name == "showcase":
                    model = factory.showcase_path()
                    if model is None:
                        raise KeyError("showcase")
                    return self._send(200, model.read_bytes(), "model/gltf-binary", cache="private, max-age=3600")
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
                    "machine_files": lambda: {"files": factory.machine_files(arg)},
                    "remove_machine_file": lambda: factory.remove_machine_file(arg, urllib.parse.unquote(match.group(2))),
                    "ensure_machine": lambda: factory.ensure_machine(str(body.get("type", ""))),
                    "print_file": lambda: factory.print_file(arg, str(body.get("file", "")), body.get("robot_id") or None),
                    "add_zone": lambda: factory.add_zone(body.get("name"), body.get("x"), body.get("y")),
                    "remove_document": lambda: factory.remove_document(arg),
                    "chat": lambda: factory.chat_message(body.get("text"), body.get("machine_id") or None,
                                                        body.get("robot_id") or None, body.get("document_id") or None,
                                                        body.get("print_file") or None),
                    "start_run": lambda: factory.start_run(body),
                    "run_action": lambda: factory.set_run_status(arg, match.group(2)),
                }
                return self._send(200, handlers[name]())
            except KeyError as e:
                return self._send(404, {"error": f"unknown: {e}"})
            except (ValueError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})

        def _static(self, rel: str) -> None:
            """Bundled browser files (the 3D viewer). Only files inside the package's static folder."""
            target = (STATIC_DIR / urllib.parse.unquote(rel)).resolve()
            if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
                return self._send(404, {"error": "not found"})
            kind = STATIC_TYPES.get(target.suffix.lower(), "application/octet-stream")
            return self._send(200, target.read_bytes(), kind, cache="public, max-age=86400")

        def _receive_print_file(self, machine_id: str) -> dict:
            """Streams a sliced file to disk (it can be large), then hands it to the factory to check and store."""
            if machine_id not in factory.machines:
                raise KeyError(machine_id)
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                raise ValueError("empty upload")
            if length > MAX_PRINT_FILE_BYTES:
                raise ValueError(f"file is larger than {MAX_PRINT_FILE_BYTES // (1024 * 1024)} MB")
            folder = factory.upload_folder(machine_id)
            folder.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".upload-", suffix=".part", dir=folder)
            try:
                with open(fd, "wb") as out:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(remaining, 1 << 20))
                        if not chunk:
                            raise ValueError("upload interrupted")
                        out.write(chunk)
                        remaining -= len(chunk)
                filename = urllib.parse.unquote(self.headers.get("X-Filename", ""))
                return factory.add_machine_file(machine_id, filename, Path(tmp))
            finally:
                Path(tmp).unlink(missing_ok=True)

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_DELETE(self):
            self._route("DELETE")

    return ThreadingHTTPServer((host, port), Handler)


def serve(factory: Factory, host: str = "127.0.0.1", port: int = 8080, token: str | None = None,
          tick_s: float = 2.0, httpd: ThreadingHTTPServer | None = None) -> None:
    """Runs the control loop and the HTTP API until Ctrl+C. Pass ``httpd`` from make_server() to bind first."""
    httpd = httpd or make_server(factory, host, port, token)
    stop = threading.Event()
    threading.Thread(target=factory.run_forever, args=(tick_s, stop), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        factory.stop_simulated_robots()
        httpd.server_close()
