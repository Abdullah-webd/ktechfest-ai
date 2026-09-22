from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pymongo import DESCENDING
from pymongo.errors import DuplicateKeyError
from sse_starlette.sse import EventSourceResponse

from . import gemini
from .auth import COOKIE, current_user, hash_password, make_session_cookie, optional_user, responder_user, verify_password
from .config import LANGUAGES, settings
from .db import (Alert, Delivery, PushSubscription, Question, User, alerts, deliveries, find, get, get_media, init_db,
                 insert, push_subs, put_media, questions, update, users, utcnow)
from .notify import broker, push_to_users
from .pipelines import common
from .pipelines.alerts import handle_report
from .pipelines.questions import handle_ask

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("saferoad")

BASE = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await init_db()
        log.info("MongoDB ready")
    except Exception as e:  # noqa: BLE001
        log.error("MongoDB not reachable at startup (%s). Check Atlas Network Access allows 0.0.0.0/0.", str(e)[:200])
    yield


app = FastAPI(title="SafeRoad", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "web" / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "web" / "templates")
templates.env.globals.update(LANGUAGES=LANGUAGES, VAPID_PUBLIC_KEY=settings.vapid_public_key)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def _home_for(user: User) -> str:
    return "/responder" if user.role == "responder" else "/app"


def _login_response(user: User) -> RedirectResponse:
    resp = RedirectResponse(_home_for(user), status_code=303)
    resp.set_cookie(COOKIE, make_session_cookie(user.id), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


# ---------- PWA files at root ----------
@app.get("/sw.js")
async def service_worker():
    return FileResponse(BASE / "web" / "static" / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/media/{name}")
async def media_file(name: str):
    doc = await get_media(name)
    if not doc:
        raise HTTPException(404)
    return Response(content=doc["data"], media_type=doc.get("content_type", "application/octet-stream"),
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(BASE / "web" / "static" / "manifest.webmanifest", media_type="application/manifest+json")


# ---------- Pages ----------
@app.get("/", response_class=HTMLResponse)
async def landing(request: Request, user: Optional[User] = Depends(optional_user)):
    return render(request, "landing.html", user=user)


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request, role: str = "resident", user: Optional[User] = Depends(optional_user)):
    if user:
        return RedirectResponse(_home_for(user), status_code=303)
    return render(request, "signup.html", role="responder" if role == "responder" else "resident", error=None, user=None)


@app.post("/signup")
async def signup(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...),
                 language: str = Form("en"), role: str = Form("resident"), invite_code: str = Form(""),
                 phone: str = Form(""), area: str = Form(""), photo: Optional[UploadFile] = File(None)):
    email = email.strip().lower()
    role = "responder" if role == "responder" else "resident"
    err = None
    if len(password) < 4:
        err = "Password must be at least 4 characters."
    elif await users.find_one({"email": email}):
        err = "That email is already registered. Log in instead."
    elif role == "responder" and invite_code.strip().upper() not in settings.invite_codes:
        err = "Invalid responder invite code."
    elif role == "responder" and len(phone.strip()) < 7:
        err = "Responders need a phone number residents can call."
    if err:
        return render(request, "signup.html", role=role, error=err, user=None)
    photo_url = None
    if photo and photo.filename:
        data = await photo.read()
        if data:
            ext = os.path.splitext(photo.filename)[1].lower() or ".jpg"
            photo_url = await put_media(f"photo_{uuid.uuid4().hex}{ext}", data, photo.content_type or "image/jpeg")
    user = User(email=email, password_hash=hash_password(password), name=name.strip()[:60], role=role,
                language=language if language in LANGUAGES else "en", photo_url=photo_url,
                phone=phone.strip() or None, area=area.strip()[:80] or None, on_duty=(role == "responder"))
    try:
        await insert(users, user)
    except DuplicateKeyError:
        return render(request, "signup.html", role=role, error="That email is already registered.", user=None)
    return _login_response(user)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user: Optional[User] = Depends(optional_user)):
    if user:
        return RedirectResponse(_home_for(user), status_code=303)
    return render(request, "login.html", error=None, user=None)


@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    user = User.from_doc(await users.find_one({"email": email.strip().lower()}))
    if not user or not verify_password(password, user.password_hash):
        return render(request, "login.html", error="Wrong email or password.", user=None)
    return _login_response(user)


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/app", response_class=HTMLResponse)
async def resident_home(request: Request, user: User = Depends(current_user)):
    if user.role == "responder":
        return RedirectResponse("/responder", status_code=303)
    alist = await common.alerts_to_dicts(await common.open_responder_alerts())
    qs = await find(questions, Question, {"user_id": user.id}, sort=[("created_at", DESCENDING)], limit=20)
    qlist = await common.questions_to_dicts(list(reversed(qs)))
    return render(request, "resident.html", user=user, alerts=alist, questions=qlist)


@app.get("/responder", response_class=HTMLResponse)
async def responder_home(request: Request, user: User = Depends(responder_user)):
    alist = await common.alerts_to_dicts(await common.open_responder_alerts())
    open_qs = await common.questions_to_dicts(await common.open_questions())
    reports = await common.alerts_to_dicts(await common.unconfirmed_resident_reports())
    return render(request, "responder.html", user=user, alerts=alist, open_questions=open_qs, reports=reports)


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, user: Optional[User] = Depends(optional_user)):
    alist = await common.alerts_to_dicts(await find(alerts, Alert, {}, sort=[("created_at", DESCENDING)], limit=50))
    qlist = await common.questions_to_dicts(await find(questions, Question, {}, sort=[("created_at", DESCENDING)], limit=50))
    ulist = await find(users, User, {}, sort=[("created_at", DESCENDING)])
    subs = await push_subs.count_documents({})
    dlist = await find(deliveries, Delivery, {}, sort=[("created_at", DESCENDING)], limit=30)
    return render(request, "admin.html", user=user, alerts=alist, questions=qlist, users=ulist, subs=subs, deliveries=dlist)


# ---------- APIs ----------
async def _read_upload(audio: Optional[UploadFile]) -> tuple[Optional[bytes], str]:
    if not audio or not audio.filename:
        return None, "audio/wav"
    data = await audio.read()
    if not data:
        return None, "audio/wav"
    mime = audio.content_type or "audio/wav"
    if mime not in ("audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp3", "audio/ogg", "audio/aac", "audio/flac", "audio/webm", "audio/mp4"):
        mime = "audio/wav"
    return data, mime


@app.post("/api/ask")
async def api_ask(text: str = Form(""), audio: Optional[UploadFile] = File(None), user: User = Depends(current_user)):
    data, mime = await _read_upload(audio)
    if not data and not text.strip():
        raise HTTPException(400, "Say or type something.")
    try:
        return await handle_ask(user=user, text=text.strip() or None, audio=data, mime=mime)
    except Exception as e:  # noqa: BLE001
        log.exception("ask failed")
        raise HTTPException(500, f"AI error: {str(e)[:200]}")


@app.post("/api/report")
async def api_report(text: str = Form(""), audio: Optional[UploadFile] = File(None), user: User = Depends(responder_user)):
    data, mime = await _read_upload(audio)
    if not data and not text.strip():
        raise HTTPException(400, "Say or type something.")
    await update(users, user.id, {"last_active_at": utcnow()})
    try:
        return await handle_report(author=user, text=text.strip() or None, audio=data, mime=mime)
    except Exception as e:  # noqa: BLE001
        log.exception("report failed")
        raise HTTPException(500, f"AI error: {str(e)[:200]}")


@app.post("/api/speak")
async def api_speak(kind: str = Form(...), id: str = Form(...), user: User = Depends(current_user)):
    """Generate (or reuse) a native-sounding voice file for a question answer or an alert, in the user's language."""
    if kind == "question":
        q = await get(questions, Question, id)
        if not q or (q.user_id != user.id and user.role != "responder"):
            raise HTTPException(404)
        if q.answer_audio_url:
            return {"url": q.answer_audio_url}
        text, lang = q.answer_local or q.answer_en, q.language
        url = await put_media(f"q{q.id}_{lang}_{uuid.uuid4().hex[:6]}.wav", await gemini.speak(text, lang), "audio/wav")
        await update(questions, q.id, {"answer_audio_url": url})
        return {"url": url}
    if kind == "alert":
        a = await get(alerts, Alert, id)
        if not a:
            raise HTTPException(404)
        lang = user.language if user.id != a.author_id else a.language
        if user.id == a.author_id and a.readback_local:
            text, fname = a.readback_local, f"a{a.id}_readback_{lang}.wav"
        else:
            text = a.translations.get(lang)
            if not text:
                tr = dict(a.translations)
                tr.update(await gemini.translate_many(a.summary_en, [lang]))
                await update(alerts, a.id, {"translations": tr})
                text = tr.get(lang) or a.summary_en
            fname = f"a{a.id}_{lang}.wav"
        if not await get_media(fname):
            await put_media(fname, await gemini.speak(text, lang), "audio/wav")
        return {"url": f"/media/{fname}", "text": text}
    raise HTTPException(400)


@app.post("/api/duty")
async def api_duty(on_duty: bool = Form(...), user: User = Depends(responder_user)):
    await update(users, user.id, {"on_duty": on_duty, "last_active_at": utcnow()})
    return {"ok": True, "on_duty": on_duty}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request, user: User = Depends(current_user)):
    body = await request.json()
    endpoint, keys = body.get("endpoint"), body.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(400, "bad subscription")
    await push_subs.update_one({"endpoint": endpoint},
                               {"$set": {"user_id": user.id, "p256dh": keys["p256dh"], "auth": keys["auth"]},
                                "$setOnInsert": {"created_at": utcnow()}}, upsert=True)
    return {"ok": True}


@app.post("/api/push/test")
async def push_test(user: User = Depends(current_user)):
    return await push_to_users([user.id], {"title": "🔔 SafeRoad alerts are on",
                                           "body": f"Hi {user.name.split(' ')[0]}, you will now hear from responders here.",
                                           "url": _home_for(user)})


@app.get("/api/alerts")
async def api_alerts(user: User = Depends(current_user)):
    return await common.alerts_to_dicts(await common.open_responder_alerts())


@app.get("/events")
async def events(request: Request, user: Optional[User] = Depends(optional_user)):
    role = "admin" if request.query_params.get("admin") == "1" else (user.role if user else "anon")
    q = broker.connect(user_id=user.id if user else "", role=role)

    async def gen():
        try:
            yield {"event": "hello", "data": "{}"}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20)
                    yield {"event": "message", "data": msg}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            broker.disconnect(q)

    return EventSourceResponse(gen())


@app.get("/healthz")
async def healthz():
    try:
        await users.estimated_document_count()
        return {"ok": True, "db": "ok"}
    except Exception as e:  # noqa: BLE001
        return {"ok": True, "db": f"error: {str(e)[:120]}"}
