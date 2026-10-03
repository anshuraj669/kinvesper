"""Claude access, via Amazon Bedrock (Mantle endpoint) or the Claude API.

LLM_PROVIDER=none keeps everything working without credentials: tools fall back to
rule-based logic and the voice simulator uses a keyword router.
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic

from .config import settings

log = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | anthropic.AsyncAnthropicBedrockMantle | None = None


def enabled() -> bool:
    return settings.llm_provider in ("bedrock", "anthropic")


def client():
    global _client
    if _client is None and enabled():
        if settings.llm_provider == "bedrock":
            _client = anthropic.AsyncAnthropicBedrockMantle(aws_region=settings.aws_region)
        else:
            _client = anthropic.AsyncAnthropic()
    return _client


async def create_message(**params: Any):
    """messages.create with Kinvesper defaults (model, effort, refusal fallback where supported)."""
    params.setdefault("model", settings.model_id)
    params.setdefault("max_tokens", 4096)
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
