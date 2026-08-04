"""Viewer 账号密码哈希（标准库 pbkdf2，不落明文）。"""

from __future__ import annotations

import hashlib
import secrets

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("ascii"),
        _ITERATIONS,
    )
    return f"{_ALGO}${_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not password or not stored:
        return False
    try:
        algo, iters_s, salt, hash_hex = stored.split("$", 3)
        if algo != _ALGO:
            return False
        iters = int(iters_s)
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("ascii"),
            iters,
        )
        return secrets.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False
