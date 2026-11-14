"""Botão TESTAR PROCESSAMENTO REAL: fonte autorizada real → 1 clipe → envio real → status real.
Nunca simula sucesso. Nunca altera/exclui o original (conteúdo marcado como protegido)."""
from __future__ import annotations

import json
import threading
import time
import traceback

from . import captions, scheduler
from .retry import PermanentError, XhError
from .sources import make_connector


class RealTest:
    def __init__(self, engine):
        self.e, self.db = engine, engine.db
        self.lock = threading.Lock()

    def latest(self) -> dict | None:
        r = self.db.one("SELECT * FROM realtests ORDER BY id DESC LIMIT 1")
        if r:
            r["steps"], r["result"] = json.loads(r["steps"]), json.loads(r["result"])
        return r

    def start(self, platform: str | None = None) -> int:
        cur = self.db.one("SELECT id FROM realtests WHERE status='running'")
        if cur:
            raise PermanentError("já existe um teste em andamento")
        rid = self.db.x("INSERT INTO realtests(started,status,platform) VALUES(?,?,?)", (time.time(), "running", platform))
        threading.Thread(target=self._run, args=(rid, platform), daemon=True).start()
        return rid

    # -------------------------------------------------------------- util
    def _save(self, rid, steps, result, status=None):
        self.db.x("UPDATE realtests SET steps=?, result=?, status=COALESCE(?,status), finished=CASE WHEN ? IN ('done','failed','awaiting_confirmation') THEN ? ELSE finished END WHERE id=?",
                  (json.dumps(steps), json.dumps(result, default=str), status, status, time.time(), rid))

    def _run(self, rid: int, platform: str | None, resume_pub: int | None = None):
        steps: list[dict] = []
        res: dict = {"t0": time.time()}

        def step(name: str, ok: bool | None, detail: str = ""):
            steps.append({"name": name, "ok": ok, "detail": detail[:400], "t": round(time.time() - res["t0"], 1)})
            self._save(rid, steps, res)

        try:
            # 1-2. fonte real autorizada
            src = None
            for s in self.db.q("SELECT * FROM sources WHERE enabled=1 AND authorized=1 AND platform IN ('youtube','twitch','instagram') ORDER BY id"):
                t = make_connector(s, self.e.s, self.db).test()
                if t["ok"]:
                    src = s
                    break
                step(f"Testar conexão da fonte '{s['name']}'", False, t["detail"])
            if not src:
                step("Selecionar fonte conectada e autorizada", False, "Nenhuma fonte conectada/autorizada. Cadastre e teste uma fonte na aba Fontes.")
                return self._end(rid, steps, res, "failed")
            step("Selecionar fonte conectada e autorizada", True, f"{src['name']} ({src['platform']})")
            conn = make_connector(src, self.e.s, self.db)
            # 3. escolher vídeo (curto, p/ economizar recursos)
            infos, _ = conn.list_archive(None, 25)
            infos = [i for i in infos if i.live_state not in ("live", "upcoming")]
            good = [i for i in infos if i.duration is None or 180 <= i.duration <= 40 * 60] or infos
            if not good:
                step("Escolher vídeo real do canal", False, "Nenhum vídeo acessível encontrado")
                return self._end(rid, steps, res, "failed")
            good.sort(key=lambda i: i.duration or 1e9)
            v = good[0]
            step("Escolher vídeo real do canal", True, f"{v.title[:80]} — {int(v.duration or 0)}s — {v.url}")
            res["source_video"] = {"title": v.title, "url": v.url, "duration": v.duration, "source": src["name"]}
            row = self.db.one("SELECT * FROM contents WHERE source_id=? AND ext_id=?", (src["id"], v.ext_id))
            if row:
                cid = row["id"]
                self.db.x("UPDATE contents SET protected=1 WHERE id=?", (cid,))
            else:
                cid = self.db.x("INSERT INTO contents(source_id,ext_id,kind,title,url,duration,status,discovered,protected,meta) VALUES(?,?,?,?,?,?,?,?,1,?)",
                                (src["id"], v.ext_id, "vod", v.title, v.url, v.duration, "queued", time.time(), json.dumps(v.meta)))
            # 4. aquisição permitida
            t = time.time()
            self.e.pl.h_acquire({"payload": {"content_id": cid}, "id": 0})
            for j in self.db.q("SELECT id FROM jobs WHERE content_id=? AND status='pending' AND type='process'", (cid,)):
                self.db.x("UPDATE jobs SET status='done' WHERE id=?", (j["id"],))           # processamos inline abaixo
            c = self.db.one("SELECT * FROM contents WHERE id=?", (cid,))
            if c["status"] == "skipped":
                step("Obter conteúdo (integração permitida)", True, "já existia/duplicado: usando cópia anterior")
            step("Obter conteúdo (integração permitida)", True, f"{round(time.time() - t, 1)}s")
            # 5-9. IA + 1 clipe 9:16
            t = time.time()
            ids = self.e.pl.process_content(cid, max_clips=1, origin="realtest")
            if not ids:
                step("Análise de IA e seleção do momento", False, "Nenhum momento passou do padrão mínimo de qualidade (nada foi inventado)")
                return self._end(rid, steps, res, "failed")
            clip = self.db.one("SELECT * FROM clips WHERE id=?", (ids[0],))
            step("Análise de IA e seleção do momento", True, f"{clip['start']:.0f}s–{clip['end']:.0f}s, nota {clip['score']} ({clip['category']})")
            step("Render 9:16 H.264, áudio normalizado, enquadramento", True, f"{round(time.time() - t, 1)}s, {clip['duration']:.0f}s de vídeo")
            res.update(clip_id=clip["id"], moment=[clip["start"], clip["end"]], score=clip["score"], preview=f"/api/clips/{clip['id']}/video")
            # 10-12. descrição + plataforma
            plats = [p for p in ([platform] if platform else ["tiktok", "instagram"]) if self.e.pl.pubs[p].connected()]
            if not plats:
                step("Selecionar plataforma de teste", False, "Nenhuma conta conectada (TikTok/Instagram). Envio NÃO TESTADO.")
                return self._end(rid, steps, res, "failed")
            plat = plats[0]
            cap = captions.generate(clip["category"], clip["transcript"] or "", src["name"], plat, ollama_url=self.e.s.ollama_url,
                                    ollama_model=self.e.s.ollama_model, credit=bool(self.db.get("credit_line")))
            res.update(description=cap["description"], hashtags=cap["hashtags"], caption_by=cap["generated_by"], platform=plat)
            step("Descrição e hashtags", True, f"gerado por: {cap['generated_by']}")
            self._publish(rid, steps, res, clip, plat, cap, step)
        except XhError as e:
            step("Erro", False, str(e))
            self._end(rid, steps, res, "failed")
        except Exception as e:  # noqa: BLE001
            step("Erro inesperado", False, f"{type(e).__name__}: {e}")
            self.db.event("error", "realtest", traceback.format_exc()[-800:])
            self._end(rid, steps, res, "failed")

    def _publish(self, rid, steps, res, clip, plat, cap, step):
        e = self.e
        st = {"stage": "init" if plat == "tiktok" else "container", "caption": cap["description"], "hashtags": cap["hashtags"],
              "force": True, "hold": plat == "instagram", "realtest": rid}
        now = time.time()
        old = self.db.one("SELECT id FROM publications WHERE clip_id=? AND platform=?", (clip["id"], plat))
        pid = old["id"] if old else self.db.x(
            "INSERT INTO publications(clip_id,platform,status,scheduled_at,state,created,updated) VALUES(?,?,?,?,?,?,?)",
            (clip["id"], plat, "scheduled", now, json.dumps(st), now, now))
        res["pub_id"] = pid
        deadline = time.time() + 900
        while time.time() < deadline:
            r = e.pl.h_publish({"payload": {"pub_id": pid, "inline": True}, "id": 0})
            p = self.db.one("SELECT * FROM publications WHERE id=?", (pid,))
            res.update(send_status=p["status"], visibility=p["visibility"], public_id=p["public_id"], url=p["url"], error=p["error"])
            if r is None or r.status == "done":
                break
            if r.status == "hold":
                step("Envio preparado — aguardando SUA confirmação para publicar", None,
                     "O Instagram não tem modo privado pela API. Confirme no painel para publicar de verdade.")
                return self._end(rid, steps, res, "awaiting_confirmation")
            time.sleep(min(r.wait_s, 30))
        p = self.db.one("SELECT * FROM publications WHERE id=?", (pid,))
        ok = p["status"] == "published"
        step("Envio e confirmação real da plataforma", ok,
             f"status real: {p['status']} | visibilidade: {p['visibility']} | id público: {p['public_id']} | {p['error'] or ''}")
        self._end(rid, steps, res, "done" if ok else "failed")

    def confirm(self, rid: int) -> None:
        r = self.latest()
        if not r or r["id"] != rid or r["status"] != "awaiting_confirmation":
            raise PermanentError("nenhum teste aguardando confirmação")
        pid = r["result"]["pub_id"]
        p = self.db.one("SELECT * FROM publications WHERE id=?", (pid,))
        st = json.loads(p["state"])
        st["confirmed"] = True
        self.db.x("UPDATE publications SET state=? WHERE id=?", (json.dumps(st), pid))
        self.db.x("UPDATE realtests SET status='running' WHERE id=?", (rid,))

        def go():
            steps, res = r["steps"], r["result"]
            res.setdefault("t0", time.time())
            for _ in range(60):
                rr = self.e.pl.h_publish({"payload": {"pub_id": pid, "inline": True}, "id": 0})
                if rr is None or rr.status == "done":
                    break
                time.sleep(min(rr.wait_s or 5, 30))
            p2 = self.db.one("SELECT * FROM publications WHERE id=?", (pid,))
            ok = p2["status"] == "published"
            res.update(send_status=p2["status"], public_id=p2["public_id"], url=p2["url"], error=p2["error"])
            steps.append({"name": "Publicação confirmada pelo administrador", "ok": ok, "detail": f"status real: {p2['status']} {p2['url'] or ''} {p2['error'] or ''}", "t": 0})
            self._end(rid, steps, res, "done" if ok else "failed")
        threading.Thread(target=go, daemon=True).start()

    def _end(self, rid, steps, res, status):
        res["elapsed_s"] = round(time.time() - res.get("t0", time.time()), 1)
        ok_steps = [s["name"] for s in steps if s["ok"]]
        bad = [s["name"] for s in steps if s["ok"] is False]
        res["diagnostic"] = {"funcionou": ok_steps, "precisa_correcao": bad}
        self._save(rid, steps, res, status)
