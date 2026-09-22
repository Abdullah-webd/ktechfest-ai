from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeSerializer

from .config import settings
from .db import User, get_user, update, users, utcnow

COOKIE = "saferoad_session"
_serializer = URLSafeSerializer(settings.app_secret, salt="session")
OTP_TTL = timedelta(minutes=10)
OTP_COOLDOWN = timedelta(seconds=45)


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except ValueError:
        return False


def make_session_cookie(user_id: str) -> str:
    return _serializer.dumps({"uid": user_id})


def read_session_cookie(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return str(_serializer.loads(value)["uid"])
    except (BadSignature, KeyError, ValueError, TypeError):
        return None


async def optional_user(request: Request) -> Optional[User]:
    uid = read_session_cookie(request.cookies.get(COOKIE))
    return await get_user(uid) if uid else None


class NeedsVerification(Exception):
    pass


async def current_user(user: Optional[User] = Depends(optional_user)) -> User:
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    if not user.verified:
        raise NeedsVerification()
    return user


async def responder_user(user: User = Depends(current_user)) -> User:
    if user.role != "responder":
        raise HTTPException(status_code=403, detail="Responders only")
    return user


# ---------- one-time codes ----------
def _otp_hash(code: str, email: str) -> str:
    return hashlib.sha256(f"{settings.app_secret}:{email}:{code}".encode()).hexdigest()


async def issue_otp(user: User, purpose: str) -> Optional[str]:
    """Create a 6-digit code for the user. Returns the code, or None if inside the resend cooldown."""
    if user.otp_sent_at and user.otp_purpose == purpose and utcnow() - user.otp_sent_at < OTP_COOLDOWN:
        return None
    code = f"{secrets.randbelow(1_000_000):06d}"
    await update(users, user.id, {"otp_hash": _otp_hash(code, user.email), "otp_expires": utcnow() + OTP_TTL,
                                  "otp_purpose": purpose, "otp_sent_at": utcnow()})
    return code


async def check_otp(user: User, code: str, purpose: str) -> bool:
    if not user.otp_hash or user.otp_purpose != purpose or not user.otp_expires:
        return False
    if utcnow() > user.otp_expires:
        return False
    return secrets.compare_digest(user.otp_hash, _otp_hash(code.strip(), user.email))


async def clear_otp(user: User) -> None:
    await update(users, user.id, {"otp_hash": None, "otp_expires": None, "otp_purpose": None})
