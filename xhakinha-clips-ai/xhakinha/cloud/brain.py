"""O "cérebro": roda o Engine existente (monitor, agenda, publicação, estoque) dentro de UM executor por vez.
 - posse exclusiva via lock distribuído "brain" com fencing (epoch);
 - SQLite local por execução, restaurado do último snapshot VERIFICADO do Drive, e salvo de volta periodicamente;
 - originais e clipes são enviados ao Drive (verificados por MD5) antes de qualquer limpeza local;
 - processamento pesado pode ser delegado a outros executores (Kaggle GPU/Colab) e ingerido com verificação de hash.
Se o lock é perdido (Fenced), o cérebro para IMEDIATAMENTE e não publica snapshot nem novas publicações."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from ..config import Settings
from ..engine import Engine
from ..retry import NotReadyYet, PermanentError, TransientError
from ..storage import sha256_file
from .coordinator import Fenced
from .drive_storage import DriveBackend, DriveFull
from .snapshots import CorruptState, SnapshotManager

log = logging.getLogger("xh.brain")
LOCK = "brain"


class Brain:
    def __init__(self, s: Settings, drive: DriveBackend, client, owner: str, *, remote_processing: bool = True,
                 remote_wait_s: float = 900.0, snapshot_every_s: float = 600.0, lock_ttl: float = 180.0,
                 engine: Engine | None = None, clock=time.time):
        self.s, self.drive, self.client, self.owner = s, drive, client, owner
        self.remote, self.remote_wait_s = remote_processing, remote_wait_s
        self.snapshot_every_s, self.lock_ttl, self.clock = snapshot_every_s, lock_ttl, clock
        self.fence: int | None = None
        self.snaps = SnapshotManager(drive, s.data_dir / "tmp_snap")
        self.engine: Engine | None = engine
        self.synced: set[str] = set()
        self.fenced = threading.Event()
        self.report: dict = {"restored": None, "snapshots": 0, "uploaded": 0, "ingested": 0, "fallback_local": 0}

    # ------------------------------------------------------------ ciclo de vida
    def acquire(self) -> bool:
        self.fence = self.client.lock_acquire(LOCK, self.owner, self.lock_ttl)
        return self.fence is not None

    def still_owner(self) -> bool:
        if self.fenced.is_set():
            return False
        try:
            self.client.lock_renew(LOCK, self.owner, self.fence, self.lock_ttl)
            return True
        except Fenced:
            self.fenced.set()
            return False

    def prepare(self) -> None:
        self.s.ensure_dirs()
        self.drive.ensure_layout()
        m = self.snaps.restore_latest(self.s.db_path)             # CorruptState => aborta: nunca inicia "vazio" por cima
        self.report["restored"] = m["file"] if m else None
        if self.engine is None:
            self.engine = Engine(self.s)
        self._install_publish_guard()
        if self.remote:
            self._install_remote()

    def snapshot(self) -> dict | None:
        if not self.still_owner():
            return None
        meta = self.snaps.create(self.engine.db.conn, self.fence, still_owner=self.still_owner)
        self.report["snapshots"] += 1
        try:
            self.snaps.prune(keep=12)
        except Exception as e:
            log.warning("prune de snapshots falhou: %s", e)
        return meta

    # ------------------------------------------------------------ sincronização de arquivos com o Drive
    def sync_files(self) -> int:
        """Envia originais e clipes locais ao Drive (idempotente, com MD5). Nunca apaga nada."""
        db, n = self.engine.db, 0
        for c in db.q("SELECT id,fingerprint,local_path FROM contents WHERE local_path IS NOT NULL AND fingerprint IS NOT NULL"):
            k = f"o{c['id']}"
            p = Path(c["local_path"])
            if k in self.synced or not p.is_file() or not self.still_owner():
                continue
            try:
                self.drive.upload(p, f"originals/{c['fingerprint'][:32]}{p.suffix}")
                self.synced.add(k); n += 1
            except DriveFull as e:
                db.event("error", "drive", f"Drive cheio: ingestão pausada ({e})")
                db.set("ingest_paused", 1)
                break
            except (TransientError, PermanentError) as e:
                db.event("warn", "drive", f"upload do original {c['id']} falhou: {e}")
        for cl in db.q("SELECT id,path,sha256 FROM clips WHERE path IS NOT NULL AND sha256 IS NOT NULL"):
            k = f"c{cl['id']}"
            p = Path(cl["path"])
            if k in self.synced or not p.is_file() or not self.still_owner():
                continue
            try:
                self.drive.upload(p, f"clips/pending/clip_{cl['id']}_{cl['sha256'][:12]}.mp4", expect_sha256=cl["sha256"])
                self.synced.add(k); n += 1
            except DriveFull as e:
                db.event("error", "drive", f"Drive cheio: {e}")
                break
            except (TransientError, PermanentError) as e:
                db.event("warn", "drive", f"upload do clipe {cl['id']} falhou: {e}")
        self.report["uploaded"] += n
        return n

    # ------------------------------------------------------------ processamento remoto
    def _install_remote(self) -> None:
        pl = self.engine.pl
        local = pl.h_process

        def h_process(job: dict):
            cid = job["payload"]["content_id"]
            c = pl.db.one("SELECT * FROM contents WHERE id=?", (cid,))
            if not c or not c["local_path"] or not c["fingerprint"] or not Path(c["local_path"]).is_file():
                return local(job)
            key = c["fingerprint"][:32]
            dp = f"originals/{key}{Path(c['local_path']).suffix}"
            try:
                self.drive.upload(Path(c["local_path"]), dp)
            except (TransientError, PermanentError) as e:
                pl.db.event("warn", "remote", f"original {cid} não está no Drive ({e}); processando localmente")
                return local(job)
            tkey = f"process:{key}"
            t = self.client.get_by_key(tkey)
            if t is None:
                cfg = pl.db.all_settings()
                used = [(r["start"], r["end"]) for r in pl.db.q("SELECT start,end FROM segments_used WHERE content_id=? OR fingerprint=?", (cid, c["fingerprint"]))]
                from .. import learning
                weights, _ = learning.learned_weights(pl.db)
                self.client.submit("process_content", {
                    "content_key": key, "drive_path": dp, "sha256": None, "cfg": cfg, "used": used, "weights": weights,
                    "max_clips": job["payload"].get("max_clips"), "whisper": pl.s.whisper_enabled, "whisper_model": pl.s.whisper_model},
                    key=tkey, priority=6, prefer=["kaggle-gpu", "colab"], est_seconds=max(300.0, (c["duration"] or 600) * 0.6))
                t = self.client.get_by_key(tkey)
            st = t["status"]
            if st == "done":
                return self._ingest(c, json.loads(t["result"]))
            if st in ("dead", "cancelled") or (st == "queued" and self.clock() - t["created"] > self.remote_wait_s):
                if st == "queued" and not self.client.cancel_if_queued(tkey):
                    raise NotReadyYet("tarefa remota acabou de ser assumida por outro executor", retry_after=60)
                self.report["fallback_local"] += 1
                pl.db.event("warn", "remote", f"processamento remoto de {cid} indisponível ({st}); usando o próprio executor")
                return local(job)
            raise NotReadyYet(f"processamento remoto em andamento ({st})", retry_after=120)

        pl.h_process = h_process

    def _install_publish_guard(self) -> None:
        pl = self.engine.pl
        orig = pl.h_publish

        def guarded(job: dict):
            if not self.still_owner():                        # nunca publica sem a posse do lock (evita duplicação entre cérebros)
                raise NotReadyYet("cérebro perdeu o lock; publicação adiada", retry_after=600)
            return orig(job)
        pl.h_publish = guarded

    def _ingest(self, c: dict, res: dict) -> None:
        db, pl = self.engine.db, self.engine.pl
        origin = "archive" if c["kind"] == "archive" else c["kind"] if c["kind"] in ("live", "upload") else "vod"
        ids = []
        for info in res["clips"]:
            s, e = info["start"], info["end"]
            row = db.one("SELECT * FROM clips WHERE content_id=? AND start=? AND end=?", (c["id"], s, e))
            if row and row["path"] and Path(row["path"]).is_file():
                ids.append(row["id"]); continue                      # idempotente (também cobre o fallback local)
            now = time.time()
            cid = row["id"] if row else db.x(
                "INSERT INTO clips(content_id,start,end,score,scores,category,status,transcript,origin,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (c["id"], s, e, info["score"], json.dumps(info["scores"]), info["category"], "processing", info["transcript"], origin, now, now))
            dest = pl.st.clip_path(cid)
            self.drive.download(info["path"], dest, sha256=info["sha256"])            # hash do executor remoto conferido
            db.x("UPDATE clips SET status='awaiting', path=?, sha256=?, size=?, duration=?, updated=? WHERE id=?",
                 (str(dest), info["sha256"], info["size"], info["duration"], time.time(), cid))
            db.x("INSERT INTO segments_used(content_id,fingerprint,start,end) VALUES(?,?,?,?)", (c["id"], c["fingerprint"], s, e))
            db.x("UPDATE contents SET clips_made=clips_made+1 WHERE id=?", (c["id"],))
            ids.append(cid)
        db.x("UPDATE contents SET status='processed', processed_at=?, error=NULL WHERE id=?", (time.time(), c["id"]))
        db.event("info", "remote", f"Conteúdo {c['id']}: {len(ids)} clipes recebidos de {res.get('executor')}", content=c["id"])
        self.report["ingested"] += len(ids)

    # ------------------------------------------------------------ execução
    def run(self, budget_s: float, *, tick_s: float = 20.0, stop_margin_s: float = 420.0, sleep=time.sleep) -> dict:
        """Executa até `budget_s` (menos a margem de saída). Retorna o relatório."""
        if not self.acquire():
            return {**self.report, "status": "outro_cerebro_ativo"}
        try:
            self.prepare()
        except CorruptState:
            self.client.lock_release(LOCK, self.owner, self.fence)
            raise
        self.engine.start()
        t0, last_snap = self.clock(), self.clock()
        status = "ok"
        try:
            while self.clock() - t0 < budget_s - stop_margin_s:
                if not self.still_owner():
                    status = "fenced"
                    break
                self.sync_files()
                if self.clock() - last_snap >= self.snapshot_every_s:
                    self.snapshot(); last_snap = self.clock()
                sleep(tick_s)
        finally:
            self.engine.stop()                                      # para workers ANTES do snapshot final
            if status != "fenced" and self.still_owner():
                self.sync_files()
                self.snapshot()
                self.client.lock_release(LOCK, self.owner, self.fence)
            elif status == "fenced":
                self.engine.db.event("error", "brain", "lock perdido: sem snapshot final nem novas publicações")
        return {**self.report, "status": status}
