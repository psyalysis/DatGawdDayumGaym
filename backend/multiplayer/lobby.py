"""
Lobby and player models; spice presets and game states.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..rank import rank_public_dict

# Match spec: three spice cards map to these generation intensities.
SPICE_MILD = 0.25
SPICE_MEDIUM = 0.5
SPICE_HOT = 0.85
ALLOWED_SPICES: frozenset[float] = frozenset({SPICE_MILD, SPICE_MEDIUM, SPICE_HOT})


def canonical_spice(value: float) -> float | None:
    """Map a float (e.g. from JSON) to the nearest allowed spice preset."""
    v = float(value)
    for a in (SPICE_MILD, SPICE_MEDIUM, SPICE_HOT):
        if abs(v - a) < 0.02:
            return a
    return None


COOK_DURATION_S = 600
# Host-selectable cook length (minutes); UI/backend clamp to these values.
COOK_DURATION_MIN_OPTIONS: tuple[int, ...] = (5, 10, 15, 20, 30)
DEFAULT_COOK_DURATION_MIN = 10
# Upload window after cook ends (2 minutes).
UPLOAD_PHASE_S = 120
# Seconds per beat in slideshow (1–45s playback) plus small pad before votes unlock.
SLIDESHOW_SEGMENT_S = 46
# Stored/downloaded beat length cap; matches frontend CLIP_MAX_SEC (voting_slideshow.js).
BEAT_MAX_PLAYBACK_S = 45
# Max time to collect votes after the slideshow unlocks (1 minute).
VOTING_COLLECT_S = 60
# Lobbies in results older than this are purged (uploads deleted).
LOBBY_RESULTS_TTL_S = 3600

# Max players per lobby (join + server browser).
MAX_LOBBY_PLAYERS = 10


# These are all of the states which a lobby can go in, in chronological order :)
class LobbyState(str, Enum):
    WAITING = "waiting"
    LOBBY = "lobby"
    GENERATING = "generating"
    COOKING = "cooking"
    UPLOAD = "upload"
    VOTING = "voting"
    RESULTS = "results"


@dataclass
class Player:
    id: str
    name: str
    user_id: int
    wins: int = 0
    ready: bool = False


@dataclass
class Lobby:
    id: str
    spice: float
    is_public: bool = True
    players: dict[str, Player] = field(default_factory=dict)
    state: LobbyState = LobbyState.LOBBY
    seed: int | None = None
    sounds: dict[str, str] | None = None
    votes: dict[str, str] = field(default_factory=dict)
    uploaded: set[str] = field(default_factory=set)
    slideshow_completed: set[str] = field(default_factory=set)
    votes_unlock_at: float | None = None
    created_at: float = field(default_factory=time.time)
    results_at: float | None = None
    cook_duration_min: int = DEFAULT_COOK_DURATION_MIN
    anonymous_voting: bool = False
    cook_finished: set[str] = field(default_factory=set)
    # Wall-clock unix seconds; set during COOKING / UPLOAD for HTTP + WS recovery.
    cook_deadline_ts: float | None = None
    upload_deadline_ts: float | None = None
    # player_id -> unix time of last mp_chat send (text or emoji)
    chat_last_sent: dict[str, float] = field(default_factory=dict)
    # RESULTS only: player_ids who voted to rematch
    rematch_pending: set[str] = field(default_factory=set)

    def lobby_snapshot(self) -> dict[str, Any]:
        host_id = next(iter(self.players)) if self.players else ""
        drumkit: dict[str, Any] = {}
        if self.seed is not None:
            drumkit["seed"] = self.seed
        if self.seed is not None and self.state in (
            LobbyState.COOKING,
            LobbyState.UPLOAD,
            LobbyState.VOTING,
            LobbyState.RESULTS,
        ):
            drumkit["has_kit"] = True
        return {
            "lobby_id": self.id,
            "spice": self.spice,
            "is_public": self.is_public,
            "state": self.state.value,
            "host_id": host_id,
            "cook_duration_min": self.cook_duration_min,
            "anonymous_voting": self.anonymous_voting,
            "players": [
                {
                    "id": p.id,
                    "name": p.name,
                    "ready": p.ready,
                    "rank": rank_public_dict(p.wins),
                }
                for p in self.players.values()
            ],
            "drumkit": drumkit,
            "votes": dict(self.votes),
            "player_count": len(self.players),
            "cook_finished": sorted(self.cook_finished),
            "uploaded": sorted(self.uploaded),
            "slideshow_completed": sorted(self.slideshow_completed),
        }


def beat_display_name(lobby: Lobby, owner_id: str, entry_index_1based: int) -> str:
    """During anonymous voting, beats are labeled without real usernames."""
    if lobby.anonymous_voting:
        return f"Entry {entry_index_1based}"
    p = lobby.players.get(owner_id)
    return p.name if p else owner_id
