"""
mythos/executor.py
------------------
Task executor for the Mythos autonomous agent.

The executor is responsible for driving a single Task to completion by running
the agent's inner tool-calling loop until the task is marked done, the tool
``finish`` is called, or the monitor raises an alert.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict

from .memory import Memory
from .monitor import Monitor
from .planner import Plan, Task, TaskStatus
from .tools import ToolRegistry

if TYPE_CHECKING:
    from .llm import BaseLLM


FINISH_TOOL = "finish"


class Executor:
    """
    Drives a single task to completion using the LLM + tool loop.

    The executor does NOT own the memory, monitor, or registry – it operates on
    shared references provided by the agent.
    """

    def __init__(
        self,
        llm: "BaseLLM",
        memory: Memory,
        registry: ToolRegistry,
        monitor: Monitor,
        verbose: bool = True,
    ) -> None:
        self._llm = llm
        self._memory = memory
        self._registry = registry
        self._monitor = monitor
        self._verbose = verbose
        self._call_seq = 0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run_task(self, task: Task, plan: Plan, temperature: float, max_tokens: int) -> str:
        """
        Execute *task* within *plan* and return the conclusion string.

        The method mutates *task*.status and *task*.result / *task*.error.
        """
        task.status = TaskStatus.IN_PROGRESS
        conclusion = ""

        while True:
            health = self._monitor.health()

            # Hard stop on monitor alert
            if not health.is_healthy:
                msg = f"[Monitor] {health.alert}"
                self._print(msg)
                task.mark_failed(msg)
                return msg

            # Trigger reflection if interval reached
            if health.needs_reflection:
                self._inject_reflection_prompt(plan)

            # --- LLM call ---
            try:
                response = self._llm.chat(
                    messages=self._memory.get_messages(),
                    tools=self._registry.openai_specs(),
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception as exc:  # noqa: BLE001
                self._monitor.record_llm_error(str(exc))
                # Count the failed attempt as an iteration so a persistently
                # failing backend hits the iteration cap instead of looping
                # forever (reflection no longer resets the failure counter).
                self._monitor.record_iteration()
                self._memory.add_message("user", f"[System] LLM error: {exc}. Please continue.")
                continue

            self._monitor.record_iteration()
            self._monitor.record_usage(response.usage)

            # --- Tool dispatch ---
            if response.has_tool_call:
                tool_name = response.tool_name
                tool_args = self._coerce_args(response.tool_args)
                call_id = response.tool_call_id or self._next_call_id()

                # Record the assistant's tool-calling turn (with any stated
                # reasoning) so the provider history stays wire-valid.
                self._memory.add_message(
                    "assistant",
                    response.content or "",
                    tool_name=tool_name,
                    tool_args=tool_args,
                    tool_call_id=call_id,
                )
                if response.content:
                    self._print(f"[Mythos] {response.content}")
                self._print(f"  → Calling tool: {tool_name}({tool_args})")

                # finish – break the loop
                if tool_name == FINISH_TOOL:
                    conclusion = tool_args.get("conclusion", "")
                    self._monitor.record_tool_call(tool_name, True, conclusion)
                    self._monitor.record_goal_complete(conclusion)
                    task.mark_done(conclusion)
                    self._memory.add_message(
                        "tool", conclusion or "(done)", name=tool_name, tool_call_id=call_id
                    )
                    return conclusion

                # Normal tool call
                result = self._registry.call(tool_name, tool_args)
                success = not result.startswith("ERROR:")
                self._monitor.record_tool_call(
                    tool_name, success, result[:120], signature=self._signature(tool_name, tool_args)
                )

                # Feed the tool result back into memory, linked to the call.
                self._memory.add_message("tool", result, name=tool_name, tool_call_id=call_id)
                self._print(f"  ← {tool_name}: {result[:200]}")

            else:
                # Model returned plain text without a tool call.
                if response.content:
                    self._memory.add_message("assistant", response.content)
                    self._print(f"[Mythos] {response.content}")
                # Nudge it to either call a tool or call finish.
                self._memory.add_message(
                    "user",
                    (
                        "[System] Please continue by calling a tool or, "
                        "if the task is complete, call the 'finish' tool with your conclusion."
                    ),
                )

        # unreachable – loop is exited via return statements above

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _next_call_id(self) -> str:
        self._call_seq += 1
        return f"call_{self._call_seq}"

    @staticmethod
    def _coerce_args(args: Any) -> Dict[str, Any]:
        """Ensure tool arguments are a mapping so ``**args`` never crashes."""
        return args if isinstance(args, dict) else {}

    @staticmethod
    def _signature(tool_name: str, tool_args: Dict[str, Any]) -> str:
        """A stable per-call signature (name + arguments) for loop detection."""
        try:
            arg_repr = json.dumps(tool_args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            arg_repr = repr(tool_args)
        return f"{tool_name}:{arg_repr}"

    def _inject_reflection_prompt(self, plan: Plan) -> None:
        """Inject a reflection prompt so the agent can self-assess."""
        reflection_prompt = (
            "[System – Reflection checkpoint]\n"
            f"{plan.summary()}\n\n"
            "Please review your progress: are you on track? "
            "Is there anything you should adjust before continuing?"
        )
        self._memory.add_message("user", reflection_prompt)
        self._monitor.record_reflection("Reflection checkpoint injected.")
        self._print("[Monitor] Reflection checkpoint triggered.")

    def _print(self, msg: str) -> None:
        if self._verbose:
            print(msg)
