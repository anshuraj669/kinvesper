"""Claude access, via Amazon Bedrock (Mantle endpoint) or the Claude API. LLM_PROVIDER=ollama
sends the same Messages API requests to a local Ollama model instead (no key, no cloud).

LLM_PROVIDER=none keeps everything working without credentials: tools fall back to
rule-based logic and the voice simulator uses a keyword router.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import anthropic

from .config import settings

log = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | anthropic.AsyncAnthropicBedrockMantle | None = None


def enabled() -> bool:
    return settings.llm_provider in ("bedrock", "anthropic", "ollama")


def local() -> bool:
    return settings.llm_provider == "ollama"


def server_tools() -> list[dict]:
    """Tools Anthropic runs for us: web search answers questions about today (weather, news, opening hours).

    The basic search, once per question: ~4.5 s and ~14k input tokens for "weather in Kathmandu today",
    against ~12 s and ~20k for the dynamic-filtering version (web_search_20260209), which runs code
    over the results first. A spoken answer needs one good result, and Alexa waits 8 seconds.
    """
    if settings.llm_provider != "anthropic":
        return []
    return [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}]


async def warm_up() -> None:
    """Load a local model into memory before the first real request (the first load can take most of a minute)."""
    if not local():
        return
    try:
        await create_message(max_tokens=1, messages=[{"role": "user", "content": "hi"}])
    except anthropic.APIError as e:
        log.warning("Could not warm up the local model: %s", e)


def client():
    global _client
    if _client is None and enabled():
        if settings.llm_provider == "bedrock":
            _client = anthropic.AsyncAnthropicBedrockMantle(aws_region=settings.aws_region)
        elif settings.llm_provider == "ollama":
            # Ollama speaks the Anthropic Messages API; it requires a token but ignores its value.
            _client = anthropic.AsyncAnthropic(base_url=settings.ollama_url, api_key="ollama")
        else:
            _client = anthropic.AsyncAnthropic()
    return _client


async def create_message(**params: Any):
    """messages.create with Kinvesper defaults (model, effort, refusal fallback where supported)."""
    params.setdefault("model", settings.model_id)
    params.setdefault("max_tokens", 4096)
    if settings.llm_provider == "ollama":
        return await client().messages.create(**params)  # effort is a Claude setting
    params.setdefault("output_config", {"effort": settings.llm_effort})
    if settings.llm_provider == "anthropic":
        # Server-side refusal fallback is a Claude API feature (not on Bedrock).
        return await client().beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params
        )
    return await client().messages.create(**params)


def text_of(response) -> str:
    return "".join(b.text for b in response.content if b.type == "text").strip()


async def complete(system: str, prompt: str, max_tokens: int = 2048) -> str | None:
    """One-shot text completion. Returns None when the LLM is disabled or the call fails."""
    if not enabled():
        return None
    try:
        response = await create_message(
            system=system, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}]
        )
        if response.stop_reason == "refusal":
            return None
        return text_of(response) or None
    except anthropic.APIError as e:
        log.warning("LLM call failed: %s", e)
        return None


async def structured(system: str, content: str | list, schema: dict, max_tokens: int = 2048) -> dict | None:
    """One call whose reply is JSON matching `schema` (Claude's structured outputs; a local model is asked
    for JSON and parsed). `content` is the user turn: text, or content blocks such as an image.
    Returns None when the LLM is off or the call fails."""
    if not enabled():
        return None
    params: dict[str, Any] = {"system": system, "max_tokens": max_tokens, "messages": [{"role": "user", "content": content}]}
    if not local():
        params["output_config"] = {"effort": settings.llm_effort, "format": {"type": "json_schema", "schema": schema}}
    try:
        response = await create_message(**params)
    except anthropic.APIError as e:
        log.warning("LLM call failed: %s", e)
        return None
    if response.stop_reason in ("refusal", "max_tokens"):
        return None
    text = text_of(response)
    try:
        return json.loads(text)
    except ValueError:
        if match := re.search(r"\{.*\}", text, re.S):
            try:
                return json.loads(match[0])
            except ValueError:
                pass
    log.warning("LLM reply wasn't JSON: %.200s", text)
    return None
