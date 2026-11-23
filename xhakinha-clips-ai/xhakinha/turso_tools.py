"""Ferramentas de verificação e migração do Turso (usadas por scripts/turso_check.py e scripts/turso_migrate.py).
Nunca imprimem URL completa nem token."""
from __future__ import annotations

import os
import secrets as _secrets
import sqlite3
from pathlib import Path
from typing import Callable

from . import dbdriver
from .db import SCHEMA, SCHEMA_VERSION

# ordem segura para cópia (tabelas "pai" primeiro)
TABLE_ORDER = ["settings", "users", "secrets", "sources", "contents", "jobs", "clips", "publications", "attempts_log",
               "events", "quota", "metrics", "segments_used", "realtests", "logins"]


def mask(url: str) -> str:
    host = url.split("://", 1)[-1].split("/", 1)[0]
    return host[:6] + "…" + host[-14:] if len(host) > 22 else host


def probe(url: str, token: str, module=None, out: Callable[[str], None] = print) -> bool:
    """Testa NO TURSO REAL (ou no fake, se `module` for dado) cada capacidade que o Xhakinha usa. Só toca em uma tabela
    temporária própria (xh_probe_*), removida no final. Retorna True se tudo passou."""
    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn):
        try:
            detail = fn()
            results.append((name, True, str(detail or "")))
        except Exception as e:                                    # noqa: BLE001
            results.append((name, False, f"{type(e).__name__}: {str(e)[:160]}"))

    tbl = "xh_probe_" + _secrets.token_hex(4)
    st: dict = {}

    def connect():
        st["c"] = dbdriver.LibsqlConn(url, token, module=module)
        return "conectou"

    def version():
        return "SQLite " + st["c"].execute("SELECT sqlite_version() v").fetchone()["v"]

    def create():
        st["c"].execute(f"CREATE TABLE {tbl}(id INTEGER PRIMARY KEY AUTOINCREMENT, k TEXT UNIQUE, n INTEGER DEFAULT 0)")
        return "CREATE TABLE ok (token tem permissão de ESCRITA)"

    def ins():
        cur = st["c"].execute(f"INSERT INTO {tbl}(k,n) VALUES(?,?)", ("a", 1))
        assert cur.lastrowid == 1, f"lastrowid={cur.lastrowid}"
        return f"lastrowid={cur.lastrowid}"

    def rowc():
        cur = st["c"].execute(f"UPDATE {tbl} SET n=n+1 WHERE k=?", ("a",))
        c2 = st["c"].execute(f"UPDATE {tbl} SET n=n+1 WHERE k=?", ("zzz",))
        assert cur.rowcount == 1 and c2.rowcount == 0, f"rowcount {cur.rowcount}/{c2.rowcount}"
        return "rowcount 1/0"

    def names():
        r = st["c"].execute(f"SELECT * FROM {tbl} WHERE k=?", ("a",)).fetchone()
        assert r["k"] == "a" and r[2] == 2 and dict(r)["n"] == 2
        return "linhas por nome ok"

    def unique():
        try:
            st["c"].execute(f"INSERT INTO {tbl}(k,n) VALUES(?,?)", ("a", 9))
        except Exception as e:
            assert "UNIQUE" in str(e).upper(), str(e)[:100]
            return "UNIQUE reportado (dedupe da fila funciona)"
        raise AssertionError("UNIQUE não foi imposto")

    def returning():
        st["c"].execute(f"INSERT INTO {tbl}(k,n) VALUES('b',0)")
        rows = st["c"].execute(f"UPDATE {tbl} SET n=n+10 WHERE id=(SELECT id FROM {tbl} WHERE n=0 LIMIT 1) RETURNING *").fetchall()
        assert len(rows) == 1 and rows[0]["n"] == 10, rows
        return "UPDATE…RETURNING ok (claim atômico)"

    def upsert_guard():
        sql = (f"INSERT INTO {tbl}(k,n) VALUES('q',5) ON CONFLICT(k) DO UPDATE SET n=n+5 WHERE n+5<=8")
        a = st["c"].execute(sql).rowcount          # insere (1)
        b = st["c"].execute(sql).rowcount          # 5+5>8 -> 0
        assert (a, b) == (1, 0), (a, b)
        return "upsert com WHERE ok (cota atômica)"

    def script():
        st["c"].executescript(f"CREATE TABLE IF NOT EXISTS {tbl}_s(x INTEGER);\nINSERT INTO {tbl}_s VALUES(1);\n")
        assert st["c"].execute(f"SELECT COUNT(*) n FROM {tbl}_s").fetchone()["n"] == 1
        return "executescript ok"

    def visible():
        other = dbdriver.LibsqlConn(url, token, module=module)
        n = other.execute(f"SELECT COUNT(*) n FROM {tbl}").fetchone()["n"]
        other.close()
        assert n >= 3, n
        return "escrita visível por outra conexão (Render e Kaggle enxergam o mesmo dado)"

    def latency():
        import time
        t = time.time()
        for _ in range(5):
            st["c"].execute("SELECT 1")
        ms = (time.time() - t) / 5 * 1000
        return f"~{ms:.0f} ms por consulta"

    def cleanup():
        for t in (tbl, tbl + "_s"):
            st["c"].execute(f"DROP TABLE IF EXISTS {t}")
        return "tabelas de teste removidas"

    for n, f in (("conexão", connect), ("versão", version), ("criar tabela", create), ("INSERT/lastrowid", ins),
                 ("UPDATE/rowcount", rowc), ("linhas por nome", names), ("UNIQUE", unique), ("RETURNING", returning),
                 ("upsert condicional", upsert_guard), ("executescript", script), ("visibilidade", visible),
                 ("latência", latency)):
        check(n, f)
        if n in ("conexão", "criar tabela") and not results[-1][1]:
            break
    if "c" in st:
        check("limpeza", cleanup)
    out(f"Banco: {mask(url)}")
    for n, ok, d in results:
        out(f"  [{'OK ' if ok else 'FALHOU'}] {n}: {d}")
    good = all(ok for _, ok, _ in results)
    out("RESULTADO: " + ("TUDO OK — pode seguir para o init" if good else "HÁ FALHAS — NÃO prossiga; envie esta saída (não contém segredos)"))
    return good


def counts(conn) -> dict[str, int]:
    out = {}
    for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        out[r["name"]] = conn.execute(f"SELECT COUNT(*) n FROM {r['name']}").fetchone()["n"]
    return out


def init_schema(store_factory: Callable):
    """Cria o esquema (idempotente) abrindo o Store com XH_TURSO_INIT=1."""
    os.environ["XH_TURSO_INIT"] = "1"
    return store_factory()


def import_sqlite(conn, src_path: str | Path, out: Callable[[str], None] = print) -> dict[str, int]:
    """Copia um .db SQLite local (ex.: backup do Render ou snapshot do Drive descompactado) para o Turso.
    SEGURANÇA: recusa se o Turso já tiver QUALQUER dado de negócio (nunca mistura/sobrescreve); confere contagens no final."""
    have = counts(conn)
    business = {t: n for t, n in have.items() if t not in ("settings",) and n > 0}
    if business:
        raise RuntimeError("O Turso já contém dados (" + ", ".join(f"{t}={n}" for t, n in business.items()) +
                           "). Importação RECUSADA para não misturar fontes. O Turso é a fonte canônica.")
    src = sqlite3.connect(str(src_path))
    src.row_factory = sqlite3.Row
    try:
        if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("arquivo de origem falhou no integrity_check")
        src_tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        copied = {}
        for t in TABLE_ORDER:
            if t not in src_tables or t not in have:
                continue
            dst_cols = {r["name"] for r in conn.execute(f"SELECT name FROM pragma_table_info('{t}')").fetchall()}
            rows = src.execute(f"SELECT * FROM {t}").fetchall()
            for r in rows:
                cols = [k for k in r.keys() if k in dst_cols]
                conn.execute(f"INSERT OR REPLACE INTO {t}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [r[k] for k in cols])
            copied[t] = len(rows)
            out(f"  {t}: {len(rows)} linhas copiadas")
        after = counts(conn)
        bad = [t for t, n in copied.items() if after.get(t) != n and t != "settings"]
        if bad:
            raise RuntimeError("contagem divergente após importar: " + ", ".join(bad))
        out("Importação conferida (contagens iguais).")
        return copied
    finally:
        src.close()
