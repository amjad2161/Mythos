"""
mythos/orchestration/server.py
------------------------------
The local control panel: ``mythos --serve``.

A dependency-free (stdlib ``http.server``) dashboard for running the swarm on
a personal machine:

* ``GET  /``               – single-page dashboard (submit goals, watch runs);
* ``GET  /api/status``     – backends, mode, roles, governor spend;
* ``POST /api/goals``      – ``{"goal": "..."}`` → queue a run, returns its id;
* ``GET  /api/runs``       – all runs (newest first, summaries);
* ``GET  /api/runs/<id>``  – one run incl. its live Task Ledger document.

Runs execute **serially** on a background thread over one shared
``SwarmRuntime`` (the orchestrator's result correlation is per-goal); queued
goals wait their turn.  The server binds to 127.0.0.1 by default – it is a
local control panel, not a public service.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import queue
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

from .events import EventHub
from .ledger import TaskLedger
from .runtime import SwarmRuntime
from .schemas import SchemaError


# ---------------------------------------------------------------------------
# Run registry
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Run:
    """One submitted goal and its lifecycle."""

    run_id: str
    goal: str
    status: str = "queued"           # queued | running | completed | failed
    conclusion: str = ""
    error: str = ""
    ledger_id: str = ""
    submitted_at: str = ""
    finished_at: str = ""

    def summary(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


class RunManager:
    """Serial executor of goals over one shared SwarmRuntime."""

    def __init__(self, runtime_factory: Callable[[], SwarmRuntime]) -> None:
        self._runtime_factory = runtime_factory
        self._runtime: Optional[SwarmRuntime] = None
        self._runs: Dict[str, Run] = {}
        self._order: List[str] = []
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # A stable hub the SSE endpoint subscribes to before any goal exists;
        # each runtime's per-run hub is forwarded into it.
        self.hub = EventHub()
        self._thread = threading.Thread(
            target=self._worker, name="run-manager", daemon=True
        )
        self._thread.start()

    # -- public API -------------------------------------------------------

    def submit(self, goal: str) -> Run:
        run = Run(
            run_id=f"run_{uuid.uuid4().hex[:10]}",
            goal=goal,
            submitted_at=_utcnow(),
        )
        with self._lock:
            self._runs[run.run_id] = run
            self._order.append(run.run_id)
        self._queue.put(run.run_id)
        return run

    def list_runs(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [self._runs[rid].summary() for rid in reversed(self._order)]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            data = run.summary()
        data["ledger"] = self._read_ledger(data.get("ledger_id", ""))
        return data

    def event_hub(self):  # noqa: ANN201 – EventHub or None
        """The live event hub of the running swarm (None before first goal)."""
        return self._runtime.events if self._runtime is not None else None

    def status(self) -> Dict[str, Any]:
        runtime = self._runtime
        info: Dict[str, Any] = {
            "started": runtime is not None,
            "runs": len(self._order),
        }
        if runtime is not None:
            info.update({
                "bus": runtime.config.bus_backend,
                "matrix": runtime.config.matrix_backend,
                "dynamic": runtime.config.dynamic,
                "workflow": runtime.workflow.name,
                "roles": sorted(w.role for w in runtime.workers),
                "tokens_last_hour": runtime.governor.window_total,
                "posture": runtime.governor.posture().posture.name,
            })
        return info

    def shutdown(self) -> None:
        self._stop.set()
        self._queue.put("")  # unblock the worker
        self.hub.close()
        self._thread.join(timeout=5)
        if self._runtime is not None:
            self._runtime.shutdown()

    # -- internals ----------------------------------------------------------

    def _ensure_runtime(self) -> SwarmRuntime:
        if self._runtime is None:
            self._runtime = self._runtime_factory()
            self._runtime.start()
            # Forward the runtime's live events into the manager's stable hub
            # so SSE subscribers attached before the first goal keep receiving.
            threading.Thread(
                target=self._forward_events,
                args=(self._runtime.events,),
                name="event-forwarder",
                daemon=True,
            ).start()
        return self._runtime

    def _forward_events(self, source) -> None:  # noqa: ANN001 – EventHub
        sub = source.subscribe()
        try:
            for event in sub.stream(self._stop):
                if event.kind != "heartbeat":
                    self.hub.emit(
                        event.kind, trace_id=event.trace_id, task_id=event.task_id,
                        role=event.role, ts_ms=event.ts_ms, **event.detail,
                    )
        finally:
            source.unsubscribe(sub)

    def _worker(self) -> None:
        while not self._stop.is_set():
            run_id = self._queue.get()
            if not run_id or self._stop.is_set():
                continue
            with self._lock:
                run = self._runs[run_id]
                run.status = "running"
            try:
                runtime = self._ensure_runtime()
                # Track the ledger as soon as the orchestrator creates it so
                # the dashboard can show live per-step progress.
                watcher = threading.Thread(
                    target=self._watch_ledger, args=(run_id,), daemon=True
                )
                watcher.start()
                conclusion = runtime.run(run.goal)
                with self._lock:
                    run.status = "completed"
                    run.conclusion = conclusion
                    # Fast runs can finish before the watcher fires; runs are
                    # serial, so the orchestrator's last ledger is this run's.
                    run.ledger_id = runtime.orchestrator.last_ledger_id or run.ledger_id
            except Exception as exc:  # noqa: BLE001 – a failed run must not kill the server
                with self._lock:
                    run.status = "failed"
                    run.error = f"{type(exc).__name__}: {exc}"
            finally:
                with self._lock:
                    run.finished_at = _utcnow()

    def _watch_ledger(self, run_id: str) -> None:
        """Grab the run's ledger id once the orchestrator publishes it."""
        runtime = self._runtime
        if runtime is None:
            return
        for _ in range(100):  # up to ~10s; the id appears at dispatch time
            ledger_id = runtime.orchestrator.last_ledger_id
            if ledger_id:
                with self._lock:
                    run = self._runs.get(run_id)
                    if run is not None and run.status == "running":
                        run.ledger_id = ledger_id
                return
            if self._stop.wait(0.1):
                return

    def _read_ledger(self, ledger_id: str) -> Optional[Dict[str, Any]]:
        if not ledger_id or self._runtime is None:
            return None
        try:
            return TaskLedger(self._runtime.matrix).read(ledger_id)
        except SchemaError:
            return None


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    manager: RunManager  # injected via type() in create_server

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 – http.server API
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, DASHBOARD_HTML, content_type="text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._send_json(200, self.manager.status())
        elif self.path == "/api/runs":
            self._send_json(200, {"runs": self.manager.list_runs()})
        elif self.path == "/api/events":
            self._stream_events()
        elif self.path.startswith("/api/runs/"):
            run = self.manager.get_run(self.path.rsplit("/", 1)[-1])
            if run is None:
                self._send_json(404, {"error": "unknown run id"})
            else:
                self._send_json(200, run)
        else:
            self._send_json(404, {"error": "not found"})

    def _stream_events(self) -> None:
        """Server-Sent Events: push swarm lifecycle events to the browser live."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        stop = threading.Event()
        sub = self.manager.hub.subscribe()
        try:
            # Replay recent history so a freshly-opened tab has context.
            for event in self.manager.hub.recent(30):
                self._write_sse(event)
            for event in sub.stream(stop):
                if event.kind == "heartbeat":
                    self.wfile.write(b": keep-alive\n\n")
                else:
                    self._write_sse(event)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # the browser closed the tab
        finally:
            stop.set()
            self.manager.hub.unsubscribe(sub)

    def _write_sse(self, event) -> None:  # noqa: ANN001 – Event
        payload = json.dumps(event.to_dict(), ensure_ascii=False)
        self.wfile.write(f"event: {event.kind}\ndata: {payload}\n\n".encode("utf-8"))

    def do_POST(self) -> None:  # noqa: N802 – http.server API
        if self.path != "/api/goals":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            goal = str(body.get("goal", "")).strip()
        except (ValueError, TypeError):
            goal = ""
        if not goal:
            self._send_json(400, {"error": "body must be JSON with a non-empty 'goal'"})
            return
        run = self.manager.submit(goal)
        self._send_json(202, run.summary())

    # -- helpers ----------------------------------------------------------

    def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False),
                   content_type="application/json; charset=utf-8")

    def _send(self, code: int, body: str, content_type: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass  # keep the console clean; the dashboard is the log


def create_server(
    runtime_factory: Callable[[], SwarmRuntime],
    host: str = "127.0.0.1",
    port: int = 8642,
) -> "tuple[ThreadingHTTPServer, RunManager]":
    """Build the dashboard server (not yet serving) and its RunManager."""
    manager = RunManager(runtime_factory)
    handler = type("BoundHandler", (_Handler,), {"manager": manager})
    server = ThreadingHTTPServer((host, port), handler)
    return server, manager


def serve_forever(
    runtime_factory: Callable[[], SwarmRuntime],
    host: str = "127.0.0.1",
    port: int = 8642,
) -> None:
    """Run the dashboard until interrupted (the ``mythos --serve`` loop)."""
    server, manager = create_server(runtime_factory, host, port)
    print(f"Mythos control panel: http://{host}:{port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        server.server_close()
        manager.shutdown()


# ---------------------------------------------------------------------------
# Dashboard page (inline: the server must stay dependency- and asset-free)
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mythos Control Panel</title>
<style>
:root{
  --bg:#070b12; --bg2:#0b1220; --panel:rgba(18,26,42,.72); --line:rgba(120,160,220,.16);
  --text:#e8f1ff; --dim:#8aa0c2; --accent:#38e0ff; --accent2:#7c5cff;
  --ok:#37e39b; --bad:#ff5c6c; --run:#ffbe4d; --warn:#ff8a3d;
  --glow:0 0 0 1px rgba(56,224,255,.18), 0 8px 40px rgba(8,16,32,.6);
}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{
  font:14px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  color:var(--text); background:
    radial-gradient(1200px 600px at 80% -10%, rgba(124,92,255,.18), transparent 60%),
    radial-gradient(900px 500px at 0% 0%, rgba(56,224,255,.12), transparent 55%),
    linear-gradient(180deg,var(--bg),var(--bg2));
  background-attachment:fixed;
}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{
  position:sticky;top:0;z-index:5;backdrop-filter:blur(12px);
  padding:14px 22px;border-bottom:1px solid var(--line);
  display:flex;gap:18px;align-items:center;flex-wrap:wrap;
  background:linear-gradient(180deg,rgba(8,14,24,.85),rgba(8,14,24,.55));
}
.brand{display:flex;align-items:center;gap:12px;font-weight:800;letter-spacing:.5px;font-size:17px}
.reactor{width:26px;height:26px;border-radius:50%;position:relative;flex:0 0 auto;
  background:radial-gradient(circle at 50% 50%,#eafcff 0 18%,var(--accent) 22% 40%,transparent 46%);
  box-shadow:0 0 14px var(--accent),0 0 28px rgba(56,224,255,.5);animation:pulse 3s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:.85;transform:scale(1)}50%{opacity:1;transform:scale(1.08)}}
.brand small{color:var(--dim);font-weight:600;letter-spacing:3px;font-size:10px;display:block;margin-top:-2px}
#statusbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-left:auto}
.chip{font-size:11px;font-weight:700;color:var(--dim);border:1px solid var(--line);
  border-radius:999px;padding:3px 10px;background:rgba(120,160,220,.06);white-space:nowrap}
.chip b{color:var(--text);font-weight:700}
.pill{font-size:11px;font-weight:800;border-radius:999px;padding:3px 11px;letter-spacing:.4px}
.p-NORMAL{background:rgba(55,227,155,.16);color:var(--ok);border:1px solid rgba(55,227,155,.4)}
.p-REDUCED{background:rgba(255,190,77,.16);color:var(--run);border:1px solid rgba(255,190,77,.4)}
.p-PAUSED{background:rgba(255,138,61,.16);color:var(--warn);border:1px solid rgba(255,138,61,.4)}
.p-HALT{background:rgba(255,92,108,.18);color:var(--bad);border:1px solid rgba(255,92,108,.5)}
main{max-width:1100px;margin:0 auto;padding:22px;display:grid;gap:16px;
  grid-template-columns:1fr 1fr}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px;
  box-shadow:var(--glow);backdrop-filter:blur(6px)}
.span2{grid-column:1/-1}
.label{font-size:11px;letter-spacing:2.5px;color:var(--dim);font-weight:700;margin-bottom:10px}
form{display:flex;gap:10px}
input[type=text]{flex:1;background:rgba(6,11,18,.7);border:1px solid var(--line);border-radius:10px;
  color:var(--text);padding:13px 14px;font:inherit;outline:none;transition:.15s}
input[type=text]:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(56,224,255,.15)}
button{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#06121a;border:0;
  border-radius:10px;padding:13px 22px;font:inherit;font-weight:800;cursor:pointer;letter-spacing:.3px;
  box-shadow:0 6px 20px rgba(56,224,255,.25);transition:.15s}
button:hover{filter:brightness(1.08);transform:translateY(-1px)}
button:active{transform:translateY(0)}
.run{display:flex;gap:10px;align-items:center;border-radius:10px;padding:10px 12px;cursor:pointer;
  border:1px solid transparent;transition:.12s}
.run:hover{background:rgba(120,160,220,.07);border-color:var(--line)}
.run.sel{background:rgba(56,224,255,.08);border-color:rgba(56,224,255,.35)}
.badge{display:inline-flex;align-items:center;justify-content:center;min-width:82px;border-radius:999px;
  padding:3px 10px;font-size:11px;font-weight:800;letter-spacing:.3px}
.queued,.pending{background:rgba(120,160,220,.16);color:var(--dim)}
.running,.dispatched{background:rgba(255,190,77,.18);color:var(--run)}
.completed,.validated{background:rgba(55,227,155,.18);color:var(--ok)}
.failed{background:rgba(255,92,108,.18);color:var(--bad)}
.goal{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dim{color:var(--dim)}.tiny{font-size:11px}
#detail pre{white-space:pre-wrap;word-break:break-word;background:rgba(6,11,18,.7);
  border:1px solid var(--line);border-radius:10px;padding:12px;max-height:320px;overflow:auto;font-size:12.5px}
.step{display:flex;gap:10px;padding:6px 0;align-items:baseline;border-top:1px dashed var(--line)}
.step:first-child{border-top:0}
#events{max-height:340px;overflow:auto;font-size:12.5px;line-height:1.7}
.ev{display:flex;gap:8px;align-items:baseline;padding:2px 0;opacity:0;animation:in .25s forwards}
@keyframes in{to{opacity:1}}
.ev .ico{width:16px;text-align:center;flex:0 0 auto;font-weight:800}
.ev .t{color:var(--dim);font-size:10.5px;flex:0 0 auto;width:62px}
.ev-goalstarted .ico{color:var(--accent)} .ev-taskdispatched .ico{color:var(--run)}
.ev-taskvalidated .ico{color:var(--ok)} .ev-taskfailed .ico,.ev-goalfailed .ico{color:var(--bad)}
.ev-goalcompleted .ico{color:var(--ok)}
.empty{color:var(--dim);font-style:italic;padding:6px 2px}
@media(max-width:720px){main{grid-template-columns:1fr}}
</style></head><body>
<header>
  <div class="brand"><span class="reactor"></span><span>MYTHOS<small>CONTROL&nbsp;PANEL</small></span></div>
  <div id="statusbar"><span class="chip dim">connecting…</span></div>
</header>
<main>
  <div class="panel span2">
    <div class="label">NEW DIRECTIVE</div>
    <form id="f"><input type="text" id="goal" placeholder="Give the swarm a goal…" autocomplete="off">
    <button>Dispatch</button></form>
  </div>
  <div class="panel">
    <div class="label">RUNS</div>
    <div id="runs"><div class="empty">no runs yet</div></div>
  </div>
  <div class="panel">
    <div class="label">LIVE EVENT STREAM · SSE</div>
    <div id="events"><div class="empty">awaiting events…</div></div>
  </div>
  <div class="panel span2" id="detail" hidden>
    <div class="label">RUN DETAIL · TASK LEDGER</div>
    <div id="detailBody"></div>
  </div>
</main>
<script>
let selected=null;
const $=id=>document.getElementById(id);
const esc=t=>{const d=document.createElement('div');d.textContent=t==null?'':t;return d.innerHTML};
const clk=s=>({validated:'completed',failed:'failed',dispatched:'running',pending:'queued'})[s]||'queued';
async function jget(u){const r=await fetch(u);return r.json()}
function statusBar(s){
  if(!s.started)return '<span class="chip dim">swarm idle · starts on first goal</span>';
  const p=s.posture||'NORMAL';
  const roles=(s.roles||[]).map(r=>'<span class="chip">'+esc(r)+'</span>').join('');
  return `<span class="pill p-${esc(p)}">${esc(p)}</span>`
    +`<span class="chip">bus <b>${esc(s.bus)}</b></span>`
    +`<span class="chip">matrix <b>${esc(s.matrix)}</b></span>`
    +`<span class="chip">${s.dynamic?'<b>dynamic</b>':'wf <b>'+esc(s.workflow)+'</b>'}</span>`
    +`<span class="chip">tok/h <b>${s.tokens_last_hour??0}</b></span>`
    +roles;
}
async function refresh(){
  try{
    const s=await jget('/api/status');
    $('statusbar').innerHTML=statusBar(s);
    const d=await jget('/api/runs');
    $('runs').innerHTML=d.runs.length?d.runs.map(r=>
      `<div class="run ${r.run_id===selected?'sel':''}" onclick="select('${r.run_id}')">
        <span class="badge ${r.status}">${r.status}</span>
        <span class="goal">${esc(r.goal)}</span>
        <span class="dim tiny">${(r.submitted_at||'').slice(11,19)}</span></div>`).join('')
      :'<div class="empty">no runs yet</div>';
    if(selected)showDetail(await jget('/api/runs/'+selected));
  }catch(e){$('statusbar').innerHTML='<span class="chip" style="color:var(--bad)">error: '+esc(e)+'</span>'}
}
window.select=async id=>{selected=id;$('detail').hidden=false;showDetail(await jget('/api/runs/'+id));refresh()};
function showDetail(r){
  let h=`<div style="display:flex;gap:10px;align-items:center;margin-bottom:12px">
    <span class="badge ${r.status}">${r.status}</span><b>${esc(r.goal)}</b></div>`;
  if(r.ledger&&r.ledger.steps)h+=r.ledger.steps.map(s=>
    `<div class="step"><span class="badge ${clk(s.status)}">${esc(s.status)}</span>
     <span class="goal" style="white-space:normal">[${esc(s.role)}] ${esc(s.objective)}</span>
     <span class="dim tiny">${s.attempts?('×'+s.attempts):''}</span></div>`).join('');
  if(r.conclusion)h+=`<pre>${esc(r.conclusion)}</pre>`;
  if(r.error)h+=`<pre style="border-color:rgba(255,92,108,.4)">${esc(r.error)}</pre>`;
  $('detailBody').innerHTML=h;
}
$('f').addEventListener('submit',async e=>{
  e.preventDefault();const goal=$('goal').value.trim();if(!goal)return;$('goal').value='';
  const r=await fetch('/api/goals',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({goal})});
  const run=await r.json();if(run.run_id)select(run.run_id);refresh();
});
const ICON={'goal.started':'▶','task.dispatched':'→','task.validated':'✓',
  'task.failed':'✗','goal.completed':'★','goal.failed':'✗'};
let evEmpty=true;
function onEvent(ev){
  if(evEmpty){$('events').innerHTML='';evEmpty=false}
  const cls='ev-'+(ev.kind||'').replace(/\./g,'');
  const t=new Date().toTimeString().slice(0,8);
  const row=document.createElement('div');row.className='ev '+cls;
  row.innerHTML=`<span class="t mono">${t}</span><span class="ico">${ICON[ev.kind]||'·'}</span>`
    +`<span class="mono">${esc(ev.kind)}${ev.role?(' ['+esc(ev.role)+']'):''}</span>`;
  const c=$('events');c.insertBefore(row,c.firstChild);
  while(c.children.length>60)c.removeChild(c.lastChild);
  refresh();
}
(function connectSSE(){
  const src=new EventSource('/api/events');
  src.onmessage=e=>onEvent(JSON.parse(e.data));
  Object.keys(ICON).forEach(k=>src.addEventListener(k,e=>onEvent(JSON.parse(e.data))));
  src.onerror=()=>{};
})();
refresh();setInterval(refresh,2500);
</script></body></html>"""
