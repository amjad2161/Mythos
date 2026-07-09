"""
mythos/agent.py
---------------
MythosAgent – the core autonomous AI agent.

Architecture overview
---------------------

  User / external trigger
         │
         ▼
  ┌─────────────────────────────────────────────────────┐
  │  MythosAgent.run(goal)                              │
  │                                                     │
  │  1. Build system prompt (with tool list + plan)     │
  │  2. Planner creates initial Plan                    │
  │  3. Executor drives each task:                      │
  │       LLM → tool call → result → LLM (loop)        │
  │  4. Monitor watches for anomalies / reflection      │
  │  5. Return final conclusion                         │
  └─────────────────────────────────────────────────────┘

The agent is fully autonomous – it decides *what* to do and *when* to
stop.  The caller sets the goal; everything else is up to Mythos.
"""
from __future__ import annotations

import textwrap
from typing import Callable, List, Optional

from .config import MythosConfig
from .executor import Executor
from .llm import BaseLLM, create_llm
from .memory import Memory
from .monitor import Monitor
from .planner import Plan, Planner, Task, TaskStatus
from .tools import ToolRegistry, build_default_registry


# ---------------------------------------------------------------------------
# System prompt template
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = textwrap.dedent("""\
    You are Mythos – a fully autonomous AI agent.

    IDENTITY
    --------
    You are NOT a copilot or assistant that waits for instructions.
    You pursue goals independently, reason through challenges, and
    take decisive action using the tools available to you.

    OPERATING PRINCIPLES
    --------------------
    1. Understand the goal completely before acting.
    2. Break complex goals down in your reasoning and tackle them step by step.
    3. Use tools purposefully – prefer the most specific tool for the job.
    4. After each tool result, reason about what to do next.
    5. When a goal is fully achieved, call the `finish` tool immediately
       with a concise conclusion.
    6. If you encounter an obstacle, try an alternative approach rather
       than repeating the same failing action.
    7. Be decisive and efficient – do not ask the user for clarification
       unless absolutely necessary.

    AVAILABLE TOOLS
    ---------------
    {tool_list}

    CURRENT PLAN
    ------------
    {plan_summary}
""")


# ---------------------------------------------------------------------------
# MythosAgent
# ---------------------------------------------------------------------------

class MythosAgent:
    """
    The fully autonomous Mythos agent.

    Usage
    -----
    ::

        agent = MythosAgent()
        result = agent.run("Research the top 3 Python web frameworks and write a comparison to /tmp/comparison.md")
        print(result)

    The agent operates a Reason → Act → Observe loop until either the
    ``finish`` tool is called, the goal is complete, or the monitor
    raises a hard stop.
    """

    def __init__(
        self,
        config: Optional[MythosConfig] = None,
        llm: Optional[BaseLLM] = None,
        registry: Optional[ToolRegistry] = None,
    ) -> None:
        self.config = config or MythosConfig.from_env()

        # LLM – allow injection for testing
        self._llm: BaseLLM = llm or create_llm(
            provider=self.config.llm_provider,
            model=self.config.llm_model,
            api_key=self.config.llm_api_key,
        )

        # Memory, planner, monitor
        self._memory = Memory(
            window=self.config.memory_window,
            persist=self.config.persist_memory,
            path=self.config.memory_path,
        )
        self._planner = Planner()
        self._monitor = Monitor(
            max_iterations=self.config.max_iterations,
            max_consecutive_failures=self.config.max_consecutive_failures,
            reflection_interval=self.config.reflection_interval,
            max_total_tokens=self.config.max_total_tokens,
            max_wall_seconds=self.config.max_wall_seconds,
        )

        # Tool registry – allow injection for testing
        self._registry: ToolRegistry = registry or build_default_registry()
        self._wire_memory_tools()

        # Executor
        self._executor = Executor(
            llm=self._llm,
            memory=self._memory,
            registry=self._registry,
            monitor=self._monitor,
            verbose=self.config.verbose,
        )

        # Structured outcome of the most recent run() – callers that need to
        # branch on success (e.g. swarm workers) read these instead of
        # pattern-matching the conclusion text.
        self.last_run_ok: bool = True
        self.last_halt_reason: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, goal: str) -> str:
        """
        Pursue *goal* autonomously and return the final conclusion.

        This is the primary entry point.  The method blocks until the
        agent either completes the goal or hits a safety limit.
        """
        if self.config.verbose:
            print(f"\n{'='*60}")
            print(f"  Mythos  –  Autonomous AI Agent")
            print(f"{'='*60}")
            print(f"  Goal: {goal}")
            print(f"{'='*60}\n")

        # Reset per-run state so a reused agent starts each goal cleanly
        # (fresh iteration/failure counters, no leftover system prompt).
        self._monitor.reset()
        self.last_run_ok = True
        self.last_halt_reason = None

        # Create initial plan
        plan = self._planner.new_plan(goal)

        # Build and inject system prompt
        system_prompt = self._build_system_prompt(plan)
        self._memory.reset_short_term()
        self._memory.add_message("system", system_prompt)
        self._memory.add_message("user", f"Goal: {goal}")

        # Main task loop
        conclusion = ""
        while not plan.is_complete():
            task = plan.next_task()
            if task is None:
                if plan.has_failures():
                    conclusion = "Some tasks failed. " + self._collect_results(plan)
                    self.last_run_ok = False
                    self.last_halt_reason = "task_failures"
                elif not plan.is_complete():
                    # Remaining tasks exist but none is runnable (unsatisfied
                    # dependencies) – report the deadlock instead of silently
                    # claiming success.
                    stuck = [
                        f"[{t.id}] {t.description}"
                        for t in plan.all_tasks()
                        if t.status == TaskStatus.PENDING
                    ]
                    conclusion = (
                        "Agent halted: "
                        f"{len(stuck)} task(s) could not be started due to "
                        f"unsatisfied dependencies: " + "; ".join(stuck)
                    )
                    self.last_run_ok = False
                    self.last_halt_reason = "deadlocked_plan"
                break

            if self.config.verbose:
                print(f"\n[Plan] Executing task [{task.id}]: {task.description}")

            # Update system prompt with current plan state
            self._refresh_system_prompt(plan)

            # Run the task
            result = self._executor.run_task(
                task=task,
                plan=plan,
                temperature=self.config.llm_temperature,
                max_tokens=self.config.llm_max_tokens,
            )

            # Check monitor health after each task
            health = self._monitor.health()
            if not health.is_healthy and not plan.is_complete():
                if self.config.verbose:
                    print(f"\n[Monitor] Alert: {health.alert}")
                conclusion = f"Agent stopped: {health.alert}"
                self.last_run_ok = False
                self.last_halt_reason = health.alert
                break

            conclusion = result

        if not conclusion:
            conclusion = self._collect_results(plan)

        if self.config.verbose:
            print(f"\n{'='*60}")
            print(f"  Final conclusion: {conclusion}")
            print(f"  {self._monitor.stats()}")
            print(f"{'='*60}\n")

        return conclusion

    def add_tool(self, tool) -> None:  # noqa: ANN001
        """Register a custom tool with the agent."""
        self._registry.register(tool)

    @property
    def monitor(self) -> Monitor:
        """The agent's monitor (read access for callers tracking usage/health)."""
        return self._monitor

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_system_prompt(self, plan: Plan) -> str:
        tool_names = ", ".join(self._registry.names())
        prompt = _SYSTEM_PROMPT.format(
            tool_list=tool_names,
            plan_summary=plan.summary(),
        )
        if self.config.system_suffix:
            prompt = f"{prompt}\n{self.config.system_suffix}"
        return prompt

    def _refresh_system_prompt(self, plan: Plan) -> None:
        """Replace the system message with an updated plan summary."""
        messages = self._memory.short.get_all()
        for msg in messages:
            if msg.role == "system":
                msg.content = self._build_system_prompt(plan)
                return
        # If no system message found, add one
        self._memory.add_message("system", self._build_system_prompt(plan))

    def _collect_results(self, plan: Plan) -> str:
        """Build a summary string from all completed tasks."""
        parts = []
        for task in plan.all_tasks():
            if task.status == TaskStatus.DONE and task.result:
                parts.append(task.result)
        return " | ".join(parts) if parts else "Goal processing finished."

    def _wire_memory_tools(self) -> None:
        """Replace stub memory tool functions with closures over the real memory."""
        mem = self._memory

        def memory_store(key: str, value: str) -> str:
            mem.long.set(key, value)
            return f"Stored '{key}' in long-term memory."

        def memory_recall(key: str) -> str:
            val = mem.long.get(key)
            if val is None:
                return f"Key '{key}' not found in long-term memory."
            return str(val)

        def memory_list() -> str:
            keys = mem.long.keys()
            return ", ".join(keys) if keys else "(long-term memory is empty)"

        for name, func in [
            ("memory_store", memory_store),
            ("memory_recall", memory_recall),
            ("memory_list", memory_list),
        ]:
            tool = self._registry.get(name)
            if tool:
                tool.func = func
