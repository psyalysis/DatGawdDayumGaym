"""
Pydantic schemas for auth and API responses.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

USERNAME_PATTERN = re.compile(r"^[a-z0-9_]{3,20}$")


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=20)
    password: str = Field(..., min_length=8, max_length=128)

    @field_validator("username")
    @classmethod
    def username_chars(cls, v: str) -> str:
        v = v.strip()
        if not USERNAME_PATTERN.match(v):
            raise ValueError(
                "Username must be 3–20 characters: lowercase letters, numbers, underscores only."
            )
        return v


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    token: str
    username: str


class RegisterResponse(BaseModel):
    message: str


class RankInfo(BaseModel):
    key: str
    abbrev: str
    label: str
    color: str


class LeaderboardEntry(BaseModel):
    username: str
    wins: int
    rank: RankInfo | None = None
    rank_index: int = 0


class MeResponse(BaseModel):
    username: str
    wins: int
    rank: RankInfo | None = None
    rank_index: int = 0
