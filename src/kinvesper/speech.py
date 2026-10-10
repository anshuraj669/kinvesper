"""Server-side speech for the voice simulator.

Browsers differ a lot in Web Speech support: Brave and Firefox can't recognise speech, and on
Linux the browser often has no voices at all. So the page only records and plays audio:
speech-to-text runs here with Whisper (faster-whisper, on the CPU, no account needed), and
text-to-speech uses Amazon Polly. When Polly isn't available (no AWS credentials) the page falls
back to the browser's own voice.
"""

from __future__ import annotations

import io
import logging
import threading

import anyio

from .config import settings

log = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 5 * 1024 * 1024  # ~5 minutes of Opus; a spoken request is a few seconds
MAX_SPEECH_CHARS = 1500

_model = None
_model_lock = threading.Lock()
_polly = None
_polly_failed = False
PERMANENT_POLLY_ERRORS = {"AccessDeniedException", "UnrecognizedClientException", "SubscriptionRequiredException"}


class SpeechUnavailable(Exception):
    pass


def _whisper():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            log.info("Loading Whisper model %s", settings.stt_model)
            # CPU int8 is fast enough for short requests and leaves the GPU to a local LLM.
            _model = WhisperModel(settings.stt_model, device="cpu", compute_type="int8")
    return _model


def stt_available() -> bool:
    return settings.stt_model.lower() != "off"


def hint_for(name: str, caregiver: str, medications: list[str]) -> str:
    """A prompt that teaches Whisper the names it would otherwise mishear ("a tortoise dad in")."""
    meds = ", ".join(medications) or "my pills"
    return f"{name} is talking to the assistant. Tell {caregiver} I'm fine. My medicines are {meds}."


def _transcribe(audio: bytes, hint: str) -> str:
    segments, _ = _whisper().transcribe(
        io.BytesIO(audio), language="en", beam_size=1, vad_filter=True, initial_prompt=hint or None)
    return " ".join(s.text.strip() for s in segments).strip()  # segments are lazy: this runs the model


async def transcribe(audio: bytes, hint: str = "") -> str:
    if not stt_available():
        raise SpeechUnavailable("Voice input is off on this server")
    try:
        return await anyio.to_thread.run_sync(_transcribe, audio, hint)
    except Exception as e:  # undecodable audio, missing model download, ...
        log.warning("Transcription failed: %s", e)
        raise SpeechUnavailable("Sorry, I couldn't make that out.") from e


async def warm_up() -> None:
    """Load Whisper before the first recording arrives (a few seconds on first use)."""
    if not stt_available():
        return
    try:
        await anyio.to_thread.run_sync(_whisper)
    except Exception as e:  # e.g. offline on first run: the model can't be downloaded
        log.warning("Could not load Whisper model %s: %s", settings.stt_model, e)


def tts_available() -> bool:
    return settings.tts_provider == "polly" and not _polly_failed


def _synthesize(text: str) -> bytes:
    global _polly, _polly_failed
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

    try:
        if _polly is None:
            _polly = boto3.client("polly", region_name=settings.aws_region)
        response = _polly.synthesize_speech(
            Text=text[:MAX_SPEECH_CHARS], OutputFormat="mp3", VoiceId=settings.tts_voice, Engine="neural")
        return response["AudioStream"].read()
    except (BotoCoreError, ClientError) as e:
        code = e.response["Error"]["Code"] if isinstance(e, ClientError) else ""
        if isinstance(e, NoCredentialsError) or code in PERMANENT_POLLY_ERRORS:
            _polly_failed = True  # won't fix itself: use the browser's voice from now on
            log.warning("Amazon Polly unavailable, using the browser's voice instead: %s", e)
        else:
            log.warning("Amazon Polly request failed: %s", e)
        raise SpeechUnavailable(str(e)) from e


async def synthesize(text: str) -> bytes:
    if not tts_available():
        raise SpeechUnavailable("Server speech is off")
    return await anyio.to_thread.run_sync(_synthesize, text)
