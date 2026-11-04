"""Single-user password login with a signed session cookie.

The password comes from the NIGHTSHIFT_PASSWORD environment variable (a Fly.io
secret in the cloud). Without it, the app only runs on localhost.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Optional

COOKIE = "ns_session"
MAX_AGE = 60 * 60 * 24 * 90  # stay signed in on your phone for 90 days
OPEN_PATHS = ("/login", "/static/", "/manifest.webmanifest", "/sw.js", "/healthz")


def password() -> Optional[str]:
    return os.environ.get("NIGHTSHIFT_PASSWORD") or None


def _secret(home: Path) -> bytes:
    env = os.environ.get("NIGHTSHIFT_SECRET")
    if env:
        return env.encode()
    path = home / "data" / ".session_secret"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    # rotating the password signs everyone out
    return (path.read_text().strip() + (password() or "")).encode()


def make_token(home: Path, now: Optional[int] = None) -> str:
    expires = (now or int(time.time())) + MAX_AGE
    sig = hmac.new(_secret(home), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def valid_token(home: Path, token: Optional[str], now: Optional[int] = None) -> bool:
    if not token or "." not in token:
        return False
    expires, sig = token.split(".", 1)
    if not expires.isdigit() or int(expires) < (now or int(time.time())):
        return False
    good = hmac.new(_secret(home), expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, sig)


def check_password(attempt: str) -> bool:
    pw = password()
    return bool(pw) and hmac.compare_digest(pw.encode(), attempt.encode())


def is_open(path: str) -> bool:
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in OPEN_PATHS)
