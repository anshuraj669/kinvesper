"""Server-side speech for the voice simulator.

Browsers differ a lot in Web Speech support: Brave and Firefox can't recognise speech, and on
Linux the browser often has no voices at all. So the page only records and plays audio, and the
server does the rest:

- speech-to-text (STT_PROVIDER): "whisper" runs Whisper locally on the CPU (no account needed;
  about a second on a laptop), "transcribe" streams to Amazon Transcribe (about a second anywhere;
  used in the cloud, where a small App Runner CPU makes Whisper slow), "off" means type only.
- text-to-speech: Amazon Polly. Without AWS credentials the page uses the browser's own voice.
"""

from __future__ import annotations

import asyncio
import difflib
import io
import logging
import math
import os
import re
import threading
from dataclasses import dataclass

import anyio

from .config import settings

log = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 5 * 1024 * 1024  # ~5 minutes of Opus; a spoken request is a few seconds
MAX_SPEECH_CHARS = 1500
TRANSCRIBE_TIMEOUT_S = 20
NAME_MATCH = 0.75  # how close a heard word must be to a known name to be corrected ("Pri" -> "Priya")

_model = None
_model_lock = threading.Lock()
_transcribe_client = None
_polly = None
_polly_failed = False
PERMANENT_POLLY_ERRORS = {"AccessDeniedException", "UnrecognizedClientException", "SubscriptionRequiredException"}


class SpeechUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Vocabulary:
    """Names speech-to-text should expect: the person, their caregiver and their medicines."""

    patient: str
    caregiver: str
    medications: tuple[str, ...] = ()

    @property
    def hint(self) -> str:
        """A prompt that teaches Whisper the names it would otherwise mishear ("a tortoise dad in")."""
        meds = ", ".join(self.medications) or "my pills"
        return f"{self.patient} is talking to the assistant. Tell {self.caregiver} I'm fine. My medicines are {meds}."

    def correct(self, text: str) -> str:
        """Snap near-misses of known names to the real spelling ("Tell Pri" -> "Tell Priya")."""
        known = {n.lower(): n for n in (self.patient, self.caregiver, *self.medications) if n}

        def fix(match: re.Match) -> str:
            word = match.group(0)
            if word.lower() in known:
                return known[word.lower()]
            close = difflib.get_close_matches(word.lower(), known, n=1, cutoff=NAME_MATCH) if len(word) >= 3 else []
            return known[close[0]] if close else word

        return re.sub(r"[A-Za-z]+", fix, text)


def stt_available() -> bool:
    return settings.stt_provider in ("whisper", "transcribe")


async def transcribe(audio: bytes, vocabulary: Vocabulary) -> str:
    if not stt_available():
        raise SpeechUnavailable("Voice input is off on this server")
    try:
        if settings.stt_provider == "transcribe":
            text = await _transcribe_aws(audio)
        else:
            text = await anyio.to_thread.run_sync(_transcribe_whisper, audio, vocabulary.hint)
    except Exception as e:  # undecodable audio, missing model or credentials, timeout, ...
        log.warning("Transcription failed: %s", e)
        raise SpeechUnavailable("Sorry, I couldn't make that out.") from e
    return vocabulary.correct(text)


# ---------- Whisper (local) ----------


def available_cpus(cpu_max: str = "/sys/fs/cgroup/cpu.max") -> int:
    """CPUs this process may really use: a container's CPU limit, not the host's core count."""
    cpus = len(os.sched_getaffinity(0))
    try:
        quota, period = open(cpu_max).read().split()[:2]
        if quota != "max":
            cpus = min(cpus, math.ceil(int(quota) / int(period)))
    except (OSError, ValueError):
        pass  # no cgroup v2 limit visible
    return max(cpus, 1)


def _whisper():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            # One thread per usable CPU: faster-whisper's default of 4 threads fights over a 1-vCPU host.
            threads = available_cpus()
            log.info("Loading Whisper model %s (%d CPU thread%s)", settings.stt_model, threads, "s"[: threads > 1])
            # CPU int8 is fast enough for short requests and leaves the GPU to a local LLM.
            _model = WhisperModel(settings.stt_model, device="cpu", compute_type="int8", cpu_threads=threads)
    return _model


def _transcribe_whisper(audio: bytes, hint: str) -> str:
    segments, _ = _whisper().transcribe(
        io.BytesIO(audio), language="en", beam_size=1, vad_filter=True, initial_prompt=hint or None)
    return " ".join(s.text.strip() for s in segments).strip()  # segments are lazy: this runs the model


# ---------- Amazon Transcribe (cloud) ----------


def _pcm16k(audio: bytes) -> bytes:
    """Browser recordings (WebM/Opus, or MP4 on Safari) -> 16 kHz mono 16-bit PCM for Transcribe."""
    import av

    out = bytearray()
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    with av.open(io.BytesIO(audio)) as src:
        for frame in src.decode(audio=0):
            for chunk in resampler.resample(frame):
                out += bytes(chunk.planes[0])[: chunk.samples * 2]
    for chunk in resampler.resample(None):  # flush
        out += bytes(chunk.planes[0])[: chunk.samples * 2]
    return bytes(out)


def _client():
    """One streaming client per process, signing with boto3's credentials (refreshed per request)."""
    global _transcribe_client
    if _transcribe_client is None:
        import boto3
        from amazon_transcribe.auth import CredentialResolver, Credentials
        from amazon_transcribe.client import TranscribeStreamingClient

        class Boto3Credentials(CredentialResolver):
            async def get_credentials(self) -> Credentials | None:
                found = boto3.Session().get_credentials()  # env, profile, or the App Runner instance role
                if found is None:
                    return None
                c = found.get_frozen_credentials()
                return Credentials(c.access_key, c.secret_key, c.token)

        _transcribe_client = TranscribeStreamingClient(region=settings.aws_region, credential_resolver=Boto3Credentials())
    return _transcribe_client


async def _transcribe_aws(audio: bytes) -> str:
    from amazon_transcribe.model import TranscriptEvent

    pcm = await anyio.to_thread.run_sync(_pcm16k, audio)
    if not pcm:
        return ""
    with anyio.fail_after(TRANSCRIBE_TIMEOUT_S):
        stream = await _client().start_stream_transcription(
            language_code="en-US", media_sample_rate_hz=16000, media_encoding="pcm")

        async def send() -> None:
            for i in range(0, len(pcm), 8000):  # 0.25 s per event; the whole recording is already here
                await stream.input_stream.send_audio_event(audio_chunk=pcm[i : i + 8000])
            await stream.input_stream.end_stream()

        heard: list[str] = []

        async def receive() -> None:
            async for event in stream.output_stream:
                if isinstance(event, TranscriptEvent):
                    heard.extend(r.alternatives[0].transcript for r in event.transcript.results
                                 if not r.is_partial and r.alternatives)

        await asyncio.gather(send(), receive())
    return " ".join(heard).strip()


async def warm_up() -> None:
    """Load Whisper before the first recording arrives (a few seconds on first use)."""
    if settings.stt_provider != "whisper":
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
