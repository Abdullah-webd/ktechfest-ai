"""Live feed (Server-Sent Events) and Web Push. No email, by decision."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from pywebpush import WebPushException, webpush

from .config import settings
from .db import Delivery, PushSubscription, deliveries, find, insert, push_subs

log = logging.getLogger("saferoad.notify")


class Broker:
    """Fan-out of events to connected browsers. Each connection has a queue.
    audience: "all" | "responders" | "user:<id>" | "admin". Admin connections receive everything."""

    def __init__(self) -> None:
        self._conns: dict[asyncio.Queue, dict[str, Any]] = {}

    def connect(self, *, user_id: str, role: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._conns[q] = {"user_id": user_id, "role": role}
        return q

    def disconnect(self, q: asyncio.Queue) -> None:
        self._conns.pop(q, None)

    def publish(self, event: str, data: dict[str, Any], *, audience: str = "all") -> None:
        payload = json.dumps({"event": event, "data": data}, default=str)
        for q, meta in list(self._conns.items()):
            ok = meta["role"] == "admin" or audience == "all" \
                or (audience == "responders" and meta["role"] == "responder") \
                or audience == f"user:{meta['user_id']}"
            if ok:
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    pass


broker = Broker()


def _send_one(sub: PushSubscription, payload: dict[str, Any]) -> Optional[str]:
    try:
        webpush(
            subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
            data=json.dumps(payload),
            vapid_private_key=settings.vapid_private_key,
            vapid_claims={"sub": settings.vapid_claims_email},
            ttl=600,
        )
        return None
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        return "gone" if status in (404, 410) else f"{status}: {str(e)[:120]}"
    except Exception as e:  # noqa: BLE001
        return str(e)[:160]


async def push_to_users(user_ids: list[str], payload: dict[str, Any], *, alert_id: Optional[str] = None,
                        question_id: Optional[str] = None) -> dict[str, int]:
    """Send a push to every subscription of the given users. Records deliveries. Returns counts."""
    if not user_ids:
        return {"sent": 0, "failed": 0}
    subs = await find(push_subs, PushSubscription, {"user_id": {"$in": user_ids}})
    if not subs:
        return {"sent": 0, "failed": 0}
    loop = asyncio.get_running_loop()
    results = await asyncio.gather(*[loop.run_in_executor(None, _send_one, sub, payload) for sub in subs])
    sent = failed = 0
    for sub, err in zip(subs, results):
        if err == "gone":
            await push_subs.delete_one({"endpoint": sub.endpoint})
        await insert(deliveries, Delivery(alert_id=alert_id, question_id=question_id, user_id=sub.user_id, channel="push",
                                          status="failed" if err else "sent", error=err or ""))
        if err:
            failed += 1
        else:
            sent += 1
    log.info("push sent=%s failed=%s users=%s", sent, failed, len(user_ids))
    return {"sent": sent, "failed": failed}
