"""Snapshots versionados e VERIFICADOS do SQLite no Drive.
 - arquivo `db-<ts>-e<epoch>.sqlite` + manifesto `.json` gravado POR ÚLTIMO (o manifesto é o ponteiro);
 - ordenação por (epoch do lock, ts): um "cérebro" antigo (fenced) nunca supera um mais novo;
 - restauração só aceita snapshot cujo sha256 e integrity_check conferem; se existirem snapshots mas nenhum válido,
   RECUSA iniciar vazio (evita sobrescrever o estado real)."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Callable

from .drive_storage import DriveBackend, sha256_file

P = "db/snapshots/"


class CorruptState(RuntimeError):
    pass


def _integrity(path: Path) -> bool:
    c = sqlite3.connect(str(path))
    try:
        return c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        c.close()


class SnapshotManager:
    def __init__(self, drive: DriveBackend, tmpdir: Path):
        self.drive, self.tmp = drive, Path(tmpdir)
        self.tmp.mkdir(parents=True, exist_ok=True)

    def create(self, conn: sqlite3.Connection, epoch: int, *, still_owner: Callable[[], bool] | None = None,
               tables: tuple[str, ...] = ("contents", "clips", "publications"),
               sanitize: Callable[[sqlite3.Connection], None] | None = None) -> dict:
        now = time.time()
        ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)) + f"{int(now * 1000) % 1000:03d}"      # ms: dois snapshots no mesmo segundo não colidem
        copy = self.tmp / f"snap-{ts}-e{epoch}.sqlite"
        dst = sqlite3.connect(str(copy))
        try:
            conn.backup(dst)                 # cópia consistente, sem parar escritas
        finally:
            dst.close()
        if sanitize:                                  # remove credenciais temporárias da CÓPIA (o banco vivo não muda)
            c = sqlite3.connect(str(copy))
            try:
                c.execute("PRAGMA secure_delete=ON")
                sanitize(c)
                c.commit()
                c.execute("VACUUM")                   # não deixa resíduo dos valores removidos nas páginas livres
            finally:
                c.close()
        if not _integrity(copy):
            copy.unlink(missing_ok=True)
            raise CorruptState("snapshot local falhou no integrity_check; NÃO enviado")
        gz = copy.with_suffix(".sqlite.gz")
        with open(copy, "rb") as a, gzip.open(gz, "wb", compresslevel=6) as b:
            shutil.copyfileobj(a, b)
        counts = {}
        c = sqlite3.connect(str(copy))
        try:
            for t in tables:
                try:
                    counts[t] = c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                except sqlite3.Error:
                    pass
        finally:
            c.close()
        name = f"db-{ts}-e{epoch:06d}"
        meta = {"file": f"{P}{name}.sqlite.gz", "sha256": sha256_file(gz), "size": gz.stat().st_size, "epoch": epoch,
                "ts": time.time(), "raw_sha256": sha256_file(copy), "integrity": "ok", "counts": counts}
        try:
            self.drive.upload(gz, meta["file"])
            if still_owner and not still_owner():
                from .coordinator import Fenced
                raise Fenced("lock do cérebro perdido durante o snapshot; manifesto NÃO publicado (arquivo fica órfão)")
            self.drive.write_json(f"{P}{name}.json", meta)         # ponteiro só no fim
        finally:
            copy.unlink(missing_ok=True)
            gz.unlink(missing_ok=True)
        return meta

    def manifests(self) -> list[dict]:
        out = []
        for f in self.drive.listdir(P.rstrip("/")):
            if f["path"].endswith(".json"):
                try:
                    m = self.drive.read_json(f["path"])
                    if m:
                        out.append(m)
                except Exception:
                    continue
        return sorted(out, key=lambda m: (m["epoch"], m["ts"]), reverse=True)

    def restore_latest(self, dest: Path) -> dict | None:
        ms = self.manifests()
        listing = self.drive.listdir(P.rstrip("/"))
        if not ms:
            if any(f["path"].endswith(".gz") for f in listing):
                raise CorruptState("há snapshots no Drive mas nenhum manifesto: recusando iniciar vazio")
            return None
        errors = []
        for m in ms:
            gz = self.tmp / "restore.sqlite.gz"
            try:
                self.drive.download(m["file"], gz, sha256=m["sha256"])
                raw = self.tmp / "restore.sqlite"
                with gzip.open(gz, "rb") as a, open(raw, "wb") as b:
                    shutil.copyfileobj(a, b)
                if sha256_file(raw) != m["raw_sha256"] or not _integrity(raw):
                    raise CorruptState("integridade do snapshot falhou")
                dest = Path(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                for ext in ("-wal", "-shm"):
                    Path(str(dest) + ext).unlink(missing_ok=True)
                os.replace(raw, dest)
                return m
            except Exception as e:                      # tenta a versão anterior
                errors.append(f"{m['file']}: {e}")
            finally:
                gz.unlink(missing_ok=True)
        raise CorruptState("nenhum snapshot válido: " + " | ".join(errors))

    def prune(self, keep: int = 10) -> int:
        ms = self.manifests()
        n = 0
        for m in ms[keep:]:
            for p in (m["file"], m["file"].replace(".sqlite.gz", ".json")):
                self.drive.delete(p)
            n += 1
        return n
