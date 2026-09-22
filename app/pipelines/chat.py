"""One conversation per person. Every turn: store the person's message, run the agent, run its tools, store the reply."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from pymongo import DESCENDING

from .. import gemini
from ..db import Message, User, find, messages, put_media, update, users, utcnow
from ..notify import broker
from . import common, tools

log = logging.getLogger("saferoad.chat")


async def history(user_id: str, limit: int = 60) -> list[Message]:
    msgs = await find(messages, Message, {"user_id": user_id}, sort=[("created_at", DESCENDING)], limit=limit)
    return list(reversed(msgs))


async def handle_turn(*, user: User, text: Optional[str], audio: Optional[bytes], mime: str) -> dict:
    # 1. store the person's message (voice notes are kept so they show in the chat like WhatsApp)
    audio_url = None
    if audio:
        audio_url = await put_media(f"v_{user.id}_{uuid.uuid4().hex[:10]}.wav", audio, mime)
    user_msg = Message(user_id=user.id, role="user", kind="voice" if audio else "text", text=text or "", language=user.language,
                       audio_url=audio_url)
    await tools.deliver(user, user_msg)
    await update(users, user.id, {"last_active_at": utcnow()})

    # 2. build context
    hist = common.fmt_history([m for m in await history(user.id, 14) if m.id != user_msg.id])
    if user.role == "resident":
        on_duty = [u for u in await common.on_duty_responders() if u.phone]
        contact = on_duty[0] if on_duty else None
        ctx = dict(responder_alerts=await common.fmt_alerts(await common.open_responder_alerts()),
                   resident_reports=common.fmt_reports(await common.unconfirmed_resident_reports()),
                   contact=f"{contact.name} · {contact.area or 'area not set'}" if contact else "")
    else:
        contact = None
        ctx = dict(open_alerts=await common.fmt_alerts([a for a in await common.open_responder_alerts() if a.status == "open"]),
                   open_questions=common.fmt_questions(await common.open_questions()),
                   resident_reports=common.fmt_reports(await common.unconfirmed_resident_reports()))

    # 3. the agent decides
    r = await gemini.run_agent(role=user.role, name=user.name, profile_lang=user.language, now=common.now_local_str(),
                               history=hist, text=text, audio=audio, mime=mime, **ctx)
    lang = r.get("language") if r.get("language") in gemini.LANGUAGES else user.language
    transcript = r.get("transcript") or text or ""
    if audio and transcript:
        await update(messages, user_msg.id, {"text": transcript, "text_en": r.get("text_en") or transcript, "language": lang})
        user_msg.text = transcript
        broker.publish("message", common.message_to_dict(user_msg), audience=f"user:{user.id}")
    else:
        await update(messages, user_msg.id, {"text_en": r.get("text_en") or text or "", "language": lang})

    # 4. store the reply first so the person sees it immediately, then run tools
    status = r.get("status") if r.get("status") not in (None, "", "n/a") else None
    reply = Message(user_id=user.id, role="assistant", kind="answer", text=r.get("reply") or "", text_en=r.get("reply_en") or r.get("reply") or "",
                    language=lang, status=status)
    tool_calls = r.get("tools") or []
    if user.role == "resident" and any(t.get("name") == "escalate_to_responders" for t in tool_calls) and contact:
        reply.contact = common.contact_to_dict(contact)
    await tools.deliver(user, reply)

    asyncio.create_task(_run_tools(user, r, reply, lang, transcript))
    asyncio.create_task(_voice(reply, want_autoplay=bool(audio)))
    return {"ok": True, "user_message": common.message_to_dict(user_msg), "reply": common.message_to_dict(reply), "autoplay": bool(audio)}


async def _run_tools(user: User, r: dict, reply: Message, lang: str, transcript: str) -> None:
    for call in r.get("tools") or []:
        name, args = call.get("name"), call.get("args") or {}
        try:
            if user.role == "responder" and name == "post_alert":
                a = await tools.post_alert(user, category=args.get("category") or "other", severity=args.get("severity") or "caution",
                                           location=args.get("location") or "", summary_en=args.get("summary_en") or (r.get("text_en") or transcript),
                                           broadcast_local=args.get("broadcast_local") or "", closes_alert_ids=args.get("closes_alert_ids"),
                                           answers_question_ids=args.get("answers_question_ids"), confirms_report_ids=args.get("confirms_report_ids"),
                                           language=lang, transcript=transcript)
                card = await common.alert_to_dict(a, user)
                await update(messages, reply.id, {"alert": card, "alert_id": a.id, "kind": "readback"})
                reply.alert, reply.alert_id, reply.kind = card, a.id, "readback"
                broker.publish("message", common.message_to_dict(reply), audience=f"user:{user.id}")
            elif user.role == "responder" and name == "dismiss_report":
                await tools.dismiss_report(user, report_ids=args.get("report_ids") or [], note_en=args.get("note_en") or "")
            elif user.role == "resident" and name == "escalate_to_responders":
                q = await tools.escalate_to_responders(user, question_en=args.get("question_en") or r.get("text_en") or transcript,
                                                       location=args.get("location") or r.get("location") or "", category=args.get("category") or r.get("category") or "other",
                                                       language=lang, status=r.get("status") or "no_information", message_id=reply.id)
                await update(messages, reply.id, {"question_id": q.id})
            elif user.role == "resident" and name == "file_resident_report":
                a = await tools.file_resident_report(user, report_en=args.get("report_en") or r.get("text_en") or transcript,
                                                     location=args.get("location") or "", category=args.get("category") or "other",
                                                     severity=args.get("severity") or "caution", language=lang, transcript=transcript)
                await update(messages, reply.id, {"alert_id": a.id})
        except Exception:  # noqa: BLE001
            log.exception("tool %s failed", name)


async def _voice(reply: Message, *, want_autoplay: bool) -> None:
    """Generate the spoken reply in the background and push it to the chat when ready."""
    if not reply.text:
        return
    try:
        wav = await gemini.speak(reply.text, reply.language)
        url = await put_media(f"tts_{reply.id}.wav", wav, "audio/wav")
        await update(messages, reply.id, {"audio_url": url})
        broker.publish("message_audio", {"id": reply.id, "audio_url": url, "autoplay": want_autoplay}, audience=f"user:{reply.user_id}")
    except Exception:  # noqa: BLE001
        log.exception("tts failed for %s", reply.id)


async def ensure_audio(msg: Message) -> Optional[str]:
    if msg.audio_url or not msg.text:
        return msg.audio_url
    wav = await gemini.speak(msg.text, msg.language)
    url = await put_media(f"tts_{msg.id}.wav", wav, "audio/wav")
    await update(messages, msg.id, {"audio_url": url})
    return url
