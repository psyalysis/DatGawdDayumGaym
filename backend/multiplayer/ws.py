"""
WebSocket /ws — JSON messages routed through LobbyManager.
Requires ?token=<JWT> (browser WebSockets cannot send Authorization headers).
Optional ?resume_player_id=<id> to reclaim a seat after a soft disconnect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..auth import redeem_ws_ticket, try_validate_ws_token

from .ws_rate_limit import SlidingWindowRateLimiter

logger = logging.getLogger("cookup.ws")

router = APIRouter()

# Per-IP new connections (after accept) — reduces connection spam.
_CONNECT_LIMIT = SlidingWindowRateLimiter(max_events=45, window_s=60.0)
# Per player_id inbound text messages.
_MSG_LIMIT = SlidingWindowRateLimiter(max_events=100, window_s=10.0)
_MAX_TEXT_BYTES = 65_536


def _log_ws(event: str, **fields: Any) -> None:
    payload = {k: v for k, v in fields.items() if v is not None}
    logger.warning("%s | %s", event, json.dumps(payload, default=str, sort_keys=True))


def get_manager(ws: WebSocket):
    return ws.app.state.manager


@router.websocket("/ws")
async def multiplayer_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    client_host = (websocket.client.host if websocket.client else None) or "unknown"
    ip_key = f"conn:{client_host}"
    if not _CONNECT_LIMIT.check(ip_key):
        _log_ws("ws_connect_rate_limited", client_host=client_host)
        await websocket.close(code=1008)
        return

    token = websocket.query_params.get("token")

    ticket_result = await asyncio.to_thread(redeem_ws_ticket, token or "")
    if ticket_result is not None:
        auth = ticket_result
        auth_reason = None
    else:
        auth, auth_reason = await asyncio.to_thread(try_validate_ws_token, token)

    if auth is None:
        _log_ws("ws_auth_failed", reason=auth_reason, client_host=client_host)
        await websocket.close(code=4401)
        return

    user_id, username = auth
    manager = get_manager(websocket)

    resume_raw = websocket.query_params.get(
        "resume_player_id"
    ) or websocket.query_params.get("resumePlayerId")
    resume_id = str(resume_raw).strip() if resume_raw else ""

    player_id: str | None = None
    try:
        if resume_id:
            ok, resume_reason = await manager.try_resume_player(
                user_id, username, resume_id, websocket
            )
            if not ok:
                _log_ws(
                    "ws_resume_failed",
                    reason=resume_reason,
                    client_host=client_host,
                    resume_player_id=resume_id,
                )
                await websocket.close(code=4404)
                return
            player_id = resume_id
        else:
            player_id = secrets.token_urlsafe(12)
            manager.register_auth_session(player_id, user_id, username)
            manager.attach_ws(player_id, websocket)

        await websocket.send_json(
            {"type": "connected", "player_id": player_id, "resumed": bool(resume_id)}
        )
        if resume_id:
            await manager.send_match_resync_to_player(player_id)

        assert player_id is not None
        while True:
            raw = await websocket.receive_text()
            byte_len = len(raw.encode("utf-8"))
            if byte_len > _MAX_TEXT_BYTES:
                ref = secrets.token_hex(4).upper()
                _log_ws(
                    "ws_message_too_large",
                    player_id=player_id,
                    byte_length=byte_len,
                    error_ref=ref,
                )
                await websocket.close(code=1009)
                return
            if not _MSG_LIMIT.check(player_id):
                ref = await manager.send_player_error(
                    player_id,
                    "Too many messages. Please slow down.",
                    error_code="RATE_LIMITED",
                )
                _log_ws("ws_message_rate_limited", player_id=player_id, error_ref=ref)
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                ref = await manager.send_player_error(
                    player_id,
                    "Invalid JSON in message.",
                    error_code="BAD_JSON",
                )
                _log_ws(
                    "ws_json_decode_error",
                    player_id=player_id,
                    error_ref=ref,
                    json_error=str(e)[:240],
                )
                continue
            if not isinstance(data, dict):
                ref = await manager.send_player_error(
                    player_id,
                    "Message must be a JSON object.",
                    error_code="BAD_MESSAGE_SHAPE",
                )
                _log_ws("ws_invalid_message_shape", player_id=player_id, error_ref=ref)
                continue
            msg_type = data.get("type")
            try:
                close_after = await manager.handle_message(player_id, data, websocket)
            except Exception:
                ref = secrets.token_hex(4).upper()
                logger.exception(
                    "ws_handle_message_failed | player_id=%s error_ref=%s message_type=%r",
                    player_id,
                    ref,
                    msg_type,
                )
                _log_ws(
                    "ws_handle_message_failed",
                    player_id=player_id,
                    error_ref=ref,
                    message_type=msg_type,
                )
                await manager.send_to(
                    player_id,
                    {
                        "type": "error",
                        "message": "An unexpected server error occurred. Please try again.",
                        "error_ref": ref,
                        "error_code": "INTERNAL",
                    },
                )
                continue
            if close_after:
                await websocket.close()
                break
    except WebSocketDisconnect:
        pass
    finally:
        if player_id is not None:
            _MSG_LIMIT.forget(player_id)
            await manager.detach_connection(player_id, websocket)
