"""Armazenamento: produção (temporário) x permanente (clipes). Limpeza SEGURA com regras explícitas.

Regra de ouro: NADA é apagado dentro do pipeline. Só `Cleaner` apaga, e só quando
`can_delete_*` retorna True com um motivo registrado."""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from .config import Settings
from .db import Store
from .queue import JobQueue

H = 3600.0


def sha256_file(p: Path, block: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(block):
            h.update(chunk)
    return h.hexdigest()


def quick_fingerprint(p: Path, duration: float | None = None) -> str:
    """Impressão digital de conteúdo (não depende de nome): tamanho + duração + hash de 3 blocos."""
    size = p.stat().st_size
    h = hashlib.sha256()
    h.update(str(size).encode())
    h.update(f"{round(duration or 0)}".encode())
    with open(p, "rb") as f:
        for off in (0, max(0, size // 2 - 524288), max(0, size - 1048576)):
            f.seek(off)
            h.update(f.read(1 << 20))
    return h.hexdigest()


class Storage:
    def __init__(self, s: Settings, db: Store, q: JobQueue):
        self.s, self.db, self.q = s, db, q

    # caminhos ----------------------------------------------------------
    def download_path(self, content_id: int, ext: str = "mp4") -> Path:
        d = self.s.production_dir / "downloads"
        return d / f"c{content_id}.{ext}"

    def work_dir(self, content_id: int) -> Path:
        d = self.s.production_dir / "work" / f"c{content_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def clip_path(self, clip_id: int, when: float | None = None) -> Path:
        day = datetime.fromtimestamp(when or time.time(), self.s.tz).strftime("%Y/%m/%d")
        d = self.s.clips_dir / "stock" / day
        d.mkdir(parents=True, exist_ok=True)
        return d / f"clip_{clip_id}.mp4"

    def ready_path(self, pub_id: int, platform: str) -> Path:
        d = self.s.production_dir / "ready"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"pub{pub_id}_{platform}.mp4"

    def inside_managed(self, p: Path) -> bool:
        try:
            rp = p.resolve()
            return any(rp.is_relative_to(r.resolve()) for r in (self.s.production_dir, self.s.clips_dir))
        except Exception:
            return False

    # espaço ------------------------------------------------------------
    def usage(self) -> dict:
        def size(d: Path) -> int:
            t = 0
            for root, _, files in os.walk(d):
                for f in files:
                    try:
                        t += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            return t
        du = shutil.disk_usage(self.s.data_dir)
        return {
            "production_bytes": size(self.s.production_dir),
            "clips_bytes": size(self.s.clips_dir),
            "backups_bytes": size(self.s.backup_dir),
            "disk_total": du.total, "disk_free": du.free,
            "min_free_gb": self.s.min_free_gb,
            "ok": du.free / 1e9 >= self.s.min_free_gb,
        }

    def has_space(self, need_bytes: int = 0) -> bool:
        du = shutil.disk_usage(self.s.data_dir)
        ok = (du.free - need_bytes) / 1e9 >= self.s.min_free_gb
        if ok and self.s.max_disk_gb:
            u = self.usage()
            ok = (u["production_bytes"] + u["clips_bytes"] + need_bytes) / 1e9 <= self.s.max_disk_gb
        return ok

    # integridade ---------------------------------------------------------
    def verify_clip(self, clip: dict) -> bool:
        p = Path(clip["path"] or "")
        if not p.is_file() or (clip.get("size") and p.stat().st_size != clip["size"]):
            return False
        return not clip.get("sha256") or sha256_file(p) == clip["sha256"]

    def verify_all_stock(self) -> dict:
        bad = []
        for c in self.db.q("SELECT * FROM clips WHERE status IN ('awaiting','scheduled','retry_wait','publish_failed')"):
            if not self.verify_clip(c):
                bad.append(c["id"])
                self.db.event("error", "storage", f"Clipe {c['id']} corrompido/ausente", clip=c["id"])
        return {"corrupted": bad}

    # backup ------------------------------------------------------------
    def backup_db(self) -> Path:
        self.s.backup_dir.mkdir(parents=True, exist_ok=True)
        dest = self.s.backup_dir / f"xhakinha-{datetime.now(self.s.tz).strftime('%Y%m%d-%H%M%S')}.db"
        self.db.export_sqlite(dest)
        keep = int(self.db.get("backup_keep", 14))
        files = sorted(self.s.backup_dir.glob("xhakinha-*.db"))
        for old in files[:-keep]:
            old.unlink(missing_ok=True)
        return dest


class Cleaner:
    """Todas as exclusões do sistema passam por aqui."""

    def __init__(self, st: Storage):
        self.st, self.db, self.q, self.s = st, st.db, st.q, st.s

    # --- decisões (puras, testáveis) ------------------------------------
    def can_delete_original(self, content: dict, now: float | None = None) -> tuple[bool, str]:
        now = now or time.time()
        if content.get("protected"):
            return False, "conteúdo protegido (upload do administrador/teste real)"
        if not content.get("local_path"):
            return False, "sem arquivo"
        if content["status"] != "processed":
            return False, f"estado '{content['status']}' ainda não é 'processed' (falha/pendente: preservar p/ retentativa)"
        if self.q.open_for_content(content["id"]):
            return False, "há jobs abertos para este conteúdo"
        grace = float(self.db.get("original_grace_h", 24)) * H
        if now - (content.get("processed_at") or now) < grace:
            return False, "dentro do período de carência"
        return True, "processado com sucesso, sem jobs abertos, carência cumprida"

    def can_delete_clip(self, clip: dict, now: float | None = None) -> tuple[bool, str]:
        now = now or time.time()
        pubs = self.db.q("SELECT status, published_at FROM publications WHERE clip_id=?", (clip["id"],))
        if not pubs:
            ret = float(self.db.get("unpublished_retention_days", 0))
            if ret > 0 and now - clip["created"] > ret * 86400 and clip["status"] == "archived":
                return True, "arquivado e fora da retenção configurada"
            return False, "clipe ainda não publicado (estoque protegido)"
        if any(p["status"] != "published" and p["status"] != "cancelled" for p in pubs):
            return False, "há publicação pendente/falha/aguardando"
        if all(p["status"] == "cancelled" for p in pubs):
            return False, "sem publicação confirmada"
        ret = float(self.db.get("published_clip_retention_h", 0)) * H
        last = max((p["published_at"] or 0) for p in pubs)
        if now - last < ret:
            return False, "retenção pós-publicação"
        return True, "publicado e confirmado em todas as plataformas"

    # --- execução ------------------------------------------------------------
    def _rm(self, path: str | Path | None, why: str, ref: str) -> bool:
        if not path:
            return False
        p = Path(path)
        if not p.exists():
            return False
        if not self.st.inside_managed(p):
            self.db.event("error", "cleanup", f"Recusado apagar fora das pastas gerenciadas: {p}")
            return False
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            self.db.event("info", "cleanup", f"Apagado {ref}: {p.name} ({why})")
            return True
        except OSError as e:
            self.db.event("warn", "cleanup", f"Falha ao apagar {p}: {e}")
            return False

    def run(self, now: float | None = None) -> dict:
        now = now or time.time()
        out = {"originals": 0, "clips": 0, "work": 0, "ready": 0, "orphans": 0}
        for c in self.db.q("SELECT * FROM contents WHERE local_path IS NOT NULL AND status='processed'"):
            ok, why = self.can_delete_original(c, now)
            if ok and self._rm(c["local_path"], why, f"original c{c['id']}"):
                self.db.x("UPDATE contents SET local_path=NULL WHERE id=?", (c["id"],))
                out["originals"] += 1
            if ok or c["status"] == "processed":
                wd = self.s.production_dir / "work" / f"c{c['id']}"
                if wd.exists() and now - wd.stat().st_mtime > float(self.db.get("work_retention_h", 6)) * H \
                        and not self.q.open_for_content(c["id"]):
                    out["work"] += int(self._rm(wd, "intermediários de conteúdo processado", f"work c{c['id']}"))
        for cl in self.db.q("SELECT * FROM clips WHERE path IS NOT NULL AND status IN ('published','archived','awaiting','scheduled','retry_wait','publish_failed')"):
            ok, why = self.can_delete_clip(cl, now)
            if ok and self._rm(cl["path"], why, f"clipe {cl['id']}"):
                self.db.x("UPDATE clips SET path=NULL, status='published', updated=? WHERE id=?", (now, cl["id"]))
                out["clips"] += 1
        # versões por plataforma: só enquanto a publicação não estiver concluída
        for p in self.db.q("SELECT * FROM publications WHERE file_path IS NOT NULL AND status IN ('published','cancelled')"):
            if self._rm(p["file_path"], "publicação confirmada", f"versão pub{p['id']}"):
                self.db.x("UPDATE publications SET file_path=NULL WHERE id=?", (p["id"],))
                out["ready"] += 1
        # órfãos em ready/ (sem publicação ativa que os referencie) com 24h+
        ready = self.s.production_dir / "ready"
        active = {r["file_path"] for r in self.db.q("SELECT file_path FROM publications WHERE file_path IS NOT NULL")}
        for f in ready.glob("*.mp4"):
            if str(f) not in active and now - f.stat().st_mtime > 24 * H:
                out["orphans"] += int(self._rm(f, "órfão sem publicação", "ready"))
        return out

    def enforce_disk(self, now: float | None = None) -> int:
        """Se o disco apertou, libera SOMENTE o que é seguro (originais já processados, ignorando carência)."""
        if self.st.has_space():
            return 0
        n = 0
        old_grace = self.db.get("original_grace_h", 24)
        self.db.set("original_grace_h", 0)
        try:
            n = self.run(now)["originals"]
        finally:
            self.db.set("original_grace_h", old_grace)
        self.db.event("warn", "storage", f"Disco apertado: liberados {n} originais já processados")
        return n
