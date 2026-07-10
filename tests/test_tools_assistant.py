"""
tests/test_tools_assistant.py
-----------------------------
The digital-secretary tools over a temp-dir local store.
"""
import pytest

from mythos.tools_assistant import (
    _tool_pa_add_note,
    _tool_pa_add_task,
    _tool_pa_complete_task,
    _tool_pa_daily_brief,
    _tool_pa_draft_email,
    _tool_pa_due_reminders,
    _tool_pa_list_notes,
    _tool_pa_list_tasks,
    _tool_pa_set_reminder,
)


@pytest.fixture(autouse=True)
def _store(monkeypatch, tmp_path):
    monkeypatch.setenv("MYTHOS_ASSISTANT_DIR", str(tmp_path / "assistant"))


class TestTasks:
    def test_add_list_complete_roundtrip(self):
        assert _tool_pa_add_task("Buy milk").startswith("Added task #1")
        _tool_pa_add_task("Ship release", priority="high")
        listing = _tool_pa_list_tasks()
        # high priority sorts first
        assert listing.splitlines()[0].startswith("#2 [high] Ship release")
        assert "Buy milk" in listing
        assert _tool_pa_complete_task(1).startswith("Completed task #1")
        assert "Buy milk" not in _tool_pa_list_tasks("open")
        assert "Buy milk" in _tool_pa_list_tasks("done")

    def test_ids_increment_across_calls(self):
        _tool_pa_add_task("a")
        assert _tool_pa_add_task("b").startswith("Added task #2")

    def test_bad_priority_and_due_rejected(self):
        assert _tool_pa_add_task("x", priority="urgent").startswith("ERROR:")
        assert _tool_pa_add_task("x", due="not-a-date").startswith("ERROR:")

    def test_empty_text_rejected(self):
        assert _tool_pa_add_task("   ").startswith("ERROR:")

    def test_complete_unknown_id(self):
        assert _tool_pa_complete_task(99).startswith("ERROR:")

    def test_list_empty(self):
        assert _tool_pa_list_tasks() == "(no open tasks)"


class TestNotes:
    def test_add_and_query(self):
        _tool_pa_add_note("Sarah prefers mornings", tags="sarah, prefs")
        _tool_pa_add_note("Order more coffee")
        assert "Sarah prefers mornings" in _tool_pa_list_notes("sarah")
        assert "Order more coffee" not in _tool_pa_list_notes("sarah")
        assert "coffee" in _tool_pa_list_notes("coffee")

    def test_no_notes(self):
        assert _tool_pa_list_notes() == "(no notes)"


class TestReminders:
    def test_set_and_due(self):
        _tool_pa_set_reminder("standup", "2026-07-10T09:00")
        _tool_pa_set_reminder("future", "2999-01-01T00:00")
        due = _tool_pa_due_reminders("2026-07-10T12:00")
        assert "standup" in due
        assert "future" not in due

    def test_bad_time_rejected(self):
        assert _tool_pa_set_reminder("x", "whenever").startswith("ERROR:")

    def test_none_due(self):
        _tool_pa_set_reminder("later", "2999-01-01T00:00")
        assert _tool_pa_due_reminders("2026-07-10T00:00") == "(no reminders due)"


class TestDrafts:
    def test_draft_saved_but_not_sent(self):
        result = _tool_pa_draft_email("a@b.com", "Hi", "Body text")
        assert result.startswith("Drafted e-mail #1")
        assert "human approval" in result

    def test_missing_recipient(self):
        assert _tool_pa_draft_email("", "s", "b").startswith("ERROR:")


class TestBriefing:
    def test_brief_composes_stores(self):
        _tool_pa_add_task("Finish deck", priority="high")
        _tool_pa_set_reminder("call bank", "2026-07-10T10:00")
        _tool_pa_add_note("remember parking code 4432")
        brief = _tool_pa_daily_brief("2026-07-10")
        assert "Daily briefing for 2026-07-10" in brief
        assert "Finish deck" in brief
        assert "call bank" in brief
        assert "parking code" in brief
