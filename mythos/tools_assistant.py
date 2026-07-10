"""
mythos/tools_assistant.py
-------------------------
Personal-assistant / "digital secretary" tools for the ``assistant`` role.

A dependency-light, offline-first secretary: tasks, notes, reminders, e-mail
*drafts*, and a composed daily briefing — all persisted as JSON under a local
store (``MYTHOS_ASSISTANT_DIR``, default ``~/.mythos/assistant``).  This is the
``connectors/local.py`` tier of the assistant design (see
``docs/JARVIS_BLUEPRINT.md``): the system is fully functional for the user's
own data with zero cloud dependency; cloud calendar/mail adapters plug in
behind the same tool surface later.

Design rules shared with the other tool modules:
* Pure stdlib, no third-party imports.
* Every failure path returns an ``"ERROR: ..."`` string; tools never raise.
* Drafting an e-mail is *safe* and writes a local file; actually **sending**
  is an outward action that belongs behind the human-in-the-loop gate and is
  deliberately not implemented here.
"""
from __future__ import annotations

import datetime
import json
import os
import threading
from typing import Any, Dict, List, Optional

from .tools import Tool, _truncate

_LOCK = threading.Lock()
_PRIORITIES = ("low", "normal", "high")


def _store_dir() -> str:
    base = os.getenv("MYTHOS_ASSISTANT_DIR", "").strip()
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".mythos", "assistant")
    return base


def _path(name: str) -> str:
    return os.path.join(_store_dir(), name)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _load(name: str) -> List[Dict[str, Any]]:
    try:
        with open(_path(name), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(name: str, rows: List[Dict[str, Any]]) -> Optional[str]:
    try:
        os.makedirs(_store_dir(), exist_ok=True)
        tmp = _path(name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, _path(name))
        return None
    except OSError as exc:
        return f"ERROR: could not persist {name}: {exc}"


def _next_id(rows: List[Dict[str, Any]]) -> int:
    return max((int(r.get("id", 0)) for r in rows), default=0) + 1


def _parse_when(when: str) -> Optional[datetime.datetime]:
    """Parse an ISO-8601 datetime; treat a naive value as UTC."""
    text = when.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


# --------------------------------------------------------------------------- #
# Tasks                                                                        #
# --------------------------------------------------------------------------- #
def _tool_pa_add_task(text: str, due: str = "", priority: str = "normal") -> str:
    if not text.strip():
        return "ERROR: task text is empty"
    if priority not in _PRIORITIES:
        return f"ERROR: priority must be one of {list(_PRIORITIES)}"
    if due and _parse_when(due) is None:
        return f"ERROR: could not parse due date '{due}' (use ISO-8601, e.g. 2026-07-12T09:00)"
    with _LOCK:
        rows = _load("tasks.json")
        task = {
            "id": _next_id(rows),
            "text": text.strip(),
            "due": due.strip(),
            "priority": priority,
            "status": "open",
            "created": _now().isoformat(),
        }
        rows.append(task)
        err = _save("tasks.json", rows)
    return err or f"Added task #{task['id']}: {task['text']}"


def _tool_pa_list_tasks(status: str = "open") -> str:
    rows = _load("tasks.json")
    if status not in ("open", "done", "all"):
        return "ERROR: status must be 'open', 'done', or 'all'"
    if status != "all":
        rows = [r for r in rows if r.get("status") == status]
    if not rows:
        return f"(no {status} tasks)"
    rank = {"high": 0, "normal": 1, "low": 2}
    rows.sort(key=lambda r: (rank.get(r.get("priority"), 1), r.get("due") or "~"))
    lines = [
        f"#{r['id']} [{r.get('priority', 'normal')}] {r['text']}"
        + (f" (due {r['due']})" if r.get("due") else "")
        + ("" if r.get("status") == "open" else " ✓")
        for r in rows
    ]
    return _truncate("\n".join(lines), 4000)


def _tool_pa_complete_task(task_id: int) -> str:
    with _LOCK:
        rows = _load("tasks.json")
        for row in rows:
            if int(row.get("id", 0)) == int(task_id):
                if row.get("status") == "done":
                    return f"Task #{task_id} was already done"
                row["status"] = "done"
                row["completed"] = _now().isoformat()
                err = _save("tasks.json", rows)
                return err or f"Completed task #{task_id}: {row['text']}"
    return f"ERROR: no task with id {task_id}"


# --------------------------------------------------------------------------- #
# Notes                                                                        #
# --------------------------------------------------------------------------- #
def _tool_pa_add_note(text: str, tags: str = "") -> str:
    if not text.strip():
        return "ERROR: note text is empty"
    with _LOCK:
        rows = _load("notes.json")
        note = {
            "id": _next_id(rows),
            "text": text.strip(),
            "tags": [t.strip() for t in tags.split(",") if t.strip()],
            "created": _now().isoformat(),
        }
        rows.append(note)
        err = _save("notes.json", rows)
    return err or f"Saved note #{note['id']}"


def _tool_pa_list_notes(query: str = "") -> str:
    rows = _load("notes.json")
    q = query.strip().lower()
    if q:
        rows = [
            r for r in rows
            if q in r.get("text", "").lower()
            or q in " ".join(r.get("tags", [])).lower()
        ]
    if not rows:
        return "(no matching notes)" if q else "(no notes)"
    lines = [
        f"#{r['id']} {r['text']}" + (f"  [{', '.join(r['tags'])}]" if r.get("tags") else "")
        for r in rows
    ]
    return _truncate("\n".join(lines), 4000)


# --------------------------------------------------------------------------- #
# Reminders                                                                    #
# --------------------------------------------------------------------------- #
def _tool_pa_set_reminder(text: str, at: str) -> str:
    if not text.strip():
        return "ERROR: reminder text is empty"
    when = _parse_when(at)
    if when is None:
        return f"ERROR: could not parse time '{at}' (use ISO-8601, e.g. 2026-07-12T09:00)"
    with _LOCK:
        rows = _load("reminders.json")
        rem = {
            "id": _next_id(rows),
            "text": text.strip(),
            "at": when.isoformat(),
            "fired": False,
        }
        rows.append(rem)
        err = _save("reminders.json", rows)
    return err or f"Set reminder #{rem['id']} for {rem['at']}: {rem['text']}"


def _tool_pa_due_reminders(now: str = "") -> str:
    """List reminders due at/before *now* (default: the current time)."""
    cutoff = _parse_when(now) if now.strip() else _now()
    if cutoff is None:
        return f"ERROR: could not parse time '{now}'"
    rows = _load("reminders.json")
    due = []
    for row in rows:
        when = _parse_when(row.get("at", ""))
        if when is not None and when <= cutoff and not row.get("fired"):
            due.append(row)
    if not due:
        return "(no reminders due)"
    due.sort(key=lambda r: r.get("at", ""))
    return "\n".join(f"#{r['id']} [{r['at']}] {r['text']}" for r in due)


# --------------------------------------------------------------------------- #
# E-mail drafts (drafting only — sending is a gated outward action)           #
# --------------------------------------------------------------------------- #
def _tool_pa_draft_email(to: str, subject: str, body: str) -> str:
    if not to.strip():
        return "ERROR: recipient 'to' is empty"
    with _LOCK:
        rows = _load("drafts.json")
        draft = {
            "id": _next_id(rows),
            "to": to.strip(),
            "subject": subject.strip(),
            "body": body,
            "created": _now().isoformat(),
        }
        rows.append(draft)
        err = _save("drafts.json", rows)
    if err:
        return err
    return (
        f"Drafted e-mail #{draft['id']} to {draft['to']} "
        f"(subject: {draft['subject'] or '(none)'}). "
        "Draft saved locally; sending requires explicit human approval."
    )


# --------------------------------------------------------------------------- #
# Daily briefing (composes the other stores)                                  #
# --------------------------------------------------------------------------- #
def _tool_pa_daily_brief(date: str = "") -> str:
    day = date.strip() or _now().date().isoformat()
    parts: List[str] = [f"Daily briefing for {day}", "=" * 32]

    open_tasks = [r for r in _load("tasks.json") if r.get("status") == "open"]
    parts.append(f"\nOpen tasks ({len(open_tasks)}):")
    if open_tasks:
        rank = {"high": 0, "normal": 1, "low": 2}
        open_tasks.sort(key=lambda r: (rank.get(r.get("priority"), 1), r.get("due") or "~"))
        parts += [
            f"  - #{r['id']} [{r.get('priority', 'normal')}] {r['text']}"
            + (f" (due {r['due']})" if r.get("due") else "")
            for r in open_tasks[:20]
        ]
    else:
        parts.append("  (none)")

    due = _tool_pa_due_reminders(f"{day}T23:59:59")
    parts.append("\nReminders due today:")
    parts.append("  " + due.replace("\n", "\n  ") if not due.startswith("(") else "  (none)")

    notes = _load("notes.json")[-3:]
    parts.append("\nRecent notes:")
    parts += [f"  - {r['text']}" for r in reversed(notes)] if notes else ["  (none)"]

    return _truncate("\n".join(parts), 4000)


ASSISTANT_TOOLS: List[Tool] = [
    Tool(
        name="pa_add_task",
        description="Add a to-do task with an optional ISO-8601 due date and priority.",
        parameters={
            "text": {"type": "string", "description": "What needs doing."},
            "due": {"type": "string", "description": "ISO-8601 due date/time.", "default": ""},
            "priority": {"type": "string", "description": "low | normal | high.", "default": "normal"},
        },
        func=_tool_pa_add_task,
        required=["text"],
    ),
    Tool(
        name="pa_list_tasks",
        description="List tasks. status = open | done | all (default open).",
        parameters={
            "status": {"type": "string", "description": "open | done | all.", "default": "open"},
        },
        func=_tool_pa_list_tasks,
        required=[],
    ),
    Tool(
        name="pa_complete_task",
        description="Mark a task done by its numeric id.",
        parameters={"task_id": {"type": "integer", "description": "The task id."}},
        func=_tool_pa_complete_task,
        required=["task_id"],
    ),
    Tool(
        name="pa_add_note",
        description="Save a note with optional comma-separated tags.",
        parameters={
            "text": {"type": "string", "description": "The note body."},
            "tags": {"type": "string", "description": "Comma-separated tags.", "default": ""},
        },
        func=_tool_pa_add_note,
        required=["text"],
    ),
    Tool(
        name="pa_list_notes",
        description="List notes, optionally filtered by a text/tag query.",
        parameters={
            "query": {"type": "string", "description": "Substring/tag filter.", "default": ""},
        },
        func=_tool_pa_list_notes,
        required=[],
    ),
    Tool(
        name="pa_set_reminder",
        description="Set a reminder for a specific ISO-8601 time.",
        parameters={
            "text": {"type": "string", "description": "What to be reminded about."},
            "at": {"type": "string", "description": "ISO-8601 time, e.g. 2026-07-12T09:00."},
        },
        func=_tool_pa_set_reminder,
        required=["text", "at"],
    ),
    Tool(
        name="pa_due_reminders",
        description="List reminders due at or before a time (default: now).",
        parameters={
            "now": {"type": "string", "description": "ISO-8601 cutoff time.", "default": ""},
        },
        func=_tool_pa_due_reminders,
        required=[],
    ),
    Tool(
        name="pa_draft_email",
        description=(
            "Draft an e-mail and save it locally. Drafting only — sending is an "
            "outward action requiring explicit human approval."
        ),
        parameters={
            "to": {"type": "string", "description": "Recipient address."},
            "subject": {"type": "string", "description": "Subject line."},
            "body": {"type": "string", "description": "Message body."},
        },
        func=_tool_pa_draft_email,
        required=["to", "body"],
    ),
    Tool(
        name="pa_daily_brief",
        description="Compose a daily briefing from open tasks, due reminders, and recent notes.",
        parameters={
            "date": {"type": "string", "description": "ISO date (default today).", "default": ""},
        },
        func=_tool_pa_daily_brief,
        required=[],
    ),
]
