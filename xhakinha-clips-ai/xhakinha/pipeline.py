"""Pipeline: monitorar → adquirir → analisar → renderizar → agendar → publicar → confirmar → limpar.

REGRA: o pipeline NUNCA apaga o vídeo original. Só `storage.Cleaner` apaga, com `can_delete_*`."""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

from . import analysis, captions, editor, learning, scheduler
from .config import Settings
from .db import Store
from .media import probe
from .publish.instagram import InstagramPublisher
from .publish.tiktok import TikTokPublisher
from .queue import JobQueue
from .retry import NotReadyYet, PermanentError, QuotaExceeded, TransientError
from .sources import make_connector
from .storage import Cleaner, Storage, quick_fingerprint, sha256_file

HEAVY = {"process"}


class Pipeline:
    def __init__(self, s: Settings, db: Store, q: JobQueue, st: Storage, publishers: dict | None = None):
        self.s, self.db, self.q, self.st = s, db, q, st
        self.cleaner = Cleaner(st)
        self.pubs = publishers or {"tiktok": TikTokPublisher(s, db), "instagram": InstagramPublisher(s, db)}
        self.detector = analysis.haar_faces          # injetável nos testes
        self.clip_fetcher = None                     # nuvem: callable(clip)->"present|restored|retry|missing|corrupt|no_drive_copy"

    def ingest_paused(self) -> bool:
        """True quando o armazenamento (Drive) está no limite: NOVAS ingestões param; publicações e clipes já em estoque seguem."""
        return bool(self.db.get("ingest_paused"))

    def handle(self, job: dict):
        fn = getattr(self, "h_" + job["type"], None)
        if not fn:
            raise PermanentError(f"tipo de job desconhecido: {job['type']}")
        return fn(job)

    # ------------------------------------------------------------ monitor
    def h_monitor(self, job: dict):
        src = self.db.one("SELECT * FROM sources WHERE id=?", (job["payload"]["source_id"],))
        if not src or not src["enabled"] or self.ingest_paused():
            return
        now = time.time()
        try:
            conn = make_connector(src, self.s, self.db)
            known = {r["ext_id"]: r for r in self.db.q("SELECT ext_id, live_state, title, url, meta FROM contents WHERE source_id=?", (src["id"],))}
            for r in known.values():
                r["meta"] = json.loads(r.get("meta") or "{}")
            infos = conn.discover(known)
            self._ingest(src, infos, known)
            self.db.x("UPDATE sources SET last_check=?, last_ok=?, last_error=NULL WHERE id=?", (now, now, src["id"]))
        except QuotaExceeded as e:
            self.db.x("UPDATE sources SET last_check=?, last_error=?, interval_s=interval_s WHERE id=?", (now + (e.retry_after or 3600), str(e)[:300], src["id"]))
            self.db.event("warn", "monitor", f"[{src['name']}] cota: {e}")
        except TransientError as e:
            self.db.x("UPDATE sources SET last_check=?, last_error=? WHERE id=?", (now + 60, str(e)[:300], src["id"]))
            self.db.event("warn", "monitor", f"[{src['name']}] {e}")
        except PermanentError as e:          # um conector com problema não derruba os outros
            self.db.x("UPDATE sources SET last_check=?, last_error=? WHERE id=?", (now + 3600, str(e)[:300], src["id"]))
            self.db.event("error", "monitor", f"[{src['name']}] {e}")

    def _ingest(self, src: dict, infos, known: dict) -> None:
        now = time.time()
        dmin, dmax = float(self.db.get("vod_delay_min")), float(self.db.get("vod_delay_max"))
        authorized = bool(src["authorized"])
        for i in infos:
            row = self.db.one("SELECT * FROM contents WHERE source_id=? AND ext_id=?", (src["id"], i.ext_id))
            prev_live = (row or {}).get("live_state")
            if not row:
                status = "live" if i.live_state in ("live", "upcoming") else "discovered"
                cid = self.db.x("INSERT INTO contents(source_id,ext_id,kind,title,url,duration,status,live_state,ended_at,published_at,discovered,meta) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                (src["id"], i.ext_id, i.kind, i.title, i.url, i.duration, status, i.live_state, i.ended_at, i.published_at, now, json.dumps(i.meta)))
                self.db.event("info", "monitor", f"Novo conteúdo [{src['name']}]: {i.title[:60]}", kind=i.kind, state=i.live_state)
                row = self.db.one("SELECT * FROM contents WHERE id=?", (cid,))
                fresh = True
            else:
                fresh = False
                self.db.x("UPDATE contents SET live_state=?, ended_at=COALESCE(?,ended_at), duration=COALESCE(?,duration), title=? WHERE id=?",
                          (i.live_state, i.ended_at, i.duration, i.title or row["title"], row["id"]))
            if i.live_state in ("live", "upcoming"):
                continue
            if row["status"] == "queued":
                if authorized:
                    existing = self.db.one(
                        "SELECT id FROM jobs WHERE type=? AND content_id=? AND status IN ('pending','running') LIMIT 1",
                        ("acquire", row["id"]),
                    )
                    if not existing:
                        was_live = prev_live in ("live", "upcoming") or i.kind == "live" or bool(i.meta.get("stream_id"))
                        delay = random.uniform(dmin, dmax) * 60 if was_live else 0
                        self.q.enqueue(
                            "acquire",
                            {"content_id": row["id"], "deadline": now + 48 * 3600},
                            run_at=now + delay,
                            priority=3,
                            dedupe_key=f"acquire:{row['id']}",
                            content_id=row["id"],
                            max_attempts=8,
                        )
                continue
            if row["status"] in ("processed", "downloading", "downloaded", "analyzing", "skipped"):
                continue
            if i.kind == "live" and i.live_state == "ended" and i.ext_id.startswith("stream:"):
                self.db.x("UPDATE contents SET status='skipped', error='aguardando VOD (substituído por video:*)' WHERE id=?", (row["id"],))
                continue
            if not authorized:
                self.db.x("UPDATE contents SET status='discovered', error='fonte sem atestado de autorização' WHERE id=?", (row["id"],))
                continue
            was_live = prev_live in ("live", "upcoming") or i.kind == "live" or bool(i.meta.get("stream_id"))
            delay = random.uniform(dmin, dmax) * 60 if was_live else 0
            self.db.x("UPDATE contents SET status='queued' WHERE id=?", (row["id"],))
            self.q.enqueue("acquire", {"content_id": row["id"], "deadline": now + 48 * 3600}, run_at=now + delay, priority=3,
                           dedupe_key=f"acquire:{row['id']}", content_id=row["id"], max_attempts=8)

    def h_archive_scan(self, job: dict):
        src = self.db.one("SELECT * FROM sources WHERE id=?", (job["payload"]["source_id"],))
        if not src or not src["enabled"]:
            return
        if self.ingest_paused():
            raise NotReadyYet("ingestão pausada (armazenamento no limite)", retry_after=1800)
        conn = make_connector(src, self.s, self.db)
        page, found, pages = job["payload"].get("page"), 0, 0
        while pages < 10:
            infos, page = conn.list_archive(page, 50)
            pages += 1
            for i in infos:
                if self.db.one("SELECT 1 FROM contents WHERE source_id=? AND ext_id=?", (src["id"], i.ext_id)):
                    continue
                self.db.x("INSERT INTO contents(source_id,ext_id,kind,title,url,duration,status,published_at,discovered,meta) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (src["id"], i.ext_id, "archive", i.title, i.url, i.duration, "discovered", i.published_at, time.time(), json.dumps(i.meta)))
                found += 1
            if not page:
                self.db.x("UPDATE sources SET archive_scanned=? WHERE id=?", (time.time(), src["id"]))
                break
        else:
            self.q.enqueue("archive_scan", {"source_id": src["id"], "page": page}, dedupe_key=f"archscan:{src['id']}", priority=9)
        self.db.event("info", "archive", f"[{src['name']}] acervo: +{found} vídeos antigos encontrados")

    # ------------------------------------------------------------ adquirir
    def h_acquire(self, job: dict):
        cid = job["payload"]["content_id"]
        c = self.db.one("SELECT * FROM contents WHERE id=?", (cid,))
        if not c or c["status"] in ("processed", "skipped"):
            return
        # já temos o arquivo íntegro? (retentativa) -> não baixa de novo
        if c["local_path"] and Path(c["local_path"]).is_file():
            try:
                probe(Path(c["local_path"]))
                self.db.x("UPDATE contents SET status='downloaded' WHERE id=?", (cid,))
                self.q.enqueue("process", {"content_id": cid}, dedupe_key=f"process:{cid}", content_id=cid, priority=4)
                return
            except PermanentError:
                pass
        if self.ingest_paused():
            raise NotReadyYet("ingestão pausada (armazenamento no limite); download adiado, nada foi apagado", retry_after=1800)
        if not self.st.has_space(500 * 1024 * 1024):
            self.cleaner.enforce_disk()
            if not self.st.has_space(500 * 1024 * 1024):
                raise TransientError("espaço em disco insuficiente; aguardando liberar", retry_after=900)
        src = self.db.one("SELECT * FROM sources WHERE id=?", (c["source_id"],))
        self.db.x("UPDATE contents SET status='downloading', error=NULL WHERE id=?", (cid,))
        try:
            conn = make_connector(src, self.s, self.db)
            path = conn.download(c, self.st.download_path(cid))
            info = probe(path)
            if info["duration"] < 5:
                raise PermanentError("vídeo curto demais (<5s)")
            fp = quick_fingerprint(path, info["duration"])
            dup = self.db.one("SELECT id FROM contents WHERE fingerprint=? AND id!=? AND status IN ('processed','analyzing','downloaded')", (fp, cid))
            if dup and not c["protected"]:
                self.db.x("UPDATE contents SET status='skipped', error=? WHERE id=?", (f"duplicado do conteúdo {dup['id']} (impressão digital)", cid))
                self.db.event("info", "acquire", f"Conteúdo {cid} ignorado: duplicado de {dup['id']}")
                return
            self.db.x("UPDATE contents SET status='downloaded', local_path=?, duration=?, fingerprint=? WHERE id=?",
                      (str(path), info["duration"], fp, cid))
            self.db.attempt("content", cid, "acquire", True)
        except (NotReadyYet, TransientError, PermanentError) as e:
            # arquivo/estado preservados; só voltam a 'queued' para o painel refletir a verdade
            self.db.x("UPDATE contents SET status=?, error=? WHERE id=?", ("failed" if isinstance(e, PermanentError) else "queued", str(e)[:300], cid))
            self.db.attempt("content", cid, "acquire", False, str(e))
            raise
        self.q.enqueue("process", {"content_id": cid, **{k: v for k, v in job["payload"].items() if k in ("max_clips", "realtest_id")}},
                       dedupe_key=f"process:{cid}", content_id=cid, priority=4)

    # ------------------------------------------------------------ processar
    def process_content(self, cid: int, max_clips: int | None = None, origin: str | None = None) -> list[int]:
        c = self.db.one("SELECT * FROM contents WHERE id=?", (cid,))
        path = Path(c["local_path"] or "")
        if not path.is_file():
            # original ausente: reaquisição (sem desperdiçar retentativa de processamento)
            self.db.x("UPDATE contents SET status='queued', local_path=NULL WHERE id=?", (cid,))
            self.q.enqueue("acquire", {"content_id": cid}, dedupe_key=f"acquire:{cid}", content_id=cid, priority=4)
            raise NotReadyYet("original ausente; reaquisição agendada", retry_after=600)
        self.db.x("UPDATE contents SET status='analyzing', error=NULL WHERE id=?", (cid,))
        try:
            meta = probe(path)
            cfg = self.db.all_settings()
            weights, _ = learning.learned_weights(self.db)
            used = [(r["start"], r["end"]) for r in self.db.q(
                "SELECT start,end FROM segments_used WHERE content_id=? OR (fingerprint=? AND fingerprint IS NOT NULL)", (cid, c["fingerprint"]))]
            cands = []
            for t0, t1 in analysis.iter_chunks(meta["duration"], cfg["chunk_min"] * 60, cfg["chunk_overlap_s"]):
                cands += analysis.analyze_range(path, t0, t1, meta, cfg, used, weights, self.s.whisper_enabled, self.s.whisper_model, self.detector)
            cands.sort(key=lambda x: -x.score)
            chosen = analysis.select_best(cands, float(cfg["min_clip_score"]))
            if max_clips:
                chosen = sorted(sorted(chosen, key=lambda x: -x.score)[:max_clips], key=lambda x: x.start)
            ids = []
            origin = origin or ("archive" if c["kind"] == "archive" else c["kind"] if c["kind"] in ("live", "upload") else "vod")
            for cand in chosen:
                ids.append(self._render_candidate(c, path, meta, cand, origin))
            self.db.x("UPDATE contents SET status='processed', processed_at=?, error=NULL WHERE id=?", (time.time(), cid))
            self.db.event("info", "process", f"Conteúdo {cid} processado: {len(ids)} clipes (de {len(cands)} candidatos)", content=cid)
            return ids
        except Exception as e:
            # original PRESERVADO. Volta a 'downloaded' para permitir retentativa
            self.db.x("UPDATE contents SET status='downloaded', error=? WHERE id=?", (str(e)[:300], cid))
            raise

    def _render_candidate(self, c: dict, path: Path, meta: dict, cand, origin: str) -> int:
        s, e = round(cand.start, 1), round(cand.end, 1)
        row = self.db.one("SELECT * FROM clips WHERE content_id=? AND start=? AND end=?", (c["id"], s, e))
        if row and row["status"] != "processing" and row["path"] and Path(row["path"]).is_file():
            return row["id"]                                    # idempotente (retentativa não duplica)
        now = time.time()
        if row:
            clip_id = row["id"]
        else:
            clip_id = self.db.x("INSERT INTO clips(content_id,start,end,score,scores,category,status,transcript,origin,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                (c["id"], s, e, cand.score, json.dumps(cand.scores), cand.category, "processing", cand.transcript, origin, now, now))
        dest = self.st.clip_path(clip_id)
        info = editor.render_clip(path, dest, s, e - s, meta, self.detector)
        self.db.x("UPDATE clips SET status='awaiting', path=?, sha256=?, size=?, duration=?, updated=? WHERE id=?",
                  (str(dest), sha256_file(dest), info["size"], info["duration"], time.time(), clip_id))
        self.db.x("INSERT INTO segments_used(content_id,fingerprint,start,end) VALUES(?,?,?,?)", (c["id"], c["fingerprint"], s, e))
        self.db.x("UPDATE contents SET clips_made=clips_made+1 WHERE id=?", (c["id"],))
        return clip_id

    def h_process(self, job: dict):
        self.process_content(job["payload"]["content_id"], job["payload"].get("max_clips"))

    # ------------------------------------------------------------ publicar
    def prepare_platform_file(self, pub: dict, clip: dict) -> Path:
        if pub.get("file_path") and Path(pub["file_path"]).is_file():
            return Path(pub["file_path"])
        if self.clip_fetcher and (not clip.get("path") or not Path(clip["path"]).is_file()):
            r = self.clip_fetcher(clip)                       # reidrata do Drive (hash conferido); preserva ids e registros
            if r == "retry":
                raise TransientError("clipe sendo recuperado do Drive; nova tentativa em breve", retry_after=120)
            clip.update(self.db.one("SELECT * FROM clips WHERE id=?", (clip["id"],)))
        if not clip.get("path") or not self.st.verify_clip(clip):
            self.db.x("UPDATE clips SET error='arquivo do clipe ausente/corrompido' WHERE id=?", (clip["id"],))
            raise PermanentError("arquivo do clipe ausente ou corrompido (verificação de integridade)")
        dest = self.st.ready_path(pub["id"], pub["platform"])
        editor.apply_watermark(Path(clip["path"]), dest, self.db.get("handle_" + pub["platform"], ""),
                               float(self.db.get("watermark_opacity")), self.db.get("watermark_position"), float(self.db.get("watermark_scale")))
        self.db.x("UPDATE publications SET file_path=?, updated=? WHERE id=?", (str(dest), time.time(), pub["id"]))
        pub["file_path"] = str(dest)
        return dest

    def h_publish(self, job: dict):
        pid = job["payload"]["pub_id"]
        pub = self.db.one("SELECT * FROM publications WHERE id=?", (pid,))
        if not pub or pub["status"] in ("published", "cancelled"):
            return
        clip = self.db.one("SELECT * FROM clips WHERE id=?", (pub["clip_id"],))
        st = json.loads(pub["state"] or "{}")
        # cota diária de PUBLICAÇÃO: adia sem apagar nada e sem consumir tentativa
        if not st.get("force") and pub["status"] in ("scheduled", "retry_wait") and not pub["remote_id"] \
                and scheduler.cap_reached(self.db, pub["platform"], self.s.tz, exclude_pub=pid):
            _, nxt = scheduler.day_bounds(self.s.tz)
            self.db.x("UPDATE publications SET status='scheduled', scheduled_at=?, next_try=NULL, updated=? WHERE id=?", (nxt + 9 * 3600, time.time(), pid))
            self.db.event("info", "publish", f"Meta máxima do dia atingida ({pub['platform']}); pub {pid} adiada para amanhã (produção segue)")
            return
        publisher = self.pubs[pub["platform"]]
        try:
            self.prepare_platform_file(pub, clip)
            self.db.x("UPDATE publications SET attempts=attempts+1, error=NULL WHERE id=?", (pid,))
            for _ in range(6):                                    # avança etapas que não exigem espera
                pub = self.db.one("SELECT * FROM publications WHERE id=?", (pid,))
                res = publisher.step(pub, clip)
                pub = self.db.one("SELECT * FROM publications WHERE id=?", (pid,))
                self.db.attempt("publication", pid, f"{pub['platform']}:{res.status}", True, res.note)
                if res.status == "done":
                    scheduler.refresh_clip_status(self.db, clip["id"])
                    now = time.time()
                    for delay in (3600, 86400, 3 * 86400):
                        self.q.enqueue("metrics", {"pub_id": pid}, run_at=now + delay + 60, dedupe_key=f"metrics:{pid}:{delay}", priority=8)
                    self.db.event("info", "publish", f"{pub['platform']} publicado (pub {pid}) id={pub['public_id']}")
                    return res
                if res.status == "hold":
                    scheduler.refresh_clip_status(self.db, clip["id"])
                    return res
                if res.status == "wait":
                    if job["payload"].get("inline"):
                        return res
                    self._again(pid, res.wait_s)
                    return
            return res
        except (TransientError, PermanentError) as e:
            perm = isinstance(e, PermanentError)
            self.db.x("UPDATE publications SET status=?, error=?, needs_admin=?, updated=? WHERE id=?",
                      ("failed" if perm else "retry_wait", str(e)[:400], 1 if perm else 0, time.time(), pid))
            self.db.attempt("publication", pid, "publish", False, str(e))
            scheduler.refresh_clip_status(self.db, clip["id"])
            raise

    def _again(self, pid: int, wait_s: float):
        # re-agenda a MESMA publicação (idempotente: o estado guarda container/publish_id)
        self.db.x("UPDATE jobs SET status='done' WHERE status='running' AND dedupe_key=?", (f"pub:{pid}",))
        self.q.enqueue("publish", {"pub_id": pid}, run_at=time.time() + wait_s, dedupe_key=f"pub:{pid}", priority=2, max_attempts=12)

    def h_metrics(self, job: dict):
        pub = self.db.one("SELECT * FROM publications WHERE id=?", (job["payload"]["pub_id"],))
        if not pub or pub["status"] != "published":
            return
        m = self.pubs[pub["platform"]].fetch_metrics(pub)
        if not m:
            self.db.attempt("publication", pub["id"], "metrics", False, "métricas indisponíveis pela API oficial para esta publicação")
            return
        ret = None
        clip = self.db.one("SELECT duration FROM clips WHERE id=?", (pub["clip_id"],))
        if m.get("_avg_ms") and clip and clip["duration"]:
            ret = min(1.0, (m["_avg_ms"] / 1000.0) / clip["duration"])
        self.db.x("INSERT INTO metrics(publication_id,ts,views,likes,comments,shares,retention,raw) VALUES(?,?,?,?,?,?,?,?)",
                  (pub["id"], time.time(), m.get("views"), m.get("likes"), m.get("comments"), m.get("shares"), ret, json.dumps(m.get("raw") or {})))

    # ------------------------------------------------------------ manutenção
    def h_cleanup(self, job: dict):
        self.cleaner.run()
        self.cleaner.enforce_disk()

    def h_backup(self, job: dict):
        self.st.backup_db()
        self.st.verify_all_stock()

    # ------------------------------------------------------------ ações do administrador
    def retry_publication(self, pid: int) -> None:
        pub = self.db.one("SELECT * FROM publications WHERE id=?", (pid,))
        if not pub or pub["status"] == "published":
            return
        st = json.loads(pub["state"] or "{}")
        if not pub["remote_id"]:
            st["stage"] = "init" if pub["platform"] == "tiktok" else "container"
        self.db.x("UPDATE publications SET status='scheduled', scheduled_at=?, next_try=NULL, needs_admin=0, error=NULL, state=?, updated=? WHERE id=?",
                  (time.time(), json.dumps(st), time.time(), pid))
        self.db.x("UPDATE jobs SET status='done' WHERE dedupe_key=? AND status IN ('dead','pending')", (f"pub:{pid}",))
        self.q.enqueue("publish", {"pub_id": pid}, dedupe_key=f"pub:{pid}", priority=2, max_attempts=12)
        scheduler.refresh_clip_status(self.db, pub["clip_id"])
