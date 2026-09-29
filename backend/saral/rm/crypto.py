"""Encryption at rest for the change a customer asked the RM to make (new mobile, claim facts).

Fernet (AES-128-CBC + HMAC-SHA256) with a key from RM_REQUEST_KEY. Only the service-key RM
endpoints decrypt; transcripts, logs and case summaries never carry the plaintext.
"""

from __future__ import annotations

import base64
import hashlib
import json
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from saral.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    s = get_settings()
    key = s.rm_request_key
    if not key:  # dev/test only (prod refuses to boot without RM_REQUEST_KEY)
        digest = hashlib.sha256(f"rm-request|{s.session_secret}".encode()).digest()
        key = base64.urlsafe_b64encode(digest).decode()
    return Fernet(key.encode())


def encrypt(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data, ensure_ascii=False).encode()).decode()


def decrypt(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except InvalidToken:
        return None  # wrong key / tampered: never guess
