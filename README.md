# Mythos

**Mythos** is a small, dependency-light autonomous AI agent framework in Python.
You give it a goal; it plans, acts through tools, observes results, reflects,
and stops when the goal is achieved — a full Reason → Act → Observe loop with
self-monitoring.

```
User goal
   │
   ▼
MythosAgent.run(goal)
   ├── Planner   – tracks the goal as an ordered task list
   ├── Executor  – drives each task: LLM → tool call → result → LLM
   ├── Memory    – short-term message window + long-term key/value store
   ├── Monitor   – iteration caps, failure counters, loop detection, reflection
   └── Tools     – file I/O, shell, math, time, memory, finish
```

## Installation

```bash
git clone https://github.com/amjad2161/Mythos.git
cd Mythos
pip install -e .            # installs the anthropic SDK (default backend)
pip install -e ".[openai]"  # optional: OpenAI backend
pip install -e ".[dev]"     # optional: pytest for development
```

Requires Python 3.9+.

## Quick start

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python main.py "Write a Python script that prints the Fibonacci sequence to /tmp/fib.py"
```

Or from Python:

```python
from mythos import MythosAgent

agent = MythosAgent()
result = agent.run("Research the top 3 Python web frameworks and write a comparison to /tmp/comparison.md")
print(result)
```

Run without a goal for an interactive prompt:

```bash
python main.py
```

Try it offline (no API key needed) with the deterministic stub backend:

```bash
python main.py --provider stub "smoke test"
```

## Configuration

Everything is configurable via `MythosConfig`, CLI flags, or environment variables:

| Environment variable        | Default                | Meaning |
|-----------------------------|------------------------|---------|
| `ANTHROPIC_API_KEY`         | —                      | API key for the default Claude backend |
| `MYTHOS_API_KEY`            | —                      | Overrides the API key for any provider |
| `MYTHOS_LLM_PROVIDER`       | `anthropic`            | `anthropic` \| `openai` \| `stub` |
| `MYTHOS_LLM_MODEL`          | `claude-opus-4-8`      | Model ID |
| `MYTHOS_LLM_MAX_TOKENS`     | `8192`                | Max output tokens per LLM call |
| `MYTHOS_LLM_TEMPERATURE`    | `0.2`                  | Sampling temperature (OpenAI backend only; current Claude models don't accept it) |
| `MYTHOS_MAX_ITERATIONS`     | `50`                   | Hard cap on autonomous iterations |
| `MYTHOS_MAX_FAILURES`       | `5`                    | Consecutive failures before the monitor stops the run |
| `MYTHOS_REFLECTION_INTERVAL`| `5`                    | Inject a self-reflection checkpoint every N iterations |
| `MYTHOS_MEMORY_WINDOW`      | `20`                   | Recent messages kept in the LLM context |
| `MYTHOS_PERSIST_MEMORY`     | `false`                | Persist long-term memory to disk |
| `MYTHOS_MEMORY_PATH`        | `mythos_memory.json`   | Long-term memory file |
| `MYTHOS_VERBOSE`            | `true`                 | Set `false` to silence progress output |

CLI flags (`--provider`, `--model`, `--api-key`, `--max-iterations`, `--quiet`, …) override
environment variables. See `python main.py --help`.

## Built-in tools

`current_time`, `calculate`, `read_file`, `write_file`, `append_file`,
`list_directory`, `run_shell`, `memory_store`, `memory_recall`, `memory_list`,
and `finish` (the agent calls `finish` to end the run with its conclusion).

Register your own:

```python
from mythos import MythosAgent
from mythos.tools import Tool

def greet(name: str) -> str:
    return f"Hello, {name}!"

agent = MythosAgent()
agent.add_tool(Tool(
    name="greet",
    description="Greet a person by name.",
    parameters={"name": {"type": "string", "description": "Person to greet."}},
    func=greet,
    required=["name"],
))
```

## Safety notes

- `run_shell` executes arbitrary shell commands **by design** — run the agent in a
  sandbox/container if you don't fully trust the goal or model output.
- The monitor enforces an iteration cap, a consecutive-failure cap, and repetitive-call
  (infinite-loop) detection as guardrails.

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q
```

## License

MIT
