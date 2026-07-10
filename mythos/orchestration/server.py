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

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mythos Control Panel</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--text:#e6edf3;--dim:#8b949e;
--accent:#58a6ff;--ok:#3fb950;--bad:#f85149;--run:#d29922}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;
background:var(--bg);color:var(--text)}
header{padding:16px 24px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:baseline}
h1{font-size:18px;margin:0}#status{color:var(--dim);font-size:12px}
main{max-width:980px;margin:0 auto;padding:24px;display:grid;gap:16px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px}
form{display:flex;gap:8px}input[type=text]{flex:1;background:var(--bg);border:1px solid var(--line);
border-radius:6px;color:var(--text);padding:10px;font:inherit}
button{background:var(--accent);color:#0d1117;border:0;border-radius:6px;padding:10px 18px;
font:inherit;font-weight:700;cursor:pointer}
.run{border-top:1px solid var(--line);padding:10px 0;cursor:pointer}
.run:first-child{border-top:0}
.badge{display:inline-block;min-width:86px;text-align:center;border-radius:12px;padding:1px 10px;
font-size:12px;font-weight:700}
.queued{background:#30363d}.running{background:var(--run);color:#0d1117}
.completed{background:var(--ok);color:#0d1117}.failed{background:var(--bad);color:#0d1117}
.goal{margin-left:10px}.dim{color:var(--dim);font-size:12px}
#detail pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);
border:1px solid var(--line);border-radius:6px;padding:12px;max-height:340px;overflow:auto}
.step{display:flex;gap:10px;padding:4px 0;align-items:baseline}
.step .badge{min-width:86px}
</style></head><body>
<header><h1>&#9889; Mythos Control Panel</h1><div id="status">connecting…</div></header>
<main>
<div class="panel">
  <form id="f"><input type="text" id="goal" placeholder="Give the swarm a goal…" autocomplete="off">
  <button>Run</button></form>
</div>
<div class="panel"><div class="dim">RUNS</div><div id="runs">none yet</div></div>
<div class="panel" id="detail" hidden><div class="dim">RUN DETAIL</div><div id="detailBody"></div></div>
<div class="panel"><div class="dim">LIVE EVENT STREAM (SSE)</div><div id="events" class="dim">waiting for events…</div></div>
</main>
<script>
let selected=null;
const $=id=>document.getElementById(id);
async function jget(u){const r=await fetch(u);return r.json()}
async function refresh(){
  try{
    const s=await jget('/api/status');
    $('status').textContent=s.started
      ?`bus=${s.bus} · matrix=${s.matrix} · ${s.dynamic?'dynamic':'workflow: '+s.workflow} · roles: ${(s.roles||[]).join(', ')} · tokens/h: ${s.tokens_last_hour}`
      :'swarm idle (starts on first goal)';
    const d=await jget('/api/runs');
    $('runs').innerHTML=d.runs.length?d.runs.map(r=>
      `<div class="run" onclick="select('${r.run_id}')">
        <span class="badge ${r.status}">${r.status}</span>
        <span class="goal">${esc(r.goal)}</span>
        <span class="dim"> ${r.run_id} · ${r.submitted_at||''}</span></div>`).join(''):'none yet';
    if(selected)showDetail(await jget('/api/runs/'+selected));
  }catch(e){$('status').textContent='dashboard error: '+e}
}
function esc(t){const d=document.createElement('div');d.textContent=t;return d.innerHTML}
window.select=async id=>{selected=id;$('detail').hidden=false;
  showDetail(await jget('/api/runs/'+id))}
function showDetail(r){
  let h=`<p><span class="badge ${r.status}">${r.status}</span>
    <span class="goal">${esc(r.goal)}</span></p>`;
  if(r.ledger&&r.ledger.steps){h+=r.ledger.steps.map(s=>
    `<div class="step"><span class="badge ${
       {validated:'completed',failed:'failed',dispatched:'running',pending:'queued'}[s.status]||'queued'
     }">${s.status}</span><span>[${esc(s.role)}] ${esc(s.objective)}</span>
     <span class="dim">${s.attempts?('attempts: '+s.attempts):''}</span></div>`).join('')}
  if(r.conclusion)h+=`<pre>${esc(r.conclusion)}</pre>`;
  if(r.error)h+=`<pre>${esc(r.error)}</pre>`;
  $('detailBody').innerHTML=h;
}
$('f').addEventListener('submit',async e=>{
  e.preventDefault();
  const goal=$('goal').value.trim();if(!goal)return;
  $('goal').value='';
  const r=await fetch('/api/goals',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({goal})});
  const run=await r.json();if(run.run_id)select(run.run_id);
  refresh();
});
// Real-time push: the SSE stream drives instant refreshes (no waiting for
// the poll tick) and renders a live event log.
const evLog=[];
function connectSSE(){
  const src=new EventSource('/api/events');
  src.onmessage=e=>onEvent(JSON.parse(e.data));
  ['goal.started','task.dispatched','task.validated','task.failed','goal.completed','goal.failed']
    .forEach(k=>src.addEventListener(k,e=>onEvent(JSON.parse(e.data))));
  src.onerror=()=>{/* EventSource auto-reconnects */};
}
function onEvent(ev){
  const icon={'goal.started':'▶','task.dispatched':'→','task.validated':'✓',
    'task.failed':'✗','goal.completed':'★','goal.failed':'✗'}[ev.kind]||'·';
  const line=`${icon} ${ev.kind}${ev.role?(' ['+ev.role+']'):''}${ev.step!=null?(' #'+ev.step):''}`;
  evLog.unshift(line);if(evLog.length>40)evLog.pop();
  $('events').innerHTML=evLog.map(esc).join('<br>');
  refresh(); // push-triggered refresh = immediate, not on the 1.5s tick
}
connectSSE();refresh();setInterval(refresh,2500);
</script></body></html>
"""
