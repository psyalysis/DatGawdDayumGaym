"""
Password hashing, JWT, registration, login, and auth dependencies.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time as _time
from datetime import datetime, timedelta, timezone
from threading import Lock as _Lock
from typing import Annotated

import bcrypt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import func
from sqlalchemy.orm import Session

from .database import SessionLocal, get_db
from .models import User
from .schemas import LoginRequest, RegisterRequest, RegisterResponse, TokenResponse

SECRET_KEY = os.environ.get(
    "COOKUP_JWT_SECRET", "dev-insecure-change-me-cookup-jwt-secret"
)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 7

security = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# User cache — avoid a DB query on every authenticated request.
# JWT already carries (user_id, username). We cache the full User object for
# 30 s so win-count etc. stays fresh without hammering the DB.
# ---------------------------------------------------------------------------
_USER_CACHE_TTL_S = 30.0
_user_cache: dict[int, tuple[User, float]] = {}
_user_cache_lock = _Lock()


def _get_cached_user(user_id: int) -> User | None:
    """Return cached User if still fresh, else None."""
    with _user_cache_lock:
        entry = _user_cache.get(user_id)
    if entry is None:
        return None
    user, expires_at = entry
    if _time.time() > expires_at:
        return None
    return user


def _put_cached_user(user: User) -> None:
    with _user_cache_lock:
        _user_cache[user.id] = (user, _time.time() + _USER_CACHE_TTL_S)


def invalidate_user_cache(*user_ids: int) -> None:
    """Drop cache entries after a write (win increment, profile change, etc.)."""
    with _user_cache_lock:
        for uid in user_ids:
            _user_cache.pop(uid, None)


def _password_key_bytes(plain: str) -> bytes:
    """SHA-256 digest (32 bytes) — bcrypt input stays under 72-byte limit for any UTF-8 password."""
    return hashlib.sha256(plain.encode("utf-8")).digest()


def verify_password(plain: str, hashed: str) -> bool:
    h = hashed.encode("ascii")
    try:
        if bcrypt.checkpw(_password_key_bytes(plain), h):
            return True
    except ValueError:
        pass
    try:
        raw = plain.encode("utf-8")
        if len(raw) <= 72:
            return bcrypt.checkpw(raw, h)
    except ValueError:
        return False
    return False


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_password_key_bytes(password), bcrypt.gensalt()).decode(
        "ascii"
    )


def create_access_token(*, user_id: int, username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": str(user_id),
        "username": username,
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


def register_user(db: Session, body: RegisterRequest) -> RegisterResponse:
    existing = (
        db.query(User)
        .filter(func.lower(User.username) == body.username.lower())
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail="Username already taken.")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        wins=0,
    )
    db.add(user)
    db.commit()
    return RegisterResponse(message="Registration successful.")


def login_user(db: Session, body: LoginRequest) -> TokenResponse:
    user = (
        db.query(User)
        .filter(func.lower(User.username) == body.username.lower())
        .first()
    )
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    token = create_access_token(user_id=user.id, username=user.username)
    return TokenResponse(token=token, username=user.username)


def get_user_by_id(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def increment_wins_for_users(db: Session, user_ids: list[int]) -> None:
    seen: set[int] = set()
    for uid in user_ids:
        if uid in seen:
            continue
        seen.add(uid)
        u = db.get(User, uid)
        if u is not None:
            u.wins += 1
    db.commit()
    # Bust cache so subsequent /me or leaderboard reads see updated wins.
    invalidate_user_cache(*seen)


def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Not authenticated.")
    try:
        payload = decode_token(creds.credentials)
        uid = int(payload["sub"])
    except (JWTError, KeyError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    # Fast path: serve from cache (avoids a DB roundtrip on every request).
    cached = _get_cached_user(uid)
    if cached is not None:
        return cached
    user = get_user_by_id(db, uid)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found.")
    _put_cached_user(user)
    return user


def try_validate_ws_token(
    token: str | None,
) -> tuple[tuple[int, str], None] | tuple[None, str]:
    """
    Validate JWT and that the user still exists (for WebSocket connect).
    On failure returns ``(None, reason)`` with a stable reason code for logging — never log the token.
    """
    if not token or not str(token).strip():
        return None, "missing_token"
    try:
        payload = decode_token(str(token).strip())
        uid = int(payload["sub"])
        un = str(payload["username"])
    except (JWTError, KeyError, ValueError, TypeError):
        return None, "jwt_invalid"
    db = SessionLocal()
    try:
        user = db.get(User, uid)
        if user is None:
            return None, "user_not_found"
        if user.username != un:
            return None, "username_mismatch"
        _put_cached_user(user)
        return (uid, user.username), None
    finally:
        db.close()


def validate_ws_token(token: str | None) -> tuple[int, str] | None:
    """Validate JWT and that the user still exists (for WebSocket connect)."""
    ok, reason = try_validate_ws_token(token)
    if reason is not None:
        return None
    return ok


_ws_tickets: dict[str, tuple[int, str, float]] = {}
_ws_ticket_lock = _Lock()
_WS_TICKET_TTL_S = 30


def create_ws_ticket(user_id: int, username: str) -> str:
    """Issue a single-use token (30 s) for WebSocket auth."""
    ticket = secrets.token_urlsafe(32)
    now = _time.time()
    with _ws_ticket_lock:
        stale = [k for k, (_, _, exp) in _ws_tickets.items() if exp < now]
        for k in stale:
            del _ws_tickets[k]
        _ws_tickets[ticket] = (user_id, username, now + _WS_TICKET_TTL_S)
    return ticket


def redeem_ws_ticket(ticket: str) -> tuple[int, str] | None:
    """Pop and validate a WS ticket.  Returns ``(user_id, username)`` or ``None``."""
    with _ws_ticket_lock:
        entry = _ws_tickets.pop(ticket, None)
    if entry is None:
        return None
    user_id, username, expires_at = entry
    if _time.time() > expires_at:
        return None
    return (user_id, username)

