"""Resident asks -> verify against responder alerts -> answer (+ escalate to responders, offer on-duty contact)."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Optional

from .. import gemini
from ..db import Alert, Question, User, alerts, find, get, insert, questions, users, utcnow
from ..notify import broker, push_to_users
from . import common

log = logging.getLogger("saferoad.questions")


async def handle_ask(*, user: User, text: Optional[str], audio: Optional[bytes], mime: str) -> dict:
    responder_alerts = await common.fmt_alerts(await common.open_responder_alerts())
    reports = common.fmt_reports(await common.unconfirmed_resident_reports())
    on_duty = [u for u in await common.on_duty_responders() if u.phone]
    contact = on_duty[0] if on_duty else None
    contact_txt = f"{contact.name} · {contact.area or 'area not set'}" if contact else ""

    r = await gemini.answer_resident(name=user.name, profile_lang=user.language, now=common.now_local_str(), text=text,
                                     audio=audio, mime=mime, responder_alerts=responder_alerts, resident_reports=reports,
                                     contact=contact_txt)
    lang = r.get("language") or user.language
    if lang not in gemini.LANGUAGES:
        lang = user.language
    intent = r.get("intent") or "question"
    escalate = bool(r.get("escalate")) and intent == "question"
    q = Question(
        user_id=user.id, transcript=r.get("transcript") or (text or ""), text_en=r.get("text_en") or (text or ""),
        language=lang, intent=intent, location=r.get("location") or "", category=r.get("category") or "other",
        status=r.get("status") or ("n/a" if intent != "question" else "no_information"),
        matched_alert_ids=[str(x) for x in (r.get("matched_alert_ids") or [])],
        answer_en=r.get("answer_en") or "", answer_local=r.get("answer_local") or r.get("answer_en") or "",
        escalated=escalate, contact_responder_id=contact.id if (contact and escalate) else None,
    )
    await insert(questions, q)
    if intent == "report":
        await insert(alerts, Alert(author_id=user.id, source="resident", category=r.get("category") or "other",
                                   severity=r.get("severity") or "caution", location=r.get("location") or "",
                                   summary_en=r.get("text_en") or (text or ""), transcript=q.transcript, language=lang,
                                   status="unconfirmed", expires_at=utcnow() + timedelta(hours=6)))
    qd = await common.question_to_dict(q, user)
    broker.publish("new_question", qd, audience="admin")
    if escalate or intent == "report":
        asyncio.create_task(_escalate(qd, intent))
    return {"ok": True, **qd}


async def _escalate(qd: dict, intent: str) -> None:
    try:
        responders = [u.id for u in await find(users, User, {"role": "responder"})]
        broker.publish("escalation", qd, audience="responders")
        if intent == "report":
            title, body = "👀 Resident report (unconfirmed)", f"{qd['text_en']}\nCan you confirm? Post an update."
        else:
            title, body = "❓ Resident is asking", f"{qd['text_en']}\nAny information? Post an update and they will be notified."
        await push_to_users(responders, {"title": title, "body": body, "url": f"/responder?question={qd['id']}",
                                         "tag": f"esc-{qd['id']}"}, question_id=qd["id"])
    except Exception:  # noqa: BLE001
        log.exception("escalation failed for question %s", qd.get("id"))
