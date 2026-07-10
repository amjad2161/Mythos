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
              MYTHOS_LLM_PROVIDER     LLM provider (anthropic | openai | local | stub)  [default: anthropic]
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
        choices=["openai", "anthropic", "local", "ollama", "stub"],
        help=(
            "LLM provider to use (overrides MYTHOS_LLM_PROVIDER). "
            "'local'/'ollama' target any OpenAI-compatible endpoint "
            "(MYTHOS_LOCAL_URL, default Ollama on localhost:11434)."
        ),
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

    persona_group = parser.add_argument_group("persona library")
    persona_group.add_argument(
        "--persona",
        metavar="NAME",
        default=None,
        help=(
            "Adopt a specialist persona from the library (e.g. "
            "engineering-backend-architect) for a single-agent run."
        ),
    )
    persona_group.add_argument(
        "--list-personas",
        action="store_true",
        help="List the available specialist personas and exit.",
    )

    kb_group = parser.add_argument_group("knowledge base")
    kb_group.add_argument(
        "--ingest",
        metavar="FILE",
        default=None,
        help=(
            "Ingest a taxonomy/outline file into the Data Matrix as a linked "
            "knowledge graph (uses the configured --matrix backend), then exit."
        ),
    )
    kb_group.add_argument(
        "--kb-name",
        default=None,
        help="Name for the ingested knowledge base (default: the file stem).",
    )
    kb_group.add_argument(
        "--kb-query",
        metavar="NEED",
        default=None,
        help=(
            "Navigate the Data Matrix for NEED and print the fused context "
            "(pair with --ingest to ingest-then-query in one run), then exit."
        ),
    )

    pc_group = parser.add_argument_group("local install (PC)")
    pc_group.add_argument(
        "--init",
        action="store_true",
        help="Write the config template to ~/.mythos/env and exit.",
    )
    pc_group.add_argument(
        "--doctor",
        action="store_true",
        help="Diagnose the local environment (API key, packages, services) and exit.",
    )
    pc_group.add_argument(
        "--serve",
        action="store_true",
        help="Start the local web control panel for the swarm.",
    )
    pc_group.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address for --serve (default: 127.0.0.1).",
    )
    pc_group.add_argument(
        "--port",
        type=int,
        default=8642,
        help="Port for --serve (default: 8642).",
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

    if getattr(args, "persona", None):
        from mythos.orchestration.personas import get_library_persona  # noqa: PLC0415

        persona = get_library_persona(args.persona)
        if persona is None:
            from mythos.orchestration.personas import list_library  # noqa: PLC0415

            raise SystemExit(
                f"error: unknown persona '{args.persona}'. "
                f"Run --list-personas to see the {len(list_library())} available."
            )
        config.system_suffix = persona.compile_system_suffix()

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


def _build_orch_config(args: argparse.Namespace, config: MythosConfig):
    from mythos.orchestration import OrchestrationConfig  # noqa: PLC0415

    orch_config = OrchestrationConfig.from_env()
    if args.bus is not None:
        orch_config.bus_backend = args.bus
    if args.matrix is not None:
        orch_config.matrix_backend = args.matrix
    if args.dynamic:
        orch_config.dynamic = True
        orch_config.fallback_workflow = args.workflow
    orch_config.verbose = config.verbose
    return orch_config


def run_swarm(args: argparse.Namespace, config: MythosConfig) -> int:
    """Run one goal (or an interactive session) through the multi-agent swarm."""
    from mythos.orchestration.orchestrator import SwarmTimeoutError  # noqa: PLC0415
    from mythos.orchestration.runtime import SwarmRuntime  # noqa: PLC0415
    from mythos.orchestration.workflows import get_workflow  # noqa: PLC0415

    orch_config = _build_orch_config(args, config)
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
        if args.goal:
            try:
                conclusion = runtime.run(args.goal)
            except SwarmTimeoutError as exc:
                print(f"error: the swarm timed out: {exc}", file=sys.stderr)
                return 1
            print(conclusion)
            return 0

        # Interactive swarm session – the runtime (and its shared Data
        # Matrix) persists across goals.
        print("Mythos swarm – interactive session  (type 'exit' or Ctrl-C to quit)\n")
        while True:
            try:
                goal = input("Swarm goal > ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nGoodbye.")
                return 0
            if not goal:
                continue
            if goal.lower() in ("exit", "quit", "q"):
                print("Goodbye.")
                return 0
            try:
                print(f"\n{runtime.run(goal)}\n")
            except SwarmTimeoutError as exc:
                print(f"error: the swarm timed out: {exc}\n", file=sys.stderr)
    finally:
        runtime.shutdown()


def run_knowledge_base(args: argparse.Namespace, config: MythosConfig) -> int:
    """Ingest a taxonomy file and/or query the Data Matrix, then exit.

    Uses the configured ``--matrix`` backend: ``qdrant`` persists the knowledge
    base for later swarm runs; ``inmemory`` is process-local (useful to ingest
    and immediately ``--kb-query`` in the same invocation as a smoke test).
    """
    import os  # noqa: PLC0415

    from mythos.orchestration.ingest import ingest_taxonomy  # noqa: PLC0415
    from mythos.orchestration.matrix import fuse_context  # noqa: PLC0415
    from mythos.orchestration.runtime import create_matrix  # noqa: PLC0415

    orch_config = _build_orch_config(args, config)
    matrix = create_matrix(orch_config)

    if args.ingest:
        try:
            with open(args.ingest, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            print(f"error: cannot read '{args.ingest}': {exc}", file=sys.stderr)
            return 1
        kb_name = args.kb_name or os.path.splitext(os.path.basename(args.ingest))[0]
        result = ingest_taxonomy(matrix, text, kb_name)
        print(result.summary())

    if args.kb_query:
        nodes = matrix.navigate(args.kb_query, top_k=5, hops=1)
        if not nodes:
            print(f"(no knowledge-base matches for {args.kb_query!r})")
        else:
            print(f"\nTop knowledge for {args.kb_query!r}:")
            print(fuse_context(nodes))

    return 0


def run_serve(args: argparse.Namespace, config: MythosConfig) -> int:
    """Start the local web control panel."""
    from mythos.orchestration.runtime import SwarmRuntime  # noqa: PLC0415
    from mythos.orchestration.server import serve_forever  # noqa: PLC0415
    from mythos.orchestration.workflows import get_workflow  # noqa: PLC0415

    orch_config = _build_orch_config(args, config)
    try:
        workflow = get_workflow(args.workflow)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    def runtime_factory():
        return SwarmRuntime(
            config=orch_config,
            agent_config=config,
            workflow=workflow,
        )

    serve_forever(runtime_factory, host=args.host, port=args.port)
    return 0


def main() -> int:
    args = parse_args()

    if args.version:
        from mythos import __version__  # noqa: PLC0415
        print(f"Mythos {__version__}")
        return 0

    if args.list_personas:
        from mythos.orchestration.personas import load_library  # noqa: PLC0415

        library = load_library()
        print(f"Specialist personas ({len(library)}):\n")
        for slug, persona in library.items():
            print(f"  {slug}\n    {persona.mission[:100]}")
        print("\nUse one with:  python main.py --persona <name> \"your goal\"")
        return 0

    # PC quality-of-life: pick up ~/.mythos/env and ./.env before anything
    # reads the environment (exported variables always win).
    from mythos.envfile import load_default_env_files, write_env_template  # noqa: PLC0415

    load_default_env_files()

    if args.init:
        from mythos.envfile import USER_ENV_PATH  # noqa: PLC0415

        if write_env_template():
            print(f"Wrote config template to {USER_ENV_PATH}")
            print("Edit it (at minimum: ANTHROPIC_API_KEY), then run: mythos --doctor")
        else:
            print(f"Config already exists at {USER_ENV_PATH} - leaving it untouched.")
        return 0

    if args.doctor:
        from mythos.doctor import doctor_exit_code, format_report, run_doctor  # noqa: PLC0415

        results = run_doctor()
        print(format_report(results))
        return doctor_exit_code(results)

    config = build_config(args)

    if args.ingest or args.kb_query:
        return run_knowledge_base(args, config)

    if args.serve:
        return run_serve(args, config)

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
