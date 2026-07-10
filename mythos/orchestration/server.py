"""
mythos/orchestration/server.py
------------------------------
The local control panel: ``mythos --serve``.

A dependency-free (stdlib ``http.server``) dashboard for running the swarm on
a personal machine:

* ``GET  /``               – single-page dashboard (submit goals, watch runs);
* ``GET  /api/status``     – backends, mode, roles, governor spend;
* ``POST /api/goals``      – ``{"goal": "..."}`` → queue a run, returns its id;
* ``POST /api/runs/<id>/cancel`` – cancel a queued run / stop the running one;
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
    status: str = "queued"           # queued | running | completed | failed | cancelled
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

    def cancel(self, run_id: str) -> bool:
        """Cancel a queued run, or cooperatively stop the running one."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return False
            if run.status == "queued":
                run.status = "cancelled"
                run.finished_at = _utcnow()
                return True
            running = run.status == "running"
        if running and self._runtime is not None:
            self._runtime.orchestrator.request_cancel()
            return True
        return False

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
                if run.status == "cancelled":  # cancelled while still queued
                    continue
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
                cancelled = runtime.orchestrator.was_cancelled()
                with self._lock:
                    run.status = "cancelled" if cancelled else "completed"
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
        if self.path.startswith("/api/runs/") and self.path.endswith("/cancel"):
            run_id = self.path[len("/api/runs/"):-len("/cancel")]
            ok = self.manager.cancel(run_id)
            self._send_json(200 if ok else 404,
                            {"cancelled": ok, "run_id": run_id})
            return
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
  --bg:#070b12; --bg2:#0b1220; --panel:rgba(18,26,42,.72); --panel2:rgba(22,32,52,.55);
  --line:rgba(120,160,220,.16); --text:#e8f1ff; --dim:#8aa0c2; --accent:#38e0ff; --accent2:#7c5cff;
  --ok:#37e39b; --bad:#ff5c6c; --run:#ffbe4d; --warn:#ff8a3d;
  --glow:0 0 0 1px rgba(56,224,255,.16), 0 8px 40px rgba(8,16,32,.55);
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{font:14px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--text);
  background:radial-gradient(1200px 600px at 82% -10%,rgba(124,92,255,.16),transparent 60%),
    radial-gradient(900px 500px at 0% 0%,rgba(56,224,255,.11),transparent 55%),
    linear-gradient(180deg,var(--bg),var(--bg2));background-attachment:fixed;
  display:flex;flex-direction:column}
.mono{font-family:var(--mono)}
header{position:sticky;top:0;z-index:5;backdrop-filter:blur(12px);padding:12px 20px;
  border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:center;flex-wrap:wrap;
  background:linear-gradient(180deg,rgba(8,14,24,.85),rgba(8,14,24,.5))}
.brand{display:flex;align-items:center;gap:11px;font-weight:800;letter-spacing:.5px;font-size:16px}
.reactor{width:24px;height:24px;border-radius:50%;flex:0 0 auto;
  background:radial-gradient(circle at 50% 50%,#eafcff 0 18%,var(--accent) 22% 40%,transparent 46%);
  box-shadow:0 0 14px var(--accent),0 0 26px rgba(56,224,255,.5);animation:pulse 3.2s ease-in-out infinite}
.brand small{color:var(--dim);font-weight:600;letter-spacing:3px;font-size:9.5px;display:block;margin-top:-3px}
@keyframes pulse{0%,100%{opacity:.82;transform:scale(1)}50%{opacity:1;transform:scale(1.08)}}
#statusbar{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-left:auto}
.chip{font-size:11px;font-weight:700;color:var(--dim);border:1px solid var(--line);border-radius:999px;
  padding:3px 10px;background:rgba(120,160,220,.06);white-space:nowrap}
.chip b{color:var(--text)}
.pill{font-size:11px;font-weight:800;border-radius:999px;padding:3px 11px;letter-spacing:.4px}
.p-NORMAL{background:rgba(55,227,155,.16);color:var(--ok);border:1px solid rgba(55,227,155,.4)}
.p-REDUCED{background:rgba(255,190,77,.16);color:var(--run);border:1px solid rgba(255,190,77,.4)}
.p-PAUSED{background:rgba(255,138,61,.16);color:var(--warn);border:1px solid rgba(255,138,61,.4)}
.p-HALT{background:rgba(255,92,108,.18);color:var(--bad);border:1px solid rgba(255,92,108,.5)}
main{flex:1;display:grid;grid-template-columns:1fr 340px;gap:16px;max-width:1200px;width:100%;
  margin:0 auto;padding:16px 20px 20px;min-height:0}
@media(max-width:820px){main{grid-template-columns:1fr}}
.col{display:flex;flex-direction:column;gap:14px;min-height:0}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--glow);
  backdrop-filter:blur(6px);display:flex;flex-direction:column;min-height:0}
.label{font-size:11px;letter-spacing:2.5px;color:var(--dim);font-weight:700;padding:14px 16px 0}
/* conversation */
#chatPanel{flex:1;min-height:340px}
#transcript{flex:1;overflow:auto;padding:14px 16px;display:flex;flex-direction:column;gap:14px}
.turn{display:flex;gap:10px;max-width:92%}
.turn.user{align-self:flex-end;flex-direction:row-reverse}
.av{width:26px;height:26px;border-radius:8px;flex:0 0 auto;display:grid;place-items:center;
  font-size:12px;font-weight:800}
.turn.user .av{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#06121a}
.turn.bot .av{background:rgba(124,92,255,.18);color:var(--accent2);border:1px solid rgba(124,92,255,.4)}
.bubble{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:10px 13px;
  white-space:pre-wrap;word-break:break-word}
.turn.user .bubble{background:rgba(56,224,255,.09);border-color:rgba(56,224,255,.3)}
.steps{display:flex;flex-direction:column;gap:3px;margin-top:8px;font-family:var(--mono);font-size:11.5px}
.stp{display:flex;gap:7px;align-items:baseline;color:var(--dim)}
.stp b{color:var(--text);font-weight:600}
.thinking{display:inline-flex;gap:4px;align-items:center;color:var(--dim);font-family:var(--mono);font-size:12px}
.thinking i{width:6px;height:6px;border-radius:50%;background:var(--accent);display:inline-block;
  animation:blink 1.2s infinite}.thinking i:nth-child(2){animation-delay:.2s}.thinking i:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.25}40%{opacity:1}}
.cancel{align-self:flex-start;margin-top:8px;background:rgba(255,92,108,.14);color:var(--bad);
  border:1px solid rgba(255,92,108,.4);border-radius:8px;padding:5px 12px;font:inherit;font-size:12px;
  font-weight:700;cursor:pointer}
.empty{color:var(--dim);font-style:italic;padding:8px 2px;text-align:center}
form{display:flex;gap:10px;padding:12px 14px;border-top:1px solid var(--line)}
input[type=text]{flex:1;background:rgba(6,11,18,.7);border:1px solid var(--line);border-radius:10px;
  color:var(--text);padding:12px 14px;font:inherit;outline:none;transition:.15s}
input[type=text]:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(56,224,255,.15)}
button.send{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#06121a;border:0;
  border-radius:10px;padding:12px 20px;font:inherit;font-weight:800;cursor:pointer;letter-spacing:.3px}
button.send:hover{filter:brightness(1.08)}
/* activity rail */
#activity{max-height:300px;overflow:auto;padding:8px 14px 14px;font-family:var(--mono);font-size:12px;line-height:1.7}
.ev{display:flex;gap:8px;align-items:baseline;opacity:0;animation:in .25s forwards}
@keyframes in{to{opacity:1}}
.ev .t{color:var(--dim);font-size:10.5px;width:60px;flex:0 0 auto}.ev .ico{width:14px;text-align:center;font-weight:800}
.ev-goalstarted .ico{color:var(--accent)}.ev-taskdispatched .ico{color:var(--run)}
.ev-taskvalidated .ico,.ev-goalcompleted .ico{color:var(--ok)}
.ev-taskfailed .ico,.ev-goalfailed .ico,.ev-goalcancelled .ico{color:var(--bad)}
#runs{overflow:auto;max-height:230px;padding:6px 12px 12px}
.run{display:flex;gap:9px;align-items:center;border-radius:9px;padding:8px 10px;cursor:pointer;border:1px solid transparent}
.run:hover{background:rgba(120,160,220,.07);border-color:var(--line)}
.badge{display:inline-flex;align-items:center;justify-content:center;min-width:74px;border-radius:999px;
  padding:2px 9px;font-size:10.5px;font-weight:800}
.queued,.pending{background:rgba(120,160,220,.16);color:var(--dim)}
.running,.dispatched{background:rgba(255,190,77,.18);color:var(--run)}
.completed,.validated{background:rgba(55,227,155,.18);color:var(--ok)}
.failed{background:rgba(255,92,108,.18);color:var(--bad)}
.cancelled{background:rgba(255,138,61,.18);color:var(--warn)}
.rgoal{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dim{color:var(--dim)}.tiny{font-size:11px}
@media(prefers-reduced-motion:reduce){.reactor,.thinking i{animation:none}}
</style></head><body>
<header>
  <div class="brand"><span class="reactor"></span><span>MYTHOS<small>BOSS&nbsp;CONSOLE</small></span></div>
  <div id="statusbar"><span class="chip dim">connecting…</span></div>
</header>
<main>
  <div class="col">
    <div class="panel" id="chatPanel">
      <div class="label">CONVERSATION</div>
      <div id="transcript"><div class="empty">Tell the swarm what you need. It plans, delegates to specialist agents, verifies, and reports back here.</div></div>
      <form id="f"><input type="text" id="goal" placeholder="Ask Mythos to do something…" autocomplete="off">
      <button class="send">Send</button></form>
    </div>
  </div>
  <div class="col">
    <div class="panel"><div class="label">LIVE ACTIVITY · SSE</div>
      <div id="activity"><div class="empty">awaiting events…</div></div></div>
    <div class="panel"><div class="label">RUNS</div>
      <div id="runs"><div class="empty">no runs yet</div></div></div>
  </div>
</main>
<script>
const $=id=>document.getElementById(id);
const esc=t=>{const d=document.createElement('div');d.textContent=t==null?'':t;return d.innerHTML};
const clk=s=>({validated:'completed',failed:'failed',dispatched:'running',pending:'queued'})[s]||'queued';
async function jget(u){const r=await fetch(u);return r.json()}
let chat=[];           // {role, text, runId?, status?, steps?}
function botLive(){for(let i=chat.length-1;i>=0;i--){if(chat[i].role==='bot'&&chat[i].status==='running')return chat[i]}return null}
function render(){
  const t=$('transcript');
  if(!chat.length){t.innerHTML='<div class="empty">Tell the swarm what you need. It plans, delegates to specialist agents, verifies, and reports back here.</div>';return}
  t.innerHTML=chat.map(m=>{
    const av=m.role==='user'?'<div class="av">You</div>':'<div class="av">◈</div>';
    let inner='';
    if(m.role==='user'){inner=`<div class="bubble">${esc(m.text)}</div>`}
    else{
      let body='';
      if(m.status==='running'){
        const steps=(m.steps||[]).map(s=>`<div class="stp"><span>${s.ico}</span><b>${esc(s.role||'')}</b> ${esc(s.label)}</div>`).join('');
        body=`<div class="thinking"><i></i><i></i><i></i>&nbsp;working…</div>`+(steps?`<div class="steps">${steps}</div>`:'')
          +(m.runId?`<button class="cancel" onclick="cancelRun('${m.runId}')">Stop</button>`:'');
      }else{body=`<div class="bubble">${esc(m.text||'(no output)')}</div>`}
      inner=body;
    }
    return `<div class="turn ${m.role==='user'?'user':'bot'}">${av}<div>${inner}</div></div>`;
  }).join('');
  t.scrollTop=t.scrollHeight;
}
function statusBar(s){
  if(!s.started)return '<span class="chip dim">swarm idle · starts on first message</span>';
  const p=s.posture||'NORMAL';
  return `<span class="pill p-${esc(p)}">${esc(p)}</span>`
    +`<span class="chip">bus <b>${esc(s.bus)}</b></span>`
    +`<span class="chip">matrix <b>${esc(s.matrix)}</b></span>`
    +`<span class="chip">${s.dynamic?'<b>dynamic</b>':'wf <b>'+esc(s.workflow)+'</b>'}</span>`
    +`<span class="chip">tok/h <b>${s.tokens_last_hour??0}</b></span>`
    +(s.roles||[]).map(r=>'<span class="chip">'+esc(r)+'</span>').join('');
}
async function refresh(){
  try{
    const s=await jget('/api/status');$('statusbar').innerHTML=statusBar(s);
    const d=await jget('/api/runs');
    $('runs').innerHTML=d.runs.length?d.runs.map(r=>
      `<div class="run"><span class="badge ${r.status}">${r.status}</span>
        <span class="rgoal">${esc(r.goal)}</span>
        <span class="dim tiny">${(r.submitted_at||'').slice(11,19)}</span></div>`).join('')
      :'<div class="empty">no runs yet</div>';
    // resolve any finished bot turn
    const live=botLive();
    if(live&&live.runId){
      const run=await jget('/api/runs/'+live.runId);
      if(run&&run.status&&run.status!=='running'&&run.status!=='queued'){
        live.status=run.status;
        live.text=run.conclusion||run.error||'(no output)';
        render();
      }
    }
  }catch(e){$('statusbar').innerHTML='<span class="chip" style="color:var(--bad)">error: '+esc(e)+'</span>'}
}
$('f').addEventListener('submit',async e=>{
  e.preventDefault();const goal=$('goal').value.trim();if(!goal)return;$('goal').value='';
  chat.push({role:'user',text:goal});
  const bot={role:'bot',status:'running',steps:[],runId:null,text:''};chat.push(bot);render();
  try{
    const r=await fetch('/api/goals',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({goal})});
    const run=await r.json();bot.runId=run.run_id;render();refresh();
  }catch(err){bot.status='failed';bot.text='Could not reach the swarm: '+err;render()}
});
window.cancelRun=async id=>{await fetch('/api/runs/'+id+'/cancel',{method:'POST'});refresh()};
const ICON={'goal.started':'▶','task.dispatched':'→','task.validated':'✓','task.failed':'✗',
  'goal.completed':'★','goal.failed':'✗','goal.cancelled':'⊘'};
let evEmpty=true;
function onEvent(ev){
  if(evEmpty){$('activity').innerHTML='';evEmpty=false}
  const cls='ev-'+(ev.kind||'').replace(/\./g,'');
  const row=document.createElement('div');row.className='ev '+cls;
  row.innerHTML=`<span class="t">${new Date().toTimeString().slice(0,8)}</span>`
    +`<span class="ico">${ICON[ev.kind]||'·'}</span>`
    +`<span>${esc(ev.kind)}${ev.role?(' ['+esc(ev.role)+']'):''}</span>`;
  const c=$('activity');c.insertBefore(row,c.firstChild);while(c.children.length>60)c.removeChild(c.lastChild);
  const live=botLive();
  if(live&&(ev.kind||'').startsWith('task.')){
    live.steps.push({ico:ICON[ev.kind]||'·',role:ev.role,label:ev.kind.replace('task.','')});
    render();
  }
  refresh();
}
(function sse(){const src=new EventSource('/api/events');
  src.onmessage=e=>onEvent(JSON.parse(e.data));
  Object.keys(ICON).forEach(k=>src.addEventListener(k,e=>onEvent(JSON.parse(e.data))));
  src.onerror=()=>{};})();
render();refresh();setInterval(refresh,2200);
</script></body></html>"""
