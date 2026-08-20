"""
HTTP middleware for the chat backend. Every caller is a known server (WatchTower's Next.js
backend, or the standalone tool-exec service), never a browser directly, so there's no
Origin header or public Host to restrict against — CORS/trusted-host don't apply here.
Security headers and rate limiting apply regardless of caller.
"""

import time

import jwt
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from config.settings import settings


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        return response


def _rate_limit_identity(request: Request) -> str:
    """Prefers the verified WatchTower user id (X-Radar-Assertion's 'id' claim) over client IP —
    every real chat/notification request arrives proxied through WatchTower's own backend, so
    keying by request.client.host would collapse the limit into one shared global counter for
    every end user behind that proxy IP. Falls back to IP for requests with no assertion at
    all (the HMAC-signed intake webhook has no per-user identity to key on) — this is a
    best-effort key choice for rate-limiting only, never an auth decision; an invalid/expired
    token still gets its real 401 from the route's own dependency, not here."""
    assertion = request.headers.get("X-Radar-Assertion")
    if assertion:
        try:
            payload = jwt.decode(
                assertion, settings.radar_assertion_secret, algorithms=["HS256"]
            )
            user_id = payload.get("id")
            if user_id:
                return f"user:{user_id}"
        except jwt.InvalidTokenError:
            pass
    client_ip = request.client.host if request.client else "unknown"
    return f"ip:{client_ip}"


class RateLimitingMiddleware(BaseHTTPMiddleware):
    """Redis-backed fixed-window counter, keyed by verified user id where available (falling
    back to client IP) — shared across every replica. Redis already runs here
    (gateway/concurrency.py's DistributedSemaphore uses the same instance), so this adds no
    new infrastructure."""

    def __init__(self, app, calls_per_minute: int = 100):
        super().__init__(app)
        self._calls_per_minute = calls_per_minute

    async def dispatch(self, request: Request, call_next):
        # Read from app.state at request time, not construction time — the middleware stack is
        # built (add_middleware calls run) before lifespan() creates app.state.redis, so a
        # constructor-time reference would just be None.
        redis = request.app.state.redis
        window = int(time.time() // 60)
        key = f"ratelimit:{_rate_limit_identity(request)}:{window}"

        # One round trip instead of two: EXPIRE is idempotent/cheap to reissue every call within
        # the window, so there's no need to branch on count == 1 first.
        async with redis.pipeline(transaction=False) as pipe:
            pipe.incr(key)
            pipe.expire(key, 60)
            count, _ = await pipe.execute()

        if count > self._calls_per_minute:
            return JSONResponse(
                status_code=429, content={"detail": "Rate limit exceeded"}
            )

        return await call_next(request)
