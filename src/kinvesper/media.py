"""Voice notes and photos between the family's Telegram chat and the Echo.

Nothing is stored here: a message keeps Telegram's file_id, and /media/<message id> fetches the file
from Telegram when the simulator or the Echo plays or shows it. The links are signed, because Alexa
fetches audio and images without any of our headers.

Audio is converted with PyAV (bundled FFmpeg, no system install): Telegram voice notes are OGG/Opus,
Alexa's SSML <audio> needs MP3 (48 kbps, 24 kHz), and a voice note sent to Telegram must be OGG/Opus.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import time
from collections import OrderedDict

import anyio

from .config import household_id, settings

CACHE_SIZE = 16
LINK_LIFETIME_S = 7 * 24 * 3600
_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()


def _key() -> bytes:
    secret = settings.telegram_bot_token or settings.dashboard_key or "kinvesper-local"
    return hmac.new(secret.encode(), b"kinvesper-media", hashlib.sha256).digest()


def sign(message_id: str, expires: int, household: str = "") -> str:
    """The first household's links sign "<id>:<expires>"; another's "<household>/<id>:<expires>"."""
    subject = f"{household}/{message_id}" if household and household != settings.patient_id else message_id
    return hmac.new(_key(), f"{subject}:{expires}".encode(), hashlib.sha256).hexdigest()[:32]


def url(message_id: str, absolute: bool = False) -> str:
    """A signed link to a family message's voice note (served as MP3, which every browser and Alexa play) or photo."""
    expires, h = int(time.time()) + LINK_LIFETIME_S, household_id()
    link = f"/media/{message_id}?e={expires}&s={sign(message_id, expires, h)}"
    if h != settings.patient_id:
        link += f"&h={h}"
    return settings.base_url.rstrip("/") + link if absolute else link


def signature_ok(message_id: str, expires: str, signature: str, household: str = "") -> bool:
    try:
        if int(expires) < time.time():
            return False
    except ValueError:
        return False
    return hmac.compare_digest(sign(message_id, int(expires), household), signature or "")


def cached(key: str) -> tuple[bytes, str] | None:
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    return None


def remember(key: str, data: bytes, media_type: str) -> None:
    _cache[key] = (data, media_type)
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)


def _transcode(audio: bytes, container: str, codec: str, rate: int, bit_rate: int) -> bytes:
    import av

    out = io.BytesIO()
    with av.open(io.BytesIO(audio)) as src, av.open(out, "w", format=container) as dst:
        stream = dst.add_stream(codec, rate=rate)
        stream.bit_rate = bit_rate
        stream.layout = "mono"
        resampler = av.AudioResampler(format=stream.format.name, layout="mono", rate=rate)
        for frame in src.decode(audio=0):
            frame.pts = None
            for chunk in resampler.resample(frame):
                for packet in stream.encode(chunk):
                    dst.mux(packet)
        for chunk in resampler.resample(None):
            for packet in stream.encode(chunk):
                dst.mux(packet)
        for packet in stream.encode(None):
            dst.mux(packet)
    return out.getvalue()


async def to_mp3(audio: bytes) -> bytes:
    """Any recording -> MP3 that Alexa's SSML <audio> accepts (48 kbps, 24 kHz)."""
    return await anyio.to_thread.run_sync(_transcode, audio, "mp3", "libmp3lame", 24000, 48000)


async def to_voice_note(audio: bytes) -> bytes:
    """A browser recording (WebM/Opus or MP4) -> OGG/Opus, which Telegram shows as a voice note."""
    return await anyio.to_thread.run_sync(_transcode, audio, "ogg", "libopus", 48000, 32000)
