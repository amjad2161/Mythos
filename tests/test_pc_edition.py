"""
tests/test_pc_edition.py
------------------------
The local-install layer: env files, the doctor, and the web control panel.
"""
import http.client
import json
import threading
import time

import pytest

from mythos.doctor import FAIL, OK, WARN, doctor_exit_code, format_report, run_doctor
from mythos.envfile import load_env_file, parse_env_file, write_env_template
from mythos.llm import LLMResponse, StubLLM
from mythos.orchestration.bus import InMemoryBus
from mythos.orchestration.matrix import HashEmbedder, InMemoryDataMatrix
from mythos.orchestration.runtime import SwarmRuntime
from mythos.orchestration.server import create_server
from mythos.orchestration.workflows import Workflow, WorkflowStep

from .orchestration.conftest import make_agent_config, make_orch_config


class TestEnvFile:
    def test_parse_basics(self):
        parsed = parse_env_file(
            "# comment\n"
            "PLAIN=value\n"
            "QUOTED=\"hello world\"\n"
            "SINGLE='x=y'\n"
            "\n"
            "garbage line without equals\n"
        )
        assert parsed == {"PLAIN": "value", "QUOTED": "hello world", "SINGLE": "x=y"}

    def test_existing_environment_wins(self, tmp_path, monkeypatch):
        env = tmp_path / "env"
        env.write_text("MYTHOS_TEST_KEY=from_file\nMYTHOS_TEST_NEW=fresh\n")
        monkeypatch.setenv("MYTHOS_TEST_KEY", "from_env")
        monkeypatch.delenv("MYTHOS_TEST_NEW", raising=False)
        applied = load_env_file(str(env))
        import os
        assert os.environ["MYTHOS_TEST_KEY"] == "from_env"   # not overridden
        assert os.environ["MYTHOS_TEST_NEW"] == "fresh"
        assert applied == {"MYTHOS_TEST_NEW": "fresh"}
        monkeypatch.delenv("MYTHOS_TEST_NEW")

    def test_missing_file_is_noop(self):
        assert load_env_file("/no/such/env/file") == {}

    def test_template_written_once(self, tmp_path):
        path = str(tmp_path / "conf" / "env")
        assert write_env_template(path) is True
        assert write_env_template(path) is False   # never clobbers
        content = open(path).read()
        assert "ANTHROPIC_API_KEY" in content


class TestDoctor:
    def test_missing_api_key_is_blocking(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("MYTHOS_API_KEY", raising=False)
        results = run_doctor()
        api = next(r for r in results if r.name == "LLM API key")
        assert api.status == FAIL
        assert doctor_exit_code(results) == 1

    def test_api_key_present_is_ok(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        results = run_doctor()
        api = next(r for r in results if r.name == "LLM API key")
        assert api.status == OK

    def test_report_formats_every_check(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        results = run_doctor()
        report = format_report(results)
        for r in results:
            assert r.name in report
        assert any(r.status in (OK, WARN, FAIL) for r in results)


@pytest.fixture()
def dashboard():
    """A control panel over a stub swarm, on an ephemeral port."""
    workflow = Workflow(
        name="pc_demo",
        steps=[WorkflowStep(role="backend_dev", objective_template="Do: {goal}",
                            validation_command_template="true")],
    )

    def runtime_factory():
        return SwarmRuntime(
            config=make_orch_config(),
            agent_config=make_agent_config(),
            workflow=workflow,
            bus=InMemoryBus(),
            matrix=InMemoryDataMatrix(HashEmbedder()),
            llm_factories={"backend_dev": lambda: StubLLM([
                LLMResponse(content=None, tool_name="finish",
                            tool_args={"conclusion": "pc demo done"}),
            ])},
        )

    server, manager = create_server(runtime_factory, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], manager
    server.shutdown()
    server.server_close()
    manager.shutdown()
    thread.join(timeout=5)


def _request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"} if body else {}
    conn.request(method, path, body=json.dumps(body) if body else None, headers=headers)
    response = conn.getresponse()
    data = response.read().decode("utf-8")
    conn.close()
    return response.status, data


class TestControlPanel:
    def test_dashboard_page_served(self, dashboard):
        port, _ = dashboard
        status, body = _request(port, "GET", "/")
        assert status == 200
        assert "Mythos Control Panel" in body

    def test_status_before_first_goal(self, dashboard):
        port, _ = dashboard
        status, body = _request(port, "GET", "/api/status")
        assert status == 200
        assert json.loads(body)["started"] is False

    def test_goal_lifecycle(self, dashboard):
        port, _ = dashboard
        status, body = _request(port, "POST", "/api/goals", {"goal": "make the demo"})
        assert status == 202
        run_id = json.loads(body)["run_id"]

        deadline = time.monotonic() + 20
        final = None
        while time.monotonic() < deadline:
            _, body = _request(port, "GET", f"/api/runs/{run_id}")
            final = json.loads(body)
            if final["status"] in ("completed", "failed"):
                break
            time.sleep(0.1)

        assert final is not None
        assert final["status"] == "completed"
        assert "pc demo done" in final["conclusion"]
        # Live ledger is attached to the run detail.
        assert final["ledger"]["steps"][0]["status"] == "validated"

        _, listing = _request(port, "GET", "/api/runs")
        runs = json.loads(listing)["runs"]
        assert runs[0]["run_id"] == run_id

        _, status_body = _request(port, "GET", "/api/status")
        assert json.loads(status_body)["started"] is True

    def test_bad_goal_rejected(self, dashboard):
        port, _ = dashboard
        status, _ = _request(port, "POST", "/api/goals", {"goal": "  "})
        assert status == 400
        status, _ = _request(port, "GET", "/api/runs/nope")
        assert status == 404
