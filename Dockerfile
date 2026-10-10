# Kinvesper MCP server. Multi-stage: uv resolves locked deps, the runtime image has no build tools.
FROM ghcr.io/astral-sh/uv:0.9-python3.12-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable
# Bake the speech-to-text model into the image so the first recording doesn't wait on a download.
RUN .venv/bin/python -c "from faster_whisper import download_model; download_model('small.en', output_dir='/app/whisper/small.en')"

FROM python:3.12-slim-bookworm
RUN useradd --create-home --uid 10001 kinvesper
COPY --from=build --chown=kinvesper /app/.venv /app/.venv
COPY --from=build --chown=kinvesper /app/whisper /app/whisper
USER kinvesper
WORKDIR /home/kinvesper
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 PORT=8080 DATA_FILE=/home/kinvesper/data/state.json \
    STT_MODEL=/app/whisper/small.en HF_HUB_OFFLINE=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=4)"
CMD ["kinvesper"]
