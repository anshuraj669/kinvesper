#!/usr/bin/env bash
# One-command local run (`make dev`): install dependencies, pick the brain, start Kinvesper.
#
# Settings come from .env (see .env.example); variables already set in the shell win. Data stays in
# data/state.json (seeded with the demo household on first run). The brain is Claude when
# ANTHROPIC_API_KEY is set, else a local Ollama model when one is running, else the offline keyword
# router, so this works with no accounts or keys. Overrides: LLM_PROVIDER, LLM_MODEL, OLLAMA_URL, PORT.
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/load_env.sh
load_env .env

if ! command -v uv >/dev/null; then
  echo "error: uv is not installed. Install it with:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

echo "Installing dependencies..."
uv sync --quiet --inexact

OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
OLLAMA_MODEL="${LLM_MODEL:-llama3.1}"
if [[ "${LLM_PROVIDER:-}" == anthropic && -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is empty (put your sk-ant-... key in .env)."
  LLM_PROVIDER=
fi
if [[ -z "${LLM_PROVIDER:-}" ]]; then
  if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    LLM_PROVIDER=anthropic
  elif models=$(curl -sf --max-time 2 "$OLLAMA_URL/api/tags") && grep -q "\"$OLLAMA_MODEL" <<<"$models"; then
    LLM_PROVIDER=ollama
  else
    LLM_PROVIDER=none
    echo "No ANTHROPIC_API_KEY, and Ollama with '$OLLAMA_MODEL' not found at $OLLAMA_URL: using the offline keyword router."
    echo "  For the AI voice agent: put ANTHROPIC_API_KEY=sk-ant-... in .env, or run 'ollama pull $OLLAMA_MODEL'."
  fi
fi
[[ "$LLM_PROVIDER" == ollama ]] && export LLM_MODEL="$OLLAMA_MODEL"  # Claude uses its own default model
export LLM_PROVIDER OLLAMA_URL

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
