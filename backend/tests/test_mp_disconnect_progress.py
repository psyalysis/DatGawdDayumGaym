"""Disconnect cleanup, snapshot progress fields, and match_sync extras."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from backend.multiplayer.lobby import Lobby, LobbyState, Player
from backend.multiplayer.manager import LobbyManager


def test_disconnect_voting_finalizes_when_only_non_voter_left(tmp_path: Path) -> None:
    """If everyone required to vote has voted and the last holdout disconnects, go to results."""

    async def run() -> None:
        mgr = LobbyManager(tmp_path)
        lid = "VOTEF1NAL"
        p1, p2, p3 = "voteP1xxx", "voteP2xxx", "voteP3xxx"
        lobby = Lobby(id=lid, spice=0.25, is_public=True)
        lobby.state = LobbyState.VOTING
        lobby.votes_unlock_at = time.time() - 1.0
        lobby.players[p1] = Player(id=p1, name="A", user_id=10, wins=0)
        lobby.players[p2] = Player(id=p2, name="B", user_id=20, wins=0)
        lobby.players[p3] = Player(id=p3, name="C", user_id=30, wins=0)
        lobby.uploaded = {p1, p2, p3}
        lobby.votes = {p1: p2, p2: p1}
        mgr.lobbies[lid] = lobby
        mgr.player_lobby[p1] = lid
        mgr.player_lobby[p2] = lid
        mgr.player_lobby[p3] = lid

        class FakeWS:
            def __init__(self) -> None:
                self.sent: list[str] = []

            async def send_text(self, raw: str) -> None:
                self.sent.append(raw)

        ws1 = FakeWS()
        ws2 = FakeWS()
        mgr.attach_ws(p1, ws1)  # type: ignore[arg-type]
        mgr.attach_ws(p2, ws2)  # type: ignore[arg-type]

        await mgr.disconnect(p3)

        assert lobby.state == LobbyState.RESULTS
        payloads = [json.loads(s) for s in ws1.sent + ws2.sent]
        assert any(p.get("type") == "results" for p in payloads)

    asyncio.run(run())


def test_lobby_snapshot_includes_progress_fields() -> None:
    lobby = Lobby(id="AB12CD", spice=0.5, is_public=True)
    lobby.state = LobbyState.VOTING
    lobby.players["x"] = Player(id="x", name="X", user_id=1, wins=0)
    lobby.players["y"] = Player(id="y", name="Y", user_id=2, wins=0)
    lobby.cook_finished = {"x"}
    lobby.uploaded = {"x", "y"}
    lobby.slideshow_completed = {"y"}
    lobby.votes = {"x": "y"}

    snap = lobby.lobby_snapshot()
    assert snap["player_count"] == 2
    assert snap["cook_finished"] == ["x"]
    assert snap["uploaded"] == ["x", "y"]
    assert snap["slideshow_completed"] == ["y"]
    assert snap["votes"] == {"x": "y"}


def test_disconnect_prunes_uploaded_votes_slideshow(tmp_path: Path) -> None:
    async def run() -> None:
        mgr = LobbyManager(tmp_path)
        lid = "CAFEBEEF"
        p1, p2, p3 = "playerAaaa", "playerBbbb", "playerCccc"
        lobby = Lobby(id=lid, spice=0.25, is_public=True)
        lobby.state = LobbyState.VOTING
        lobby.players[p1] = Player(id=p1, name="A", user_id=10, wins=0)
        lobby.players[p2] = Player(id=p2, name="B", user_id=20, wins=0)
        lobby.players[p3] = Player(id=p3, name="C", user_id=30, wins=0)
        lobby.uploaded = {p1, p2, p3}
        lobby.votes = {p1: p2, p2: p1, p3: p1}
        lobby.slideshow_completed = {p1}
        mgr.lobbies[lid] = lobby
        mgr.player_lobby[p1] = lid
        mgr.player_lobby[p2] = lid
        mgr.player_lobby[p3] = lid

        class FakeWS:
            def __init__(self) -> None:
                self.sent: list[str] = []

            async def send_text(self, raw: str) -> None:
                self.sent.append(raw)

        ws2 = FakeWS()
        ws3 = FakeWS()
        mgr.attach_ws(p2, ws2)  # type: ignore[arg-type]
        mgr.attach_ws(p3, ws3)  # type: ignore[arg-type]

        await mgr.disconnect(p1)

        assert p1 not in lobby.players
        assert p1 not in lobby.uploaded
        assert p1 not in lobby.slideshow_completed
        assert p1 not in lobby.votes
        assert p2 not in lobby.votes
        assert lobby.votes == {}

        payloads = [json.loads(s) for s in ws2.sent]
        assert any(p.get("type") == "player_leave" for p in payloads)
        upd = [p for p in payloads if p.get("type") == "lobby_update"]
        assert upd
        inner = upd[-1]["lobby"]
        assert inner["player_count"] == 2
        assert set(inner["uploaded"]) == {p2, p3}

    asyncio.run(run())


def test_disconnect_cooking_rebroadcasts_cook_finished(tmp_path: Path) -> None:
    async def run() -> None:
        mgr = LobbyManager(tmp_path)
        lid = "DEADBEEF"
        p1, p2, p3 = "cookP1xxx", "cookP2xxx", "cookP3xxx"
        lobby = Lobby(id=lid, spice=0.5, is_public=True)
        lobby.state = LobbyState.COOKING
        lobby.players[p1] = Player(id=p1, name="A", user_id=1, wins=0)
        lobby.players[p2] = Player(id=p2, name="B", user_id=2, wins=0)
        lobby.players[p3] = Player(id=p3, name="C", user_id=3, wins=0)
        lobby.cook_finished = {p1, p2, p3}
        mgr.lobbies[lid] = lobby
        mgr.player_lobby[p1] = lid
        mgr.player_lobby[p2] = lid
        mgr.player_lobby[p3] = lid

        class FakeWS:
            def __init__(self) -> None:
                self.sent: list[str] = []

            async def send_text(self, raw: str) -> None:
                self.sent.append(raw)

        ws2 = FakeWS()
        mgr.attach_ws(p2, ws2)  # type: ignore[arg-type]

        await mgr.disconnect(p1)

        payloads = [json.loads(s) for s in ws2.sent]
        cook_upd = [p for p in payloads if p.get("type") == "cook_finished_update"]
        assert cook_upd
        assert cook_upd[-1]["player_count"] == 2
        assert set(cook_upd[-1]["finished_player_ids"]) == {p2, p3}

    asyncio.run(run())


def test_get_match_sync_includes_snapshot_progress(tmp_path: Path) -> None:
    mgr = LobbyManager(tmp_path)
    lid = "FEEDFACE"
    p1 = "soloPlayer"
    lobby = Lobby(id=lid, spice=0.85, is_public=False)
    lobby.state = LobbyState.UPLOAD
    lobby.players[p1] = Player(id=p1, name="Solo", user_id=99, wins=0)
    lobby.upload_deadline_ts = 1_700_000_000.0
    lobby.uploaded = {p1}
    mgr.lobbies[lid] = lobby
    mgr.player_lobby[p1] = lid

    sync = mgr.get_match_sync_for_user(lid, 99)
    assert sync is not None
    assert sync["player_count"] == 1
    assert sync["uploaded"] == [p1]
    assert sync["votes"] == {}
    assert sync["players"][0]["id"] == p1


def test_get_match_sync_includes_drumkit_when_cooking(tmp_path: Path) -> None:
    """Menu resume must receive seed via drumkit (match_resync / HTTP match_sync)."""
    mgr = LobbyManager(tmp_path)
    lid = "ABC123"
    p1 = "pCook"
    lobby = Lobby(id=lid, spice=0.42, is_public=True)
    lobby.state = LobbyState.COOKING
    lobby.seed = 999_888_777
    lobby.cook_deadline_ts = time.time() + 600.0
    lobby.players[p1] = Player(id=p1, name="Chef", user_id=42, wins=0)
    mgr.lobbies[lid] = lobby
    mgr.player_lobby[p1] = lid

    sync = mgr.get_match_sync_for_user(lid, 42)
    assert sync is not None
    assert sync["match_state"] == "cooking"
    assert sync["drumkit"]["seed"] == 999_888_777
    assert sync["spice"] == 0.42
    assert sync["is_public"] is True
