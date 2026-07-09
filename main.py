#!/usr/bin/env python3
"""
main.py
-------
Command-line entry point for the Mythos autonomous AI agent.

Usage
-----
    python main.py "Your goal here"
    python main.py --provider stub "Calculate the area of a circle with radius 7"
    python main.py --help
"""
from __future__ import annotations

import argparse
import sys
import textwrap

from mythos import MythosAgent, MythosConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mythos",
        description=(
            "Mythos – Full Autonomous AI System\n"
            "A self-directed agent that plans, acts, and reflects autonomously."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python main.py "Write a Python script that prints the Fibonacci sequence to /tmp/fib.py"
              python main.py --provider stub --verbose "What is 2 ** 32?"
              python main.py --model claude-opus-4-5 --max-iterations 20 "Research quantum computing and summarise"

            Environment variables:
              ANTHROPIC_API_KEY       Anthropic API key (used by the default Claude backend)
              MYTHOS_API_KEY          Override API key for any LLM provider
              MYTHOS_LLM_PROVIDER     LLM provider (anthropic | openai | stub)  [default: anthropic]
              MYTHOS_LLM_MODEL        Model name  [default: claude-opus-4-5]
              MYTHOS_LLM_TEMPERATURE  Sampling temperature (0.0 – 1.0)
              MYTHOS_MAX_ITERATIONS   Hard iteration cap (default: 50)
              MYTHOS_VERBOSE          Set to 'false' to suppress output
        """),
    )

    parser.add_argument(
        "goal",
        nargs="?",
        help="The goal for the agent to pursue. If omitted, enter interactive mode.",
    )
    parser.add_argument(
        "--provider",
        default=None,
        choices=["openai", "anthropic", "stub"],
        help="LLM provider to use (overrides MYTHOS_LLM_PROVIDER).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="LLM model name (overrides MYTHOS_LLM_MODEL).",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key for the LLM provider (overrides MYTHOS_API_KEY).",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Maximum number of autonomous iterations (default: 50).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=None,
        help="Enable verbose output (default: enabled).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        default=False,
        help="Suppress all output except the final conclusion.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the Mythos version and exit.",
    )

    swarm_group = parser.add_argument_group("multi-agent swarm (Phase A)")
    swarm_group.add_argument(
        "--swarm",
        action="store_true",
        help="Run the goal through the multi-agent swarm instead of a single agent.",
    )
    swarm_group.add_argument(
        "--bus",
        default=None,
        choices=["rabbitmq", "inmemory"],
        help="Message bus backend for --swarm (overrides MYTHOS_BUS).",
    )
    swarm_group.add_argument(
        "--matrix",
        default=None,
        choices=["qdrant", "inmemory"],
        help="Data Matrix backend for --swarm (overrides MYTHOS_MATRIX).",
    )
    swarm_group.add_argument(
        "--workflow",
        default="code_delivery",
        help="Named rigid workflow to run for --swarm (default: code_delivery).",
    )
    swarm_group.add_argument(
        "--dynamic",
        action="store_true",
        help=(
            "Decompose the goal dynamically with a routing LLM instead of a "
            "rigid workflow (the named --workflow becomes the fallback)."
        ),
    )

    args = parser.parse_args()

    if args.max_iterations is not None and args.max_iterations < 1:
        parser.error("--max-iterations must be a positive integer")

    return args


def build_config(args: argparse.Namespace) -> MythosConfig:
    config = MythosConfig.from_env()

    if args.provider is not None:
        config.llm_provider = args.provider
    if args.model is not None:
        config.llm_model = args.model
    if args.api_key is not None:
        config.llm_api_key = args.api_key
    if args.max_iterations is not None:
        config.max_iterations = args.max_iterations
    if args.quiet:
        config.verbose = False
    elif args.verbose:
        config.verbose = True

    return config


def interactive_mode(agent: MythosAgent) -> None:
    """Simple read-eval-print loop for interactive use."""
    print("Mythos – Autonomous AI Agent  (type 'exit' or Ctrl-C to quit)\n")
    while True:
        try:
            goal = input("Goal > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye.")
            break

        if not goal:
            continue
        if goal.lower() in ("exit", "quit", "q"):
            print("Goodbye.")
            break

        conclusion = agent.run(goal)
        print(f"\nConclusion: {conclusion}\n")


def run_swarm(args: argparse.Namespace, config: MythosConfig) -> int:
    """Run the goal through the Phase A multi-agent swarm."""
    from mythos.orchestration import OrchestrationConfig  # noqa: PLC0415
    from mythos.orchestration.runtime import SwarmRuntime  # noqa: PLC0415
    from mythos.orchestration.workflows import get_workflow  # noqa: PLC0415

    if not args.goal:
        print("error: --swarm requires a goal argument", file=sys.stderr)
        return 2

    from mythos.orchestration.orchestrator import SwarmTimeoutError  # noqa: PLC0415

    orch_config = OrchestrationConfig.from_env()
    if args.bus is not None:
        orch_config.bus_backend = args.bus
    if args.matrix is not None:
        orch_config.matrix_backend = args.matrix
    if args.dynamic:
        orch_config.dynamic = True
        orch_config.fallback_workflow = args.workflow
    orch_config.verbose = config.verbose

    try:
        workflow = get_workflow(args.workflow)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    runtime = SwarmRuntime(
        config=orch_config,
        agent_config=config,
        workflow=workflow,
    )
    try:
        conclusion = runtime.run(args.goal)
    except SwarmTimeoutError as exc:
        print(f"error: the swarm timed out: {exc}", file=sys.stderr)
        return 1
    finally:
        runtime.shutdown()
    print(conclusion)
    return 0


def main() -> int:
    args = parse_args()

    if args.version:
        from mythos import __version__  # noqa: PLC0415
        print(f"Mythos {__version__}")
        return 0

    config = build_config(args)

    if args.swarm:
        return run_swarm(args, config)

    agent = MythosAgent(config=config)

    if args.goal:
        conclusion = agent.run(args.goal)
        # In verbose mode the agent already prints the conclusion in its banner;
        # otherwise (quiet or MYTHOS_VERBOSE=false) print it here so the CLI is
        # never silent about its result.
        if not config.verbose:
            print(conclusion)
        return 0

    # Interactive mode
    interactive_mode(agent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
