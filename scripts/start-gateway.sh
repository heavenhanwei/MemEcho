#!/usr/bin/env bash
# start-gateway.sh — Start the memEcho analysis gateway for local development.
#
# Usage:
#   ./scripts/start-gateway.sh                    # default port 8787, mock provider
#   ./scripts/start-gateway.sh -p 9000            # custom port
#   ./scripts/start-gateway.sh -P bailian         # use real Bailian backend

set -euo pipefail

PORT=8787
PROVIDER="mock"
TOKEN="change-me"

while getopts "p:P:t:" opt; do
  case $opt in
    p) PORT="$OPTARG" ;;
    P) PROVIDER="$OPTARG" ;;
    t) TOKEN="$OPTARG" ;;
    *) echo "Usage: $0 [-p port] [-P provider] [-t token]" >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GATEWAY_DIR="$(cd "$SCRIPT_DIR/../services/gateway" && pwd)"

if [ ! -f "$GATEWAY_DIR/pyproject.toml" ]; then
  echo "Error: Gateway not found at $GATEWAY_DIR" >&2
  exit 1
fi

if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
  echo "Error: Python 3.12+ is required but not found on PATH." >&2
  exit 1
fi

PYTHON=$(command -v python3 || command -v python)

export MEMECHO_PROVIDER="$PROVIDER"
export MEMECHO_DEMO_TOKEN="$TOKEN"
export MEMECHO_DATA_DIR="$GATEWAY_DIR/tmp"
export MEMECHO_PUBLIC_BASE_URL="http://127.0.0.1:$PORT"

echo ""
echo "Starting memEcho gateway on http://127.0.0.1:$PORT"
echo "  Provider: $PROVIDER"
echo "  Token:    ${TOKEN:0:4}****${TOKEN: -4}"
echo ""
echo "Press Ctrl+C to stop."
echo ""

cd "$GATEWAY_DIR"
exec "$PYTHON" -m uvicorn memecho_gateway.main:app --host 127.0.0.1 --port "$PORT"
