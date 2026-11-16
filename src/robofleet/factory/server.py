"""HTTP API for the factory brain (standard library only).

    GET  /                               live dashboard
    GET  /api/state                      machines, robots, jobs, tasks, events
    POST /api/requests        {"text"}   user request -> model -> job or task
    POST /api/jobs/<id>/confirm          person confirms the machine may start
    POST /api/robots/<id>/heartbeat      robot state in; next task + instructions out
         {"zone", "battery", "status": idle|busy|charging|fault, ...}
    POST /api/tasks/<id>/complete        {"robot_id", "ok": true|false, "note"}

If a token is set, every /api call needs the header ``Authorization: Bearer <token>``
(the dashboard reads it from the page address: http://host:8080/#token=<token>).
"""

from __future__ import annotations

import hmac
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .runtime import Factory

ROUTES = [
    ("POST", re.compile(r"^/api/requests$"), "request"),
    ("POST", re.compile(r"^/api/jobs/([\w.-]+)/confirm$"), "confirm"),
    ("POST", re.compile(r"^/api/robots/([\w.-]+)/heartbeat$"), "heartbeat"),
    ("POST", re.compile(r"^/api/tasks/([\w.-]+)/complete$"), "complete"),
    ("GET", re.compile(r"^/api/state$"), "state"),
]


def make_server(factory: Factory, host: str = "127.0.0.1", port: int = 8080, token: str | None = None) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "robofleet"

        def log_message(self, *args):
            pass

        def _send(self, code: int, body, content_type: str = "application/json") -> None:
            data = body.encode() if isinstance(body, str) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            if not token:
                return True
            given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            return hmac.compare_digest(given, token)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                raise ValueError("body too large")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            return data

        def _route(self, method: str) -> None:
            path = self.path.split("?")[0]
            if method == "GET" and path in {"/", "/index.html"}:
                return self._send(200, DASHBOARD_HTML, "text/html; charset=utf-8")
            for m, pattern, name in ROUTES:
                match = pattern.match(path)
                if m == method and match:
                    break
            else:
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return self._send(401, {"error": "missing or wrong token"})
            try:
                body = self._body() if method == "POST" else {}
                if name == "state":
                    return self._send(200, factory.state())
                if name == "request":
                    text = str(body.get("text", "")).strip()
                    if not text:
                        return self._send(400, {"error": "text is required"})
                    return self._send(200, factory.submit_request(text))
                if name == "confirm":
                    return self._send(200, factory.confirm_job(match.group(1)))
                if name == "heartbeat":
                    return self._send(200, factory.heartbeat(match.group(1), body))
                if name == "complete":
                    return self._send(200, factory.complete_task(match.group(1), str(body.get("robot_id", "")),
                                                                 bool(body.get("ok", True)), str(body.get("note", ""))))
            except KeyError as e:
                return self._send(404, {"error": f"unknown: {e}"})
            except (ValueError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

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
        httpd.server_close()


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Factory Brain</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#667085;--line:#e4e7ec;--accent:#2563eb;--ok:#15803d;--warn:#b45309;--bad:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#171b23;--text:#e6e9ef;--muted:#98a2b3;--line:#2a303c;--accent:#60a5fa;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1200px;margin:0 auto;padding:16px}h1{font-size:20px;margin:0 0 2px}h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:0 0 8px}
.sub{color:var(--muted);margin-bottom:14px}.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;overflow-x:auto}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);font-weight:500}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid currentColor}
.idle,.done,.delivered{color:var(--ok)}.busy,.running,.starting,.assigned,.in_progress,.awaiting_confirmation,.pending{color:var(--warn)}.offline,.fault,.failed,.error,.delivery_failed{color:var(--bad)}.charging{color:var(--accent)}
form{display:flex;gap:8px;margin-bottom:14px}input{flex:1;padding:10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);font:inherit}
button{padding:8px 14px;border-radius:8px;border:0;background:var(--accent);color:#fff;font:inherit;cursor:pointer}button:disabled{opacity:.5}
.ev{font-family:ui-monospace,Menlo,monospace;font-size:12px;white-space:normal}.ev td{white-space:normal}.muted{color:var(--muted)}#msg{min-height:20px;margin:-6px 0 12px;color:var(--muted)}
</style></head><body><main>
<h1 id="name">Factory Brain</h1><div class="sub" id="kind"></div>
<form id="f"><input id="q" placeholder="Ask the factory, e.g. print the actuator housing" autocomplete="off"><button id="go">Send</button></form>
<div id="msg"></div>
<div class="grid">
<div class="card"><h2>Jobs</h2><table id="jobs"></table></div>
<div class="card"><h2>Machines</h2><table id="machines"></table></div>
<div class="card"><h2>Robots</h2><table id="robots"></table></div>
<div class="card"><h2>Robot tasks</h2><table id="tasks"></table></div>
</div>
<div class="card" style="margin-top:14px"><h2>Events</h2><table class="ev" id="events"></table></div>
</main><script>
const token=(location.hash.match(/token=([^&]+)/)||[])[1];
const H=token?{"Authorization":"Bearer "+decodeURIComponent(token)}:{};
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pill=s=>`<span class="pill ${esc(s)}">${esc(s)}</span>`;
const t=ts=>ts?new Date(ts*1000).toLocaleTimeString():"";
function table(id,head,rows){document.getElementById(id).innerHTML="<tr>"+head.map(h=>`<th>${h}</th>`).join("")+"</tr>"+(rows.length?rows.join(""):`<tr><td colspan="${head.length}" class="muted">none</td></tr>`)}
async function api(path,body){const r=await fetch(path,{method:body?"POST":"GET",headers:{...H,"Content-Type":"application/json"},body:body?JSON.stringify(body):undefined});const j=await r.json();if(!r.ok)throw new Error(j.error||r.status);return j}
async function confirmJob(id){if(!confirm("Start "+id+" now? Make sure the machine is safe to start (bed clear)."))return;try{await api(`/api/jobs/${id}/confirm`,{});refresh()}catch(e){msg.textContent=e.message}}
async function refresh(){try{const s=await api("/api/state");
document.getElementById("name").textContent=s.factory.name||"Factory Brain";
document.getElementById("kind").textContent=`${s.factory.type||""} · model: ${s.factory.model||"none (rules)"} · output: ${s.factory.default_output_zone||"-"}`;
table("jobs",["Job","Machine","File","Deliver to","Status",""],s.jobs.map(j=>`<tr><td>${esc(j.id)}</td><td>${esc(j.machine_id)}</td><td>${esc(j.file)}</td><td>${esc(j.deliver_to)}</td><td>${pill(j.status)}</td><td>${j.status==="awaiting_confirmation"?`<button onclick="confirmJob('${esc(j.id)}')">Start</button>`:esc(j.task_id||j.error||"")}</td></tr>`));
table("machines",["Machine","Type","Zone","State","Progress","Job"],s.machines.map(m=>`<tr><td>${esc(m.id)}</td><td>${esc(m.type)}</td><td>${esc(m.zone)}</td><td>${pill(m.state)}</td><td>${m.progress_pct!=null?esc(m.progress_pct)+"%":""}</td><td>${esc(m.job||"")}</td></tr>`));
table("robots",["Robot","Zone","Battery","Status","Tasks","Seen"],s.robots.map(r=>`<tr><td>${esc(r.id)} <span class="muted">${r.name!==r.id?esc(r.name):""}</span></td><td>${esc(r.zone||"?")}</td><td>${esc(r.battery)}%</td><td>${pill(r.status)}</td><td>${esc(r.active_tasks.join(", "))}</td><td class="muted">${r.seen_s_ago==null?"never":esc(r.seen_s_ago)+"s"}</td></tr>`));
table("tasks",["Task","Robot","From","To","Status"],s.tasks.map(k=>`<tr><td>${esc(k.id)}</td><td>${esc(k.robot_id||"-")}</td><td>${esc(k.from_zone)}</td><td>${esc(k.to_zone)}</td><td>${pill(k.status)}</td></tr>`));
table("events",["Time","Event"],s.events.map(e=>`<tr><td class="muted">${t(e.ts)}</td><td>${esc(e.text)}</td></tr>`));
}catch(e){msg.textContent="Cannot reach the factory: "+e.message}}
const msg=document.getElementById("msg");
document.getElementById("f").onsubmit=async e=>{e.preventDefault();const q=document.getElementById("q"),go=document.getElementById("go");if(!q.value.trim())return;go.disabled=true;msg.textContent="Thinking…";
try{const r=await api("/api/requests",{text:q.value});msg.textContent=(r.ok?"✓ ":"✗ ")+r.summary+(r.model&&r.model.latency_s!=null?` (${r.model.latency_s}s)`:"");if(r.ok)q.value="";refresh()}catch(err){msg.textContent=err.message}finally{go.disabled=false}};
refresh();setInterval(refresh,2000);
</script></body></html>
"""
