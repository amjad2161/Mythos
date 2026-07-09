"""
tests/orchestration/test_personas.py
------------------------------------
Persona parsing, loading/overrides, and injection into worker prompts.
"""
import json

import pytest

from mythos.llm import LLMResponse, StubLLM
from mythos.orchestration.bus import InMemoryBus
from mythos.orchestration.matrix import HashEmbedder, InMemoryDataMatrix
from mythos.orchestration.personas import (
    Persona,
    PersonaError,
    builtin_personas,
    load_personas,
    parse_persona,
)
from mythos.orchestration.worker import WorkerAgent

from .conftest import make_agent_config, make_orch_config, make_payload

VALID = """\
---
name: Tester
role: backend_dev
mission: Test things thoroughly.
rules:
  - Never guess.
  - Always verify.
success_metrics:
  - Zero regressions.
---
Body guidance here.
"""


class TestParsing:
    def test_happy_path(self):
        persona = parse_persona(VALID)
        assert persona.name == "Tester"
        assert persona.role == "backend_dev"
        assert persona.rules == ["Never guess.", "Always verify."]
        assert persona.success_metrics == ["Zero regressions."]
        assert "Body guidance" in persona.body

    def test_compile_system_suffix(self):
        suffix = parse_persona(VALID).compile_system_suffix()
        assert "You are Tester" in suffix
        assert "1. Never guess." in suffix
        assert "- Zero regressions." in suffix
        assert "Body guidance here." in suffix

    def test_missing_role_rejected(self):
        text = "---\nname: X\nmission: Y\n---\n"
        with pytest.raises(PersonaError, match="role"):
            parse_persona(text)

    def test_missing_frontmatter_rejected(self):
        with pytest.raises(PersonaError):
            parse_persona("just some text")

    def test_unterminated_frontmatter_rejected(self):
        with pytest.raises(PersonaError, match="unterminated"):
            parse_persona("---\nname: X\n")

    def test_list_item_outside_list_rejected(self):
        text = "---\nname: X\nrole: r\nmission: m\n- stray item\n---\n"
        with pytest.raises(PersonaError):
            parse_persona(text)


class TestLoading:
    def test_builtin_personas_cover_all_worker_roles(self):
        personas = builtin_personas()
        assert set(personas) >= {"backend_dev", "critic", "researcher", "navigator", "voice"}

    def test_override_dir_wins(self, tmp_path):
        override = tmp_path / "backend_dev.md"
        override.write_text(
            "---\nname: Override\nrole: backend_dev\nmission: Override mission.\n---\n"
        )
        personas = builtin_personas(str(tmp_path))
        assert personas["backend_dev"].name == "Override"
        assert "critic" in personas  # non-overridden roles survive

    def test_duplicate_role_in_dir_rejected(self, tmp_path):
        for name in ("a.md", "b.md"):
            (tmp_path / name).write_text(
                "---\nname: X\nrole: dup\nmission: m\n---\n"
            )
        with pytest.raises(PersonaError, match="duplicate"):
            load_personas(str(tmp_path))

    def test_missing_dir_is_empty(self):
        assert load_personas("/no/such/dir") == {}


class TestWiring:
    def test_persona_reaches_worker_system_prompt(self):
        seen = []

        class RecordingStub(StubLLM):
            def chat(self, messages, tools=None, temperature=0.2, max_tokens=4096):
                seen.append(json.dumps(messages))
                return LLMResponse(content=None, tool_name="finish",
                                   tool_args={"conclusion": "ok"})

        persona = Persona(
            name="Marker", role="backend_dev",
            mission="UNIQUE_PERSONA_MARKER mission.",
        )
        worker = WorkerAgent(
            role="backend_dev",
            bus=InMemoryBus(),
            matrix=InMemoryDataMatrix(HashEmbedder()),
            config=make_orch_config(),
            agent_config=make_agent_config(),
            llm_factory=RecordingStub,
            persona=persona,
        )
        worker.handle(make_payload())
        assert "UNIQUE_PERSONA_MARKER" in seen[0]
        assert "You are Marker" in seen[0]
