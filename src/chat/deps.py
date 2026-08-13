"""
Current-user identification for chat endpoints.

Verifies the X-Radar-Assertion header (a short-lived HS256 JWT signed with
RADAR_ASSERTION_SECRET) that WatchTower's proxy attaches to every forwarded request. `id` is
WatchTower's public.User.id and is the only claim RADAR's authorization checks key on.
"""

import jwt
from fastapi import Header, HTTPException

from config.settings import settings


def _decode_assertion(x_radar_assertion: str | None) -> dict:
    if not x_radar_assertion:
        raise HTTPException(status_code=401, detail="Missing X-Radar-Assertion")
    try:
        return jwt.decode(
            x_radar_assertion, settings.radar_assertion_secret, algorithms=["HS256"]
        )
    except jwt.InvalidTokenError as err:
        raise HTTPException(
            status_code=401, detail="Invalid or expired X-Radar-Assertion"
        ) from err


async def get_current_user_id(
    x_radar_assertion: str | None = Header(default=None, alias="X-Radar-Assertion"),
) -> str:
    payload = _decode_assertion(x_radar_assertion)
    user_id = payload.get("id")
    if not user_id:
        raise HTTPException(
            status_code=401, detail="X-Radar-Assertion missing 'id' claim"
        )
    return user_id
