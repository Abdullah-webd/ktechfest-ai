"""Speech in and out through Spitch (Nigerian-language models).
Transcription: long clips are split into 20 s chunks (Spitch fails above ~30 s), and every clip is transcribed twice in
parallel, auto-detected and with the speaker's language hint, because auto-detect can garble accented English. The agent
then picks the coherent transcript. Gemini is a last-resort fallback."""
from __future__ import annotations

import asyncio
import io
import logging
import wave
from typing import Optional

from spitch import AsyncSpitch

from .config import LANGUAGES, settings

log = logging.getLogger("saferoad.speech")
_client: Optional[AsyncSpitch] = AsyncSpitch(api_key=settings.spitch_api_key, max_retries=1) if settings.spitch_api_key else None

TTS_LANG = {"en": "en", "yo": "yo", "ha": "ha", "ig": "ig", "pcm": "pi"}
STT_LANG = {"en": "en", "yo": "yo", "ha": "ha", "ig": "ig", "pcm": "en"}
VOICE = {"en": "lucy", "yo": "sade", "ha": "amina", "ig": "ngozi", "pcm": "tega"}
CHUNK_SECONDS = 20
_sem = asyncio.Semaphore(3)  # Spitch Tier 1 allows 3 concurrent requests


def _chunks(audio: bytes) -> list[bytes]:
    """Split a WAV into <=20 s pieces. Non-WAV audio is sent whole."""
    try:
        w = wave.open(io.BytesIO(audio))
        rate, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        if n / rate <= CHUNK_SECONDS + 5:
            return [audio]
        out = []
        per = CHUNK_SECONDS * rate
        for start in range(0, n, per):
            w.setpos(start)
            frames = w.readframes(min(per, n - start))
            if len(frames) < rate * width * ch // 2:  # < 0.5 s
                continue
            b = io.BytesIO()
            o = wave.open(b, "wb")
            o.setnchannels(ch); o.setsampwidth(width); o.setframerate(rate); o.writeframes(frames); o.close()
            out.append(b.getvalue())
        return out or [audio]
    except Exception:  # noqa: BLE001
        return [audio]


async def _one(chunk: bytes, language: Optional[str]) -> str:
    async with _sem:
        res = await _client.speech.transcribe(content=chunk, language=language) if language else await _client.speech.transcribe(content=chunk)
    return (res.text or "").strip()


async def _variant(chunks: list[bytes], language: Optional[str]) -> str:
    parts = await asyncio.gather(*[_one(c, language) for c in chunks])
    return " ".join(p for p in parts if p).strip()


async def transcribe(audio: bytes, *, hint_lang: Optional[str] = None) -> dict:
    """Returns {"auto": str, "hinted": str, "hint_lang": str}. Raises only if nothing could be transcribed."""
    chunks = _chunks(audio)
    hint = STT_LANG.get(hint_lang or "", "en")
    auto = hinted = ""
    if _client:
        # Two candidates: the speaker's own language and English (people mix both). Auto-detect garbles accented English.
        langs = [hint] + (["en"] if hint != "en" else [])
        res = await asyncio.gather(*[_variant(chunks, l) for l in langs], return_exceptions=True)
        hinted = res[0] if isinstance(res[0], str) else ""
        auto = res[1] if len(res) > 1 and isinstance(res[1], str) else ""
        for r in res:
            if isinstance(r, Exception):
                log.warning("spitch transcribe failed: %s", str(r)[:100])
    if not auto and not hinted and settings.all_gemini_keys:
        from . import gemini
        auto = await gemini.transcribe(audio)
    if not (auto or hinted).strip():
        raise RuntimeError("transcription failed or empty")
    return {"auto": auto, "hinted": hinted, "hint_lang": hint_lang or "en"}


async def speak(text: str, lang: str) -> bytes:
    lang = lang if lang in LANGUAGES else "en"
    if _client:
        try:
            res = await _client.speech.generate(text=text, language=TTS_LANG[lang], voice=VOICE[lang], format="wav")
            return await res.read()
        except Exception as e:  # noqa: BLE001
            log.warning("spitch tts failed (%s), falling back", str(e)[:120])
    from . import gemini
    return await gemini.speak(text, lang)


async def translate(text: str, target: str, *, source: Optional[str] = None) -> str:
    if not _client:
        raise RuntimeError("SPITCH_API_KEY not set")
    res = await _client.text.translate(text=text, target=target, source=source, tone="warm", formality="casual")
    return (res.text or "").strip()
