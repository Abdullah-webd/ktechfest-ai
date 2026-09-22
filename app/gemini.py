"""All AI work goes through Gemini: understanding voice/text, verifying against
responder alerts, translating, and native-sounding text-to-speech."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import wave
from typing import Any, Optional

from google import genai
from google.genai import types

from .config import LANGUAGES, settings

log = logging.getLogger("saferoad.gemini")
client = genai.Client(api_key=settings.gemini_api_key)

FAST = types.ThinkingConfig(thinking_level="minimal")
CAREFUL = types.ThinkingConfig(thinking_level="low")

CATEGORIES = "fight | robbery | kidnapping | roadblock | fire | accident | suspicious_movement | protest | all_clear | other"
SEVERITIES = "info | caution | danger"
LANG_CODES = ", ".join(f"{k}={v}" for k, v in LANGUAGES.items())

# ---------- Voice: make every language sound native ----------
VOICE_FOR = {"en": "Kore", "yo": "Kore", "ha": "Charon", "ig": "Leda", "pcm": "Puck"}
VOICE_STYLE = {
    "yo": (
        "You are a native Yorùbá speaker from Ibadan, a trusted community radio announcer. "
        "Pronounce every word with correct Yorùbá tones (dò, re, mí), natural rhythm and nasal vowels. "
        "Speak calmly, warmly and clearly, slightly slower than conversation, so a worried listener understands. "
        "Do not sound like an English speaker reading Yorùbá."
    ),
    "ha": (
        "You are a native Hausa speaker from Kano, a trusted community radio announcer. "
        "Use authentic Hausa pronunciation: implosive ɓ and ɗ, ejective ƙ, long and short vowels, correct tone. "
        "Speak calmly, respectfully and clearly, slightly slower than conversation, the way a Kano FM presenter gives a security notice. "
        "Do not sound like an English speaker reading Hausa."
    ),
    "ig": (
        "You are a native Igbo speaker from Enugu, a trusted community radio announcer. "
        "Use authentic Igbo pronunciation with correct high and low tones, nasal sounds, and dotted vowels (ọ, ụ, ị). "
        "Speak calmly, warmly and clearly, slightly slower than conversation. "
        "Do not sound like an English speaker reading Igbo."
    ),
    "pcm": (
        "You are a native Nigerian Pidgin speaker from Lagos, a trusted community radio presenter on a Naija Pidgin station. "
        "Speak real Naija Pidgin with its natural Nigerian rhythm, intonation and expressions, not English with an accent. "
        "Sound calm, street-smart, reassuring and clear, like a presenter telling the area what is happening."
    ),
    "en": (
        "You are a Nigerian English speaker, a trusted community radio announcer. "
        "Speak calmly, warmly and clearly with a natural Nigerian accent, slightly slower than conversation, "
        "the way a presenter gives an important security notice."
    ),
}


def _json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


def _audio_part(audio: bytes, mime: str) -> types.Part:
    return types.Part.from_bytes(data=audio, mime_type=mime)


FALLBACK_MODELS = ["gemini-3.7-flash", "gemini-3.5-flash"]


async def _with_retries(call, *, models: list[str], attempts: int = 3):
    """Retry transient Gemini errors (503 high demand, 429, 5xx) with backoff, then fall back to other models.
    Primary model gets `attempts` tries; each fallback gets one, so a user never waits more than ~15 s."""
    last: Exception | None = None
    for n, model in enumerate(models):
        for i in range(attempts if n == 0 else 1):
            try:
                return await call(model)
            except Exception as e:  # noqa: BLE001
                last = e
                code = getattr(e, "code", None) or getattr(e, "status_code", None)
                transient = code in (429, 500, 502, 503, 504) or "UNAVAILABLE" in str(e) or "high demand" in str(e)
                if not transient:
                    raise
                log.warning("gemini %s transient error (try %s): %s", model, i + 1, str(e)[:100])
                await asyncio.sleep(1.5 * (i + 1))
    raise last  # type: ignore[misc]


async def _generate(contents, *, thinking=FAST, json_mode=True, model: Optional[str] = None) -> str:
    async def call(m: str) -> str:
        cfg = types.GenerateContentConfig(
            response_mime_type="application/json" if json_mode else None,
            thinking_config=thinking if m.startswith("gemini-3") else None,
            temperature=0.2,
        )
        resp = await client.aio.models.generate_content(model=m, contents=contents, config=cfg)
        return resp.text or ""

    primary = model or settings.gemini_model
    return await _with_retries(call, models=[primary] + [m for m in FALLBACK_MODELS if m != primary])


# ---------- 1. Responder posts a report ----------
REPORT_PROMPT = """You are SafeRoad, a community safety assistant for a Nigerian town.
A RESPONDER (trusted member of the local vigilante / community security group) named "{name}" just sent a report.
Their profile language is {profile_lang}. They may speak/write in Yoruba, Hausa, Igbo, Nigerian Pidgin or English.

Currently OPEN alerts (id · category · location · summary · minutes ago):
{open_alerts}

Currently UNANSWERED resident questions (id · location · category · question in English):
{open_questions}

Tasks:
1. Transcribe exactly what they said (if audio) in the original language.
2. Detect the language code ({lang_codes}).
3. Extract: category ({categories}), severity ({severities}), location (short place name as said, e.g. "Kaduna road near the market"; empty string if none), and a one or two sentence english_summary written as a public alert (what, where, what people should do).
4. is_all_clear = true if the responder is saying an area/situation is now safe or resolved. If so, list closes_alert_ids: open alert ids that this clears.
5. answers_question_ids: ids of unanswered resident questions that this report directly answers (same place/topic). Empty list if none.
6. readback_local: a short confirmation in the responder's language, e.g. "Recorded: fight near the market, danger. Sending to residents now." (translate that meaning naturally).
7. broadcast_local: the public alert text in the responder's language (what, where, what to do). Natural, calm, 1-2 sentences.

Return ONLY JSON with keys: transcript, language, category, severity, location, english_summary, is_all_clear, closes_alert_ids, answers_question_ids, readback_local, broadcast_local, is_report (false if this is not a safety report at all, e.g. a greeting; then english_summary should say what it was).
"""


async def understand_report(*, name: str, profile_lang: str, text: Optional[str], audio: Optional[bytes], mime: str,
                            open_alerts: str, open_questions: str) -> dict[str, Any]:
    prompt = REPORT_PROMPT.format(name=name, profile_lang=LANGUAGES.get(profile_lang, profile_lang), open_alerts=open_alerts or "(none)",
                                  open_questions=open_questions or "(none)", lang_codes=LANG_CODES,
                                  categories=CATEGORIES, severities=SEVERITIES)
    contents: list[Any] = [prompt]
    if audio:
        contents.append(_audio_part(audio, mime))
        contents.append("The responder's voice note is attached above.")
    if text:
        contents.append(f"Responder's text message: {text}")
    return _json(await _generate(contents, thinking=FAST))


# ---------- 2. Resident asks / reports ----------
ASK_PROMPT = """You are SafeRoad, a community safety assistant for a Nigerian town. You answer residents ONLY from what trusted RESPONDERS (local vigilante / community security) have reported. You never invent facts and never say something is safe or true unless a responder alert says so.

The resident is "{name}" (profile language {profile_lang}). They may speak/write Yoruba, Hausa, Igbo, Nigerian Pidgin or English. Local time now: {now}.

RESPONDER alerts from the last 24 hours (id · status · category · severity · location · summary · by · minutes ago):
{responder_alerts}

Unconfirmed RESIDENT reports from the last 6 hours (id · location · summary · minutes ago):
{resident_reports}

Responder currently ON DUTY and reachable by phone (name · area), or "(none)":
{contact}

Steps:
1. Transcribe what they said (if audio) in the original language. Detect language code ({lang_codes}).
2. intent: "question" (asking if something is true / if a place is safe / what is happening), "report" (telling you something they saw), or "chat" (greeting, thanks, unrelated).
3. Extract location (short) and category ({categories}).
4. For a question, decide status:
   - "confirmed": a responder alert (status open) matches the place/topic and confirms it. Cite it.
   - "contradicted": a responder alert says the opposite (e.g. all_clear for that place, or the responder alert describes something clearly different/smaller than the rumour). Explain the difference.
   - "unconfirmed_reports": no responder alert, but resident reports match.
   - "no_information": nothing matches.
   For a report or chat, status = "n/a".
5. matched_alert_ids: the responder alert ids you relied on (empty if none).
6. escalate: true when status is "unconfirmed_reports" or "no_information" for a real safety question (so responders will be asked).
7. answer_en: the reply in English. Rules: 2-3 short sentences. ALWAYS name the responder and how many minutes ago for anything confirmed or contradicted (e.g. "Musa confirmed a fight near the market 12 minutes ago"). If nothing is known, say clearly that no responder has reported it and that you have asked the responders and will notify them; if a responder is on duty, add that they can call {{name}} directly right now (the app shows the call button; do not invent a phone number). Give practical advice (avoid the area / stay put / go carefully). Never say "safe" without a responder alert. For a report: acknowledge, say it is recorded as unconfirmed and responders have been asked to check. For chat: reply briefly and tell them they can ask if any road or area is safe.
8. answer_local: answer_en translated naturally into the language they used (if they used English, same as answer_en). Keep names and places.
9. text_en: their message in English (one line).

Return ONLY JSON with keys: transcript, language, intent, location, category, status, matched_alert_ids, escalate, answer_en, answer_local, text_en, severity (for reports: {severities}; else "info").
"""


async def answer_resident(*, name: str, profile_lang: str, now: str, text: Optional[str], audio: Optional[bytes], mime: str,
                          responder_alerts: str, resident_reports: str, contact: str = "") -> dict[str, Any]:
    prompt = ASK_PROMPT.format(name=name, profile_lang=LANGUAGES.get(profile_lang, profile_lang), now=now,
                               responder_alerts=responder_alerts or "(none)", resident_reports=resident_reports or "(none)",
                               contact=contact or "(none)",
                               lang_codes=LANG_CODES, categories=CATEGORIES, severities=SEVERITIES)
    contents: list[Any] = [prompt]
    if audio:
        contents.append(_audio_part(audio, mime))
        contents.append("The resident's voice note is attached above.")
    if text:
        contents.append(f"Resident's text message: {text}")
    return _json(await _generate(contents, thinking=CAREFUL))


# ---------- 3. Translate a broadcast into many languages at once ----------
async def translate_many(text_en: str, langs: list[str], *, style: str = "public safety alert") -> dict[str, str]:
    langs = [l for l in dict.fromkeys(langs) if l in LANGUAGES]
    if not langs:
        return {}
    prompt = (
        f"Translate this {style} naturally into each language. Keep place names and people's names unchanged. "
        f"Nigerian Pidgin must read like real Naija Pidgin. Return ONLY JSON mapping language code to text.\n"
        f"Languages: {', '.join(f'{l}={LANGUAGES[l]}' for l in langs)}\nText: {text_en}"
    )
    out = _json(await _generate([prompt], thinking=FAST))
    out["en"] = text_en if "en" in langs else out.get("en", text_en)
    return {k: v for k, v in out.items() if k in LANGUAGES}


# ---------- 4. Native-sounding voice ----------
def _pcm_to_wav(pcm: bytes, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


async def speak(text: str, lang: str) -> bytes:
    """Return WAV bytes of `text` spoken by a native-sounding voice for `lang`."""
    lang = lang if lang in LANGUAGES else "en"
    style = VOICE_STYLE[lang]
    prompt = f"{style}\n\nRead the following {LANGUAGES[lang]} message aloud exactly as written:\n\n{text}"
    cfg = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE_FOR[lang]))
        ),
    )
    async def call(m: str):
        return await client.aio.models.generate_content(model=m, contents=prompt, config=cfg)

    resp = await _with_retries(call, models=[settings.gemini_tts_model, "gemini-2.5-flash-preview-tts"])
    part = resp.candidates[0].content.parts[0].inline_data
    rate = 24000
    if part.mime_type and "rate=" in part.mime_type:
        try:
            rate = int(part.mime_type.split("rate=")[1].split(";")[0])
        except ValueError:
            pass
    return _pcm_to_wav(part.data, rate)
