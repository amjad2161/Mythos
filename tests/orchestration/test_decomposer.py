"""
tests/orchestration/test_decomposer.py
--------------------------------------
Two-stage dynamic decomposition: prefilter, strict parsing, re-prompt,
fallback, and the full dynamic swarm run over in-memory drivers.
"""
import json

import pytest

from mythos.llm import LLMResponse, StubLLM
from mythos.orchestration.decomposer import (
    DynamicDecomposer,
    parse_decomposition,
    prefilter_roles,
)
from mythos.orchestration.schemas import SchemaError

from .conftest import make_agent_config, make_orch_config


def make_dynamic_config(**overrides):
    return make_orch_config(dynamic=True, **overrides)


def decomposition_json(steps):
    return json.dumps({"steps": steps, "rationale": "test"})


class TestPrefilter:
    def test_backend_dev_is_always_a_candidate(self):
        assert prefilter_roles("write a script") == ["backend_dev"]

    def test_navigation_keywords_route_to_navigator(self):
        roles = prefilter_roles("Plan the fastest route from Haifa to Eilat")
        assert "navigator" in roles

    def test_voice_keywords_route_to_voice(self):
        assert "voice" in prefilter_roles("speak the answer out loud")

    def test_research_keywords_route_to_researcher(self):
        assert "researcher" in prefilter_roles("research the best frameworks")


class TestParsing:
    ALLOWED = ["backend_dev", "researcher"]

    def test_valid_json_parses(self):
        raw = decomposition_json([
            {"role": "backend_dev", "objective": "implement it"},
        ])
        steps, rationale = parse_decomposition(raw, self.ALLOWED, max_steps=6)
        assert steps[0].role == "backend_dev"
        assert rationale == "test"

    def test_fenced_json_accepted(self):
        raw = "```json\n" + decomposition_json(
            [{"role": "backend_dev", "objective": "x"}]
        ) + "\n```"
        steps, _ = parse_decomposition(raw, self.ALLOWED, max_steps=6)
        assert len(steps) == 1

    def test_unknown_role_rejected(self):
        raw = decomposition_json([{"role": "astronaut", "objective": "x"}])
        with pytest.raises(SchemaError, match="astronaut"):
            parse_decomposition(raw, self.ALLOWED, max_steps=6)

    def test_empty_objective_rejected(self):
        raw = decomposition_json([{"role": "backend_dev", "objective": "  "}])
        with pytest.raises(SchemaError, match="objective"):
            parse_decomposition(raw, self.ALLOWED, max_steps=6)

    def test_too_many_steps_rejected(self):
        raw = decomposition_json(
            [{"role": "backend_dev", "objective": f"s{i}"} for i in range(7)]
        )
        with pytest.raises(SchemaError, match="1..6"):
            parse_decomposition(raw, self.ALLOWED, max_steps=6)

    def test_non_json_rejected(self):
        with pytest.raises(SchemaError, match="JSON"):
            parse_decomposition("I think we should...", self.ALLOWED, max_steps=6)

    def test_empty_content_rejected(self):
        with pytest.raises(SchemaError, match="no content"):
            parse_decomposition("", self.ALLOWED, max_steps=6)

    def test_depends_on_parsed(self):
        raw = decomposition_json([
            {"role": "backend_dev", "objective": "a", "depends_on": []},
            {"role": "backend_dev", "objective": "b", "depends_on": []},
            {"role": "backend_dev", "objective": "join", "depends_on": [0, 1]},
        ])
        steps, _ = parse_decomposition(raw, self.ALLOWED, max_steps=6)
        assert steps[0].depends_on == []
        assert steps[2].depends_on == [0, 1]

    def test_depends_on_forward_reference_rejected(self):
        raw = decomposition_json([
            {"role": "backend_dev", "objective": "a", "depends_on": [1]},
            {"role": "backend_dev", "objective": "b"},
        ])
        with pytest.raises(SchemaError, match="depends_on"):
            parse_decomposition(raw, self.ALLOWED, max_steps=6)

    def test_depends_on_self_reference_rejected(self):
        raw = decomposition_json([
            {"role": "backend_dev", "objective": "a", "depends_on": [0]},
        ])
        with pytest.raises(SchemaError, match="depends_on"):
            parse_decomposition(raw, self.ALLOWED, max_steps=6)


class TestDynamicDecomposer:
    def test_valid_first_response_builds_literal_workflow(self):
        llm = StubLLM([LLMResponse(content=decomposition_json([
            {"role": "backend_dev", "objective": "implement {with braces}"},
        ]))])
        workflow = DynamicDecomposer(llm, make_dynamic_config()).decompose("build it")
        assert workflow.name == "dynamic"
        [step] = workflow.steps
        assert step.literal is True
        assert step.objective("ignored-goal") == "implement {with braces}"

    def test_reprompt_carries_parse_error_verbatim(self):
        seen = []

        class RecordingStub(StubLLM):
            def chat(self, messages, tools=None, temperature=0.2, max_tokens=4096):
                seen.append(list(messages))
                return super().chat(messages, tools, temperature, max_tokens)

        llm = RecordingStub([
            LLMResponse(content="not json at all"),
            LLMResponse(content=decomposition_json(
                [{"role": "backend_dev", "objective": "second try"}]
            )),
        ])
        workflow = DynamicDecomposer(llm, make_dynamic_config()).decompose("build it")
        assert workflow.steps[0].objective_template == "second try"
        # The second request contains the rejection with the exact error.
        second_request = json.dumps(seen[1])
        assert "rejected" in second_request
        assert "not valid JSON" in second_request

    def test_double_failure_falls_back_to_configured_workflow(self):
        llm = StubLLM([
            LLMResponse(content="garbage one"),
            LLMResponse(content="garbage two"),
        ])
        workflow = DynamicDecomposer(llm, make_dynamic_config()).decompose("build it")
        assert workflow.name == "code_delivery"

    def test_stub_fallback_tool_call_counts_as_parse_failure(self):
        # The bare StubLLM fallback returns a finish TOOL CALL with no
        # content - the decomposer must treat that as a failure, not crash.
        workflow = DynamicDecomposer(StubLLM(), make_dynamic_config()).decompose("x")
        assert workflow.name == "code_delivery"


class TestDynamicEndToEnd:
    def test_two_step_dynamic_run_over_the_real_queue_flow(self, tmp_path):
        from mythos.orchestration.bus import InMemoryBus
        from mythos.orchestration.matrix import HashEmbedder, InMemoryDataMatrix
        from mythos.orchestration.runtime import SwarmRuntime

        target = tmp_path / "dyn.txt"
        decomposition = decomposition_json([
            {"role": "backend_dev",
             "objective": f"Write 'alpha' to {target}",
             "validation_command": f"grep -q alpha {target}"},
            {"role": "researcher",
             "objective": "Confirm the artifact mentions alpha",
             "validation_command": f"grep -q alpha {target}"},
        ])

        runs = {
            "backend_dev": [
                LLMResponse(content=None, tool_name="write_file",
                            tool_args={"path": str(target), "content": "alpha\n"}),
                LLMResponse(content=None, tool_name="finish",
                            tool_args={"conclusion": f"wrote alpha to {target}"}),
            ],
            "researcher": [
                LLMResponse(content=None, tool_name="read_file",
                            tool_args={"path": str(target)}),
                LLMResponse(content=None, tool_name="finish",
                            tool_args={"conclusion": "confirmed alpha"}),
            ],
        }

        runtime = SwarmRuntime(
            config=make_dynamic_config(),
            agent_config=make_agent_config(),
            bus=InMemoryBus(),
            matrix=InMemoryDataMatrix(HashEmbedder()),
            llm_factories={
                role: (lambda r=role: StubLLM(list(runs[r])))
                for role in runs
            },
            decomposer_llm_factory=lambda: StubLLM([
                LLMResponse(content=decomposition),
            ]),
        )
        try:
            # "research" routes the researcher role through the prefilter.
            conclusion = runtime.run("build alpha and research that it is correct")
        finally:
            runtime.shutdown()

        assert target.read_text() == "alpha\n"
        assert "wrote alpha" in conclusion
        assert "confirmed alpha" in conclusion
