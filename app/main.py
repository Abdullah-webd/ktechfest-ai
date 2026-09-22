from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pymongo import DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from sse_starlette.sse import EventSourceResponse

from .auth import (COOKIE, NeedsVerification, check_otp, clear_otp, current_user, hash_password, issue_otp, make_session_cookie,
                   optional_user, verify_password)
from .config import LANGUAGES, settings
from .db import (Alert, Delivery, Message, Question, User, alerts, deliveries, find, get, get_media, init_db, insert, messages,
                 push_subs, put_media, questions, update, users, utcnow)
from .emails import send_code
from .notify import broker, push_to_users
from .pipelines import chat, common

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("saferoad")
BASE = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await init_db()
        log.info("MongoDB ready")
    except Exception as e:  # noqa: BLE001
        log.error("MongoDB not reachable at startup: %s", str(e)[:200])
    yield


app = FastAPI(title="SafeRoad", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "web" / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "web" / "templates")
templates.env.globals.update(LANGUAGES=LANGUAGES, VAPID_PUBLIC_KEY=settings.vapid_public_key)


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _set_session(resp: Response, user: User) -> Response:
    resp.set_cookie(COOKIE, make_session_cookie(user.id), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


# ---------- error handling: never show a raw 500 ----------
@app.exception_handler(NeedsVerification)
async def _needs_verification(request: Request, exc: NeedsVerification):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Verify your email first"}, status_code=403)
    return RedirectResponse("/verify", status_code=303)


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    if exc.status_code == 401 and not request.url.path.startswith("/api/"):
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    user = await optional_user(request)
    return render(request, "error.html", status_code=exc.status_code, user=user, code=exc.status_code,
                  message=exc.detail if isinstance(exc.detail, str) else "Something went wrong.")


@app.exception_handler(PyMongoError)
async def _db_error(request: Request, exc: PyMongoError):
    log.error("database error: %s", str(exc)[:200])
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "The database is unreachable right now. Please try again in a moment."}, status_code=503)
    return render(request, "error.html", status_code=503, user=None, code=503,
                  message="We can't reach the database right now. Please try again in a moment.")


@app.exception_handler(Exception)
async def _any_error(request: Request, exc: Exception):
    log.exception("unhandled error")
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Something went wrong on our side. Please try again."}, status_code=500)
    return render(request, "error.html", status_code=500, user=None, code=500, message="Something went wrong on our side. Please try again.")


# ---------- PWA + media ----------
@app.get("/sw.js")
async def service_worker():
    return FileResponse(BASE / "web" / "static" / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(BASE / "web" / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/media/{name}")
async def media_file(name: str):
    doc = await get_media(name)
    if not doc:
        raise HTTPException(404, "File not found")
    return Response(content=doc["data"], media_type=doc.get("content_type", "application/octet-stream"),
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ---------- Public pages ----------
@app.get("/", response_class=HTMLResponse)
async def landing(request: Request, user: Optional[User] = Depends(optional_user)):
    return render(request, "landing.html", user=user)


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request, role: str = "resident", user: Optional[User] = Depends(optional_user)):
    if user and user.verified:
        return RedirectResponse("/chat", status_code=303)
    return render(request, "signup.html", role="responder" if role == "responder" else "resident", error=None, user=None, form={})


@app.post("/signup")
async def signup(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...),
                 language: str = Form("en"), role: str = Form("resident"), invite_code: str = Form(""),
                 phone: str = Form(""), area: str = Form(""), photo: Optional[UploadFile] = File(None)):
    email = email.strip().lower()
    role = "responder" if role == "responder" else "resident"
    form = {"name": name, "email": email, "language": language, "phone": phone, "area": area, "invite_code": invite_code}
    err = None
    if len(name.strip()) < 2:
        err = "Please enter your name."
    elif len(password) < 6:
        err = "Password must be at least 6 characters."
    elif role == "responder" and invite_code.strip().upper() not in settings.invite_codes:
        err = "That responder invite code is not valid. Ask your group leader for the current code."
    elif role == "responder" and len(phone.strip()) < 7:
        err = "Responders need a phone number residents can call."
    else:
        existing = User.from_doc(await users.find_one({"email": email}))
        if existing and existing.verified:
            err = "That email is already registered. Log in instead."
        elif existing:
            await users.delete_one({"_id": __import__("bson").ObjectId(existing.id)})
    if err:
        return render(request, "signup.html", role=role, error=err, user=None, form=form)
    photo_url = None
    if photo and photo.filename:
        data = await photo.read()
        if data:
            ext = os.path.splitext(photo.filename)[1].lower() or ".jpg"
            photo_url = await put_media(f"photo_{uuid.uuid4().hex}{ext}", data, photo.content_type or "image/jpeg")
    user = User(email=email, password_hash=hash_password(password), name=name.strip()[:60], role=role,
                language=language if language in LANGUAGES else "en", photo_url=photo_url,
                phone=phone.strip() or None, area=area.strip()[:80] or None, on_duty=(role == "responder"), verified=False)
    try:
        await insert(users, user)
    except DuplicateKeyError:
        return render(request, "signup.html", role=role, error="That email is already registered. Log in instead.", user=None, form=form)
    code = await issue_otp(user, "verify")
    if code:
        await send_code(user.email, code, "verify")
    return _set_session(RedirectResponse("/verify", status_code=303), user)


@app.get("/verify", response_class=HTMLResponse)
async def verify_page(request: Request, user: Optional[User] = Depends(optional_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.verified:
        return RedirectResponse("/chat", status_code=303)
    return render(request, "verify.html", user=user, error=None, sent=request.query_params.get("sent"))


@app.post("/verify")
async def verify(request: Request, code: str = Form(...), user: Optional[User] = Depends(optional_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.verified:
        return RedirectResponse("/chat", status_code=303)
    if await check_otp(user, code, "verify"):
        await update(users, user.id, {"verified": True})
        await clear_otp(user)
        greeting = await _welcome_message(user)
        await insert(messages, greeting)
        return RedirectResponse("/chat", status_code=303)
    return render(request, "verify.html", user=user, error="That code is wrong or has expired. Check the email and try again.", sent=None)


@app.post("/verify/resend")
async def verify_resend(user: Optional[User] = Depends(optional_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    code = await issue_otp(user, "verify")
    if code:
        await send_code(user.email, code, "verify")
    return RedirectResponse("/verify?sent=1", status_code=303)


async def _welcome_message(user: User) -> Message:
    if user.role == "responder":
        en = (f"Welcome, {user.first_name}. Tell me what you see, by voice or text, in any language. I will turn it into an alert and "
              f"send it to every resident in their own language within seconds. Say “all clear” when a situation is over. "
              f"When residents ask about something nobody has reported, I will forward their question to you here.")
    else:
        en = (f"Hello {user.first_name}. Ask me whether any road or area is safe, or send me a message you received and I will check it "
              f"against what the community responders have reported. Speak or type in Yoruba, Hausa, Igbo, Pidgin or English. "
              f"Alerts from responders will appear here the moment they are posted.")
    text = en
    if user.language != "en":
        try:
            from . import agent, speech
            text = (await agent.translate_many(en, [user.language], style="friendly welcome message")).get(user.language) or en
        except Exception:  # noqa: BLE001
            pass
    return Message(user_id=user.id, role="assistant", kind="system", text=text, text_en=en, language=user.language)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user: Optional[User] = Depends(optional_user)):
    if user and user.verified:
        return RedirectResponse("/chat", status_code=303)
    return render(request, "login.html", error=None, user=None, reset=request.query_params.get("reset"))


@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    user = User.from_doc(await users.find_one({"email": email.strip().lower()}))
    if not user or not verify_password(password, user.password_hash):
        return render(request, "login.html", error="Wrong email or password.", user=None, reset=None)
    if not user.verified:
        code = await issue_otp(user, "verify")
        if code:
            await send_code(user.email, code, "verify")
        return _set_session(RedirectResponse("/verify", status_code=303), user)
    return _set_session(RedirectResponse("/chat", status_code=303), user)


@app.get("/forgot", response_class=HTMLResponse)
async def forgot_page(request: Request):
    return render(request, "forgot.html", user=None, error=None)


@app.post("/forgot")
async def forgot(request: Request, email: str = Form(...)):
    email = email.strip().lower()
    user = User.from_doc(await users.find_one({"email": email}))
    if user:
        code = await issue_otp(user, "reset")
        if code:
            await send_code(user.email, code, "reset")
    # always continue, so the form does not reveal which emails exist
    return RedirectResponse(f"/reset?email={email}", status_code=303)


@app.get("/reset", response_class=HTMLResponse)
async def reset_page(request: Request, email: str = ""):
    return render(request, "reset.html", user=None, error=None, email=email)


@app.post("/reset")
async def reset(request: Request, email: str = Form(...), code: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    user = User.from_doc(await users.find_one({"email": email}))
    if len(password) < 6:
        return render(request, "reset.html", user=None, error="Password must be at least 6 characters.", email=email)
    if not user or not await check_otp(user, code, "reset"):
        return render(request, "reset.html", user=None, error="That code is wrong or has expired. Request a new one.", email=email)
    await update(users, user.id, {"password_hash": hash_password(password), "verified": True})
    await clear_otp(user)
    return RedirectResponse("/login?reset=1", status_code=303)


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ---------- App pages ----------
@app.get("/app")
async def app_redirect():
    return RedirectResponse("/chat", status_code=303)


@app.get("/responder")
async def responder_redirect():
    return RedirectResponse("/chat", status_code=303)


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, user: User = Depends(current_user)):
    msgs = [common.message_to_dict(m) for m in await chat.history(user.id, 80)]
    live = await common.alerts_to_dicts([a for a in await common.open_responder_alerts() if a.status == "open"])
    waiting = await _waiting(user)
    return render(request, "chat.html", user=user, messages=msgs, live_alerts=live, waiting=waiting, page="chat")


async def _waiting(user: User) -> list[dict]:
    if user.role != "responder":
        return []
    qs = await common.open_questions()
    askers = await common.users_by_id({q.user_id for q in qs})
    return [await common.question_to_dict(q, askers.get(q.user_id)) for q in qs]


@app.get("/alerts", response_class=HTMLResponse)
async def alerts_page(request: Request, user: User = Depends(current_user)):
    alist = await common.alerts_to_dicts(await find(alerts, Alert, {"source": "responder"}, sort=[("created_at", DESCENDING)], limit=100))
    reports = await common.alerts_to_dicts(await common.unconfirmed_resident_reports()) if user.role == "responder" else []
    return render(request, "alerts.html", user=user, alerts=alist, reports=reports, waiting=await _waiting(user), page="alerts")


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, user: User = Depends(current_user)):
    subs = await push_subs.count_documents({"user_id": user.id})
    return render(request, "profile.html", user=user, subs=subs, saved=request.query_params.get("saved"), page="profile")


@app.post("/profile")
async def profile_save(request: Request, name: str = Form(...), language: str = Form("en"), phone: str = Form(""), area: str = Form(""),
                       photo: Optional[UploadFile] = File(None), user: User = Depends(current_user)):
    fields = {"name": name.strip()[:60] or user.name, "language": language if language in LANGUAGES else user.language,
              "phone": phone.strip() or None, "area": area.strip()[:80] or None}
    if photo and photo.filename:
        data = await photo.read()
        if data:
            ext = os.path.splitext(photo.filename)[1].lower() or ".jpg"
            fields["photo_url"] = await put_media(f"photo_{uuid.uuid4().hex}{ext}", data, photo.content_type or "image/jpeg")
    await update(users, user.id, fields)
    return RedirectResponse("/profile?saved=1", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, user: Optional[User] = Depends(optional_user)):
    alist = await common.alerts_to_dicts(await find(alerts, Alert, {}, sort=[("created_at", DESCENDING)], limit=50))
    qs = await find(questions, Question, {}, sort=[("created_at", DESCENDING)], limit=50)
    askers = await common.users_by_id({q.user_id for q in qs})
    qlist = [await common.question_to_dict(q, askers.get(q.user_id)) for q in qs]
    ulist = await find(users, User, {}, sort=[("created_at", DESCENDING)])
    subs = await push_subs.count_documents({})
    dlist = await find(deliveries, Delivery, {}, sort=[("created_at", DESCENDING)], limit=30)
    return render(request, "admin.html", user=user, alerts=alist, questions=qlist, users=ulist, subs=subs, deliveries=dlist, page="admin")


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


@app.post("/api/chat")
async def api_chat(text: str = Form(""), audio: Optional[UploadFile] = File(None), user: User = Depends(current_user)):
    data, mime = await _read_upload(audio)
    if not data and not text.strip():
        raise HTTPException(400, "Say or type something.")
    try:
        return await chat.handle_turn(user=user, text=text.strip() or None, audio=data, mime=mime)
    except Exception as e:  # noqa: BLE001
        log.exception("chat turn failed")
        raise HTTPException(502, "The assistant is busy right now. Please try again in a few seconds.")


@app.get("/api/messages")
async def api_messages(before: str = "", user: User = Depends(current_user)):
    return [common.message_to_dict(m) for m in await chat.history(user.id, 80)]


@app.post("/api/speak")
async def api_speak(id: str = Form(...), user: User = Depends(current_user)):
    m = await get(messages, Message, id)
    if not m or m.user_id != user.id:
        raise HTTPException(404, "Message not found")
    url = await chat.ensure_audio(m)
    return {"url": url}


@app.post("/api/duty")
async def api_duty(on_duty: bool = Form(...), user: User = Depends(current_user)):
    if user.role != "responder":
        raise HTTPException(403, "Responders only")
    await update(users, user.id, {"on_duty": on_duty, "last_active_at": utcnow()})
    return {"ok": True, "on_duty": on_duty}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request, user: User = Depends(current_user)):
    body = await request.json()
    endpoint, keys = body.get("endpoint"), body.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(400, "bad subscription")
    await push_subs.update_one({"endpoint": endpoint}, {"$set": {"user_id": user.id, "p256dh": keys["p256dh"], "auth": keys["auth"]},
                                                        "$setOnInsert": {"created_at": utcnow()}}, upsert=True)
    return {"ok": True}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request, user: User = Depends(current_user)):
    body = await request.json()
    await push_subs.delete_one({"endpoint": body.get("endpoint"), "user_id": user.id})
    return {"ok": True}


@app.post("/api/push/test")
async def push_test(user: User = Depends(current_user)):
    return await push_to_users([user.id], {"title": "SafeRoad alerts are on", "body": f"Hi {user.first_name}, you will now hear from responders here.", "url": "/chat"})


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
