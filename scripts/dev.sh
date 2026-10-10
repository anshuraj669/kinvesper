#!/usr/bin/env bash
# One-command local run (`make dev`): install dependencies, pick the brain, start Kinvesper.
#
# Data stays in data/state.json (seeded with the demo household on first run). The brain is a
# local Ollama model when one is running, otherwise the offline keyword router, so this works
# with no accounts or keys. Overrides: LLM_PROVIDER, LLM_MODEL, OLLAMA_URL, PORT.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null; then
  echo "error: uv is not installed. Install it with:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

echo "Installing dependencies..."
uv sync --quiet --inexact

OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
LLM_MODEL="${LLM_MODEL:-llama3.1}"
if [[ -z "${LLM_PROVIDER:-}" ]]; then
  if models=$(curl -sf --max-time 2 "$OLLAMA_URL/api/tags") && grep -q "\"$LLM_MODEL" <<<"$models"; then
    LLM_PROVIDER=ollama
  else
    LLM_PROVIDER=none
    echo "Ollama with '$LLM_MODEL' not found at $OLLAMA_URL: using the offline keyword router."
    echo "  For the AI voice agent: install Ollama, run 'ollama pull $LLM_MODEL', then 'make dev' again."
  fi
fi
export LLM_PROVIDER OLLAMA_URL LLM_MODEL

PORT="${PORT:-8000}"
export PORT
cat <<EOF

Kinvesper is starting (brain: $LLM_PROVIDER). Open:
  Voice simulator     http://127.0.0.1:$PORT/voice
  Caregiver dashboard http://127.0.0.1:$PORT/dashboard
  MCP endpoint        http://127.0.0.1:$PORT/mcp
Press Ctrl+C to stop.

EOF
exec uv run kinvesper
