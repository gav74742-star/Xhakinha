"""Diário de publicações no Drive (write-ahead). Resolve o risco de DUPLICAÇÃO entre sessões: o snapshot do SQLite pode estar
até alguns minutos defasado, mas cada mudança de estado de publicação (container/publish_id/enviado/publicado) é gravada
aqui NA HORA, em arquivos pequenos e imutáveis (`logs/pubjournal/p<id>-<ts>.json`). Ao iniciar uma sessão nova, o cérebro
reconcilia o banco restaurado com o diário antes de publicar qualquer coisa.
Só guarda campos de estado em lista branca: NUNCA tokens, upload_url assinada nem credenciais."""
from __future__ import annotations

import json
import re
import time

from ..retry import TransientError
from .drive_storage import DriveBackend

PREFIX = "logs/pubjournal"
STATE_KEYS = ("stage", "publish_id", "container_id", "media_id", "privacy", "created_ts", "upload_started", "reinit", "errors", "confirmed")
COLS = ("status", "remote_id", "public_id", "url", "visibility", "published_at", "needs_admin", "error")


def entry_name(pub_id: int, ts_ms: int) -> str:
    return f"{PREFIX}/p{pub_id:08d}-{ts_ms:015d}.json"


class PubJournal:
    def __init__(self, drive: DriveBackend, clock=time.time):
        self.drive, self.clock = drive, clock
        self.backlog: list[dict] = []
        self._last_ms = 0

    def _entry(self, pub: dict) -> dict:
        st = json.loads(pub.get("state") or "{}")
        e = {"pub_id": pub["id"], "clip_id": pub["clip_id"], "platform": pub["platform"], "ts": self.clock(),
             "state": {k: st[k] for k in STATE_KEYS if k in st}}
        for c in COLS:
            e[c] = pub.get(c)
        if e.get("error"):
            e["error"] = str(e["error"])[:200]
        return e

    def _write(self, e: dict) -> None:
        ms = max(int(e["ts"] * 1000), self._last_ms + 1)
        self._last_ms = ms
        self.drive.write_json(entry_name(e["pub_id"], ms), e)

    def record(self, pub: dict, strict: bool = True) -> None:
        """strict=True: se o Drive falhar, levanta TransientError (o passo irreversível seguinte NÃO acontece).
        strict=False (publicação já concluída): guarda no backlog e tenta de novo depois."""
        e = self._entry(pub)
        try:
            self._write(e)
        except Exception as ex:
            if strict:
                raise TransientError(f"diário de publicação no Drive indisponível ({type(ex).__name__}); passo adiado") from ex
            self.backlog.append(e)

    def flush(self) -> int:
        n = 0
        while self.backlog:
            try:
                self._write(self.backlog[0])
            except Exception:
                break
            self.backlog.pop(0)
            n += 1
        return n

    def latest(self) -> dict[int, dict]:
        """Última entrada de cada publicação (pelo nome, que ordena por tempo)."""
        best: dict[int, str] = {}
        for f in self.drive.listdir(PREFIX):
            m = re.search(r"p(\d{8})-(\d{15})\.json$", f["path"])
            if m:
                pid = int(m[1])
                if pid not in best or f["path"] > best[pid]:
                    best[pid] = f["path"]
        out = {}
        for pid, path in best.items():
            d = self.drive.read_json(path)
            if d:
                out[pid] = d
        return out

    def reconcile(self, db, refresh_clip=None) -> dict:
        """Aplica ao banco restaurado o que o diário sabe e o snapshot não. Nunca regride uma publicação já concluída."""
        rep = {"adopted": [], "published": []}
        for pid, e in self.latest().items():
            pub = db.one("SELECT * FROM publications WHERE id=?", (pid,))
            if not pub or pub["clip_id"] != e["clip_id"] or pub["platform"] != e["platform"]:
                continue                                          # outro histórico de banco: ignora
            if pub["status"] in ("published", "cancelled") or (pub["updated"] or 0) >= e["ts"]:
                continue
            st = json.loads(pub["state"] or "{}")
            st.update(e["state"])
            cols = {c: e.get(c) for c in COLS if e.get(c) is not None}
            sets = ["state=?", "updated=?"] + [f"{c}=?" for c in cols]
            db.x(f"UPDATE publications SET {', '.join(sets)} WHERE id=?", [json.dumps(st), time.time(), *cols.values(), pid])
            if e["status"] == "published":
                db.x("UPDATE jobs SET status='done', locked_by=NULL WHERE dedupe_key=? AND status IN ('pending','running')", (f"pub:{pid}",))
                if refresh_clip:
                    refresh_clip(pub["clip_id"])
                rep["published"].append(pid)
                db.event("warn", "journal", f"Publicação {pid} ({e['platform']}) já havia sido concluída antes da queda da sessão; "
                                            f"estado recuperado do diário (sem republicar)")
            else:
                rep["adopted"].append(pid)
                db.event("info", "journal", f"Publicação {pid}: estado '{e['status']}'/{e['state'].get('stage')} recuperado do diário")
        return rep
