"""Mints a test X-Radar-Assertion JWT signed with your local RADAR_ASSERTION_SECRET.
Usage: uv run python tests/_mint_jwt.py <user-id-uuid> [email]  (run from the radar/ repo root)

email is optional — RADAR's authorization checks are keyed entirely on `id` now (chat/access.py),
not email, so a token with no email claim at all works identically to one with a real email.
"""

import os
import sys
import time

import jwt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from config.settings import settings

user_id = sys.argv[1]
email = sys.argv[2] if len(sys.argv) > 2 else None

payload = {"id": user_id, "iat": int(time.time()), "exp": int(time.time()) + 3600}
if email:
    payload["email"] = email

token = jwt.encode(payload, settings.radar_assertion_secret, algorithm="HS256")
print(token)
