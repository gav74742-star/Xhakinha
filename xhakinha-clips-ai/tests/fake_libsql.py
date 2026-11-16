"""Substituto SIMULADO do pacote `libsql` (Turso) para testes offline.
Imita o que o cliente real expõe: execute() -> cursor com description/fetchall (linhas como TUPLAS, sem row_factory),
transação implícita até commit(), e cursor SEM lastrowid/rowcount confiáveis (força os caminhos de fallback do adaptador).
Várias conexões com a mesma URL enxergam o mesmo banco (como um banco remoto). Isto NÃO prova o comportamento do Turso real:
quem prova é `scripts/turso_check.py` rodando contra o Turso de verdade."""
from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

REGISTRY: dict[str, Path] = {}
STATS = {"connects": 0, "executes": 0}
FAIL_NEXT: list[str] = []          # injeta erro de conexão na próxima execução (teste de reconexão)


class _Cur:
    def __init__(self, c: sqlite3.Cursor):
        self._c = c
        self.description = c.description
        self.rowcount = None
        self.lastrowid = None

    def fetchall(self):
        return self._c.fetchall() if self.description else []

    def fetchone(self):
        return self._c.fetchone() if self.description else None


class _Conn:
    def __init__(self, path: Path):
        self._c = sqlite3.connect(str(path), timeout=30, check_same_thread=False)   # isolation padrão = transação implícita
        self._c.execute("PRAGMA journal_mode=WAL")

    def execute(self, sql, params=()):
        STATS["executes"] += 1
        if FAIL_NEXT:
            raise RuntimeError(FAIL_NEXT.pop())
        return _Cur(self._c.execute(sql, tuple(params)))

    def commit(self):
        self._c.commit()

    def rollback(self):
        self._c.rollback()

    def close(self):
        self._c.close()


def connect(url, auth_token=None, **kw):
    if not auth_token:
        raise ValueError("auth_token ausente")
    STATS["connects"] += 1
    return _Conn(REGISTRY[url])


def install(url: str, path: Path):
    REGISTRY[url] = Path(path)
    mod = types.ModuleType("libsql")
    mod.connect = connect
    sys.modules["libsql"] = mod
