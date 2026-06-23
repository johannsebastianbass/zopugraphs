"""Autenticação simples baseada em SQLite (PBKDF2-HMAC-SHA256, stdlib)."""

from __future__ import annotations

import binascii
import hashlib
import hmac
import os
from typing import Optional, Tuple

import db

_ITERATIONS = 200_000


def hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    """Devolve (salt_hex, hash_hex). Gera salt novo se não informado."""
    if salt is None:
        salt_bytes = os.urandom(16)
    else:
        salt_bytes = binascii.unhexlify(salt)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, _ITERATIONS)
    return binascii.hexlify(salt_bytes).decode(), binascii.hexlify(dk).decode()


def verify(password: str, salt_hex: str, hash_hex: str) -> bool:
    _, calc = hash_password(password, salt_hex)
    return hmac.compare_digest(calc, hash_hex)


def authenticate(username: str, password: str) -> Optional[dict]:
    u = db.get_user(username)
    if not u or not u["ACTIVE"]:
        return None
    if verify(password, u["SALT"], u["PASSWORD_HASH"]):
        return u
    return None


def create_user(username: str, password: str, role: str = "client",
                tenant_id: Optional[int] = None, name: str = "", scope: str = "all") -> int:
    salt, h = hash_password(password)
    return db.add_user(username, h, salt, role, tenant_id, name, scope)


def change_password(username: str, new_password: str) -> None:
    salt, h = hash_password(new_password)
    db.set_user_password(username, h, salt)
