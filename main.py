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

    return parser.parse_args()


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


def main() -> int:
    import textwrap  # noqa: PLC0415 – delayed to avoid polluting module top-level

    args = parse_args()

    if args.version:
        from mythos import __version__  # noqa: PLC0415
        print(f"Mythos {__version__}")
        return 0

    config = build_config(args)
    agent = MythosAgent(config=config)

    if args.goal:
        conclusion = agent.run(args.goal)
        if args.quiet:
            print(conclusion)
        return 0

    # Interactive mode
    interactive_mode(agent)
    return 0


if __name__ == "__main__":
    import textwrap
    sys.exit(main())
