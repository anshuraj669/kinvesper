"""Runtime configuration, read once from environment variables."""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _local_tz() -> tzinfo:
    name = _env("CARE_TZ") or _env("TZ").lstrip(":")
    if name:
        return ZoneInfo(name)
    # /etc/localtime usually links to .../zoneinfo/<Area>/<City>
    link = os.path.realpath("/etc/localtime")
    if "zoneinfo/" in link:
        with contextlib.suppress(ValueError, KeyError):
            return ZoneInfo(link.split("zoneinfo/", 1)[1])
    # last resort: this machine's current UTC offset (never silently UTC)
    return datetime.now().astimezone().tzinfo or timezone.utc


@dataclass(frozen=True)
class Settings:
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("PORT", "8000")))
    public_url: str = field(default_factory=lambda: _env("PUBLIC_URL"))
    tz: tzinfo = field(default_factory=_local_tz)
    patient_id: str = field(default_factory=lambda: _env("PATIENT_ID", "margaret"))

    # Storage: "json" (local file) or "dynamodb"
    store: str = field(default_factory=lambda: _env("STORE", "json"))
    data_file: str = field(default_factory=lambda: _env("DATA_FILE", "data/state.json"))
    dynamodb_table: str = field(default_factory=lambda: _env("DYNAMODB_TABLE", "kinvesper"))
    aws_region: str = field(default_factory=lambda: _env("AWS_REGION", _env("AWS_DEFAULT_REGION", "us-east-1")))

    # LLM: "bedrock", "anthropic", "ollama" (local model), or "none" (rule-based fallback, no credentials needed)
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "none"))
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://localhost:11434"))

    # Voice simulator speech: Whisper model (name, local path, or "off") for speech-to-text; "polly" or
    # "browser" for text-to-speech (Polly falls back to the browser's voice when it can't be used).
    stt_model: str = field(default_factory=lambda: _env("STT_MODEL", "small.en"))
    tts_provider: str = field(default_factory=lambda: _env("TTS_PROVIDER", "polly"))
    tts_voice: str = field(default_factory=lambda: _env("TTS_VOICE", "Joanna"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL"))
    llm_effort: str = field(default_factory=lambda: _env("LLM_EFFORT", "low"))

    # Caregiver notifications: SNS topic ARN or a direct E.164 phone number. Empty = log only.
    sns_topic_arn: str = field(default_factory=lambda: _env("SNS_TOPIC_ARN"))
    caregiver_sms: str = field(default_factory=lambda: _env("CAREGIVER_SMS"))

    # OAuth (Cognito) for the MCP endpoint. Empty issuer = auth disabled (local dev).
    oauth_issuer: str = field(default_factory=lambda: _env("OAUTH_ISSUER"))
    oauth_audience: str = field(default_factory=lambda: _env("OAUTH_AUDIENCE"))
    oauth_scopes: str = field(default_factory=lambda: _env("OAUTH_SCOPES"))  # space-separated, all required
    oauth_jwks_url: str = field(default_factory=lambda: _env("OAUTH_JWKS_URL"))  # default: <issuer>/.well-known/jwks.json
    # Bearer token the built-in voice simulator uses to reach /mcp when OAuth is on.
    service_token: str = field(default_factory=lambda: _env("SERVICE_TOKEN"))
    # Access key for the voice simulator page when OAuth is on (open /voice?key=...).
    simulator_key: str = field(default_factory=lambda: _env("SIMULATOR_KEY"))
    # Access key for the caregiver dashboard when OAuth is on (open /dashboard?key=...).
    dashboard_key: str = field(default_factory=lambda: _env("DASHBOARD_KEY"))

    # Care rules
    missed_dose_grace_min: int = field(default_factory=lambda: int(_env("MISSED_DOSE_GRACE_MIN", "60")))
    inactivity_hours: float = field(default_factory=lambda: float(_env("INACTIVITY_HOURS", "4")))
    low_stock_threshold: int = field(default_factory=lambda: int(_env("LOW_STOCK_THRESHOLD", "5")))
    refill_step_seconds: float = field(default_factory=lambda: float(_env("REFILL_STEP_SECONDS", "4")))
    scheduler_interval_s: float = field(default_factory=lambda: float(_env("SCHEDULER_INTERVAL_S", "60")))

    @property
    def model_id(self) -> str:
        if self.llm_model:
            return self.llm_model
        # Bedrock model IDs carry the "anthropic." provider prefix.
        return {"bedrock": "anthropic.claude-opus-5-5", "ollama": "llama3.1"}.get(self.llm_provider, "claude-opus-5-5")

    @property
    def oauth_scope_list(self) -> list[str]:
        return self.oauth_scopes.split()

    @property
    def base_url(self) -> str:
        return self.public_url or f"http://127.0.0.1:{self.port}"


settings = Settings()


def now() -> datetime:
    return datetime.now(settings.tz)
