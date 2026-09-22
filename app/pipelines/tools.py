"""Tools the agent can run. Each one changes the world: publishes alerts, notifies people, files reports."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from .. import agent
from ..config import settings
from ..db import Alert, Message, Question, User, alerts, find, get, insert, messages, oid, questions, update, users, utcnow
from ..notify import broker, push_to_users
from . import common

log = logging.getLogger("saferoad.tools")


async def _translations(text_en: str, langs: list[str], *, style: str) -> dict[str, str]:
    """One Gemini call for all non-English languages in the list (never raises)."""
    need = sorted({l for l in langs if l != "en"})
    out = {"en": text_en}
    if need:
        try:
            out.update(await agent.translate_many(text_en, need, style=style))
        except Exception:  # noqa: BLE001
            log.exception("translation failed")
    return out


async def deliver(user: User, msg: Message, *, push: Optional[dict] = None) -> Message:
    """Append a message to a user's conversation, push it over SSE, and optionally as a notification."""
    await insert(messages, msg)
    broker.publish("message", common.message_to_dict(msg), audience=f"user:{user.id}")
    if push:
        await push_to_users([user.id], {**push, "url": push.get("url", "/chat"), "tag": push.get("tag", f"m-{msg.id}")},
                            alert_id=msg.alert_id, question_id=msg.question_id)
    return msg


# ---------------- Responder tools ----------------
async def post_alert(author: User, *, category: str, severity: str, location: str, summary_en: str, broadcast_local: str = "",
                     closes_alert_ids: Optional[list] = None, answers_question_ids: Optional[list] = None,
                     confirms_report_ids: Optional[list] = None, language: str = "en", transcript: str = "") -> Alert:
    is_clear = category == "all_clear"
    alert = Alert(author_id=author.id, source="responder", category=category or "other",
                  severity="info" if is_clear else (severity or "caution"), location=location or "", summary_en=summary_en,
                  transcript=transcript, language=language, translations={language: broadcast_local or "", "en": summary_en},
                  status="open", expires_at=utcnow() + timedelta(hours=settings.alert_ttl_hours))
    await insert(alerts, alert)
    ids = [oid(x) for x in (closes_alert_ids or []) if oid(x)]
    if ids:
        await alerts.update_many({"_id": {"$in": ids}, "status": "open"}, {"$set": {"status": "closed"}})
    rids = [oid(x) for x in (confirms_report_ids or []) if oid(x)]
    if rids:
        await alerts.update_many({"_id": {"$in": rids}}, {"$set": {"status": "closed"}})

    # translate once per language present in the community
    everyone = await find(users, User, {"verified": True})
    langs = sorted({u.language for u in everyone} | {"en"})
    tr = dict(alert.translations)
    need = [l for l in langs if not tr.get(l)]
    if need:
        try:
            tr.update(await agent.translate_many(summary_en, need))
        except Exception:  # noqa: BLE001
            log.exception("translation failed")
    alert.translations = tr
    await update(alerts, alert.id, {"translations": tr})
    card = await common.alert_to_dict(alert, author)
    broker.publish("new_alert", card, audience="all")

    icon = "✅" if is_clear else ("🚨" if alert.severity == "danger" else "⚠️")
    prefix = {"danger": "DANGER", "caution": "Caution", "info": "Update"}.get(alert.severity, "Alert")
    for u in everyone:
        if u.id == author.id:
            continue
        body = tr.get(u.language) or summary_en
        msg = Message(user_id=u.id, role="assistant", kind="alert", text=body, text_en=summary_en, language=u.language,
                      alert=card, alert_id=alert.id)
        await deliver(u, msg, push={"title": f"{icon} {prefix}: {location or category.replace('_', ' ')}",
                                    "body": f"{body}\n— {author.name}, {common.local_time(alert.created_at)}",
                                    "tag": f"alert-{alert.id}"})

    for qid in answers_question_ids or []:
        await _answer_waiting(str(qid), alert, author)
    return alert


async def _answer_waiting(question_id: str, a: Alert, author: User) -> None:
    q = await get(questions, Question, question_id)
    if not q or q.resolved_alert_id:
        return
    asker = await get(users, User, q.user_id)
    if not asker:
        return
    when = common.human_ago(a.created_at)
    if a.category == "all_clear":
        answer_en = f"Update on what you asked: {author.name} reported all clear {when}. {a.summary_en}"
        status = "contradicted"
    else:
        answer_en = f"Update on what you asked: {author.name} confirmed {when}. {a.summary_en}"
        status = "confirmed"
    local = answer_en
    if asker.language != "en":
        try:
            local = (await agent.translate_many(answer_en, [asker.language], style="reply to a worried resident")).get(asker.language) or answer_en
        except Exception:  # noqa: BLE001
            log.exception("translate answer failed")
    await update(questions, q.id, {"status": status, "resolved_alert_id": a.id, "matched_alert_ids": [a.id]})
    msg = Message(user_id=asker.id, role="assistant", kind="update", text=local, text_en=answer_en, language=asker.language,
                  status=status, question_id=q.id, alert_id=a.id)
    await deliver(asker, msg, push={"title": "✅ Answer to your question", "body": local, "tag": f"q-{q.id}"})
    q.status, q.resolved_alert_id = status, a.id
    broker.publish("question_resolved", await common.question_to_dict(q, asker), audience="responders")


async def dismiss_report(author: User, *, report_ids: list, note_en: str = "") -> None:
    ids = [oid(x) for x in (report_ids or []) if oid(x)]
    if not ids:
        return
    await alerts.update_many({"_id": {"$in": ids}}, {"$set": {"status": "contradicted"}})
    for a in await find(alerts, Alert, {"_id": {"$in": ids}}):
        reporter = await get(users, User, a.author_id)
        if not reporter:
            continue
        text_en = f"{author.name} checked your report ({a.summary_en}) and found nothing there. {note_en}".strip()
        local = text_en
        if reporter.language != "en":
            try:
                local = (await agent.translate_many(text_en, [reporter.language], style="reply to a resident")).get(reporter.language) or text_en
            except Exception:  # noqa: BLE001
                pass
        await deliver(reporter, Message(user_id=reporter.id, role="assistant", kind="update", text=local, text_en=text_en,
                                        language=reporter.language, status="contradicted", alert_id=a.id),
                      push={"title": "Update on your report", "body": local})


# ---------------- Resident tools ----------------
async def escalate_to_responders(asker: User, *, question_en: str, location: str = "", category: str = "other",
                                 language: str = "en", status: str = "no_information", message_id: Optional[str] = None) -> Question:
    on_duty = [u for u in await common.on_duty_responders() if u.phone]
    contact = on_duty[0] if on_duty else None
    q = Question(user_id=asker.id, text_en=question_en, language=language, location=location or "", category=category or "other",
                 status=status, escalated=True, contact_responder_id=contact.id if contact else None, message_id=message_id)
    await insert(questions, q)
    qd = await common.question_to_dict(q, asker)
    broker.publish("escalation", qd, audience="responders")
    responders = await find(users, User, {"role": "responder", "verified": True})
    text_en = f"{asker.first_name} is asking: “{question_en}”" + (f" (near {location})" if location else "") + ". Any information? Post an update and they will be notified automatically."
    tr = await _translations(text_en, [r.language for r in responders], style="message to a responder")
    for r in responders:
        local = tr.get(r.language) or text_en
        await deliver(r, Message(user_id=r.id, role="assistant", kind="escalation", text=local, text_en=text_en, language=r.language,
                                 question_id=q.id),
                      push={"title": "❓ A resident is asking", "body": question_en, "url": "/chat", "tag": f"esc-{q.id}"})
    q.contact_responder_id = contact.id if contact else None
    return q


async def file_resident_report(reporter: User, *, report_en: str, location: str = "", category: str = "other",
                               severity: str = "caution", language: str = "en", transcript: str = "") -> Alert:
    a = Alert(author_id=reporter.id, source="resident", category=category or "other", severity=severity or "caution",
              location=location or "", summary_en=report_en, transcript=transcript, language=language, status="unconfirmed",
              expires_at=utcnow() + timedelta(hours=6))
    await insert(alerts, a)
    broker.publish("new_report", await common.alert_to_dict(a, reporter), audience="responders")
    responders = await find(users, User, {"role": "responder", "verified": True})
    text_en = f"{reporter.first_name} reported: “{report_en}”" + (f" (near {location})" if location else "") + ". Not yet confirmed. Can you check and post an update?"
    tr = await _translations(text_en, [r.language for r in responders], style="message to a responder")
    for r in responders:
        local = tr.get(r.language) or text_en
        await deliver(r, Message(user_id=r.id, role="assistant", kind="escalation", text=local, text_en=text_en, language=r.language, alert_id=a.id),
                      push={"title": "👀 Unconfirmed resident report", "body": report_en, "url": "/chat", "tag": f"rep-{a.id}"})
    return a
