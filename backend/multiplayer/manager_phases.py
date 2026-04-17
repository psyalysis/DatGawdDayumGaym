"""
Timed match phases: cook countdown, upload window, slideshow → vote collect, then results.
Lives in its own file because the asyncio + lock dance is long and easy to break — read top to bottom!
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from ..auth import increment_wins_for_users
from ..database import SessionLocal
from .lobby import (
    COOK_DURATION_MIN_OPTIONS,
    COOK_DURATION_S,
    Lobby,
    LobbyState,
    SLIDESHOW_SEGMENT_S,
    UPLOAD_PHASE_S,
    VOTING_COLLECT_S,
    beat_display_name,
)
from .manager_results import results_ws_payload_and_winner_user_ids

if TYPE_CHECKING:
    from .manager import LobbyManager


def required_voters(lobby: Lobby, beat_owners: set[str]) -> set[str]:
    """If you're the only one with a beat, you don't *have* to vote for anyone — weird but true."""
    req: set[str] = set()
    for pid in lobby.players:
        if any(b != pid for b in beat_owners):
            req.add(pid)
    return req


def all_players_finished_slideshow(lobby: Lobby) -> bool:
    """Everyone tapped through the slideshow — we can unlock votes early."""
    if not lobby.players:
        return True
    return len(lobby.slideshow_completed) >= len(lobby.players)


async def cook_loop(manager: LobbyManager, lobby_id: str) -> None:
    duration_s = COOK_DURATION_S
    async with manager._lobby_lock(lobby_id):
        lobby = manager.lobbies.get(lobby_id)
        if not lobby:
            return
        duration_s = max(
            60,
            min(max(COOK_DURATION_MIN_OPTIONS) * 60, int(lobby.cook_duration_min) * 60),
        )
        lobby.cook_deadline_ts = time.time() + duration_s
        lobby.upload_deadline_ts = None

    early_all_done = False
    try:
        # Broadcast every SYNC_INTERVAL_S seconds (not every 1 s — client runs its own
        # local countdown from cook_deadline_ts; these are drift-correction syncs).
        SYNC_INTERVAL_S = 5
        for remaining in range(duration_s, -1, -1):
            # Broadcast on first tick, last tick, and every SYNC_INTERVAL_S seconds.
            if remaining == duration_s or remaining == 0 or remaining % SYNC_INTERVAL_S == 0:
                await manager.broadcast(
                    lobby_id,
                    {
                        "type": "timer_update",
                        "phase": "cooking",
                        "remaining_s": remaining,
                    },
                )
            async with manager._lobby_lock(lobby_id):
                lobby = manager.lobbies.get(lobby_id)
                if not lobby or lobby.state != LobbyState.COOKING:
                    return
                # Advance when everyone still connected has finished — don't block on soft-DC seats.
                connected = {
                    pid
                    for pid in lobby.players
                    if manager.player_ws.get(pid) is not None
                }
                if (
                    len(lobby.players) > 0
                    and connected
                    and connected.issubset(lobby.cook_finished)
                ):
                    early_all_done = True
                    break
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        return
    finally:
        # Rematch might have spawned a newer cook task — only the current one clears the slot
        me = asyncio.current_task()
        t = manager._cook_tasks.get(lobby_id)
        if t is me:
            manager._cook_tasks.pop(lobby_id, None)

    if early_all_done:
        await manager.broadcast(
            lobby_id,
            {
                "type": "timer_update",
                "phase": "cooking",
                "remaining_s": 0,
            },
        )

    upload_deadline_ts = time.time() + UPLOAD_PHASE_S
    async with manager._lobby_lock(lobby_id):
        lobby = manager.lobbies.get(lobby_id)
        if not lobby or lobby.state != LobbyState.COOKING:
            return
        lobby.state = LobbyState.UPLOAD
        lobby.cook_deadline_ts = None
        lobby.upload_deadline_ts = upload_deadline_ts

    await manager.broadcast(
        lobby_id,
        {
            "type": "upload_phase_start",
            "lobby_id": lobby_id,
            "upload_deadline_ts": upload_deadline_ts,
        },
    )

    if lobby_id in manager._upload_tasks:
        manager._upload_tasks[lobby_id].cancel()
    manager._upload_tasks[lobby_id] = asyncio.create_task(
        upload_phase(manager, lobby_id, upload_deadline_ts)
    )


async def upload_phase(
    manager: LobbyManager, lobby_id: str, deadline_ts: float
) -> None:
    try:
        while time.time() < deadline_ts:
            async with manager._lobby_lock(lobby_id):
                lobby = manager.lobbies.get(lobby_id)
                if not lobby or lobby.state != LobbyState.UPLOAD:
                    return
                if len(lobby.uploaded) >= len(lobby.players):
                    break
            await asyncio.sleep(0.4)
        await asyncio.sleep(0.2)
        await begin_voting(manager, lobby_id)
    except asyncio.CancelledError:
        return
    finally:
        me = asyncio.current_task()
        t = manager._upload_tasks.get(lobby_id)
        if t is me:
            manager._upload_tasks.pop(lobby_id, None)


async def begin_voting(manager: LobbyManager, lobby_id: str) -> None:
    async with manager._lobby_lock(lobby_id):
        lobby = manager.lobbies.get(lobby_id)
        if not lobby:
            return
        if lobby.state != LobbyState.UPLOAD:
            return
        lobby.state = LobbyState.VOTING
        lobby.votes.clear()
        lobby.slideshow_completed.clear()
        n = len(lobby.uploaded)
        unlock = time.time() + (0 if n == 0 else SLIDESHOW_SEGMENT_S * n)
        lobby.votes_unlock_at = unlock

    lobby_v = manager.lobbies[lobby_id]
    beats: list[dict[str, Any]] = []
    for i, owner_id in enumerate(sorted(lobby_v.uploaded, key=lambda x: x), start=1):
        nm = beat_display_name(lobby_v, owner_id, i)
        beats.append(
            {
                "player_id": owner_id,
                "name": nm,
                "url": f"/beats/{lobby_id}/{owner_id}",
            }
        )

    unlock_ts = lobby_v.votes_unlock_at
    voting_msg: dict[str, Any] = {
        "type": "voting_start",
        "lobby_id": lobby_id,
        "beats": beats,
        "votes_unlock_at": unlock_ts,
        "anonymous_voting": lobby_v.anonymous_voting,
    }
    if unlock_ts is not None:
        voting_msg["votes_close_at"] = float(unlock_ts) + float(VOTING_COLLECT_S)
    await manager.broadcast(lobby_id, voting_msg)

    if lobby_id in manager._vote_tasks:
        manager._vote_tasks[lobby_id].cancel()
    manager._vote_tasks[lobby_id] = asyncio.create_task(
        vote_collection_loop(manager, lobby_id)
    )


async def vote_collection_loop(manager: LobbyManager, lobby_id: str) -> None:
    try:
        while True:
            async with manager._lobby_lock(lobby_id):
                lobby = manager.lobbies.get(lobby_id)
                if not lobby or lobby.state != LobbyState.VOTING:
                    return
                unlock = lobby.votes_unlock_at
            u = unlock if unlock else time.time()
            if time.time() >= u:
                break
            await asyncio.sleep(0.2)

        async with manager._lobby_lock(lobby_id):
            lobby = manager.lobbies.get(lobby_id)
            if not lobby or lobby.state != LobbyState.VOTING:
                return
            unlock = lobby.votes_unlock_at or time.time()
        deadline = unlock + VOTING_COLLECT_S
        while time.time() < deadline:
            async with manager._lobby_lock(lobby_id):
                lobby = manager.lobbies.get(lobby_id)
                if not lobby or lobby.state != LobbyState.VOTING:
                    return
                beat_owners = set(lobby.uploaded)
                req = required_voters(lobby, beat_owners)
                if req.issubset(lobby.votes.keys()):
                    break
            await asyncio.sleep(0.3)

        await finalize_results(manager, lobby_id)
    except asyncio.CancelledError:
        return
    finally:
        me = asyncio.current_task()
        t = manager._vote_tasks.get(lobby_id)
        if t is me:
            manager._vote_tasks.pop(lobby_id, None)


async def finalize_results(manager: LobbyManager, lobby_id: str) -> None:
    winner_user_ids: list[int] = []
    payload: dict[str, Any] | None = None
    winners: list[str] = []
    async with manager._lobby_lock(lobby_id):
        lobby = manager.lobbies.get(lobby_id)
        if not lobby or lobby.state != LobbyState.VOTING:
            return
        lobby.state = LobbyState.RESULTS
        lobby.results_at = time.time()
        lobby.rematch_pending.clear()
        payload, winner_user_ids = results_ws_payload_and_winner_user_ids(
            lobby, lobby_id
        )
        winners = list(payload.get("winner_ids") or [])

    if payload is None:
        return

    await manager.broadcast(lobby_id, payload)

    if winner_user_ids:

        def _persist_wins() -> None:
            db = SessionLocal()
            try:
                increment_wins_for_users(db, winner_user_ids)
            finally:
                db.close()

        await asyncio.to_thread(_persist_wins)

    async with manager._lobby_lock(lobby_id):
        lobby_after = manager.lobbies.get(lobby_id)
        if lobby_after:
            for wid in winners:
                pl = lobby_after.players.get(wid)
                if pl is not None:
                    pl.wins += 1

