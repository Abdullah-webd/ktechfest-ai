from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from pymongo import DESCENDING

from ..config import settings
from ..db import Alert, Message, Question, User, alerts, find, get_user, questions, users, utcnow

TZ = ZoneInfo(settings.timezone)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo("UTC"))


def minutes_ago(dt: datetime) -> int:
    return max(0, int((utcnow() - _aware(dt)).total_seconds() // 60))


def human_ago(dt: datetime) -> str:
    m = minutes_ago(dt)
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m} min ago"
    h = m // 60
    if h < 24:
        return f"{h} h ago"
    return f"{h // 24} d ago"


def local_time(dt: datetime) -> str:
    return _aware(dt).astimezone(TZ).strftime("%-I:%M %p")


def local_day(dt: datetime) -> str:
    d = _aware(dt).astimezone(TZ).date()
    today = datetime.now(TZ).date()
    if d == today:
        return "Today"
    if (today - d).days == 1:
        return "Yesterday"
    return d.strftime("%a %-d %b")


def now_local_str() -> str:
    return datetime.now(TZ).strftime("%A %-I:%M %p")


async def on_duty_responders() -> list[User]:
    return await find(users, User, {"role": "responder", "on_duty": True, "verified": True}, sort=[("last_active_at", DESCENDING)])


def contact_to_dict(u: Optional[User]) -> Optional[dict]:
    if not u or not u.phone:
        return None
    return {"id": u.id, "name": u.name, "phone": u.phone, "area": u.area or "", "photo": u.photo_url}


async def open_responder_alerts(hours: int = 24) -> list[Alert]:
    since = utcnow() - timedelta(hours=hours)
    return await find(alerts, Alert, {"source": "responder", "created_at": {"$gte": since}}, sort=[("created_at", DESCENDING)])


async def unconfirmed_resident_reports(hours: int = 6) -> list[Alert]:
    since = utcnow() - timedelta(hours=hours)
    return await find(alerts, Alert, {"source": "resident", "status": "unconfirmed", "created_at": {"$gte": since}}, sort=[("created_at", DESCENDING)])


async def open_questions(hours: int = 12) -> list[Question]:
    since = utcnow() - timedelta(hours=hours)
    return await find(questions, Question, {"escalated": True, "resolved_alert_id": None, "created_at": {"$gte": since}}, sort=[("created_at", DESCENDING)])


async def users_by_id(ids: set[str]) -> dict[str, User]:
    out: dict[str, User] = {}
    for i in ids:
        u = await get_user(i)
        if u:
            out[i] = u
    return out


async def fmt_alerts(alist: list[Alert]) -> str:
    authors = await users_by_id({a.author_id for a in alist})
    return "\n".join(f"{a.id} · {a.status} · {a.category} · {a.severity} · {a.location or '-'} · {a.summary_en} · by "
                     f"{authors[a.author_id].name if a.author_id in authors else '?'} · {minutes_ago(a.created_at)} min ago" for a in alist)


def fmt_reports(alist: list[Alert]) -> str:
    return "\n".join(f"{a.id} · {a.location or '-'} · {a.summary_en} · {minutes_ago(a.created_at)} min ago" for a in alist)


def fmt_questions(qs: list[Question]) -> str:
    return "\n".join(f"{q.id} · {q.location or '-'} · {q.category} · {q.text_en}" for q in qs)


def fmt_history(msgs: list[Message]) -> str:
    lines = []
    for m in msgs:
        who = "Person" if m.role == "user" else "SafeRoad"
        body = m.text_en or m.text
        if m.kind == "alert":
            body = f"[alert broadcast] {body}"
        elif m.kind == "escalation":
            body = f"[resident question forwarded] {body}"
        lines.append(f"{who}: {body[:300]}")
    return "\n".join(lines)


async def alert_to_dict(a: Alert, author: Optional[User] = None) -> dict:
    author = author or await get_user(a.author_id)
    return {"id": a.id, "category": a.category, "severity": a.severity, "location": a.location, "summary_en": a.summary_en,
            "status": a.status, "source": a.source, "author": author.name if author else "Unknown",
            "author_photo": author.photo_url if author else None, "time": local_time(a.created_at), "ago": human_ago(a.created_at),
            "language": a.language, "translations": a.translations, "created_at": a.created_at.isoformat()}


async def alerts_to_dicts(alist: list[Alert]) -> list[dict]:
    authors = await users_by_id({a.author_id for a in alist})
    return [await alert_to_dict(a, authors.get(a.author_id)) for a in alist]


def message_to_dict(m: Message) -> dict:
    return {"id": m.id, "role": m.role, "kind": m.kind, "text": m.text, "language": m.language, "audio_url": m.audio_url,
            "status": m.status, "contact": m.contact, "alert": m.alert, "question_id": m.question_id, "alert_id": m.alert_id,
            "time": local_time(m.created_at), "day": local_day(m.created_at), "created_at": m.created_at.isoformat()}


async def question_to_dict(q: Question, asker: Optional[User] = None) -> dict:
    asker = asker or await get_user(q.user_id)
    return {"id": q.id, "text_en": q.text_en, "language": q.language, "location": q.location, "category": q.category,
            "status": q.status, "escalated": q.escalated, "resolved": q.resolved_alert_id is not None,
            "asker": asker.name if asker else "?", "asker_id": q.user_id, "time": local_time(q.created_at), "ago": human_ago(q.created_at)}
