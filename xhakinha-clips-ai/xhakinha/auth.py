"""Senhas com scrypt, sessão JWT em cookie HttpOnly, limitação de tentativas. Nenhuma senha/segredo padrão."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

import jwt

from .db import Store


def hash_password(pw: str) -> str:
    if len(pw) < 10:
        raise ValueError("senha precisa ter pelo menos 10 caracteres")
    salt = os.urandom(16)
    n, r, p = 2 ** 14, 8, 1
    h = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def create_user(db: Store, username: str, password: str) -> None:
    db.x("INSERT INTO users(username,pw_hash,created) VALUES(?,?,?) ON CONFLICT(username) DO UPDATE SET pw_hash=excluded.pw_hash",
         (username, hash_password(password), time.time()))


def make_token(secret: str, username: str, ttl: int = 12 * 3600) -> str:
    return jwt.encode({"sub": username, "iat": int(time.time()), "exp": int(time.time()) + ttl, "jti": secrets.token_hex(8)}, secret, algorithm="HS256")


def read_token(secret: str, token: str | None) -> str | None:
    if not token:
        return None
    try:
        return jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})["sub"]
    except jwt.PyJWTError:
        return None


def too_many_failures(db: Store, ip: str, limit: int = 8, window: int = 600) -> bool:
    db.x("DELETE FROM logins WHERE ts<?", (time.time() - window,))
    return db.one("SELECT COUNT(*) n FROM logins WHERE ip=?", (ip,))["n"] >= limit


def record_failure(db: Store, ip: str) -> None:
    db.x("INSERT INTO logins(ip,ts) VALUES(?,?)", (ip, time.time()))
