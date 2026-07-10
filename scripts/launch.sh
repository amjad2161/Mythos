#!/usr/bin/env bash
# One-command Mythos launcher for Linux/macOS.
#
#   ./scripts/launch.sh            # setup + doctor + web control panel
#   ./scripts/launch.sh --offline  # in-memory backends (no docker, stub-friendly)
set -euo pipefail
cd "$(dirname "$0")/.."

OFFLINE=0
[ "${1:-}" = "--offline" ] && OFFLINE=1

# 1. Virtualenv + install
if [ ! -d .venv ]; then
  echo "[launch] creating virtualenv…"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -e ".[orchestration]" || pip install -q -e .

# 2. Config
python main.py --init || true

# 3. Infrastructure
ARGS=(--serve)
if [ "$OFFLINE" = "1" ]; then
  ARGS+=(--bus inmemory --matrix inmemory)
elif command -v docker >/dev/null 2>&1; then
  echo "[launch] starting RabbitMQ + Qdrant…"
  docker compose up -d || echo "[launch] docker compose failed - continuing; use --offline for in-memory mode"
else
  echo "[launch] docker not found - using in-memory backends"
  ARGS+=(--bus inmemory --matrix inmemory)
fi

# 4. Diagnose + launch the control panel
python main.py --doctor || true
echo
exec python main.py "${ARGS[@]}"
