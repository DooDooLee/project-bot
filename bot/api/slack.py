"""Slack 요청 검증. Slack은 요청마다 서명을 붙이고, 우리는 Signing Secret으로 같은 서명을 만들어 비교한다."""
from __future__ import annotations

import hashlib
import hmac
import time

MAX_AGE_SECONDS = 60 * 5


def sign(secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def verify(secret: str, timestamp: str | None, signature: str | None, body: bytes, now: float | None = None) -> bool:
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > MAX_AGE_SECONDS:
        return False  # 오래된 요청을 다시 보내는 공격 방지
    return hmac.compare_digest(sign(secret, timestamp, body), signature)
