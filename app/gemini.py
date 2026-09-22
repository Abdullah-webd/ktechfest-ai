"""All AI work goes through Gemini: the conversational agent (which decides which tools to run),
translation, and native-sounding text-to-speech. No browser speech APIs anywhere."""
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
_clients = [genai.Client(api_key=k) for k in settings.all_gemini_keys]
_rr = 0


def _next_clients() -> list[genai.Client]:
    """Round-robin over API keys so free-tier per-minute quotas add up across keys."""
    global _rr
    _rr = (_rr + 1) % len(_clients)
    return _clients[_rr:] + _clients[:_rr]


_cooldown: dict = {}
FAST = types.ThinkingConfig(thinking_level="minimal")
CAREFUL = types.ThinkingConfig(thinking_level="low")
FALLBACK_MODELS = ["gemini-3.5-flash-lite", "gemini-3.5-flash"]
MINIMAL_OK = {"gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"}


def _thinking_for(model: str, requested):
    if not model.startswith("gemini-3"):
        return None
    if requested is FAST and model not in MINIMAL_OK:
        return types.ThinkingConfig(thinking_level="low")
    return requested

CATEGORIES = "fight | robbery | kidnapping | roadblock | fire | accident | suspicious_movement | protest | all_clear | other"
SEVERITIES = "info | caution | danger"
LANG_CODES = ", ".join(f"{k}={v}" for k, v in LANGUAGES.items())

VOICE_FOR = {"en": "Kore", "yo": "Kore", "ha": "Charon", "ig": "Leda", "pcm": "Puck"}
VOICE_STYLE = {
    "yo": "You are a native Yorùbá speaker from Ibadan, a trusted community radio announcer. Pronounce every word with correct Yorùbá tones (dò, re, mí), natural rhythm and nasal vowels. Speak calmly, warmly and clearly, slightly slower than conversation, so a worried listener understands. Do not sound like an English speaker reading Yorùbá.",
    "ha": "You are a native Hausa speaker from Kano, a trusted community radio announcer. Use authentic Hausa pronunciation: implosive ɓ and ɗ, ejective ƙ, long and short vowels, correct tone. Speak calmly, respectfully and clearly, slightly slower than conversation, the way a Kano FM presenter gives a security notice. Do not sound like an English speaker reading Hausa.",
    "ig": "You are a native Igbo speaker from Enugu, a trusted community radio announcer. Use authentic Igbo pronunciation with correct high and low tones, nasal sounds, and dotted vowels (ọ, ụ, ị). Speak calmly, warmly and clearly, slightly slower than conversation. Do not sound like an English speaker reading Igbo.",
    "pcm": "You are a native Nigerian Pidgin speaker from Lagos, a trusted community radio presenter on a Naija Pidgin station. Speak real Naija Pidgin with its natural Nigerian rhythm, intonation and expressions, not English with an accent. Sound calm, street-smart, reassuring and clear.",
    "en": "You are a Nigerian English speaker, a trusted community radio announcer. Speak calmly, warmly and clearly with a natural Nigerian accent, slightly slower than conversation, the way a presenter gives an important security notice.",
}


def _json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


async def _with_retries(call, *, models: list[str], attempts: int = 2, hedge_after: float = 1.5, initial: int = 3):
    """Hedged requests: start the primary (key, model); if it has not answered after `hedge_after`
    seconds, start the next candidate too, and so on. The first success wins, the rest are cancelled.
    Free-tier 'high demand' (503) and quota (429) errors therefore cost almost no wall-clock time."""
    clients = _next_clients()
    # interleave keys per model so the first few candidates span different keys
    candidates = [(c, m) for m in models for c in clients]
    # Never start with a (key, model) pair that recently hit a quota wall
    now = asyncio.get_running_loop().time()
    candidates.sort(key=lambda cm: _cooldown.get(f"{cm[1]}@key{_clients.index(cm[0])}", 0) > now)
    tasks: set[asyncio.Task] = set()
    errors: list[Exception] = []
    idx = 0

    def launch():
        nonlocal idx
        if idx < len(candidates):
            c, m = candidates[idx]
            idx += 1
            tasks.add(asyncio.create_task(call(c, m), name=f"{m}@key{_clients.index(c)}"))

    for _ in range(initial):
        launch()
    t0 = asyncio.get_running_loop().time()
    try:
        while tasks:
            done, _ = await asyncio.wait(tasks, timeout=hedge_after, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                tasks.discard(t)
                try:
                    res = t.result()
                    log.info("gemini winner %s in %.1fs", t.get_name(), asyncio.get_running_loop().time() - t0)
                    return res
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
                    msg = str(e)
                    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                        _cooldown[t.get_name()] = asyncio.get_running_loop().time() + 60
                    log.warning("gemini %s failed: %s", t.get_name(), msg[:70])
                    launch()
            if not done:
                launch()
            if not tasks and idx >= len(candidates):
                break
    finally:
        for t in tasks:
            t.cancel()
    raise errors[-1] if errors else RuntimeError("no gemini candidates")


async def _generate(contents, *, thinking=FAST, json_mode=True) -> str:
    async def call(client: genai.Client, m: str) -> str:
        cfg = types.GenerateContentConfig(response_mime_type="application/json" if json_mode else None,
                                          thinking_config=_thinking_for(m, thinking), temperature=0.2)
        resp = await client.aio.models.generate_content(model=m, contents=contents, config=cfg)
        return resp.text or ""
    primary = settings.gemini_model
    return await _with_retries(call, models=[primary] + [m for m in FALLBACK_MODELS if m != primary])


# ---------------- The agent ----------------
SHARED_RULES = """You are SafeRoad, a calm, trustworthy community-safety assistant for a Nigerian town. You chat like a helpful person on WhatsApp: short, warm, specific. People write or speak in Yoruba, Hausa, Igbo, Nigerian Pidgin or English. Always reply in the language the person used in THIS message (if they switch, you switch). Never invent facts. Never say a place is "safe" or a rumour is "true" unless a RESPONDER alert says so; instead say exactly who reported what and how many minutes ago.

Language codes: {lang_codes}. Categories: {categories}. Severities: {severities}. Local time now: {now}.

Recent conversation (oldest first, for context and follow-ups):
{history}
"""

RESIDENT_PROMPT = SHARED_RULES + """
The person is a RESIDENT named "{name}" (profile language {profile_lang}).

RESPONDER alerts from the last 24 hours (id · status · category · severity · location · summary · by · minutes ago):
{responder_alerts}

Unconfirmed RESIDENT reports from the last 6 hours (id · location · summary · minutes ago):
{resident_reports}

Responder ON DUTY and reachable by phone right now (name · area), or (none):
{contact}

You have TOOLS. Decide which to run (zero or more) and put them in "tools":
- {{"name":"escalate_to_responders","args":{{"question_en":"...","location":"...","category":"..."}}}} — when a real safety question has no responder answer (status unconfirmed_reports or no_information). Responders get notified and the resident will be told automatically when one answers.
- {{"name":"file_resident_report","args":{{"report_en":"...","location":"...","category":"...","severity":"..."}}}} — when the resident is REPORTING something they saw (not asking). It is stored as unconfirmed and responders are asked to check.

Reply rules for "reply": 1-3 short sentences, WhatsApp style. For questions: name the responder and minutes ago for anything confirmed/contradicted; if nothing is known say clearly that no responder has reported it, that you have asked the responders and will message them when one replies, and (if a responder is on duty) that they can tap the call button to reach {{contact name}} directly. Give one practical tip (avoid the area / go carefully / stay put). For reports: thank them, say it is recorded as unconfirmed and responders have been asked to check. For greetings/chat: reply briefly and remind them they can ask about any road or area, or forward any message they received to check it.

Return ONLY JSON:
{{"transcript": "<what they said, original language>", "language": "<code>", "intent": "question|report|chat",
 "location": "<short place or empty>", "category": "<category>", "severity": "<severity or info>",
 "status": "confirmed|contradicted|unconfirmed_reports|no_information|n/a", "matched_alert_ids": ["..."],
 "reply": "<reply in their language>", "reply_en": "<same reply in English>", "text_en": "<their message in English, one line>",
 "tools": [ ... ]}}
"""

RESPONDER_PROMPT = SHARED_RULES + """
The person is a RESPONDER (trusted member of the local vigilante / community security group) named "{name}" (profile language {profile_lang}). Responders tell you what they see and you publish it to every resident instantly.

Currently OPEN alerts (id · category · location · summary · minutes ago):
{open_alerts}

Residents WAITING for an answer (id · location · category · question in English):
{open_questions}

Unconfirmed RESIDENT reports (id · location · summary · minutes ago):
{resident_reports}

You have TOOLS. Decide which to run (zero or more) and put them in "tools":
- {{"name":"post_alert","args":{{"category":"...","severity":"...","location":"...","summary_en":"<1-2 sentence public alert: what, where, what to do>","broadcast_local":"<same alert in the responder's language>","closes_alert_ids":[],"answers_question_ids":[],"confirms_report_ids":[]}}}} — whenever the responder reports something happening, OR says an area is now safe (then category all_clear, severity info, and closes_alert_ids lists the open alerts it clears). answers_question_ids: waiting questions this directly answers. confirms_report_ids: resident reports this confirms.
- {{"name":"dismiss_report","args":{{"report_ids":[],"note_en":"..."}}}} — when the responder says a resident report is false/nothing there.

Reply rules for "reply": a short readback in the responder's language confirming what was recorded and that residents are being notified (e.g. "Recorded: fight near the market, danger. Sending to all residents now."), or if they only asked a question / chatted, answer briefly. If they ask what residents are asking, summarise the waiting questions.

Return ONLY JSON:
{{"transcript": "<what they said, original language>", "language": "<code>", "intent": "report|question|chat",
 "reply": "<reply in their language>", "reply_en": "<same reply in English>", "text_en": "<their message in English, one line>",
 "tools": [ ... ]}}
"""


async def run_agent(*, role: str, name: str, profile_lang: str, now: str, history: str, text: Optional[str],
                    audio: Optional[bytes], mime: str, **ctx: str) -> dict[str, Any]:
    tpl = RESIDENT_PROMPT if role == "resident" else RESPONDER_PROMPT
    prompt = tpl.format(name=name, profile_lang=LANGUAGES.get(profile_lang, profile_lang), now=now, history=history or "(new conversation)",
                        lang_codes=LANG_CODES, categories=CATEGORIES, severities=SEVERITIES,
                        **{k: (v or "(none)") for k, v in ctx.items()})
    contents: list[Any] = [prompt]
    if audio:
        contents.append(types.Part.from_bytes(data=audio, mime_type=mime))
        contents.append("The person's voice note is attached above. Transcribe it faithfully.")
    if text:
        contents.append(f"The person's message: {text}")
    return _json(await _generate(contents, thinking=CAREFUL if role == "resident" else FAST))


async def transcribe(audio: bytes, mime: str = "audio/wav") -> str:
    """Fallback transcription when Spitch is unavailable."""
    out = _json(await _generate([types.Part.from_bytes(data=audio, mime_type=mime),
                                 "Transcribe this audio exactly in its original language (Yoruba, Hausa, Igbo, Nigerian Pidgin or English). Return ONLY JSON {\"transcript\": \"...\"}."], thinking=FAST))
    return (out.get("transcript") or "").strip()


async def translate_many(text_en: str, langs: list[str], *, style: str = "public safety alert") -> dict[str, str]:
    langs = [l for l in dict.fromkeys(langs) if l in LANGUAGES]
    if not langs:
        return {}
    prompt = (f"Translate this {style} naturally into each language. Keep place names and people's names unchanged. "
              f"Nigerian Pidgin must read like real Naija Pidgin. Return ONLY JSON mapping language code to text.\n"
              f"Languages: {', '.join(f'{l}={LANGUAGES[l]}' for l in langs)}\nText: {text_en}")
    out = _json(await _generate([prompt], thinking=FAST))
    if "en" in langs:
        out["en"] = text_en
    return {k: v for k, v in out.items() if k in LANGUAGES}


def _pcm_to_wav(pcm: bytes, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


async def speak(text: str, lang: str) -> bytes:
    """WAV bytes of `text` spoken by a native-sounding voice for `lang`."""
    lang = lang if lang in LANGUAGES else "en"
    prompt = f"{VOICE_STYLE[lang]}\n\nRead the following {LANGUAGES[lang]} message aloud exactly as written:\n\n{text}"
    cfg = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE_FOR[lang]))))

    async def call(client: genai.Client, m: str):
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
