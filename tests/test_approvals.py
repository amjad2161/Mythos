"""
tests/test_approvals.py
-----------------------
The human-in-the-loop approval gate: action classification, policy gating,
pluggable approver, fail-safe defaults, and ToolRegistry integration.
"""
import pytest

from mythos.approvals import (
    ActionClass,
    ApprovalGate,
    ApprovalPolicy,
    ApprovalRequest,
    classify_action,
)
from mythos.tools import Tool, ToolRegistry


class TestClassifyAction:
    def test_safe_by_default(self):
        assert classify_action("read_file", {"path": "x"}) is ActionClass.SAFE
        assert classify_action("think", {}) is ActionClass.SAFE

    def test_reversible_tools(self):
        assert classify_action("write_file", {}) is ActionClass.REVERSIBLE
        assert classify_action("clipboard_set", {}) is ActionClass.REVERSIBLE
        assert classify_action("pa_draft_email", {}) is ActionClass.REVERSIBLE

    def test_outward_tools(self):
        assert classify_action("open_url", {"url": "x"}) is ActionClass.OUTWARD
        assert classify_action("notify", {}) is ActionClass.OUTWARD
        assert classify_action("speak", {}) is ActionClass.OUTWARD

    def test_shell_is_outward_or_destructive_by_command(self):
        assert classify_action("run_shell", {"command": "ls -la"}) is ActionClass.OUTWARD
        assert classify_action("run_shell", {"command": "rm -rf build"}) is ActionClass.DESTRUCTIVE
        assert classify_action("run_shell", {"command": "pip uninstall foo"}) is ActionClass.DESTRUCTIVE


class TestPolicy:
    def test_default_gates_outward_and_destructive(self):
        p = ApprovalPolicy()
        assert p.requires_confirmation(ActionClass.OUTWARD)
        assert p.requires_confirmation(ActionClass.DESTRUCTIVE)
        assert not p.requires_confirmation(ActionClass.SAFE)
        assert not p.requires_confirmation(ActionClass.REVERSIBLE)


class TestGate:
    def test_disabled_allows_everything(self):
        gate = ApprovalGate(enabled=False)
        assert gate.check("run_shell", {"command": "rm -rf /"}).allowed

    def test_enabled_no_approver_denies_fail_closed(self, monkeypatch):
        monkeypatch.delenv("MYTHOS_AUTO_APPROVE", raising=False)
        gate = ApprovalGate(enabled=True)
        r = gate.check("open_url", {"url": "x"})
        assert not r.allowed
        assert "fail-closed" in r.reason

    def test_enabled_auto_approve_env(self, monkeypatch):
        monkeypatch.setenv("MYTHOS_AUTO_APPROVE", "on")
        gate = ApprovalGate(enabled=True)
        assert gate.check("open_url", {"url": "x"}).allowed

    def test_approver_allows_and_denies(self):
        seen = []

        def approver(req: ApprovalRequest) -> bool:
            seen.append(req)
            return req.tool != "run_shell"  # allow everything except shell

        gate = ApprovalGate(approver=approver, enabled=True)
        assert gate.check("open_url", {"url": "x"}).allowed
        assert not gate.check("run_shell", {"command": "rm -rf build"}).allowed
        # safe actions never reach the approver
        assert gate.check("read_file", {"path": "x"}).allowed
        assert all(r.tool in ("open_url", "run_shell") for r in seen)
        assert seen[0].preview.startswith("open_url(")

    def test_broken_approver_denies(self):
        def boom(req):
            raise RuntimeError("ui crashed")

        gate = ApprovalGate(approver=boom, enabled=True)
        assert not gate.check("open_url", {"url": "x"}).allowed


class TestToolRegistryIntegration:
    def _registry(self):
        reg = ToolRegistry()
        reg.register(Tool(
            name="notify", description="d",
            parameters={"title": {"type": "string"}, "message": {"type": "string"}},
            func=lambda title="", message="": f"Notified: {title}",
            required=[],
        ))
        return reg

    def test_default_off_allows(self, monkeypatch):
        monkeypatch.delenv("MYTHOS_APPROVALS", raising=False)
        # rebuild the default gate to pick up the env
        import mythos.approvals as ap
        ap._DEFAULT_GATE = ap.ApprovalGate()
        assert self._registry().call("notify", {"title": "hi"}) == "Notified: hi"

    def test_enabled_blocks_outward_without_approver(self, monkeypatch):
        monkeypatch.setenv("MYTHOS_APPROVALS", "on")
        monkeypatch.delenv("MYTHOS_AUTO_APPROVE", raising=False)
        import mythos.approvals as ap
        ap._DEFAULT_GATE = ap.ApprovalGate()
        out = self._registry().call("notify", {"title": "hi"})
        assert out.startswith("ERROR: action requires approval")

    def test_registered_approver_allows(self, monkeypatch):
        monkeypatch.setenv("MYTHOS_APPROVALS", "on")
        import mythos.approvals as ap
        ap._DEFAULT_GATE = ap.ApprovalGate()
        ap.set_approver(lambda req: True)
        try:
            assert self._registry().call("notify", {"title": "hi"}) == "Notified: hi"
        finally:
            ap.set_approver(None)

    @pytest.fixture(autouse=True)
    def _reset_gate(self):
        yield
        import mythos.approvals as ap
        ap._DEFAULT_GATE = ap.ApprovalGate()
