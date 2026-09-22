"""MongoDB data layer (pymongo async). Documents are exposed as small Pydantic models with a string `id`."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Type, TypeVar

from bson import ObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


client = AsyncMongoClient(settings.mongodb_uri, tz_aware=True, serverSelectionTimeoutMS=10000)
db = client[settings.mongodb_db]
users = db.users
alerts = db.alerts
questions = db.questions
push_subs = db.push_subscriptions
deliveries = db.deliveries
media = db.media  # {_id: filename, data: bytes, content_type: str, created_at}


def oid(value: Any) -> Optional[ObjectId]:
    try:
        return ObjectId(str(value))
    except Exception:  # noqa: BLE001
        return None


class Doc(BaseModel):
    id: str = ""

    @classmethod
    def from_doc(cls: Type[T], d: Optional[dict]) -> Optional[T]:
        if not d:
            return None
        d = dict(d)
        d["id"] = str(d.pop("_id"))
        return cls(**{k: v for k, v in d.items() if k in cls.model_fields})

    def to_doc(self) -> dict:
        d = self.model_dump()
        d.pop("id", None)
        return d


T = TypeVar("T", bound=Doc)


class User(Doc):
    email: str
    password_hash: str
    name: str
    role: str = "resident"  # resident | responder
    language: str = "en"
    photo_url: Optional[str] = None
    phone: Optional[str] = None   # responders: shown to residents only while on duty
    area: Optional[str] = None    # responders: area they usually cover
    on_duty: bool = False
    last_active_at: datetime = Field(default_factory=utcnow)
    created_at: datetime = Field(default_factory=utcnow)


class Alert(Doc):
    author_id: str
    source: str = "responder"  # responder | resident
    category: str = "other"
    severity: str = "caution"  # info | caution | danger
    location: str = ""
    summary_en: str
    transcript: str = ""
    language: str = "en"
    readback_local: str = ""
    translations: dict[str, str] = Field(default_factory=dict)
    audio_url: Optional[str] = None
    status: str = "open"  # open | closed | unconfirmed | contradicted
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: Optional[datetime] = None


class Question(Doc):
    user_id: str
    transcript: str = ""
    text_en: str = ""
    language: str = "en"
    intent: str = "question"
    location: str = ""
    category: str = "other"
    status: str = "no_information"
    matched_alert_ids: list[str] = Field(default_factory=list)
    answer_en: str = ""
    answer_local: str = ""
    answer_audio_url: Optional[str] = None
    escalated: bool = False
    contact_responder_id: Optional[str] = None
    resolved_alert_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class PushSubscription(Doc):
    user_id: str
    endpoint: str
    p256dh: str
    auth: str
    created_at: datetime = Field(default_factory=utcnow)


class Delivery(Doc):
    alert_id: Optional[str] = None
    question_id: Optional[str] = None
    user_id: str
    channel: str = "push"
    status: str = "sent"
    error: str = ""
    created_at: datetime = Field(default_factory=utcnow)


async def init_db() -> None:
    await users.create_index([("email", ASCENDING)], unique=True)
    await users.create_index([("role", ASCENDING), ("on_duty", ASCENDING), ("last_active_at", DESCENDING)])
    await alerts.create_index([("created_at", DESCENDING)])
    await alerts.create_index([("source", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)])
    await questions.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    await questions.create_index([("escalated", ASCENDING), ("resolved_alert_id", ASCENDING), ("created_at", DESCENDING)])
    await push_subs.create_index([("endpoint", ASCENDING)], unique=True)
    await push_subs.create_index([("user_id", ASCENDING)])
    await deliveries.create_index([("created_at", DESCENDING)])


async def insert(coll, model: Doc) -> str:
    res = await coll.insert_one(model.to_doc())
    model.id = str(res.inserted_id)
    return model.id


async def get(coll, model_cls: Type[T], id_: Any) -> Optional[T]:
    _id = oid(id_)
    if not _id:
        return None
    return model_cls.from_doc(await coll.find_one({"_id": _id}))


async def find(coll, model_cls: Type[T], filter_: dict, *, sort=None, limit: int = 0) -> list[T]:
    cur = coll.find(filter_)
    if sort:
        cur = cur.sort(sort)
    if limit:
        cur = cur.limit(limit)
    return [model_cls.from_doc(d) for d in await cur.to_list(length=limit or None)]


async def update(coll, id_: Any, fields: dict) -> None:
    _id = oid(id_)
    if _id:
        await coll.update_one({"_id": _id}, {"$set": fields})


async def put_media(name: str, data: bytes, content_type: str) -> str:
    await media.replace_one({"_id": name}, {"_id": name, "data": data, "content_type": content_type, "created_at": utcnow()}, upsert=True)
    return f"/media/{name}"


async def get_media(name: str) -> Optional[dict]:
    return await media.find_one({"_id": name})


async def get_user(id_: Any) -> Optional[User]:
    return await get(users, User, id_)
