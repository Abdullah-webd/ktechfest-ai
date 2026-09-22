from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeSerializer

from .config import settings
from .db import User, get_user

COOKIE = "saferoad_session"
_serializer = URLSafeSerializer(settings.app_secret, salt="session")


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


async def current_user(user: Optional[User] = Depends(optional_user)) -> User:
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    return user


async def responder_user(user: User = Depends(current_user)) -> User:
    if user.role != "responder":
        raise HTTPException(status_code=403, detail="Responders only")
    return user
