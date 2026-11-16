"""Camada de compatibilidade entre o `Store` (escrito para sqlite3) e o Turso/libSQL (cliente `libsql`).

O Store só precisa de: execute(sql, args) -> cursor com fetchone/fetchall/lastrowid/rowcount, linhas acessíveis por nome
(`r["col"]`, `dict(r)`), executescript e autocommit. O cliente libsql devolve tuplas e segue o modelo DB-API (transação
implícita até `commit()`), então este módulo:
  * converte linhas em `Row` (tupla + nomes de coluna, via cursor.description);
  * faz `commit()` após cada escrita (autocommit);
  * obtém lastrowid/rowcount por `SELECT last_insert_rowid()/changes()` quando o cliente não os expõe;
  * implementa executescript dividindo o script em comandos completos;
  * reconecta UMA vez em leituras se a conexão remota caiu (escritas NÃO são repetidas: poderiam duplicar efeitos).
NADA aqui assume recursos não verificados: `scripts/turso_check.py` testa, contra o Turso REAL, cada capacidade usada."""
from __future__ import annotations

import importlib
import re
import sqlite3
from typing import Any, Iterable

_READ = re.compile(r"^\s*(select|with|pragma|explain)\b", re.I)
_RETURNING = re.compile(r"\breturning\b", re.I)


class DbUnavailable(RuntimeError):
    """O banco remoto não respondeu. Escritas não são repetidas automaticamente."""


class Row(tuple):
    """Tupla com acesso por nome, compatível com o que o projeto usa de sqlite3.Row (r['x'], r[0], dict(r), keys())."""

    def __new__(cls, keys: Iterable[str], values: Iterable[Any]):
        r = super().__new__(cls, values)
        r._keys = tuple(keys)
        return r

    def keys(self) -> list[str]:
        return list(self._keys)

    def __getitem__(self, k):
        if isinstance(k, str):
            try:
                return super().__getitem__(self._keys.index(k))
            except ValueError:
                raise KeyError(k) from None
        return super().__getitem__(k)


class Cursor:
    def __init__(self, rows: list[Row], lastrowid: int | None, rowcount: int):
        self._rows, self._i = rows, 0
        self.lastrowid, self.rowcount = lastrowid, rowcount

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        self._i += 1
        return self._rows[self._i - 1]

    def fetchall(self) -> list[Row]:
        out, self._i = self._rows[self._i:], len(self._rows)
        return out

    def __iter__(self):
        return iter(self.fetchall())


class LibsqlConn:
    """Conexão remota ao Turso com a superfície de sqlite3 que o Store usa."""

    backend = "turso"

    def __init__(self, url: str, token: str, module: Any = None):
        self.url, self.token = url, token
        self._mod = module or importlib.import_module("libsql")
        self._c = self._connect()

    def _connect(self):
        try:
            return self._mod.connect(self.url, auth_token=self.token)
        except Exception as e:
            raise DbUnavailable(f"não foi possível conectar ao Turso: {type(e).__name__}") from e

    def _run(self, sql: str, args: tuple):
        return self._c.execute(sql, args)

    def execute(self, sql: str, args: Iterable = ()) -> Cursor:
        args = tuple(args)
        is_read = bool(_READ.match(sql)) and not _RETURNING.search(sql)
        try:
            raw = self._run(sql, args)
        except Exception as e:
            if not is_read or not _looks_like_connection_error(e):
                raise
            self._c = self._connect()                      # leitura: reconecta e repete uma vez
            raw = self._run(sql, args)
        desc = getattr(raw, "description", None)
        keys = [d[0] for d in desc] if desc else []
        rows = [Row(keys, r) for r in raw.fetchall()] if desc else []
        lastrowid = rowcount = None
        if not is_read:
            rowcount = getattr(raw, "rowcount", None)
            if rowcount is None or rowcount < 0:
                rowcount = self._c.execute("SELECT changes()", ()).fetchall()[0][0]
            if re.match(r"^\s*(insert|replace)\b", sql, re.I):
                lastrowid = getattr(raw, "lastrowid", None)
                if not lastrowid:
                    lastrowid = self._c.execute("SELECT last_insert_rowid()", ()).fetchall()[0][0]
            self._c.commit()                               # autocommit
        return Cursor(rows, lastrowid, rowcount if rowcount is not None else -1)

    def executemany(self, sql: str, seq: Iterable[Iterable]) -> None:
        for a in seq:
            self.execute(sql, a)

    def executescript(self, script: str) -> None:
        buf = ""
        for line in script.splitlines(keepends=True):
            buf += line
            if sqlite3.complete_statement(buf):
                stmt = buf.strip()
                buf = ""
                if stmt and stmt != ";":
                    self.execute(stmt)

    def commit(self) -> None:
        self._c.commit()

    def rollback(self) -> None:
        try:
            self._c.rollback()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._c.close()
        except Exception:
            pass


def _looks_like_connection_error(e: Exception) -> bool:
    m = str(e).lower()
    return any(w in m for w in ("stream", "connection", "timed out", "timeout", "closed", "reset", "network", "broken pipe"))


def open_sqlite(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA synchronous=NORMAL")
    return c
