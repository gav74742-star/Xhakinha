"""O "cérebro": roda o Engine existente (monitor, agenda, publicação, estoque) dentro de UM executor por vez.
 - posse exclusiva via lock distribuído "brain" com fencing (epoch);
 - SQLite local por execução, restaurado do último snapshot VERIFICADO do Drive, e salvo de volta periodicamente;
 - originais e clipes são enviados ao Drive (verificados por MD5) antes de qualquer limpeza local;
 - processamento pesado pode ser delegado a outros executores (Kaggle GPU/Colab) e ingerido com verificação de hash.
Se o lock é perdido (Fenced), o cérebro para IMEDIATAMENTE e não publica snapshot nem novas publicações."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from ..config import Settings
from ..engine import Engine
from ..retry import NotReadyYet, PermanentError, TransientError
from .. import scheduler
from ..storage import sha256_file, quick_fingerprint
from .coordinator import Fenced
from .drive_storage import DriveBackend, DriveFull, IntegrityError
from .journal import PubJournal
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
        self.journal = PubJournal(drive, clock)
        self.drive_full_until = 0.0
        self.last_storage_check = 0.0
        self.last_rehydrate = 0.0
        self.fenced = threading.Event()
        self.report: dict = {"restored": None, "snapshots": 0, "uploaded": 0, "ingested": 0, "fallback_local": 0,
                       "rehydrated": 0, "journal_applied": 0}

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
        if self.s.use_turso:
            # Turso é a fonte canônica: NUNCA restaurar snapshot antigo por cima de dados novos. Snapshots do Drive = só backup.
            m = None
            self.report["state_backend"] = "turso"
        else:
            m = self.snaps.restore_latest(self.s.db_path)         # CorruptState => aborta: nunca inicia "vazio" por cima
        self.report["restored"] = m["file"] if m else None
        if self.engine is None:
            self.engine = Engine(self.s)
        self._install_publish_guard()
        self._install_journal()
        self.engine.pl.clip_fetcher = self.rehydrate_clip
        rep = self.journal.reconcile(self.engine.db, lambda cid: scheduler.refresh_clip_status(self.engine.db, cid))
        self.report["journal_applied"] = len(rep["adopted"]) + len(rep["published"])
        self.rehydrate_pending()
        if self.remote:
            self._install_remote()

    @staticmethod
    def _sanitize(c: sqlite3.Connection) -> None:
        """URLs de upload assinadas do TikTok são credenciais temporárias: não vão para o Drive. (Se faltarem ao restaurar,
        o publicador reinicia o init com segurança, ou acompanha o status quando o upload já havia começado.)"""
        c.execute("UPDATE publications SET state=json_remove(state,'$.upload_url') WHERE state LIKE '%upload_url%'")

    def snapshot(self) -> dict | None:
        if not self.still_owner():
            return None
        meta = self.snaps.create(self.engine.db.backup_source(), self.fence, still_owner=self.still_owner, sanitize=self._sanitize)
        self.report["snapshots"] += 1
        try:
            self.snaps.prune(keep=12)
        except Exception as e:
            log.warning("prune de snapshots falhou: %s", e)
        return meta

    # ------------------------------------------------------------ armazenamento / ingestão
    def set_ingest_paused(self, paused: bool, reason: str = "") -> None:
        db = self.engine.db
        if bool(db.get("ingest_paused")) == paused:
            return
        db.set("ingest_paused", 1 if paused else 0)
        db.set("ingest_paused_reason", reason if paused else "")
        db.event("warn" if paused else "info", "drive",
                 ("Ingestão PAUSADA: " + reason + " (publicações e estoque continuam; nada foi apagado)") if paused
                 else "Ingestão RETOMADA: há espaço suficiente no Drive")

    def check_storage(self) -> dict | None:
        """Pausa novas ingestões abaixo de drive_min_free_gb e só retoma acima de drive_resume_free_gb (histerese).
        Só retoma pausas que ELE mesmo criou (motivo 'drive:'); pausa manual do administrador permanece."""
        db = self.engine.db
        try:
            q = self.drive.quota()
        except Exception as e:
            db.event("warn", "drive", f"não foi possível consultar a cota do Drive: {e}")
            return None
        self.last_storage_check = self.clock()
        if not q.get("limit"):
            return {"free_gb": None}
        free = (q["limit"] - q["usage"]) / 1e9
        lo, hi = float(db.get("drive_min_free_gb")), float(db.get("drive_resume_free_gb"))
        paused, why = bool(db.get("ingest_paused")), str(db.get("ingest_paused_reason") or "")
        if not paused and free < lo:
            self.set_ingest_paused(True, f"drive: {free:.2f} GB livres (< {lo} GB de segurança)")
        elif paused and why.startswith("drive:") and free >= hi:
            self.set_ingest_paused(False)
        return {"free_gb": free}

    # ------------------------------------------------------------ sincronização de arquivos com o Drive
    def sync_files(self) -> int:
        """Envia ao Drive o que ainda não tem cópia (drive_path vazio). UMA cópia por arquivo: nada é reenviado nem duplicado.
        Nunca apaga nada."""
        db, n = self.engine.db, 0
        if self.clock() < self.drive_full_until:
            return 0
        try:
            for c in db.q("SELECT id,fingerprint,local_path,duration FROM contents WHERE local_path IS NOT NULL AND fingerprint IS NOT NULL AND drive_path IS NULL"):
                p = Path(c["local_path"])
                if not p.is_file() or not self.still_owner():
                    continue
                try:
                    actual = quick_fingerprint(p, c["duration"])
                except OSError as e:
                    db.event(
                        "warn", "drive",
                        f"leitura do original {c['id']} falhou; upload adiado: {e}"
                    )
                    continue

                if actual != c["fingerprint"]:
                    db.event(
                        "error", "integrity",
                        f"original {c['id']} diverge do fingerprint registrado; upload bloqueado"
                    )
                    continue

                dp = f"originals/{c['fingerprint'][:32]}{p.suffix}"
                try:
                    self.drive.upload(p, dp)
                    db.x("UPDATE contents SET drive_path=? WHERE id=?", (dp, c["id"])); n += 1
                except (TransientError, PermanentError) as e:
                    if isinstance(e, DriveFull):
                        raise
                    db.event("warn", "drive", f"upload do original {c['id']} falhou: {e}")
            for cl in db.q("SELECT id,path,sha256 FROM clips WHERE path IS NOT NULL AND sha256 IS NOT NULL AND drive_path IS NULL"):
                p = Path(cl["path"])
                if not p.is_file() or not self.still_owner():
                    continue
                dp = f"clips/pending/clip_{cl['id']}_{cl['sha256'][:12]}.mp4"
                try:
                    self.drive.upload(p, dp, expect_sha256=cl["sha256"])
                    db.x("UPDATE clips SET drive_path=? WHERE id=?", (dp, cl["id"])); n += 1
                except (TransientError, PermanentError) as e:
                    if isinstance(e, DriveFull):
                        raise
                    db.event("warn", "drive", f"upload do clipe {cl['id']} falhou: {e}")
        except DriveFull as e:
            self.drive_full_until = self.clock() + 900
            self.set_ingest_paused(True, f"drive: cheio ({e})")
        self.report["uploaded"] += n
        return n

    # ------------------------------------------------------------ reidratação de clipes
    def rehydrate_original(self, content: dict) -> str:
        """Restaura o original do Drive após validar seu fingerprint.
        Retorna: present | restored | retry | missing | corrupt.
        Nunca altera nem remove a cópia remota.
        """
        db, st = self.engine.db, self.engine.st
        cid = content["id"]
        fingerprint = content.get("fingerprint")
        drive_path = content.get("drive_path")
        duration = content.get("duration")

        if not fingerprint or not drive_path:
            db.event("error", "rehydrate",
                     f"original {cid} sem fingerprint ou cópia registrada no Drive")
            return "missing"

        current = Path(content.get("local_path") or "")
        if content.get("local_path") and current.is_file():
            try:
                if quick_fingerprint(current, duration) == fingerprint:
                    return "present"
            except OSError:
                pass

        try:
            info = self.drive.stat(drive_path)
        except TransientError as e:
            db.event("warn", "rehydrate",
                     f"consulta do original {cid} falhou: {e}")
            return "retry"
        except PermanentError as e:
            db.event("error", "rehydrate",
                     f"original {cid} indisponível no Drive: {e}")
            return "missing"

        if not info:
            db.event("error", "rehydrate",
                     f"original {cid} ausente no Drive: {drive_path}")
            return "missing"

        size = int(info.get("size") or 0)
        if not st.has_space(size):
            db.event("warn", "rehydrate",
                     f"sem espaço local para restaurar original {cid}")
            return "retry"

        ext = Path(drive_path).suffix.lstrip(".") or "mp4"
        dest = st.download_path(cid, ext)
        temp = dest.with_name(dest.name + ".rehydrate")
        part = temp.with_suffix(temp.suffix + ".part")

        try:
            self.drive.download(drive_path, temp)

            if quick_fingerprint(temp, duration) != fingerprint:
                db.event("error", "rehydrate",
                         f"fingerprint divergente no original {cid}; download descartado")
                return "corrupt"

            temp.replace(dest)
            db.x(
                "UPDATE contents SET local_path=? WHERE id=?",
                (str(dest), cid),
            )
            self.report["originals_rehydrated"] = (
                self.report.get("originals_rehydrated", 0) + 1
            )
            return "restored"

        except IntegrityError as e:
            db.event("error", "rehydrate",
                     f"original {cid} corrompido no Drive: {e}")
            return "corrupt"
        except TransientError as e:
            db.event("warn", "rehydrate",
                     f"restauração do original {cid} será repetida: {e}")
            return "retry"
        except PermanentError as e:
            db.event("error", "rehydrate",
                     f"original {cid} indisponível: {e}")
            return "missing"
        except OSError as e:
            db.event("warn", "rehydrate",
                     f"falha local ao restaurar original {cid}: {e}")
            return "retry"
        finally:
            temp.unlink(missing_ok=True)
            part.unlink(missing_ok=True)

    def rehydrate_clip(self, clip: dict) -> str:
        """Garante o arquivo local do clipe a partir do Drive. Mantém id/registros; confere sha256; não reenvia nada.
        Retorna: present | restored | retry | no_drive_copy | missing | corrupt."""
        db, st = self.engine.db, self.engine.st
        p = Path(clip["path"] or "")
        if clip["path"] and p.is_file() and (not clip.get("size") or p.stat().st_size == clip["size"]):
            return "present"
        dp = clip.get("drive_path")
        if not dp:
            db.event("error", "rehydrate", f"clipe {clip['id']} sem cópia no Drive e sem arquivo local (perdido com a sessão anterior)")
            db.x("UPDATE clips SET error=? WHERE id=?", ("arquivo local ausente e sem cópia no Drive", clip["id"]))
            return "no_drive_copy"
        dest = st.clip_path(clip["id"], clip.get("created"))
        if not st.has_space(int(clip.get("size") or 0)):
            raise TransientError("sem espaço local para reidratar o clipe", retry_after=600)
        try:
            self.drive.download(dp, dest, sha256=clip.get("sha256"))
        except IntegrityError as e:
            db.event("error", "rehydrate", f"clipe {clip['id']} corrompido no Drive ({dp}): {e}")
            db.x("UPDATE clips SET error=? WHERE id=?", (f"cópia do Drive não confere com o hash: {e}"[:300], clip["id"]))
            return "corrupt"
        except TransientError as e:
            db.event("warn", "rehydrate", f"clipe {clip['id']}: download falhou agora ({e}); tentará de novo")
            return "retry"
        except PermanentError as e:
            db.event("error", "rehydrate", f"clipe {clip['id']}: {e}")
            db.x("UPDATE clips SET error=? WHERE id=?", (str(e)[:300], clip["id"]))
            return "missing"
        db.x("UPDATE clips SET path=?, error=NULL, updated=? WHERE id=?", (str(dest), time.time(), clip["id"]))
        self.report["rehydrated"] += 1
        return "restored"

    def rehydrate_pending(self, horizon_s: float = 6 * 3600, limit: int = 20) -> int:
        """Baixa os clipes que serão publicados nas próximas `horizon_s` e não estão no disco (o resto é buscado sob demanda)."""
        db, now, n = self.engine.db, self.clock(), 0
        self.last_rehydrate = now
        rows = db.q("SELECT DISTINCT c.* FROM clips c JOIN publications p ON p.clip_id=c.id "
                    "WHERE p.status IN ('scheduled','retry_wait','uploading','processing') AND COALESCE(p.next_try,p.scheduled_at)<=? "
                    "ORDER BY COALESCE(p.next_try,p.scheduled_at) LIMIT ?", (now + horizon_s, limit))
        for c in rows:
            try:
                if self.rehydrate_clip(c) == "restored":
                    n += 1
            except TransientError:
                break
        return n

    # ------------------------------------------------------------ diário de publicações
    def _install_journal(self) -> None:
        for pub in self.engine.pl.pubs.values():
            orig = pub.save

            def save(p, st, _o=orig, **cols):
                _o(p, st, **cols)
                self.journal.record(p, strict=cols.get("status") != "published")
            pub.save = save

    # ------------------------------------------------------------ processamento remoto
    def _install_remote(self) -> None:
        pl = self.engine.pl
        local = pl.h_process

        def fallback_local(job: dict, cid: int, c: dict, reason: str):
            current = pl.db.one(
                "SELECT * FROM contents WHERE id=?", (cid,)
            )
            if current:
                c = dict(current)

            local_path = c.get("local_path")
            local_exists = bool(
                local_path and Path(local_path).is_file()
            )

            if c.get("drive_path"):
                # Validar também arquivos locais existentes, pois podem
                # estar incompletos ou corrompidos.
                result = self.rehydrate_original(c)

                if result == "retry":
                    raise NotReadyYet(
                        f"restauração do original {cid} pendente; "
                        "fallback local adiado",
                        retry_after=300,
                    )

                if result in ("missing", "corrupt"):
                    raise PermanentError(
                        f"original {cid} irrecuperável no Drive: {result}"
                    )

                if result not in ("restored", "present"):
                    raise PermanentError(
                        f"estado inesperado na restauração do original "
                        f"{cid}: {result}"
                    )

                current = pl.db.one(
                    "SELECT * FROM contents WHERE id=?", (cid,)
                )
                if not current:
                    raise PermanentError(
                        f"conteúdo {cid} desapareceu após restaurar o original"
                    )
                c = dict(current)

            elif not local_exists:
                raise PermanentError(
                    f"original {cid} indisponível: sem cópia local ou no Drive"
                )

            self.report["fallback_local"] += 1
            pl.db.event(
                "warn", "remote",
                f"processamento remoto de {cid} indisponível "
                f"({reason}); usando o próprio executor"
            )
            return local(job)

        def h_process(job: dict):
            cid = job["payload"]["content_id"]
            row = pl.db.one(
                "SELECT * FROM contents WHERE id=?", (cid,)
            )
            if not row:
                return local(job)

            c = dict(row)
            fingerprint = c.get("fingerprint")
            if not fingerprint:
                return local(job)

            key = fingerprint[:32]
            local_path = c.get("local_path")
            local_exists = bool(
                local_path and Path(local_path).is_file()
            )
            dp = c.get("drive_path")

            # O executor remoto pode baixar diretamente do Drive.
            if not dp:
                if not local_exists:
                    return local(job)

                try:
                    actual = quick_fingerprint(
                        Path(local_path), c.get("duration")
                    )
                except OSError as e:
                    raise TransientError(
                        f"não foi possível validar o original {cid}; "
                        f"upload adiado: {e}"
                    ) from e

                if actual != fingerprint:
                    pl.db.event(
                        "error", "integrity",
                        f"original {cid} diverge do fingerprint registrado; "
                        "processamento remoto bloqueado"
                    )
                    raise PermanentError(
                        f"original {cid} corrompido ou alterado localmente; "
                        "fingerprint divergente"
                    )

                dp = f"originals/{key}{Path(local_path).suffix}"
                try:
                    self.drive.upload(Path(local_path), dp)
                    pl.db.x(
                        "UPDATE contents SET drive_path=? WHERE id=?",
                        (dp, cid),
                    )
                except (TransientError, PermanentError) as e:
                    pl.db.event(
                        "warn", "remote",
                        f"original {cid} não está no Drive ({e}); "
                        "processando localmente"
                    )
                    return local(job)

            tkey = f"process:{key}"
            t = self.client.get_by_key(tkey)

            if t is None:
                cfg = pl.db.all_settings()
                used = [
                    (r["start"], r["end"])
                    for r in pl.db.q(
                        "SELECT start,end FROM segments_used "
                        "WHERE content_id=? OR fingerprint=?",
                        (cid, fingerprint),
                    )
                ]
                from .. import learning
                weights, _ = learning.learned_weights(pl.db)

                self.client.submit(
                    "process_content",
                    {
                        "content_key": key,
                        "drive_path": dp,
                        "sha256": None,
                        "cfg": cfg,
                        "used": used,
                        "weights": weights,
                        "max_clips": job["payload"].get("max_clips"),
                        "whisper": pl.s.whisper_enabled,
                        "whisper_model": pl.s.whisper_model,
                    },
                    key=tkey,
                    priority=6,
                    prefer=["kaggle-gpu", "colab"],
                    est_seconds=max(
                        300.0, (c["duration"] or 600) * 0.6
                    ),
                )
                t = self.client.get_by_key(tkey)

            st = t["status"]

            if st == "done":
                return self._ingest(c, json.loads(t["result"]))

            if st in ("dead", "cancelled") or (
                st == "queued"
                and self.clock() - t["created"] > self.remote_wait_s
            ):
                if st == "queued" and not self.client.cancel_if_queued(tkey):
                    raise NotReadyYet(
                        "tarefa remota acabou de ser assumida "
                        "por outro executor",
                        retry_after=60,
                    )

                return fallback_local(job, cid, c, st)

            raise NotReadyYet(
                f"processamento remoto em andamento ({st})",
                retry_after=120,
            )

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
            db.x("UPDATE clips SET status='awaiting', path=?, sha256=?, size=?, duration=?, drive_path=?, updated=? WHERE id=?",
                 (str(dest), info["sha256"], info["size"], info["duration"], info["path"], time.time(), cid))
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
                self.journal.flush()
                if self.clock() - self.last_storage_check >= 300:
                    self.check_storage()
                if self.clock() - self.last_rehydrate >= 300:
                    self.rehydrate_pending()
                if self.clock() - last_snap >= self.snapshot_every_s:
                    self.snapshot(); last_snap = self.clock()
                sleep(tick_s)
        finally:
            self.engine.stop()                                      # para workers ANTES do snapshot final
            if status != "fenced" and self.still_owner():
                self.sync_files()
                self.journal.flush()
                self.snapshot()
                self.client.lock_release(LOCK, self.owner, self.fence)
            elif status == "fenced":
                self.engine.db.event("error", "brain", "lock perdido: sem snapshot final nem novas publicações")
        return {**self.report, "status": status}
