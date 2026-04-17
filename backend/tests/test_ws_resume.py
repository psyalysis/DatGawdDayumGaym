"""WebSocket soft disconnect, resume_player_id, leave_lobby, grace expiry."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from backend.multiplayer.lobby import Lobby, LobbyState, Player
from backend.multiplayer.manager import LobbyManager


def test_soft_disconnect_keeps_seat_and_match_sync(tmp_path) -> None:
    async def run() -> None:
        mgr = LobbyManager(tmp_path)
        lid = "AB12CD"
        p1 = "playerOneXxx"
        lobby = Lobby(id=lid, spice=0.5, is_public=True)
        lobby.state = LobbyState.COOKING
        lobby.players[p1] = Player(id=p1, name="A", user_id=10, wins=0)
        mgr.lobbies[lid] = lobby
        mgr.player_lobby[p1] = lid
        mgr.register_auth_session(p1, 10, "A")

        ws1 = MagicMock()
        ws1.close = AsyncMock()
        mgr.attach_ws(p1, ws1)  # type: ignore[arg-type]

        await mgr.detach_connection(p1, ws1)  # type: ignore[arg-type]

        assert p1 in lobby.players
        assert mgr.player_lobby.get(p1) == lid
        sync = mgr.get_match_sync_for_user(lid, 10)
        assert sync is not None
        assert sync["match_state"] == "cooking"

    asyncio.run(run())


def test_leave_lobby_hard_removes(tmp_path) -> None:
    async def run() -> None:
        mgr = LobbyManager(tmp_path)
        lid = "CD34EF"
        p1 = "playerLeave1"
        lobby = Lobby(id=lid, spice=0.25, is_public=True)
        lobby.players[p1] = Player(id=p1, name="A", user_id=11, wins=0)
        mgr.lobbies[lid] = lobby
        mgr.player_lobby[p1] = lid
        mgr.register_auth_session(p1, 11, "A")
        ws1 = MagicMock()
        mgr.attach_ws(p1, ws1)  # type: ignore[arg-type]

        await mgr.handle_message(p1, {"type": "leave_lobby"}, None)

        assert p1 not in lobby.players
        assert mgr.player_lobby.get(p1) is None

    asyncio.run(run())


def test_leave_lobby_in_match_soft_detach_like_disconnect(tmp_path) -> None:
    """In-match Leave uses grace (same as losing socket) so reconnect prompt can show."""

    async def run() -> None:
        mgr = LobbyManager(tmp_path)
        lid = "LVIN01"
        p1 = "playerLvIn1"
        lobby = Lobby(id=lid, spice=0.5, is_public=True)
        lobby.state = LobbyState.COOKING
        lobby.players[p1] = Player(id=p1, name="A", user_id=31, wins=0)
        mgr.lobbies[lid] = lobby
        mgr.player_lobby[p1] = lid
        mgr.register_auth_session(p1, 31, "A")
        ws1 = MagicMock()
        ws1.close = AsyncMock()
        mgr.attach_ws(p1, ws1)  # type: ignore[arg-type]

        should_close = await mgr.handle_message(p1, {"type": "leave_lobby"}, ws1)  # type: ignore[arg-type]
        assert should_close is True
        assert p1 in lobby.players
        assert mgr.pending_reconnect_for_user(31) is not None

    asyncio.run(run())


def test_grace_expiry_removes_player(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.multiplayer.manager.MP_WS_GRACE_S", 0.05)

    async def run() -> None:
        mgr = LobbyManager(tmp_path)
        lid = "EF56GH"
        p1 = "playerGrace1"
        lobby = Lobby(id=lid, spice=0.5, is_public=True)
        lobby.state = LobbyState.COOKING
        lobby.players[p1] = Player(id=p1, name="A", user_id=12, wins=0)
        mgr.lobbies[lid] = lobby
        mgr.player_lobby[p1] = lid
        mgr.register_auth_session(p1, 12, "A")
        ws1 = MagicMock()
        mgr.attach_ws(p1, ws1)  # type: ignore[arg-type]

        await mgr.detach_connection(p1, ws1)  # type: ignore[arg-type]
        await asyncio.sleep(0.2)

        assert p1 not in lobby.players

    asyncio.run(run())


def test_lobby_detach_hard_disconnect_no_grace(tmp_path) -> None:
    async def run() -> None:
        mgr = LobbyManager(tmp_path)
        lid = "LG01XX"
        p1 = "playerLobbyDrop"
        lobby = Lobby(id=lid, spice=0.5, is_public=True)
        lobby.state = LobbyState.LOBBY
        lobby.players[p1] = Player(id=p1, name="A", user_id=120, wins=0)
        mgr.lobbies[lid] = lobby
        mgr.player_lobby[p1] = lid
        mgr.register_auth_session(p1, 120, "A")
        ws1 = MagicMock()
        mgr.attach_ws(p1, ws1)  # type: ignore[arg-type]

        await mgr.detach_connection(p1, ws1)  # type: ignore[arg-type]

        assert p1 not in lobby.players
        assert mgr.player_lobby.get(p1) is None
        assert mgr.pending_reconnect_for_user(120) is None

    asyncio.run(run())


def test_try_resume_replaces_old_socket(tmp_path) -> None:
    async def run() -> None:
        mgr = LobbyManager(tmp_path)
        lid = "GH78IJ"
        p1 = "playerResum1"
        lobby = Lobby(id=lid, spice=0.5, is_public=True)
        lobby.state = LobbyState.LOBBY
        lobby.players[p1] = Player(id=p1, name="A", user_id=13, wins=0)
        mgr.lobbies[lid] = lobby
        mgr.player_lobby[p1] = lid
        mgr.register_auth_session(p1, 13, "A")
        old_ws = MagicMock()
        old_ws.close = AsyncMock()
        mgr.attach_ws(p1, old_ws)  # type: ignore[arg-type]

        new_ws = MagicMock()
        ok, err = await mgr.try_resume_player(13, "A", p1, new_ws)  # type: ignore[arg-type]
        assert ok is True
        assert err is None
        assert mgr.player_ws.get(p1) is new_ws
        old_ws.close.assert_awaited_once()

    asyncio.run(run())


def test_try_resume_wrong_user(tmp_path) -> None:
    async def run() -> None:
        mgr = LobbyManager(tmp_path)
        lid = "IJ90KL"
        p1 = "playerResum2"
        lobby = Lobby(id=lid, spice=0.5, is_public=True)
        lobby.players[p1] = Player(id=p1, name="A", user_id=14, wins=0)
        mgr.lobbies[lid] = lobby
        mgr.player_lobby[p1] = lid
        new_ws = MagicMock()
        ok, err = await mgr.try_resume_player(99, "Evil", p1, new_ws)  # type: ignore[arg-type]
        assert ok is False
        assert err == "wrong_user"

    asyncio.run(run())


def test_pending_reconnect_after_soft_detach(tmp_path) -> None:
    async def run() -> None:
        mgr = LobbyManager(tmp_path)
        lid = "PEND01"
        p1 = "playerPend1"
        lobby = Lobby(id=lid, spice=0.5, is_public=True)
        lobby.state = LobbyState.COOKING
        lobby.players[p1] = Player(id=p1, name="A", user_id=20, wins=0)
        mgr.lobbies[lid] = lobby
        mgr.player_lobby[p1] = lid
        mgr.register_auth_session(p1, 20, "A")
        ws1 = MagicMock()
        ws1.close = AsyncMock()
        mgr.attach_ws(p1, ws1)  # type: ignore[arg-type]

        await mgr.detach_connection(p1, ws1)  # type: ignore[arg-type]

        pending = mgr.pending_reconnect_for_user(20)
        assert pending is not None
        assert pending["lobby_id"] == lid
        assert pending["player_id"] == p1
        assert pending["seconds_remaining"] > 0
        assert pending["reconnect_until_ts"] > 0

    asyncio.run(run())


def test_pending_reconnect_null_after_grace(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.multiplayer.manager.MP_WS_GRACE_S", 0.05)

    async def run() -> None:
        mgr = LobbyManager(tmp_path)
        lid = "PEND02"
        p1 = "playerPend2"
        lobby = Lobby(id=lid, spice=0.5, is_public=True)
        lobby.state = LobbyState.COOKING
        lobby.players[p1] = Player(id=p1, name="A", user_id=21, wins=0)
        mgr.lobbies[lid] = lobby
        mgr.player_lobby[p1] = lid
        mgr.register_auth_session(p1, 21, "A")
        ws1 = MagicMock()
        mgr.attach_ws(p1, ws1)  # type: ignore[arg-type]

        await mgr.detach_connection(p1, ws1)  # type: ignore[arg-type]
        assert mgr.pending_reconnect_for_user(21) is not None
        await asyncio.sleep(0.2)
        assert mgr.pending_reconnect_for_user(21) is None

    asyncio.run(run())


def test_pending_reconnect_cleared_after_resume(tmp_path) -> None:
    async def run() -> None:
        mgr = LobbyManager(tmp_path)
        lid = "PEND03"
        p1 = "playerPend3"
        lobby = Lobby(id=lid, spice=0.5, is_public=True)
        lobby.state = LobbyState.COOKING
        lobby.players[p1] = Player(id=p1, name="A", user_id=22, wins=0)
        mgr.lobbies[lid] = lobby
        mgr.player_lobby[p1] = lid
        mgr.register_auth_session(p1, 22, "A")
        old_ws = MagicMock()
        old_ws.close = AsyncMock()
        mgr.attach_ws(p1, old_ws)  # type: ignore[arg-type]

        await mgr.detach_connection(p1, old_ws)  # type: ignore[arg-type]
        assert mgr.pending_reconnect_for_user(22) is not None

        new_ws = MagicMock()
        ok, err = await mgr.try_resume_player(22, "A", p1, new_ws)  # type: ignore[arg-type]
        assert ok is True
        assert err is None
        assert mgr.pending_reconnect_for_user(22) is None

    asyncio.run(run())


def test_pending_reconnect_results_soft_detach(tmp_path) -> None:
    """Results-phase socket drop is a full disconnect — no reconnect grace."""

    async def run() -> None:
        mgr = LobbyManager(tmp_path)
        lid = "PEND04"
        p1 = "playerPend4"
        lobby = Lobby(id=lid, spice=0.5, is_public=True)
        lobby.state = LobbyState.RESULTS
        lobby.players[p1] = Player(id=p1, name="A", user_id=23, wins=0)
        mgr.lobbies[lid] = lobby
        mgr.player_lobby[p1] = lid
        mgr.register_auth_session(p1, 23, "A")
        ws1 = MagicMock()
        ws1.close = AsyncMock()
        mgr.attach_ws(p1, ws1)  # type: ignore[arg-type]

        await mgr.detach_connection(p1, ws1)  # type: ignore[arg-type]

        assert mgr.pending_reconnect_for_user(23) is None
        assert p1 not in mgr.player_lobby
        assert lid not in mgr.lobbies

    asyncio.run(run())


def test_abandon_reconnect_clears_grace(tmp_path) -> None:
    async def run() -> None:
        mgr = LobbyManager(tmp_path)
        lid = "PEND05"
        p1 = "playerPend5"
        lobby = Lobby(id=lid, spice=0.5, is_public=True)
        lobby.state = LobbyState.COOKING
        lobby.players[p1] = Player(id=p1, name="A", user_id=24, wins=0)
        mgr.lobbies[lid] = lobby
        mgr.player_lobby[p1] = lid
        mgr.register_auth_session(p1, 24, "A")
        ws1 = MagicMock()
        mgr.attach_ws(p1, ws1)  # type: ignore[arg-type]

        await mgr.detach_connection(p1, ws1)  # type: ignore[arg-type]
        assert mgr.pending_reconnect_for_user(24) is not None

        await mgr.abandon_reconnect_grace_for_user(24)

        assert mgr.pending_reconnect_for_user(24) is None

    asyncio.run(run())
