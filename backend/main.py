"""
Beat Battle — Solo ``/generate``, multiplayer WebSocket, beat upload, static frontend.

Run from project root: ``uvicorn backend.main:app --reload --port 8000``
Then open http://127.0.0.1:8000/

Multiplayer WebSocket (``/ws``) behind a reverse proxy: idle proxies often drop
connections near 60s. Prefer native WS ping from uvicorn, e.g.
``uvicorn backend.main:app --host 0.0.0.0 --port 8000 --ws-ping-interval 25 --ws-ping-timeout 120``.
For nginx, raise ``proxy_read_timeout`` / ``proxy_send_timeout`` above the ping interval
(see ``proxy_http_version 1.1`` and ``Upgrade`` / ``Connection`` headers for WebSockets).
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, ORJSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import create_ws_ticket, get_current_user, login_user, register_user
from .beat_upload_trim import trim_beat_upload_to_ogg
from .database import get_db, init_db
from .generator import generate_kit_light
from .kit_manifest import get_kit_manifest_cached
from .kit_payload import encode_paths_to_sounds
from .models import SiteStats, Supporter, User
from .multiplayer import LobbyManager
from .multiplayer.lobby import LobbyState
from .multiplayer.ws import router as ws_router
from .multiplayer.ws_rate_limit import SlidingWindowRateLimiter
from .rank import rank_for_wins, rank_index_for_wins, rank_public_dict
from .schemas import (
    LeaderboardEntry,
    LoginRequest,
    MeResponse,
    RankInfo,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPLOADS_ROOT = _PROJECT_ROOT / "uploads"
FRONTEND_ROOT = _PROJECT_ROOT / "frontend"
_DATASET_ROOT = _PROJECT_ROOT / "dataset"


def _static_asset_build() -> str:
    """URL-safe token; bump via env BEAT_BATTLE_STATIC_BUILD on deploy."""
    raw = os.environ.get("BEAT_BATTLE_STATIC_BUILD", "1").strip() or "1"
    if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", raw):
        return raw
    return "1"


STATIC_ASSET_BUILD = _static_asset_build()


def _index_html_response() -> HTMLResponse:
    path = FRONTEND_ROOT / "index.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="index.html not found.")
    body = path.read_text(encoding="utf-8").replace(
        "__STATIC_BUILD__", STATIC_ASSET_BUILD
    )
    return HTMLResponse(content=body, headers={"Cache-Control": "no-cache"})


MAX_BEAT_BYTES = 30 * 1024 * 1024

# Must match the stored username exactly (case-sensitive); do not compare with .lower().
_ALLOWED_DEV_STATS_USERS = frozenset({"psyalysis", "polystalgia"})


def _require_dev_stats_user(user: User) -> None:
    if user.username not in _ALLOWED_DEV_STATS_USERS:
        raise HTTPException(status_code=403, detail="Not allowed.")


def _normalize_supporter_name(raw: str) -> str:
    s = raw.strip().lower()
    if not s:
        raise HTTPException(status_code=400, detail="Name required.")
    if len(s) > 64:
        raise HTTPException(status_code=400, detail="Name too long.")
    return s


def _increment_total_visits(db: Session) -> int:
    row = db.get(SiteStats, 1)
    if row is None:
        row = SiteStats(id=1, total_visits=0)
        db.add(row)
        db.flush()
    row.total_visits += 1
    db.commit()
    db.refresh(row)
    return int(row.total_visits)


def _get_total_visits(db: Session) -> int:
    row = db.get(SiteStats, 1)
    return int(row.total_visits) if row is not None else 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)
    manager = LobbyManager(UPLOADS_ROOT)
    app.state.manager = manager

    async def cleanup_loop() -> None:
        while True:
            await asyncio.sleep(120)
            await manager.cleanup_stale()

    task = asyncio.create_task(cleanup_loop())
    await asyncio.to_thread(get_kit_manifest_cached)
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Beat Battle", version="1.0.0", lifespan=lifespan)

_CORS_ORIGINS: list[str] = [
    "https://beat-battle.net",
    "https://www.beat-battle.net",
    "http://127.0.0.1:8000",
    "http://localhost:8000",
]
_extra_cors = os.environ.get("COOKUP_CORS_ORIGINS", "").strip()
if _extra_cors:
    _CORS_ORIGINS.extend(o.strip() for o in _extra_cors.split(",") if o.strip())

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _static_cache_control(path: str) -> str | None:
    """Long cache for static media; JS revalidates so module graph stays fresh."""
    if path.startswith("/js/"):
        return "public, max-age=0, must-revalidate"
    if path.startswith("/sfx/") or path.startswith("/media/"):
        return "public, max-age=86400"
    if path.endswith(
        (".css", ".woff2", ".svg", ".png", ".ico", ".webp", ".mp3", ".ogg")
    ):
        return "public, max-age=86400"
    if path == "/" or path.endswith(".html"):
        return "no-cache"
    return None


class StaticCacheControlMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.method != "GET":
            return response
        path = request.url.path
        if path.startswith("/api/"):
            return response
        cc = _static_cache_control(path)
        if cc:
            response.headers["Cache-Control"] = cc
        return response


app.add_middleware(StaticCacheControlMiddleware)

_LOGIN_LIMIT = SlidingWindowRateLimiter(max_events=10, window_s=60.0)
_REGISTER_LIMIT = SlidingWindowRateLimiter(max_events=5, window_s=60.0)

app.include_router(ws_router)


@app.get("/api/kit-manifest", response_class=ORJSONResponse)
def get_kit_manifest() -> ORJSONResponse:
    """Sorted dataset media paths per stem; client uses with :func:`pick_index` parity."""
    return ORJSONResponse(
        get_kit_manifest_cached(),
        headers={"Cache-Control": "public, max-age=3600"},
    )


class GenerateRequest(BaseModel):
    """POST /generate JSON body."""

    spice: float = Field(default=0.3, ge=0.0, le=1.0)
    seed: int | None = None


def _solo_generate_sync(seed: int, spice: float) -> dict[str, Any]:
    """Solo kit: random samples only (no heavy DSP); CPU-bound; run in a thread pool."""
    tmp = tempfile.mkdtemp(prefix="solo_kit_")
    try:
        paths = generate_kit_light(seed=seed, spice=spice, output_dir=Path(tmp))
        sounds = encode_paths_to_sounds(paths)
        return {"seed": seed, "sounds": sounds}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/generate")
async def post_generate(body: GenerateRequest) -> dict[str, Any]:
    """Solo: synthesize a kit, return base64 OGG (dataset files as stored)."""
    seed = body.seed if body.seed is not None else random.randint(0, 2**31 - 1)
    return await asyncio.to_thread(_solo_generate_sync, seed, body.spice)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/stats/visit")
def post_stats_visit(db: Session = Depends(get_db)) -> dict[str, int]:
    """Increment once per frontend boot (full page load)."""
    total = _increment_total_visits(db)
    return {"total_visits": total}


@app.get("/api/dev/site-stats")
async def get_dev_site_stats(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Live counters for owner accounts only (JWT)."""
    _require_dev_stats_user(user)
    manager: LobbyManager = request.app.state.manager
    return {
        "players_online": len(manager.player_ws),
        "servers_open": len(manager.lobbies),
        "total_visits": _get_total_visits(db),
    }


@app.get("/api/supporters")
def get_supporters(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Public list of supporter display-name keys (lowercase); used for hearts in UI."""
    rows = db.query(Supporter).order_by(Supporter.name_key).all()
    return {"names": [r.name_key for r in rows]}


class SupporterAddBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


@app.post("/api/dev/supporters")
def post_dev_supporter(
    body: SupporterAddBody,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_dev_stats_user(user)
    key = _normalize_supporter_name(body.name)
    db.add(Supporter(name_key=key))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Already exists.")
    return {"ok": True, "name_key": key}


@app.delete("/api/dev/supporters")
def delete_dev_supporter(
    name: str = Query(..., min_length=1, max_length=64),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_dev_stats_user(user)
    key = _normalize_supporter_name(name)
    row = db.query(Supporter).filter(Supporter.name_key == key).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Not found.")
    db.delete(row)
    db.commit()
    return {"ok": True}


@app.post("/register", response_model=RegisterResponse)
def post_register(
    request: Request, body: RegisterRequest, db: Session = Depends(get_db)
) -> RegisterResponse:
    ip = (request.client.host if request.client else "unknown")
    if not _REGISTER_LIMIT.check(f"reg:{ip}"):
        raise HTTPException(status_code=429, detail="Too many registrations. Try again later.")
    return register_user(db, body)


@app.post("/login", response_model=TokenResponse)
def post_login(
    request: Request, body: LoginRequest, db: Session = Depends(get_db)
) -> TokenResponse:
    ip = (request.client.host if request.client else "unknown")
    if not _LOGIN_LIMIT.check(f"login:{ip}"):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
    return login_user(db, body)


@app.post("/api/ws-ticket")
def post_ws_ticket(user: User = Depends(get_current_user)) -> dict[str, str]:
    """Issue a short-lived, single-use ticket for WebSocket auth.

    The frontend calls this before opening a WS connection, then passes the
    ticket as ``?token=...`` instead of the long-lived JWT.  Even if the URL
    leaks in server/proxy logs the ticket is already expired (30 s) and
    single-use."""
    ticket = create_ws_ticket(user.id, user.username)
    return {"ticket": ticket}


def _me_response_from_user(user: User) -> MeResponse:
    r = rank_for_wins(user.wins)
    rank = (
        RankInfo(key=r["key"], abbrev=r["abbrev"], label=r["label"], color=r["color"])
        if r
        else None
    )
    return MeResponse(
        username=user.username,
        wins=user.wins,
        rank=rank,
        rank_index=rank_index_for_wins(user.wins),
    )


@app.get("/me", response_model=MeResponse)
def get_me(user: User = Depends(get_current_user)) -> MeResponse:
    return _me_response_from_user(user)


@app.get("/api/me/mp_reconnect_pending", response_class=ORJSONResponse)
def get_mp_reconnect_pending(
    request: Request,
    user: User = Depends(get_current_user),
) -> dict[str, Any] | None:
    """Soft-disconnect grace: same seat can resume until deadline (see MP_WS_GRACE_S)."""
    manager: LobbyManager = request.app.state.manager
    return manager.pending_reconnect_for_user(user.id)


@app.post("/api/me/mp_abandon_reconnect", response_class=ORJSONResponse)
async def post_mp_abandon_reconnect(
    request: Request,
    user: User = Depends(get_current_user),
) -> dict[str, bool]:
    """After Leave: clear any soft-disconnect grace so menu reconnect does not race."""
    manager: LobbyManager = request.app.state.manager
    await manager.abandon_reconnect_grace_for_user(user.id)
    return {"ok": True}


@app.get("/leaderboard", response_model=list[LeaderboardEntry])
def get_leaderboard(db: Session = Depends(get_db)) -> list[LeaderboardEntry]:
    rows = db.query(User).order_by(desc(User.wins), User.username).limit(50).all()
    out: list[LeaderboardEntry] = []
    for r in rows:
        pub = rank_public_dict(r.wins)
        rank = RankInfo(**pub) if pub else None
        out.append(
            LeaderboardEntry(
                username=r.username,
                wins=r.wins,
                rank=rank,
                rank_index=rank_index_for_wins(r.wins),
            )
        )
    return out


@app.get("/api/lobbies")
async def list_public_lobbies(request: Request) -> list[dict[str, Any]]:
    """Joinable public lobbies (pre-game, not full)."""
    manager: LobbyManager = request.app.state.manager
    return await manager.public_lobby_list()


@app.get(
    "/api/lobby/{lobby_id}/kit",
    response_class=ORJSONResponse,
)
async def get_lobby_kit(
    lobby_id: str,
    request: Request,
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Seed/spice for rebuilding the kit in the browser (no audio payload)."""
    manager: LobbyManager = request.app.state.manager
    meta = manager.get_lobby_kit_meta_for_user(lobby_id, user.id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Kit not available.")
    return meta


@app.get(
    "/api/lobby/{lobby_id}/match_sync",
    response_class=ORJSONResponse,
)
async def get_match_sync(
    lobby_id: str,
    request: Request,
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Current match phase for HTTP recovery when WebSocket messages are missed."""
    manager: LobbyManager = request.app.state.manager
    sync = manager.get_match_sync_for_user(lobby_id, user.id)
    if sync is None:
        raise HTTPException(status_code=404, detail="Match not available.")
    return sync


def _sniff_audio(buf: bytes) -> str | None:
    if len(buf) >= 12 and buf[:4] == b"RIFF" and buf[8:12] == b"WAVE":
        return ".wav"
    if len(buf) >= 3 and buf[:3] == b"ID3":
        return ".mp3"
    if len(buf) >= 2 and buf[0] == 0xFF and (buf[1] & 0xE0) == 0xE0:
        return ".mp3"
    return None


def _unlink_legacy_beat_formats(dest_dir: Path, player_id: str) -> None:
    """Remove WAV/MP3 artifacts so ``player_id.ogg`` is the sole canonical beat file."""
    for ext in (".wav", ".mp3", ".WAV", ".MP3"):
        p = dest_dir / f"{player_id}{ext}"
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass


@app.post("/upload/beat/{lobby_id}")
async def upload_beat(
    lobby_id: str,
    player_id: str = Form(),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
) -> dict[str, bool]:
    manager: LobbyManager = app.state.manager
    if not manager.verify_player_belongs_to_user(lobby_id, player_id, user.id):
        raise HTTPException(status_code=403, detail="Not in this lobby.")
    lobby = manager.lobbies.get(lobby_id)
    if not lobby or player_id not in lobby.players:
        raise HTTPException(status_code=403, detail="Not in this lobby.")
    if lobby.state != LobbyState.UPLOAD:
        raise HTTPException(status_code=400, detail="Upload phase is not active.")

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".mp3", ".wav"):
        raise HTTPException(status_code=400, detail="Only .mp3 or .wav allowed.")

    dest_dir = UPLOADS_ROOT / lobby_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{player_id}.ogg"
    part = dest_dir / f"{player_id}{suffix}.part"

    total = 0
    first_chunk: bytes | None = None
    try:
        with part.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 512)
                if not chunk:
                    break
                if first_chunk is None:
                    first_chunk = chunk[:64]
                total += len(chunk)
                if total > MAX_BEAT_BYTES:
                    raise HTTPException(
                        status_code=400, detail="File too large (max 30MB)."
                    )
                out.write(chunk)

        if total == 0:
            raise HTTPException(status_code=400, detail="Empty file.")

        sniffed = _sniff_audio(first_chunk or b"")
        if sniffed and sniffed != suffix:
            raise HTTPException(
                status_code=400, detail="File content does not match extension."
            )

        try:
            await asyncio.to_thread(
                trim_beat_upload_to_ogg,
                part,
                dest,
                source_suffix=suffix,
            )
        except HTTPException:
            raise
        except RuntimeError as e:
            dest.unlink(missing_ok=True)
            msg = str(e)
            if "ffmpeg not found" in msg:
                raise HTTPException(
                    status_code=503,
                    detail="Beat uploads require ffmpeg (libvorbis) on the server.",
                ) from e
            raise HTTPException(
                status_code=400, detail="Could not process audio file."
            ) from e
        except Exception as e:
            dest.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400, detail="Could not process audio file."
            ) from e
    except HTTPException:
        part.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)
        raise

    _unlink_legacy_beat_formats(dest_dir, player_id)

    await manager.record_upload(lobby_id, player_id)
    return {"ok": True}


@app.get("/beats/{lobby_id}/{owner_id}")
async def get_beat(
    lobby_id: str,
    owner_id: str,
    requester: str = Query(..., description="Connecting player's id"),
    user: User = Depends(get_current_user),
) -> FileResponse:
    manager: LobbyManager = app.state.manager
    expected = manager.player_id_for_user_in_lobby(lobby_id, user.id)
    if expected is None or expected != requester:
        raise HTTPException(status_code=403, detail="Not allowed.")
    if not manager.can_access_beat(lobby_id, requester):
        raise HTTPException(status_code=403, detail="Not allowed.")
    path = manager.beat_file_path(lobby_id, owner_id)
    if not path or not path.is_file():
        raise HTTPException(status_code=404, detail="Beat not found.")
    sfx = path.suffix.lower()
    if sfx == ".mp3":
        mt = "audio/mpeg"
    elif sfx == ".ogg":
        mt = "audio/ogg"
    else:
        mt = "audio/wav"
    return FileResponse(path, media_type=mt, filename=path.name)


if _DATASET_ROOT.is_dir():
    app.mount(
        "/media/dataset",
        StaticFiles(directory=str(_DATASET_ROOT)),
        name="dataset_media",
    )

if FRONTEND_ROOT.is_dir():

    @app.get("/")
    async def _serve_index() -> HTMLResponse:
        return _index_html_response()

    @app.get("/index.html")
    async def _serve_index_named() -> HTMLResponse:
        return _index_html_response()

    app.mount("/", StaticFiles(directory=str(FRONTEND_ROOT), html=True), name="site")
