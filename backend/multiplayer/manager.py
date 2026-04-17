"""
Lobby matchmaking, timers, broadcasts, state transitions.
Thin orchestration layer — parsing/constants live in manager_helpers.py, match phases in manager_phases.py.

Concurrency mental model (read this before you "fix" a race):
- One asyncio.Lock wraps basically all mutating paths. Boring but predictable :)
- Phase work runs in asyncio Tasks (_cook_tasks / _upload_tasks / _vote_tasks). Rematch replaces a task —
  always cancel the old one in finally *only* if this task is still the registered one (see phases file).
- DB hits that could block use asyncio.to_thread or a tiny sync helper — never block the event loop for SQL.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from starlette.websockets import WebSocket

from .lobby import (
    LOBBY_RESULTS_TTL_S,
    MAX_LOBBY_PLAYERS,
    VOTING_COLLECT_S,
    Lobby,
    LobbyState,
    Player,
    beat_display_name,
)
from .manager_helpers import (
    BEAT_REACTION_KEYS,
    MP_CHAT_EMOJI_KEYS,
    MP_CHAT_STATES,
    coerce_bool,
    fetch_user_wins_sync,
    normalize_cook_duration_min,
    normalize_player_spices,
)
from .manager_phases import (
    all_players_finished_slideshow,
    cook_loop,
    finalize_results,
    required_voters,
)
from .manager_results import results_ws_payload_and_winner_user_ids
from .mp_chat_text import chat_cooldown_elapsed, normalize_and_validate_mp_chat_text

# How long we keep your seat after the tab drops (soft disconnect). Tests crank this way down.
MP_WS_GRACE_S = 120.0


class LobbyManager:
    def __init__(self, uploads_root: Path) -> None:
        self.uploads_root = uploads_root
        # Global lock — only for cross-lobby dict mutations (lobby create/delete,
        # player_lobby mapping, auth_* maps). Hold briefly, never during broadcasts/DB.
        self._lock = asyncio.Lock()
        # Per-lobby locks — all lobby-internal mutations (ready, cook, vote, chat, etc.)
        # use these so different lobbies never block each other.
        self._lobby_locks: dict[str, asyncio.Lock] = {}
        self.lobbies: dict[str, Lobby] = {}
        # player_id -> lobby_id so we don't scan every lobby on every message
        self.player_lobby: dict[str, str] = {}
        self.player_ws: dict[str, WebSocket] = {}
        self.auth_user_id: dict[str, int] = {}
        self.auth_username: dict[str, str] = {}
        # Phase timers: always cancel the old task when a phase restarts (rematch, etc.)
        self._cook_tasks: dict[str, asyncio.Task[None]] = {}
        self._upload_tasks: dict[str, asyncio.Task[None]] = {}
        self._vote_tasks: dict[str, asyncio.Task[None]] = {}
        # Tab went away but we might get resume_player_id soon — kill these on reconnect
        self._grace_tasks: dict[str, asyncio.Task[None]] = {}
        # Wall-clock deadline for menu countdown / GET pending (parallel to grace tasks)
        self._grace_deadline_ts: dict[str, float] = {}

    def _lobby_lock(self, lobby_id: str) -> asyncio.Lock:
        """Get or create a per-lobby lock. Safe to call without holding _lock."""
        lock = self._lobby_locks.get(lobby_id)
        if lock is None:
            lock = asyncio.Lock()
            self._lobby_locks[lobby_id] = lock
        return lock

    def register_auth_session(
        self, player_id: str, user_id: int, username: str
    ) -> None:
        self.auth_user_id[player_id] = user_id
        self.auth_username[player_id] = username

    def pop_auth_session(self, player_id: str) -> None:
        self.auth_user_id.pop(player_id, None)
        self.auth_username.pop(player_id, None)

    def _session_user(self, player_id: str) -> tuple[int, str] | None:
        uid = self.auth_user_id.get(player_id)
        un = self.auth_username.get(player_id)
        if uid is None or un is None:
            return None
        return uid, un

    @staticmethod
    def _lobby_has_user_id(lobby: Lobby, user_id: int) -> bool:
        return any(p.user_id == user_id for p in lobby.players.values())

    def player_id_for_user_in_lobby(self, lobby_id: str, user_id: int) -> str | None:
        lobby = self.lobbies.get(lobby_id)
        if not lobby:
            return None
        for pid, p in lobby.players.items():
            if p.user_id == user_id:
                return pid
        return None

    def verify_player_belongs_to_user(
        self, lobby_id: str, player_id: str, user_id: int
    ) -> bool:
        lobby = self.lobbies.get(lobby_id)
        if not lobby:
            return False
        p = lobby.players.get(player_id)
        return p is not None and p.user_id == user_id

    def get_lobby_kit_meta_for_user(
        self, lobby_id: str, user_id: int
    ) -> dict[str, Any] | None:
        """Return seed / spice for clients that rebuild the kit locally."""
        lobby = self.lobbies.get(lobby_id)
        if not lobby or lobby.seed is None:
            return None
        if self.player_id_for_user_in_lobby(lobby_id, user_id) is None:
            return None
        if lobby.state not in (
            LobbyState.COOKING,
            LobbyState.UPLOAD,
            LobbyState.VOTING,
            LobbyState.RESULTS,
        ):
            return None
        now = time.time()
        out: dict[str, Any] = {
            "seed": lobby.seed,
            "spice": lobby.spice,
            "match_state": lobby.state.value,
        }
        if lobby.state == LobbyState.COOKING and lobby.cook_deadline_ts is not None:
            out["cook_remaining_s"] = max(
                0, int(math.ceil(lobby.cook_deadline_ts - now))
            )
        if lobby.state == LobbyState.UPLOAD and lobby.upload_deadline_ts is not None:
            out["upload_deadline_ts"] = lobby.upload_deadline_ts
        if lobby.state == LobbyState.VOTING:
            beats: list[dict[str, Any]] = []
            for i, owner_id in enumerate(
                sorted(lobby.uploaded, key=lambda x: x), start=1
            ):
                nm = beat_display_name(lobby, owner_id, i)
                beats.append(
                    {
                        "player_id": owner_id,
                        "name": nm,
                        "url": f"/beats/{lobby_id}/{owner_id}",
                    }
                )
            out["beats"] = beats
            out["votes_unlock_at"] = lobby.votes_unlock_at
            if lobby.votes_unlock_at is not None:
                out["votes_close_at"] = float(lobby.votes_unlock_at) + float(
                    VOTING_COLLECT_S
                )
        if lobby.state == LobbyState.RESULTS:
            payload, _ = results_ws_payload_and_winner_user_ids(lobby, lobby_id)
            out["results"] = payload
        return out

    def get_match_sync_for_user(
        self, lobby_id: str, user_id: int
    ) -> dict[str, Any] | None:
        """HTTP recovery: same snapshot the socket would have pushed (refresh / flaky WS)."""
        lobby = self.lobbies.get(lobby_id)
        if not lobby:
            return None
        if self.player_id_for_user_in_lobby(lobby_id, user_id) is None:
            return None
        now = time.time()
        st = lobby.state
        out: dict[str, Any] = {
            "match_state": st.value,
            "lobby_id": lobby_id,
        }
        if st == LobbyState.COOKING and lobby.cook_deadline_ts is not None:
            out["cook_remaining_s"] = max(
                0, int(math.ceil(lobby.cook_deadline_ts - now))
            )
        if st == LobbyState.UPLOAD and lobby.upload_deadline_ts is not None:
            out["upload_deadline_ts"] = lobby.upload_deadline_ts
        if st == LobbyState.VOTING:
            beats: list[dict[str, Any]] = []
            for i, owner_id in enumerate(
                sorted(lobby.uploaded, key=lambda x: x), start=1
            ):
                nm = beat_display_name(lobby, owner_id, i)
                beats.append(
                    {
                        "player_id": owner_id,
                        "name": nm,
                        "url": f"/beats/{lobby_id}/{owner_id}",
                    }
                )
            out["beats"] = beats
            out["votes_unlock_at"] = lobby.votes_unlock_at
            if lobby.votes_unlock_at is not None:
                out["votes_close_at"] = float(lobby.votes_unlock_at) + float(
                    VOTING_COLLECT_S
                )
        if st == LobbyState.RESULTS:
            payload, _ = results_ws_payload_and_winner_user_ids(lobby, lobby_id)
            out["results"] = payload
        snap = lobby.lobby_snapshot()
        # Include full snapshot fields needed by clients (WS match_resync / HTTP match_sync).
        # drumkit carries seed for COOKING — omitting it breaks menu resume (MP_RESUME_SYNC).
        for key in (
            "player_count",
            "cook_finished",
            "uploaded",
            "slideshow_completed",
            "votes",
            "players",
            "host_id",
            "state",
            "spice",
            "cook_duration_min",
            "anonymous_voting",
            "is_public",
            "drumkit",
        ):
            if key in snap:
                out[key] = snap[key]
        self.apply_connection_fields_to_snap(out)
        return out

    def apply_connection_fields_to_snap(self, snap: dict[str, Any]) -> None:
        """Augment each player row with WebSocket + grace info for roster UI."""
        pl = snap.get("players")
        if not isinstance(pl, list):
            return
        for row in pl:
            if not isinstance(row, dict):
                continue
            pid = str(row.get("id", ""))
            if not pid:
                continue
            has_ws = self.player_ws.get(pid) is not None
            row["connected"] = has_ws
            gt = self._grace_tasks.get(pid)
            in_grace = gt is not None and not gt.done()
            if not has_ws and in_grace:
                row["grace_deadline_ts"] = self._grace_deadline_ts.get(pid)
            else:
                row["grace_deadline_ts"] = None

    def snapshot_for_broadcast(self, lobby_id: str) -> dict[str, Any] | None:
        lobby = self.lobbies.get(lobby_id)
        if not lobby:
            return None
        snap = lobby.lobby_snapshot()
        self.apply_connection_fields_to_snap(snap)
        return snap

    def pending_reconnect_for_user(self, user_id: int) -> dict[str, Any] | None:
        """Seat kept after soft WS drop; user has no socket but grace task is running."""
        now = time.time()
        for pid, uid in list(self.auth_user_id.items()):
            if uid != user_id:
                continue
            gt = self._grace_tasks.get(pid)
            if gt is None or gt.done():
                continue
            if self.player_ws.get(pid) is not None:
                continue
            lid = self.player_lobby.get(pid)
            if not lid:
                continue
            lobby = self.lobbies.get(lid)
            if not lobby or pid not in lobby.players:
                continue
            if lobby.state == LobbyState.RESULTS:
                continue
            until = self._grace_deadline_ts.get(pid)
            if until is None:
                until = now + MP_WS_GRACE_S
            remaining = max(0.0, until - now)
            return {
                "lobby_id": lid,
                "player_id": pid,
                "reconnect_until_ts": until,
                "seconds_remaining": int(math.ceil(remaining)),
            }
        return None

    async def abandon_reconnect_grace_for_user(self, user_id: int) -> None:
        """HTTP: user forfeits reconnect (e.g. dismiss) — full disconnect like grace expiry."""
        to_disconnect: list[str] = []
        for pid, uid in list(self.auth_user_id.items()):
            if uid != user_id:
                continue
            if self.player_ws.get(pid) is not None:
                continue
            gt = self._grace_tasks.get(pid)
            if gt is None or gt.done():
                continue
            to_disconnect.append(pid)
        for pid in to_disconnect:
            await self.disconnect(pid)

    def attach_ws(self, player_id: str, ws: WebSocket) -> None:
        self.player_ws[player_id] = ws

    def detach_ws(self, player_id: str) -> None:
        self.player_ws.pop(player_id, None)

    async def try_resume_player(
        self,
        user_id: int,
        username: str,
        resume_player_id: str,
        websocket: WebSocket,
    ) -> tuple[bool, str | None]:
        """Reconnect with ?resume_player_id= — same seat, new socket. Closes the old ws if it still hung around."""
        async with self._lock:
            uid = self.auth_user_id.get(resume_player_id)
            un = self.auth_username.get(resume_player_id)
            if uid is None or uid != user_id:
                return False, "wrong_user"
            if un != username:
                return False, "wrong_user"
            lid = self.player_lobby.get(resume_player_id)
            lobby = self.lobbies.get(lid) if lid else None
            if not lobby or resume_player_id not in lobby.players:
                return False, "not_in_lobby"
            prev = self.player_ws.get(resume_player_id)
            self.attach_ws(resume_player_id, websocket)
        gt = self._grace_tasks.pop(resume_player_id, None)
        if gt:
            gt.cancel()
        self._grace_deadline_ts.pop(resume_player_id, None)
        if prev is not None and prev is not websocket:
            try:
                await prev.close()
            except Exception:
                pass
        lid_resume = self.player_lobby.get(resume_player_id)
        if lid_resume:
            snap = self.snapshot_for_broadcast(lid_resume)
            if snap:
                await self.broadcast(
                    lid_resume, {"type": "lobby_update", "lobby": snap}
                )
        return True, None

    async def send_match_resync_to_player(self, player_id: str) -> None:
        """After resume — shove the same JSON shape as GET /api/lobby/.../match_sync so the client can catch up."""
        uid = self.auth_user_id.get(player_id)
        if uid is None:
            return
        lid = self.player_lobby.get(player_id)
        if not lid:
            return
        sync = self.get_match_sync_for_user(lid, uid)
        if not sync:
            return
        await self.send_to(player_id, {"type": "match_resync", **sync})

    async def detach_connection(self, player_id: str, ws: WebSocket) -> None:
        """Socket closed: soft drop (keep seat) and start grace timer — same idea as Discord voice idle.

        Pre-game (waiting / lobby / generating): treat like an intentional leave — full disconnect so
        players can browse lobbies without a reconnect window.

        Results: match is over — same as pre-game (no grace / no resume window).
        """
        if self.player_ws.get(player_id) is not ws:
            return
        self.detach_ws(player_id)

        pre_game_drop = False
        async with self._lock:
            lid = self.player_lobby.get(player_id)
            lobby = self.lobbies.get(lid) if lid else None
            if lobby is not None and player_id in lobby.players:
                pre_game_drop = lobby.state in (
                    LobbyState.WAITING,
                    LobbyState.LOBBY,
                    LobbyState.GENERATING,
                    LobbyState.RESULTS,
                )

        if pre_game_drop:
            await self.disconnect(player_id)
            return

        async def _grace() -> None:
            await asyncio.sleep(MP_WS_GRACE_S)
            if self.player_ws.get(player_id) is not None:
                return
            await self.disconnect(player_id)

        old = self._grace_tasks.pop(player_id, None)
        if old:
            old.cancel()
        self._grace_deadline_ts.pop(player_id, None)
        self._grace_tasks[player_id] = asyncio.create_task(_grace())
        self._grace_deadline_ts[player_id] = time.time() + MP_WS_GRACE_S

        lid_dc = self.player_lobby.get(player_id)
        dc_name = ""
        if lid_dc:
            lobby_dc = self.lobbies.get(lid_dc)
            if lobby_dc and player_id in lobby_dc.players:
                dc_name = lobby_dc.players[player_id].name
            await self.broadcast(
                lid_dc,
                {
                    "type": "player_disconnected",
                    "player_id": player_id,
                    "name": dc_name,
                },
            )
            snap_dc = self.snapshot_for_broadcast(lid_dc)
            if snap_dc:
                await self.broadcast(lid_dc, {"type": "lobby_update", "lobby": snap_dc})

    async def send_to_raw(self, player_id: str, raw: str) -> None:
        """Send a pre-serialized JSON string. Use when message is already json.dumps'd."""
        ws = self.player_ws.get(player_id)
        if not ws:
            return
        try:
            await ws.send_text(raw)
        except Exception:
            # Tab closed mid-send — nothing to do
            pass

    async def send_to(self, player_id: str, message: dict[str, Any]) -> None:
        await self.send_to_raw(player_id, json.dumps(message))

    async def broadcast(self, lobby_id: str, message: dict[str, Any]) -> None:
        lobby = self.lobbies.get(lobby_id)
        if not lobby:
            return
        raw = json.dumps(message)
        for pid in lobby.players:
            ws = self.player_ws.get(pid)
            if not ws:
                continue
            try:
                await ws.send_text(raw)
            except Exception:
                pass

    async def send_player_error(
        self,
        player_id: str,
        message: str,
        *,
        error_code: str | None = None,
    ) -> str:
        """type: error with a short ref for Discord bugs."""
        ref = secrets.token_hex(4).upper()
        payload: dict[str, Any] = {
            "type": "error",
            "message": message,
            "error_ref": ref,
        }
        if error_code is not None:
            payload["error_code"] = error_code
        await self.send_to(player_id, payload)
        return ref

    @staticmethod
    def _normalize_lobby_code(code: str) -> str:
        s = (
            (code or "")
            .strip()
            .upper()
            .replace(" ", "")
            .replace("-", "")
            .replace("_", "")
        )
        return s

    def _resolve_lobby_key(self, code: str) -> str | None:
        raw = self._normalize_lobby_code(code)
        if not raw:
            return None
        if raw in self.lobbies:
            return raw
        for k in self.lobbies:
            if self._normalize_lobby_code(k) == raw:
                return k
        return None

    def _new_lobby_id(self) -> str:
        # 6-char hex — retry on collision (basically never happens)
        for _ in range(20):
            lid = secrets.token_hex(3).upper()
            if lid not in self.lobbies:
                return lid
        return secrets.token_hex(4).upper()

    async def create_lobby(
        self, player_id: str, name: str, spices: list[float], is_public: bool
    ) -> None:
        if not spices:
            await self.send_player_error(
                player_id,
                "Select at least one heat level (0.25, 0.5, 0.85).",
            )
            return
        sess = self._session_user(player_id)
        if sess is None:
            await self.send_player_error(player_id, "Not authenticated.")
            return
        user_id, display_name = sess
        wins = await asyncio.to_thread(fetch_user_wins_sync, user_id)
        host_spice = sorted(spices)[0]
        async with self._lock:
            if player_id in self.player_lobby:
                await self.send_player_error(player_id, "Already in a lobby.")
                return
            lobby = Lobby(
                id=self._new_lobby_id(),
                spice=host_spice,
                is_public=is_public,
            )
            self.lobbies[lobby.id] = lobby
            lobby.players[player_id] = Player(
                id=player_id, name=display_name, user_id=user_id, wins=wins, ready=False
            )
            self.player_lobby[player_id] = lobby.id

        await self._emit_join_snapshots(lobby.id, player_id, display_name)

    async def join_lobby_by_code(self, player_id: str, name: str, code: str) -> None:
        sess = self._session_user(player_id)
        if sess is None:
            await self.send_player_error(player_id, "Not authenticated.")
            return
        user_id, display_name = sess
        wins = await asyncio.to_thread(fetch_user_wins_sync, user_id)
        async with self._lock:
            if player_id in self.player_lobby:
                await self.send_player_error(player_id, "Already in a lobby.")
                return
            key = self._resolve_lobby_key(code)
            if key is None:
                await self.send_player_error(
                    player_id,
                    "No lobby found for that code. Check the code and try again.",
                )
                return
            lobby = self.lobbies[key]
            if lobby.state != LobbyState.LOBBY:
                await self.send_player_error(
                    player_id,
                    "That match already started or ended.",
                )
                return
            if len(lobby.players) >= MAX_LOBBY_PLAYERS:
                await self.send_player_error(player_id, "Lobby is full.")
                return
            if self._lobby_has_user_id(lobby, user_id):
                await self.send_player_error(
                    player_id,
                    "You are already in this lobby.",
                )
                return
            lobby.players[player_id] = Player(
                id=player_id, name=display_name, user_id=user_id, wins=wins, ready=False
            )
            self.player_lobby[player_id] = lobby.id

        await self._emit_join_snapshots(lobby.id, player_id, display_name)

    async def join_lobby_public(self, player_id: str, name: str, lobby_id: str) -> None:
        sess = self._session_user(player_id)
        if sess is None:
            await self.send_player_error(player_id, "Not authenticated.")
            return
        user_id, display_name = sess
        wins = await asyncio.to_thread(fetch_user_wins_sync, user_id)
        async with self._lock:
            if player_id in self.player_lobby:
                await self.send_player_error(player_id, "Already in a lobby.")
                return
            key = self._resolve_lobby_key(lobby_id)
            if key is None:
                await self.send_player_error(
                    player_id,
                    "That lobby is no longer available.",
                )
                return
            lobby = self.lobbies[key]
            if not lobby.is_public:
                await self.send_player_error(
                    player_id,
                    "That lobby is code-only. Enter its code on Join with code.",
                )
                return
            if lobby.state != LobbyState.LOBBY:
                await self.send_player_error(
                    player_id,
                    "That match already started or ended.",
                )
                return
            if len(lobby.players) >= MAX_LOBBY_PLAYERS:
                await self.send_player_error(player_id, "Lobby is full.")
                return
            if self._lobby_has_user_id(lobby, user_id):
                await self.send_player_error(
                    player_id,
                    "You are already in this lobby.",
                )
                return
            lobby.players[player_id] = Player(
                id=player_id, name=display_name, user_id=user_id, wins=wins, ready=False
            )
            self.player_lobby[player_id] = lobby.id

        await self._emit_join_snapshots(lobby.id, player_id, display_name)

    async def public_lobby_list(self) -> list[dict[str, Any]]:
        async with self._lock:
            out: list[dict[str, Any]] = []
            for lid, L in self.lobbies.items():
                if L.state != LobbyState.LOBBY:
                    continue
                if not L.is_public:
                    continue
                if len(L.players) >= MAX_LOBBY_PLAYERS:
                    continue
                out.append(
                    {
                        "lobby_id": lid,
                        "spice": L.spice,
                        "player_count": len(L.players),
                        "max_players": MAX_LOBBY_PLAYERS,
                    }
                )
            return sorted(out, key=lambda x: x["lobby_id"])

    async def _emit_join_snapshots(
        self, lobby_id: str, player_id: str, name: str
    ) -> None:
        await self.broadcast(
            lobby_id,
            {
                "type": "player_join",
                "player": {"id": player_id, "name": name, "ready": False},
                "lobby_id": lobby_id,
            },
        )
        snap = self.snapshot_for_broadcast(lobby_id)
        if snap:
            await self.broadcast(lobby_id, {"type": "lobby_update", "lobby": snap})

    async def set_cook_duration(self, player_id: str, raw_minutes: Any) -> None:
        norm = normalize_cook_duration_min(raw_minutes)
        lobby_id = self.player_lobby.get(player_id)
        if not lobby_id:
            return
        async with self._lobby_lock(lobby_id):
            lobby = self.lobbies.get(lobby_id)
            if not lobby or lobby.state != LobbyState.LOBBY:
                await self.send_player_error(
                    player_id,
                    "You can only change duration before the match starts.",
                )
                return
            host_id = next(iter(lobby.players)) if lobby.players else None
            if host_id != player_id:
                await self.send_player_error(
                    player_id,
                    "Only the host can set cook duration.",
                )
                return
            if norm is None:
                await self.send_player_error(
                    player_id,
                    "Invalid duration. Use 5, 10, 15, 20, or 30 minutes.",
                )
                return
            lobby.cook_duration_min = norm

        snap = self.snapshot_for_broadcast(lobby_id)
        if snap:
            await self.broadcast(lobby_id, {"type": "lobby_update", "lobby": snap})

    async def set_anonymous_voting(self, player_id: str, raw_enabled: Any) -> None:
        enabled = coerce_bool(raw_enabled, default=False)
        lobby_id = self.player_lobby.get(player_id)
        if not lobby_id:
            return
        async with self._lobby_lock(lobby_id):
            lobby = self.lobbies.get(lobby_id)
            if not lobby or lobby.state != LobbyState.LOBBY:
                await self.send_player_error(
                    player_id,
                    "You can only change this before the match starts.",
                )
                return
            host_id = next(iter(lobby.players)) if lobby.players else None
            if host_id != player_id:
                await self.send_player_error(
                    player_id,
                    "Only the host can change anonymous voting.",
                )
                return
            lobby.anonymous_voting = enabled

        snap = self.snapshot_for_broadcast(lobby_id)
        if snap:
            await self.broadcast(lobby_id, {"type": "lobby_update", "lobby": snap})

    async def kick_player(self, host_id: str, raw_target: Any) -> None:
        """Host-only, pre-game: kicked_from_lobby then normal disconnect cleanup."""
        target_id = str(raw_target or "").strip()
        err: str | None = None
        err_code: str | None = None
        lobby_id_ok: str | None = None
        target_ok: str | None = None
        async with self._lock:
            if not target_id or target_id == host_id:
                err = "Can't kick yourself."
                err_code = "KICK_SELF"
            else:
                lobby_id = self.player_lobby.get(host_id)
                if not lobby_id:
                    err = "Not in a lobby."
                    err_code = "KICK_NOT_IN_LOBBY"
                else:
                    lobby = self.lobbies.get(lobby_id)
                    if not lobby or lobby.state != LobbyState.LOBBY:
                        err = "You can only kick players before the match starts."
                        err_code = "KICK_BAD_STATE"
                    else:
                        expected_host = (
                            next(iter(lobby.players)) if lobby.players else None
                        )
                        if expected_host != host_id:
                            err = "Only the host can kick."
                            err_code = "KICK_NOT_HOST"
                        elif target_id not in lobby.players:
                            err = "That player isn't in this lobby."
                            err_code = "KICK_NOT_FOUND"
                        elif self.player_lobby.get(target_id) != lobby_id:
                            err = "That player isn't in this lobby."
                            err_code = "KICK_NOT_FOUND"
                        else:
                            lobby_id_ok = lobby_id
                            target_ok = target_id
        if err:
            await self.send_player_error(host_id, err, error_code=err_code)
            return
        assert lobby_id_ok is not None and target_ok is not None
        await self.send_to(target_ok, {"type": "kicked_from_lobby"})
        await self.disconnect(target_ok)

    async def player_cook_finished(self, player_id: str) -> None:
        lobby_id = self.player_lobby.get(player_id)
        if not lobby_id:
            return
        async with self._lobby_lock(lobby_id):
            lobby = self.lobbies.get(lobby_id)
            if not lobby or lobby.state != LobbyState.COOKING:
                return
            if player_id not in lobby.players:
                return
            if player_id in lobby.cook_finished:
                return
            lobby.cook_finished.add(player_id)
            finished = sorted(lobby.cook_finished)
            n_players = len(lobby.players)

        await self.broadcast(
            lobby_id,
            {
                "type": "cook_finished_update",
                "finished_player_ids": finished,
                "player_count": n_players,
            },
        )

    async def player_ready(self, player_id: str) -> None:
        lobby_id = self.player_lobby.get(player_id)
        if not lobby_id:
            return
        async with self._lobby_lock(lobby_id):
            lobby = self.lobbies.get(lobby_id)
            if not lobby or lobby.state != LobbyState.LOBBY:
                return
            p = lobby.players.get(player_id)
            if not p:
                return
            p.ready = True

        await self.broadcast(
            lobby_id,
            {"type": "player_ready", "player_id": player_id, "ready": True},
        )
        snap = self.snapshot_for_broadcast(lobby_id)
        if snap:
            await self.broadcast(lobby_id, {"type": "lobby_update", "lobby": snap})
        await self._try_start_game(lobby_id)

    async def _try_start_game(self, lobby_id: str) -> None:
        async with self._lock:
            lobby = self.lobbies.get(lobby_id)
            if not lobby or lobby.state != LobbyState.LOBBY:
                return
            if len(lobby.players) < 2:
                return
            if not all(p.ready for p in lobby.players.values()):
                return
            seed = random.randint(0, 2**31 - 1)
            lobby.seed = seed
            lobby.sounds = None
            lobby.state = LobbyState.COOKING
            spice = lobby.spice
            lobby.cook_finished.clear()
            lobby.uploaded.clear()
            lobby.votes.clear()
            lobby.votes_unlock_at = None
            cook_min = lobby.cook_duration_min

        snap = self.snapshot_for_broadcast(lobby_id)
        if snap:
            await self.broadcast(lobby_id, {"type": "lobby_update", "lobby": snap})
        await self.broadcast(
            lobby_id,
            {
                "type": "start_game",
                "lobby_id": lobby_id,
                "seed": seed,
                "spice": spice,
                "cook_duration_min": cook_min,
            },
        )

        if lobby_id in self._cook_tasks:
            self._cook_tasks[lobby_id].cancel()
        self._cook_tasks[lobby_id] = asyncio.create_task(cook_loop(self, lobby_id))

    async def record_upload(self, lobby_id: str, player_id: str) -> None:
        async with self._lobby_lock(lobby_id):
            lobby = self.lobbies.get(lobby_id)
            if not lobby or lobby.state != LobbyState.UPLOAD:
                return
            if player_id not in lobby.players:
                return
            lobby.uploaded.add(player_id)
            snap = lobby.lobby_snapshot()
        await self.broadcast(
            lobby_id,
            {"type": "beat_uploaded", "player_id": player_id},
        )
        await self.broadcast(lobby_id, {"type": "lobby_update", "lobby": snap})

    async def slideshow_complete(self, player_id: str) -> None:
        """Slideshow done — can vote early if everyone skipped ahead."""
        lobby_id = self.player_lobby.get(player_id)
        if not lobby_id:
            return
        bump: tuple[str, float] | None = None
        async with self._lobby_lock(lobby_id):
            lobby = self.lobbies.get(lobby_id)
            if not lobby or lobby.state != LobbyState.VOTING:
                return
            if player_id not in lobby.players:
                return
            lobby.slideshow_completed.add(player_id)
            if (
                all_players_finished_slideshow(lobby)
                and lobby.votes_unlock_at is not None
                and time.time() < lobby.votes_unlock_at
            ):
                now = time.time()
                lobby.votes_unlock_at = now
                bump = (lobby_id, now)
        if bump is not None:
            lid, ts = bump
            await self.broadcast(
                lid,
                {
                    "type": "votes_timing",
                    "lobby_id": lid,
                    "votes_unlock_at": ts,
                    "votes_close_at": ts + float(VOTING_COLLECT_S),
                },
            )

    async def mp_chat(self, player_id: str, data: dict[str, Any]) -> None:
        text_raw = data.get("text")
        emoji_raw = data.get("emoji")
        text_nonempty = text_raw is not None and str(text_raw).strip() != ""
        emoji_nonempty = emoji_raw is not None and str(emoji_raw).strip() != ""

        if text_nonempty and emoji_nonempty:
            await self.send_player_error(
                player_id, "Send either text or emoji, not both."
            )
            return
        if not text_nonempty and not emoji_nonempty:
            await self.send_player_error(player_id, "Empty message.")
            return

        now = time.time()
        lobby_id = self.player_lobby.get(player_id)
        if not lobby_id:
            return
        async with self._lobby_lock(lobby_id):
            lobby = self.lobbies.get(lobby_id)
            if not lobby or lobby.state not in MP_CHAT_STATES:
                await self.send_player_error(
                    player_id, "Chat is not available right now."
                )
                return
            pl = lobby.players.get(player_id)
            if not pl:
                return
            last = lobby.chat_last_sent.get(player_id)
            if not chat_cooldown_elapsed(last, now):
                await self.send_player_error(
                    player_id,
                    "Wait before sending another message.",
                    error_code="MP_CHAT_COOLDOWN",
                )
                return

            if text_nonempty:
                normalized, err = normalize_and_validate_mp_chat_text(text_raw)
                if err or normalized is None:
                    await self.send_player_error(player_id, err or "Invalid message.")
                    return
                lobby.chat_last_sent[player_id] = now
                name = pl.name
                payload: dict[str, Any] = {
                    "type": "mp_chat",
                    "player_id": player_id,
                    "name": name,
                    "text": normalized,
                    "ts": int(now * 1000),
                }
            else:
                key = str(emoji_raw).strip()
                if key not in MP_CHAT_EMOJI_KEYS:
                    await self.send_player_error(player_id, "Invalid emoji.")
                    return
                lobby.chat_last_sent[player_id] = now
                name = pl.name
                payload = {
                    "type": "mp_chat",
                    "player_id": player_id,
                    "name": name,
                    "emoji": key,
                    "ts": int(now * 1000),
                }

        await self.broadcast(lobby_id, payload)

    async def lobby_emoji(self, player_id: str, emoji_key: str) -> None:
        await self.mp_chat(player_id, {"emoji": emoji_key})

    async def beat_reaction(
        self, player_id: str, target_player_id: str, reaction: str
    ) -> None:
        r = str(reaction).strip()
        if r not in BEAT_REACTION_KEYS:
            await self.send_player_error(player_id, "Invalid reaction.")
            return
        tid = str(target_player_id).strip()
        if not tid:
            await self.send_player_error(player_id, "Missing beat target.")
            return
        lobby_id = self.player_lobby.get(player_id)
        if not lobby_id:
            return
        async with self._lobby_lock(lobby_id):
            lobby = self.lobbies.get(lobby_id)
            if not lobby or lobby.state != LobbyState.VOTING:
                await self.send_player_error(
                    player_id,
                    "Reactions are only available during the listening phase.",
                )
                return
            if player_id not in lobby.players or tid not in lobby.uploaded:
                await self.send_player_error(player_id, "Invalid reaction target.")
                return
            if tid == player_id:
                return
            pl = lobby.players.get(player_id)
            from_name = pl.name if pl else player_id
            if lobby.anonymous_voting:
                from_name = "Someone"
        await self.broadcast(
            lobby_id,
            {
                "type": "beat_reaction",
                "from_player_id": player_id,
                "from_name": from_name,
                "target_player_id": tid,
                "reaction": r,
            },
        )

    async def cast_vote(self, player_id: str, target_player_id: str) -> None:
        lobby_id = self.player_lobby.get(player_id)
        if not lobby_id:
            await self.send_player_error(player_id, "Not in a lobby.")
            return
        async with self._lobby_lock(lobby_id):
            lobby = self.lobbies.get(lobby_id)
            if not lobby or lobby.state != LobbyState.VOTING:
                await self.send_player_error(player_id, "Voting is not open.")
                return
            if lobby.votes_unlock_at and time.time() < lobby.votes_unlock_at:
                if player_id not in lobby.slideshow_completed:
                    await self.send_player_error(
                        player_id,
                        "Votes unlock after the slideshow.",
                    )
                    return
            if target_player_id == player_id:
                await self.send_player_error(
                    player_id,
                    "Cannot vote for yourself.",
                )
                return
            if target_player_id not in lobby.uploaded:
                await self.send_player_error(player_id, "Invalid vote target.")
                return
            beat_owners = set(lobby.uploaded)
            if not any(b != player_id for b in beat_owners):
                await self.send_player_error(
                    player_id,
                    "No valid vote target for you.",
                )
                return
            lobby.votes[player_id] = target_player_id

        await self.broadcast(
            lobby_id,
            {
                "type": "vote_cast",
                "voter_id": player_id,
                "target_player_id": target_player_id,
            },
        )

        snap_after: dict[str, Any] | None = None
        async with self._lobby_lock(lobby_id):
            lobby_snap = self.lobbies.get(lobby_id)
            if lobby_snap:
                snap_after = lobby_snap.lobby_snapshot()
        if snap_after is not None:
            self.apply_connection_fields_to_snap(snap_after)
            await self.broadcast(
                lobby_id, {"type": "lobby_update", "lobby": snap_after}
            )

        await self._maybe_finalize_voting(lobby_id)

    async def _maybe_finalize_voting(self, lobby_id: str) -> bool:
        """If every required voter has cast, cancel the collector and finalize. Used by cast_vote and disconnect."""
        should_finalize = False
        async with self._lobby_lock(lobby_id):
            lobby = self.lobbies.get(lobby_id)
            if not lobby or lobby.state != LobbyState.VOTING:
                return False
            beat_owners = set(lobby.uploaded)
            req = required_voters(lobby, beat_owners)
            if req.issubset(lobby.votes.keys()):
                if lobby_id in self._vote_tasks:
                    self._vote_tasks[lobby_id].cancel()
                should_finalize = True
        if should_finalize:
            await finalize_results(self, lobby_id)
            return True
        return False

    def _delete_lobby_upload_dir_only(self, lobby_id: str) -> None:
        t = self._cook_tasks.pop(lobby_id, None)
        if t:
            t.cancel()
        t = self._upload_tasks.pop(lobby_id, None)
        if t:
            t.cancel()
        t = self._vote_tasks.pop(lobby_id, None)
        if t:
            t.cancel()
        d = self.uploads_root / lobby_id
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)

    async def _rematch_to_new_lobby(self, old_id: str) -> None:
        new_id: str | None = None
        async with self._lock:
            old = self.lobbies.get(old_id)
            if not old or old.state != LobbyState.RESULTS:
                return
            if set(old.players.keys()) != old.rematch_pending:
                return
            new_id = self._new_lobby_id()
            new_lobby = Lobby(
                id=new_id,
                spice=old.spice,
                is_public=old.is_public,
                cook_duration_min=old.cook_duration_min,
                anonymous_voting=old.anonymous_voting,
            )
            for pid, p in old.players.items():
                new_lobby.players[pid] = Player(
                    id=p.id,
                    name=p.name,
                    user_id=p.user_id,
                    wins=p.wins,
                    ready=False,
                )
                self.player_lobby[pid] = new_id
            self.lobbies[new_id] = new_lobby
            del self.lobbies[old_id]

        self._delete_lobby_upload_dir_only(old_id)
        if new_id is not None:
            new_snap = self.snapshot_for_broadcast(new_id)
            if new_snap:
                await self.broadcast(
                    new_id, {"type": "lobby_update", "lobby": new_snap}
                )

    async def rematch_vote(self, player_id: str) -> None:
        err: str | None = None
        lobby_id: str | None = None
        voted_ids: list[str] = []
        voter_name = ""
        voter_id = ""
        broadcast_vote = False
        all_voted = False
        async with self._lock:
            lid = self.player_lobby.get(player_id)
            if not lid:
                err = "Not in a lobby."
            else:
                lobby = self.lobbies.get(lid)
                if not lobby or lobby.state != LobbyState.RESULTS:
                    err = "Rematch is only available on the results screen."
                elif player_id not in lobby.players:
                    pass
                elif player_id in lobby.rematch_pending:
                    pass
                else:
                    lobby.rematch_pending.add(player_id)
                    lobby.rematch_pending.intersection_update(set(lobby.players.keys()))
                    lobby_id = lid
                    voted_ids = list(lobby.rematch_pending)
                    voter_name = lobby.players[player_id].name
                    voter_id = player_id
                    broadcast_vote = True
                    cur = set(lobby.players.keys())
                    all_voted = len(cur) >= 2 and cur == lobby.rematch_pending
        if err:
            await self.send_player_error(player_id, err)
            return
        if not broadcast_vote:
            return
        assert lobby_id is not None
        await self.broadcast(
            lobby_id,
            {
                "type": "rematch_vote_update",
                "voted_player_ids": voted_ids,
                "voter_id": voter_id,
                "name": voter_name,
            },
        )
        if all_voted:
            await self._rematch_to_new_lobby(lobby_id)

    async def disconnect(self, player_id: str) -> None:
        gt = self._grace_tasks.pop(player_id, None)
        if gt:
            gt.cancel()
        self._grace_deadline_ts.pop(player_id, None)
        lobby_id: str | None = None
        left_count = 0
        left_name = ""
        state_after: LobbyState | None = None
        lobby: Lobby | None = None
        async with self._lock:
            lobby_id = self.player_lobby.pop(player_id, None)
            self.detach_ws(player_id)
            if lobby_id:
                lobby = self.lobbies.get(lobby_id)
                if lobby:
                    gone = lobby.players.pop(player_id, None)
                    if gone:
                        left_name = gone.name
                    lobby.rematch_pending.discard(player_id)
                    lobby.cook_finished.discard(player_id)
                    lobby.chat_last_sent.pop(player_id, None)
                    lobby.uploaded.discard(player_id)
                    lobby.votes.pop(player_id, None)
                    for vid in [
                        k for k, tgt in lobby.votes.items() if tgt == player_id
                    ]:
                        lobby.votes.pop(vid, None)
                    lobby.slideshow_completed.discard(player_id)
                    left_count = len(lobby.players)
                    state_after = lobby.state
        self.pop_auth_session(player_id)

        if not lobby_id or not lobby or state_after is None:
            return

        self._unlink_player_beat_upload(lobby_id, player_id)

        await self.broadcast(
            lobby_id,
            {
                "type": "player_leave",
                "player_id": player_id,
                "name": left_name,
            },
        )

        if state_after == LobbyState.VOTING:
            if await self._maybe_finalize_voting(lobby_id):
                state_after = LobbyState.RESULTS

        if state_after == LobbyState.RESULTS and left_count > 0:
            rematch_voted: list[str] = []
            do_rematch = False
            async with self._lock:
                lobby_res = self.lobbies.get(lobby_id)
                if lobby_res:
                    lobby_res.rematch_pending.intersection_update(
                        set(lobby_res.players.keys())
                    )
                    rematch_voted = sorted(lobby_res.rematch_pending)
                    cur_p = set(lobby_res.players.keys())
                    do_rematch = len(cur_p) >= 2 and cur_p == lobby_res.rematch_pending
            await self.broadcast(
                lobby_id,
                {
                    "type": "rematch_vote_update",
                    "voted_player_ids": rematch_voted,
                    "voter_id": "",
                    "name": "",
                },
            )
            if do_rematch:
                await self._rematch_to_new_lobby(lobby_id)

        if left_count == 0:
            await self._purge_lobby(lobby_id)
            return

        if left_count == 1 and state_after == LobbyState.RESULTS:
            await self._dissolve_results_solo_player(lobby_id)
            return

        if (
            left_count == 1
            and state_after != LobbyState.LOBBY
            and state_after != LobbyState.RESULTS
        ):
            await self._end_match_only_player_left(lobby_id)
            return

        if state_after == LobbyState.COOKING:
            finished_ids: list[str] = []
            n_cook = 0
            async with self._lock:
                lobby_c = self.lobbies.get(lobby_id)
                if lobby_c:
                    finished_ids = sorted(lobby_c.cook_finished)
                    n_cook = len(lobby_c.players)
            await self.broadcast(
                lobby_id,
                {
                    "type": "cook_finished_update",
                    "finished_player_ids": finished_ids,
                    "player_count": n_cook,
                },
            )

        snap = self.snapshot_for_broadcast(lobby_id)
        if snap is not None:
            await self.broadcast(lobby_id, {"type": "lobby_update", "lobby": snap})

        if state_after == LobbyState.LOBBY:
            await self._try_start_game(lobby_id)

    async def _dissolve_lobby(self, lobby_id: str) -> None:
        async with self._lock:
            lobby = self.lobbies.pop(lobby_id, None)
            if not lobby:
                return
            pids = list(lobby.players.keys())
            for pid in pids:
                self.player_lobby.pop(pid, None)

        for pid in pids:
            await self.send_to(
                pid,
                {"type": "lobby_dissolved", "reason": "not_enough_players"},
            )

        await self._purge_lobby(lobby_id, remove_dir=True)

    async def _dissolve_results_solo_player(self, lobby_id: str) -> None:
        async with self._lock:
            lobby = self.lobbies.pop(lobby_id, None)
            if not lobby:
                return
            pids = list(lobby.players.keys())
            for pid in pids:
                self.player_lobby.pop(pid, None)

        for pid in pids:
            await self.send_to(
                pid,
                {"type": "lobby_dissolved", "reason": "not_enough_players"},
            )

        await self._purge_lobby(lobby_id, remove_dir=True)

    async def _end_match_only_player_left(self, lobby_id: str) -> None:
        async with self._lock:
            lobby = self.lobbies.pop(lobby_id, None)
            if not lobby:
                return
            pids = list(lobby.players.keys())
            for pid in pids:
                self.player_lobby.pop(pid, None)

        for pid in pids:
            await self.send_to(
                pid,
                {"type": "lobby_dissolved", "reason": "only_player_left"},
            )

        await self._purge_lobby(lobby_id, remove_dir=True)

    async def _purge_lobby(self, lobby_id: str, remove_dir: bool = True) -> None:
        t = self._cook_tasks.pop(lobby_id, None)
        if t:
            t.cancel()
        t = self._upload_tasks.pop(lobby_id, None)
        if t:
            t.cancel()
        t = self._vote_tasks.pop(lobby_id, None)
        if t:
            t.cancel()
        if remove_dir:
            d = self.uploads_root / lobby_id
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
        old: Lobby | None = None
        async with self._lock:
            old = self.lobbies.pop(lobby_id, None)
            if old:
                for pid in old.players:
                    self.player_lobby.pop(pid, None)
        # Clean up per-lobby lock to prevent memory leak
        self._lobby_locks.pop(lobby_id, None)
        if old:
            for pid in old.players:
                self.pop_auth_session(pid)

    def _unlink_player_beat_upload(self, lobby_id: str, player_id: str) -> None:
        base = self.uploads_root / lobby_id
        if not base.is_dir():
            return
        for ext in (".ogg", ".OGG", ".wav", ".mp3", ".WAV", ".MP3"):
            p = base / f"{player_id}{ext}"
            if p.is_file():
                try:
                    p.unlink()
                except OSError:
                    pass

    def beat_file_path(self, lobby_id: str, owner_id: str) -> Path | None:
        lobby = self.lobbies.get(lobby_id)
        if not lobby or owner_id not in lobby.uploaded:
            return None
        base = self.uploads_root / lobby_id
        for ext in (".ogg", ".OGG", ".wav", ".mp3", ".WAV", ".MP3"):
            p = base / f"{owner_id}{ext}"
            if p.is_file():
                return p
        return None

    def can_access_beat(self, lobby_id: str, requester_id: str) -> bool:
        lobby = self.lobbies.get(lobby_id)
        if not lobby:
            return False
        return requester_id in lobby.players

    async def cleanup_stale(self) -> None:
        # Garbage collector for abandoned results screens — runs on a timer from main.py
        now = time.time()
        async with self._lock:
            to_drop: list[str] = []
            for lid, L in self.lobbies.items():
                if L.state == LobbyState.RESULTS and L.results_at:
                    if now - L.results_at > LOBBY_RESULTS_TTL_S:
                        to_drop.append(lid)
        for lid in to_drop:
            await self._purge_lobby(lid, remove_dir=True)

    async def handle_leave_lobby(
        self, player_id: str, websocket: WebSocket | None
    ) -> bool:
        """Pre-game: hard disconnect. In-match: soft detach (grace) — same as losing the socket."""
        lid = self.player_lobby.get(player_id)
        lobby = self.lobbies.get(lid) if lid else None
        pre_game = False
        if lobby is not None and player_id in lobby.players:
            pre_game = lobby.state in (
                LobbyState.WAITING,
                LobbyState.LOBBY,
                LobbyState.GENERATING,
            )
        if pre_game or lobby is None:
            await self.disconnect(player_id)
            return True
        if websocket is None:
            await self.disconnect(player_id)
            return True
        await self.detach_connection(player_id, websocket)
        return True

    async def handle_message(
        self, player_id: str, data: dict[str, Any], websocket: WebSocket | None = None
    ) -> bool:
        # WebSocket fan-in — one if/elif chain. Old clients still send weird stuff; stay forgiving.
        t = data.get("type")
        if t == "create_lobby":
            spices = normalize_player_spices(data)
            if spices is None:
                await self.send_player_error(
                    player_id,
                    "Invalid spices. Send spices: [0.25, 0.5, ...].",
                )
                return False
            is_public = coerce_bool(data.get("is_public"), default=True)
            await self.create_lobby(
                player_id, str(data.get("name", "")), spices, is_public
            )
        elif t == "join_lobby":
            name = str(data.get("name", ""))
            code = data.get("lobby_code")
            lid = data.get("lobby_id")
            if code is not None and str(code).strip() != "":
                await self.join_lobby_by_code(player_id, name, str(code))
            elif lid is not None and str(lid).strip() != "":
                await self.join_lobby_public(player_id, name, str(lid))
            else:
                await self.send_player_error(
                    player_id,
                    "Send lobby_id (public list) or lobby_code.",
                )
        elif t == "player_join":
            code = data.get("lobby_code") or data.get("lobby_id")
            if code is not None and str(code).strip() != "":
                await self.join_lobby_by_code(
                    player_id, str(data.get("name", "")), str(code)
                )
            else:
                await self.send_player_error(
                    player_id,
                    "Use create_lobby or join_lobby with lobby_id / lobby_code.",
                )
        elif t == "leave_lobby":
            return await self.handle_leave_lobby(player_id, websocket)
        elif t == "player_ready":
            await self.player_ready(player_id)
        elif t == "kick_player":
            await self.kick_player(player_id, data.get("target_player_id"))
        elif t == "set_cook_duration":
            await self.set_cook_duration(player_id, data.get("minutes"))
        elif t == "set_anonymous_voting":
            await self.set_anonymous_voting(player_id, data.get("enabled"))
        elif t == "cook_finished":
            await self.player_cook_finished(player_id)
        elif t == "rematch_vote":
            await self.rematch_vote(player_id)
        elif t == "vote_cast":
            await self.cast_vote(player_id, str(data.get("target_player_id", "")))
        elif t == "slideshow_complete":
            await self.slideshow_complete(player_id)
        elif t == "mp_chat":
            await self.mp_chat(player_id, data)
        elif t == "lobby_emoji":
            await self.lobby_emoji(player_id, str(data.get("emoji", "")))
        elif t == "beat_reaction":
            await self.beat_reaction(
                player_id,
                str(data.get("target_player_id", "")),
                str(data.get("reaction", "")),
            )
        else:
            label = repr(t) if t is not None else "None"
            if len(label) > 80:
                label = label[:77] + "..."
            await self.send_player_error(
                player_id,
                f"Unknown message type: {label}",
                error_code="UNKNOWN_MESSAGE_TYPE",
            )
        return False
