"""
Current-user identification for chat endpoints.

Verifies the X-Radar-Assertion header (a short-lived HS256 JWT, {id, email, name, iat, exp})
WatchTower's proxy mints fresh on every forwarded request, signed with the shared
RADAR_ASSERTION_SECRET — proof a real, SSO-verified WatchTower user is behind the request.
`id` is WatchTower's own public.User.id (a real UUID, resolved server-side in WatchTower's
proxy via its native Prisma access) — this is what thread-claim authorization is keyed on
(see chat/service.py), not email; email is kept only for human-readable audit/actor logging.

No unverified-header fallback (removed 2026-07-29, flagged by automated security review as a
spoofable-field auth bypass — anyone able to reach this backend directly could set
X-User-Email to any address and impersonate that user). WatchTower's proxy has sent the real
assertion on every call since this endpoint was built, so the fallback was already dead
weight, not a real transitional need.
"""
import jwt
from fastapi import Header, HTTPException

from config.settings import settings


def _decode_assertion(x_radar_assertion: str | None) -> dict:
    if not x_radar_assertion:
        raise HTTPException(status_code=401, detail="Missing X-Radar-Assertion")
    try:
        return jwt.decode(x_radar_assertion, settings.radar_assertion_secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired X-Radar-Assertion")


async def get_current_user_email(
    x_radar_assertion: str | None = Header(default=None, alias="X-Radar-Assertion"),
) -> str:
    payload = _decode_assertion(x_radar_assertion)
    email = payload.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="X-Radar-Assertion missing 'email' claim")
    return email


async def get_current_user_id(
    x_radar_assertion: str | None = Header(default=None, alias="X-Radar-Assertion"),
) -> str:
    payload = _decode_assertion(x_radar_assertion)
    user_id = payload.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="X-Radar-Assertion missing 'id' claim")
    return user_id
