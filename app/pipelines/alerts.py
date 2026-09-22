"""Responder report -> structured alert -> broadcast (push + live feed) -> answer waiting residents."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Optional

from .. import gemini
from ..config import settings
from ..db import Alert, Question, User, alerts, find, get, get_user, insert, oid, questions, update, users, utcnow
from ..notify import broker, push_to_users
from . import common

log = logging.getLogger("saferoad.alerts")


async def handle_report(*, author: User, text: Optional[str], audio: Optional[bytes], mime: str) -> dict:
    open_alerts = await common.fmt_alerts([a for a in await common.open_responder_alerts() if a.status == "open"])
    open_qs = await common.open_questions()
    r = await gemini.understand_report(name=author.name, profile_lang=author.language, text=text, audio=audio, mime=mime,
                                       open_alerts=open_alerts, open_questions=common.fmt_questions(open_qs))
    lang = r.get("language") or author.language
    if lang not in gemini.LANGUAGES:
        lang = author.language
    if not r.get("is_report", True):
        return {"ok": True, "is_report": False, "transcript": r.get("transcript", ""), "language": lang,
                "readback_local": r.get("readback_local") or r.get("english_summary", ""), "alert": None}

    is_clear = bool(r.get("is_all_clear"))
    alert = Alert(
        author_id=author.id, source="responder", category="all_clear" if is_clear else (r.get("category") or "other"),
        severity="info" if is_clear else (r.get("severity") or "caution"), location=r.get("location") or "",
        summary_en=r.get("english_summary") or (text or ""), transcript=r.get("transcript") or (text or ""),
        language=lang, readback_local=r.get("readback_local") or "", status="open",
        translations={lang: r.get("broadcast_local") or "", "en": r.get("english_summary") or ""},
        expires_at=utcnow() + timedelta(hours=settings.alert_ttl_hours),
    )
    await insert(alerts, alert)
    close_ids = [oid(x) for x in (r.get("closes_alert_ids") or []) if oid(x)]
    if close_ids:
        await alerts.update_many({"_id": {"$in": close_ids}, "status": "open"}, {"$set": {"status": "closed"}})
    answered_ids = [str(x) for x in (r.get("answers_question_ids") or [])]
    alert_dict = await common.alert_to_dict(alert, author)

    asyncio.create_task(_broadcast(alert.id, author, answered_ids))
    return {"ok": True, "is_report": True, "transcript": alert.transcript, "language": lang,
            "readback_local": alert.readback_local, "alert": alert_dict}


async def _broadcast(alert_id: str, author: User, answered_question_ids: list[str]) -> None:
    try:
        alert = await get(alerts, Alert, alert_id)
        residents = await find(users, User, {"role": "resident"})
        responders = [u for u in await find(users, User, {"role": "responder"}) if u.id != author.id]
        langs = sorted({u.language for u in residents + responders} | {"en"})
        translations = dict(alert.translations)
        need = [l for l in langs if not translations.get(l)]
        if need:
            translations.update(await gemini.translate_many(alert.summary_en, need))
            await update(alerts, alert_id, {"translations": translations})
            alert.translations = translations
        broker.publish("new_alert", await common.alert_to_dict(alert, author), audience="all")

        icon = "✅" if alert.category == "all_clear" else ("🚨" if alert.severity == "danger" else "⚠️")
        title_prefix = {"danger": "DANGER", "caution": "Caution", "info": "Update"}.get(alert.severity, "Alert")
        for u in residents + responders:
            body = translations.get(u.language) or alert.summary_en
            await push_to_users([u.id], {
                "title": f"{icon} {title_prefix}: {alert.location or alert.category.replace('_', ' ')}",
                "body": f"{body}\n— {author.name}, {common.local_time(alert.created_at)}",
                "url": f"/app?alert={alert_id}", "tag": f"alert-{alert_id}",
            }, alert_id=alert_id)

        for qid in answered_question_ids:
            await _answer_waiting(qid, alert, author)
    except Exception:  # noqa: BLE001
        log.exception("broadcast failed for alert %s", alert_id)


async def _answer_waiting(question_id: str, a: Alert, author: User) -> None:
    q = await get(questions, Question, question_id)
    if not q or q.resolved_alert_id:
        return
    asker = await get_user(q.user_id)
    when = common.human_ago(a.created_at)
    if a.category == "all_clear":
        answer_en = f"Update on your question: {author.name} reported all clear {when}. {a.summary_en}"
        status = "contradicted"
    else:
        answer_en = f"Update on your question: {author.name} confirmed {when}. {a.summary_en}"
        status = "confirmed"
    local = answer_en
    if asker and asker.language != "en":
        tr = await gemini.translate_many(answer_en, [asker.language], style="reply to a worried resident")
        local = tr.get(asker.language) or answer_en
    fields = {"status": status, "answer_en": answer_en, "answer_local": local, "resolved_alert_id": a.id,
              "matched_alert_ids": [a.id], "answer_audio_url": None}
    await update(questions, question_id, fields)
    q = q.model_copy(update=fields)
    qd = await common.question_to_dict(q, asker)
    broker.publish("question_answered", qd, audience=f"user:{q.user_id}")
    broker.publish("question_resolved", qd, audience="responders")
    await push_to_users([q.user_id], {"title": "✅ Answer to your question", "body": local,
                                       "url": f"/app?question={question_id}", "tag": f"q-{question_id}"},
                        question_id=question_id)
