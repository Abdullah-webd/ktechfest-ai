"""Speech in and out through Spitch (Nigerian-language models). Gemini is only a last-resort fallback."""
from __future__ import annotations

import logging
from typing import Optional

from spitch import AsyncSpitch

from .config import LANGUAGES, settings

log = logging.getLogger("saferoad.speech")
_client: Optional[AsyncSpitch] = AsyncSpitch(api_key=settings.spitch_api_key) if settings.spitch_api_key else None

# Spitch language codes: Pidgin is "pi" for speech and "pcm" for translation.
TTS_LANG = {"en": "en", "yo": "yo", "ha": "ha", "ig": "ig", "pcm": "pi"}
STT_LANG = {"en": "en", "yo": "yo", "ha": "ha", "ig": "ig", "pcm": "en"}
VOICE = {"en": "lucy", "yo": "sade", "ha": "amina", "ig": "ngozi", "pcm": "tega"}


async def transcribe(audio: bytes, *, hint_lang: Optional[str] = None) -> str:
    """Return the transcript. Language is auto-detected by Spitch; the hint is only used on a retry."""
    if not _client:
        raise RuntimeError("SPITCH_API_KEY not set")
    try:
        res = await _client.speech.transcribe(content=audio)
        text = (res.text or "").strip()
        if text:
            return text
    except Exception as e:  # noqa: BLE001
        log.warning("spitch transcribe (auto) failed: %s", str(e)[:120])
    if hint_lang and STT_LANG.get(hint_lang):
        res = await _client.speech.transcribe(content=audio, language=STT_LANG[hint_lang])
        return (res.text or "").strip()
    raise RuntimeError("transcription failed")


async def speak(text: str, lang: str) -> bytes:
    """WAV bytes of `text` in a native voice for `lang`."""
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
