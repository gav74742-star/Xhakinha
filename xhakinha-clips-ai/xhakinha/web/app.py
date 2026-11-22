"""Painel/API (Starlette). Todas as rotas /api exigem sessão; mutações exigem cabeçalho anti-CSRF."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path

import psutil
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__, analysis, auth, learning, scheduler
from ..config import Settings, redact
from ..db import DEFAULT_SETTINGS
from ..engine import Engine
from ..media import probe
from ..publish.base import verify_media_token
from ..publish.tiktok import authorize_url
from ..realtest import RealTest
from ..retry import PermanentError, XhError
from ..sources import ytdlp
from ..storage import quick_fingerprint

STATIC = Path(__file__).parent / "static"
HANDLE = re.compile(r"^@?[A-Za-z0-9._]{1,30}$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PLATFORMS = {"youtube", "twitch", "instagram", "url"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}

# chave -> validador (retorna valor normalizado ou levanta ValueError)
def _num(lo, hi, cast=float):
    def f(v):
        v = cast(v)
        if not lo <= v <= hi:
            raise ValueError(f"deve estar entre {lo} e {hi}")
        return v
    return f

def _bool(v): return 1 if v in (1, True, "1", "true", "on") else 0
def _hh(v):
    if not HHMM.match(str(v)):
        raise ValueError("use HH:MM")
    return str(v)
def _handle(v):
    v = str(v or "").strip()
    if v and not HANDLE.match(v):
        raise ValueError("@ inválido")
    return v
def _plats(v):
    if not isinstance(v, list) or not set(v) <= {"instagram", "tiktok"}:
        raise ValueError("lista de plataformas inválida")
    return v
def _pos(v):
    if v not in ("top_left", "top_right", "bottom_left", "bottom_right", "top_center"):
        raise ValueError("posição inválida")
    return v
def _priv(v):
    if v not in ("SELF_ONLY", "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR"):
        raise ValueError("privacidade inválida")
    return v

VALIDATORS = {
    "daily_min": _num(0, 500, int), "daily_normal": _num(0, 500, int), "daily_max": _num(0, 500, int),
    "min_publish_score": _num(0, 100), "min_clip_score": _num(0, 100), "min_interval_min": _num(1, 1440, int),
    "window_start": _hh, "window_end": _hh, "clip_min_s": _num(5, 60, int), "clip_pref_max_s": _num(15, 90, int),
    "clip_hard_max_s": _num(15, 180, int), "clips_per_hour_of_video": _num(1, 60, int), "chunk_min": _num(10, 90, int),
    "chunk_overlap_s": _num(10, 600, int), "vod_delay_min": _num(0, 120, int), "vod_delay_max": _num(0, 240, int),
    "archive_clips_per_week": _num(0, 100, int), "archive_enabled": _bool, "watermark_opacity": _num(0.05, 1.0),
    "watermark_position": _pos, "watermark_scale": _num(0.015, 0.08), "handle_instagram": _handle, "handle_tiktok": _handle,
    "platforms_enabled": _plats, "tiktok_audited": _bool, "tiktok_privacy": _priv, "credit_line": _bool,
    "original_grace_h": _num(0, 720), "published_clip_retention_h": _num(0, 720), "work_retention_h": _num(0, 720),
    "unpublished_retention_days": _num(0, 3650), "backup_every_h": _num(1, 168), "backup_keep": _num(1, 90, int),
    "monitor_enabled": _bool,
}


def J(data, code=200):
    return JSONResponse(data, status_code=code)


def create_app(s: Settings, engine: Engine | None = None, run_engine: bool = True) -> Starlette:
    eng = engine or Engine(s)
    db, rt = eng.db, RealTest(eng)

    # ---------------------------------------------------------------- segurança
    def current_user(request: Request) -> str | None:
        return auth.read_token(s.secret_key, request.cookies.get("xh_session"))

    def guard(fn):
        async def wrapper(request: Request):
            u = current_user(request)
            if not u:
                return J({"error": "não autenticado"}, 401)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                if request.headers.get("x-requested-with") != "XHK":
                    return J({"error": "cabeçalho anti-CSRF ausente"}, 403)
                origin = request.headers.get("origin")
                if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
                    return J({"error": "origem inválida"}, 403)
            try:
                return await fn(request)
            except ValueError as e:
                return J({"error": str(e)}, 400)
            except XhError as e:
                return J({"error": redact(str(e))}, 400)
        return wrapper

    async def body(request: Request) -> dict:
        try:
            d = await request.json()
        except Exception:
            raise ValueError("JSON inválido")
        if not isinstance(d, dict):
            raise ValueError("JSON inválido")
        return d

    # ---------------------------------------------------------------- auth
    async def login(request: Request):
        ip = request.client.host if request.client else "?"
        if request.headers.get("x-requested-with") != "XHK":
            return J({"error": "cabeçalho anti-CSRF ausente"}, 403)
        if auth.too_many_failures(db, ip):
            return J({"error": "muitas tentativas; aguarde 10 minutos"}, 429)
        if not db.one("SELECT 1 FROM users LIMIT 1"):
            return J({"error": "nenhum administrador criado: rode 'python -m xhakinha.cli create-admin'"}, 503)
        d = await body(request)
        u = db.one("SELECT * FROM users WHERE username=?", (str(d.get("username", "")),))
        ok = auth.verify_password(str(d.get("password", "")), u["pw_hash"]) if u else auth.verify_password("x", "scrypt$16384$8$1$00$00")
        if not (u and ok):
            auth.record_failure(db, ip)
            return J({"error": "usuário ou senha inválidos"}, 401)
        r = J({"ok": True, "user": u["username"]})
        r.set_cookie("xh_session", auth.make_token(s.secret_key, u["username"]), max_age=12 * 3600, httponly=True,
                     secure=s.cookie_secure, samesite="lax", path="/")
        return r

    async def logout(request: Request):
        r = J({"ok": True})
        r.delete_cookie("xh_session", path="/")
        return r

    async def me(request: Request):
        return J({"user": current_user(request)})

    # ---------------------------------------------------------------- status
    def deps() -> dict:
        import shutil as sh
        try:
            face_detector = bool(analysis.face_detector_ok())
        except (ImportError, OSError):
            face_detector = False
        return {"ffmpeg": bool(sh.which("ffmpeg")), "yt_dlp": ytdlp.check_available(), "whisper": analysis.whisper_available(),
                "face_detector": face_detector, "ollama": bool(s.ollama_url),
                "public_url_https": s.public_base_url.startswith("https://"),
                "keys": {"youtube": bool(s.youtube_api_key), "twitch": bool(s.twitch_client_id), "tiktok": bool(s.tiktok_client_key)}}

    async def status(request: Request):
        now = time.time()
        plats = {}
        for p in db.get("platforms_enabled"):
            tg = scheduler.target_for_today(db, p, s.tz, now)
            nxt = db.q("SELECT scheduled_at FROM publications WHERE platform=? AND status='scheduled' AND scheduled_at>? ORDER BY scheduled_at LIMIT 5", (p, now))
            plats[p] = {**tg, "connected": eng.pl.pubs[p].connected(), "next": [r["scheduled_at"] for r in nxt],
                        "stock": db.one("SELECT COUNT(*) n FROM clips WHERE status IN ('awaiting','scheduled','retry_wait','publish_failed') AND path IS NOT NULL")["n"]}
        clip_counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM clips GROUP BY status")}
        pub_counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM publications GROUP BY status")}
        _, info = learning.learned_weights(db)
        try:
            vm = psutil.virtual_memory()
            mem = {"percent": vm.percent, "available": vm.available}
        except (OSError, psutil.Error):
            mem = {"percent": None, "available": None}
        try:
            cpu = psutil.cpu_percent(interval=None)
        except (OSError, psutil.Error):
            cpu = None
        return J({"version": __version__, "now": now, "tz": s.timezone, "engine": db.get("engine_state"),
                  "workers": {"target": eng.target_workers, "max": s.max_workers, "busy": list(eng.busy.values())},
                  "queue": eng.q.counts(), "platforms": plats, "clips": clip_counts, "publications": pub_counts,
                  "storage": eng.st.usage(), "cpu": cpu, "mem": mem,
                  "deps": deps(), "learning": info,
                  "quota_youtube": {"used": __import__("xhakinha.sources.quota", fromlist=["Quota"]).Quota(db, "youtube", s.youtube_daily_quota).used(), "limit": s.youtube_daily_quota},
                  "contents": {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM contents GROUP BY status")},
                  "archive": archive_stats(), "failed_jobs": eng.q.counts()["dead"],
                  "settings_targets": {k: db.get(k) for k in ("daily_min", "daily_normal", "daily_max")}})

    def archive_stats() -> dict:
        r = lambda sql, a=(): db.one(sql, a)["n"]
        return {"found": r("SELECT COUNT(*) n FROM contents WHERE kind='archive'"),
                "analyzed": r("SELECT COUNT(*) n FROM contents WHERE kind='archive' AND status='processed'"),
                "clips": r("SELECT COUNT(*) n FROM clips WHERE origin='archive'"),
                "awaiting": r("SELECT COUNT(*) n FROM clips WHERE origin='archive' AND status IN ('awaiting','scheduled','retry_wait')"),
                "used": [x["title"] for x in db.q("SELECT title FROM contents WHERE kind='archive' AND status='processed' ORDER BY processed_at DESC LIMIT 20")]}

    async def control(request: Request):
        a = (await body(request)).get("action")
        m = {"start": "running", "resume": "running", "pause": "paused", "stop": "stopped"}
        if a not in m:
            raise ValueError("ação inválida")
        db.set("engine_state", m[a])
        db.event("info", "control", f"Motor: {m[a]}")
        return J({"engine": m[a]})

    # ---------------------------------------------------------------- fontes
    async def sources(request: Request):
        if request.method == "GET":
            rows = db.q("SELECT id,platform,name,ref,enabled,authorized,interval_s,last_check,last_ok,last_error,archive_scanned FROM sources WHERE platform!='upload' ORDER BY id")
            for r in rows:
                r["contents"] = db.one("SELECT COUNT(*) n FROM contents WHERE source_id=?", (r["id"],))["n"]
            return J(rows)
        d = await body(request)
        plat, ref, name = str(d.get("platform", "")), str(d.get("ref", "")).strip(), str(d.get("name", "")).strip()
        if plat not in PLATFORMS or not (1 <= len(ref) <= 300) or not (1 <= len(name) <= 80):
            raise ValueError("plataforma/nome/referência inválidos")
        if plat == "url":
            ytdlp.validate_url(ref)
        if not d.get("authorized"):
            raise ValueError("Marque a declaração: o conteúdo é seu ou você tem autorização para cortá-lo e republicá-lo")

        video_infos = None
        if plat == "youtube":
            from ..sources.youtube import extract_video_id
            video_id = extract_video_id(ref)
            if video_id:
                video_ref = f"https://www.youtube.com/watch?v={video_id}"

                # Detecta o mesmo vídeo em qualquer formato de URL já cadastrado.
                for row in db.q("SELECT id,ref FROM sources WHERE platform='youtube'"):
                    if extract_video_id(row["ref"]) == video_id:
                        raise ValueError("este vídeo já está cadastrado")

                from ..sources import make_connector
                temporary_source = {
                    "id": 0,
                    "platform": "youtube",
                    "name": name,
                    "ref": video_ref,
                    "enabled": 1,
                    "authorized": 1,
                    "config": "{}",
                }
                video_infos = make_connector(temporary_source, s, db).discover({})
                if not video_infos:
                    raise ValueError("vídeo não encontrado ou indisponível")
                ref = video_ref

        try:
            sid = db.x("INSERT INTO sources(platform,name,ref,enabled,authorized,interval_s,created) VALUES(?,?,?,?,?,?,?)",
                       (plat, name, ref, 1, 1, max(60, int(d.get("interval_s", 300))), time.time()))
        except Exception:
            raise ValueError("fonte já cadastrada")

        if video_infos is not None:
            src = db.one("SELECT * FROM sources WHERE id=?", (sid,))
            eng.pl._ingest(src, video_infos, {})

        if plat == "url":
            from ..sources import make_connector
            for i in make_connector(db.one("SELECT * FROM sources WHERE id=?", (sid,)), s, db).discover({}):
                cid = db.x("INSERT INTO contents(source_id,ext_id,kind,title,url,status,discovered) VALUES(?,?,?,?,?,?,?)", (sid, i.ext_id, "vod", i.title, i.url, "queued", time.time()))
                eng.q.enqueue("acquire", {"content_id": cid, "deadline": time.time() + 86400}, content_id=cid, dedupe_key=f"acquire:{cid}")
        return J({"id": sid})

    async def source_item(request: Request):
        sid = int(request.path_params["sid"])
        src = db.one("SELECT * FROM sources WHERE id=?", (sid,))
        if not src:
            return J({"error": "não encontrada"}, 404)
        if request.method == "DELETE":
            db.x("UPDATE sources SET enabled=0 WHERE id=?", (sid,))
            return J({"ok": True, "note": "fonte desativada (histórico preservado)"})
        d = await body(request)
        if "enabled" in d:
            db.x("UPDATE sources SET enabled=? WHERE id=?", (1 if d["enabled"] else 0, sid))
        if "interval_s" in d:
            db.x("UPDATE sources SET interval_s=? WHERE id=?", (max(60, min(86400, int(d["interval_s"]))), sid))
        return J({"ok": True})

    async def source_test(request: Request):
        from ..sources import make_connector
        src = db.one("SELECT * FROM sources WHERE id=?", (int(request.path_params["sid"]),))
        if not src:
            return J({"error": "não encontrada"}, 404)
        r = make_connector(src, s, db).test()
        db.x("UPDATE sources SET last_error=?, last_ok=? WHERE id=?", (None if r["ok"] else r["detail"][:300], time.time() if r["ok"] else src["last_ok"], src["id"]))
        return J(r)

    # ---------------------------------------------------------------- listas
    async def contents(request: Request):
        st = request.query_params.get("status")
        sql = "SELECT id,source_id,ext_id,kind,title,duration,status,live_state,error,clips_made,discovered,processed_at,(local_path IS NOT NULL) has_file FROM contents"
        rows = db.q(sql + (" WHERE status=?" if st else "") + " ORDER BY id DESC LIMIT 200", (st,) if st else ())
        return J(rows)

    async def clips(request: Request):
        st = request.query_params.get("status")
        rows = db.q("SELECT id,content_id,start,end,score,scores,category,status,duration,origin,created,description,error,(path IS NOT NULL) has_file FROM clips"
                    + (" WHERE status=?" if st else "") + " ORDER BY id DESC LIMIT 200", (st,) if st else ())
        for r in rows:
            r["scores"] = json.loads(r["scores"] or "{}")
        return J(rows)

    async def clip_video(request: Request):
        c = db.one("SELECT path FROM clips WHERE id=?", (int(request.path_params["cid"]),))
        if not c or not c["path"] or not Path(c["path"]).is_file() or not eng.st.inside_managed(Path(c["path"])):
            return J({"error": "arquivo indisponível"}, 404)
        return FileResponse(c["path"], media_type="video/mp4")

    async def clip_action(request: Request):
        cid, act = int(request.path_params["cid"]), request.path_params["act"]
        c = db.one("SELECT * FROM clips WHERE id=?", (cid,))
        if not c:
            return J({"error": "não encontrado"}, 404)
        if act == "archive":
            db.x("UPDATE clips SET status='archived' WHERE id=? AND status IN ('awaiting','publish_failed')", (cid,))
        elif act == "restore":
            db.x("UPDATE clips SET status='awaiting' WHERE id=? AND status='archived'", (cid,))
            scheduler.refresh_clip_status(db, cid)
        else:
            raise ValueError("ação inválida")
        return J({"ok": True})

    async def publications(request: Request):
        st = request.query_params.get("status")
        rows = db.q("SELECT id,clip_id,platform,status,scheduled_at,attempts,remote_id,public_id,url,visibility,error,needs_admin,published_at,"
                    "(SELECT m.views FROM metrics m WHERE m.publication_id=publications.id ORDER BY ts DESC LIMIT 1) views FROM publications"
                    + (" WHERE status=?" if st else "") + " ORDER BY COALESCE(scheduled_at,created) DESC LIMIT 200", (st,) if st else ())
        return J(rows)

    async def pub_action(request: Request):
        pid, act = int(request.path_params["pid"]), request.path_params["act"]
        p = db.one("SELECT * FROM publications WHERE id=?", (pid,))
        if not p:
            return J({"error": "não encontrada"}, 404)
        if act == "retry":
            eng.pl.retry_publication(pid)
        elif act == "cancel":
            if p["status"] in ("published", "uploading", "processing"):
                raise ValueError("não é possível cancelar neste estado")
            db.x("UPDATE publications SET status='cancelled' WHERE id=?", (pid,))
            scheduler.refresh_clip_status(db, p["clip_id"])
        elif act == "confirm":
            if p["status"] != "awaiting_confirmation":
                raise ValueError("não está aguardando confirmação")
            st = json.loads(p["state"]); st["confirmed"] = True
            db.x("UPDATE publications SET state=? WHERE id=?", (json.dumps(st), pid))
            eng.q.enqueue("publish", {"pub_id": pid}, dedupe_key=f"pub:{pid}", priority=1)
        else:
            raise ValueError("ação inválida")
        return J({"ok": True})

    async def jobs(request: Request):
        st = request.query_params.get("status", "dead")
        if st not in ("dead", "pending", "running", "done"):
            raise ValueError("status inválido")
        return J(db.q("SELECT id,type,status,attempts,max_attempts,run_at,last_error,error_kind,content_id FROM jobs WHERE status=? ORDER BY id DESC LIMIT 200", (st,)))

    async def job_retry(request: Request):
        return J({"ok": eng.q.retry_dead(int(request.path_params["jid"]))})

    async def logs(request: Request):
        lvl = request.query_params.get("level")
        n = min(500, int(request.query_params.get("limit", 150)))
        return J(db.q("SELECT ts,level,component,message FROM events" + (" WHERE level=?" if lvl else "") + " ORDER BY id DESC LIMIT ?", ((lvl, n) if lvl else (n,))))

    async def attempts(request: Request):
        return J(db.q("SELECT ts,ref_type,ref_id,op,ok,message FROM attempts_log ORDER BY id DESC LIMIT 200"))

    # ---------------------------------------------------------------- config
    async def settings_api(request: Request):
        if request.method == "GET":
            return J(db.all_settings())
        d = await body(request)
        new = {}
        for k, v in d.items():
            if k not in VALIDATORS:
                raise ValueError(f"configuração desconhecida/protegida: {k}")
            try:
                new[k] = VALIDATORS[k](v)
            except (ValueError, TypeError) as e:
                raise ValueError(f"{k}: {e}")
        mn, nm, mx = (new.get(k, db.get(k)) for k in ("daily_min", "daily_normal", "daily_max"))
        if not mn <= nm <= mx:
            raise ValueError("metas precisam obedecer mínima ≤ normal ≤ máxima")
        for k, v in new.items():
            db.set(k, v)
        db.event("info", "settings", f"Configurações alteradas: {', '.join(new)}")
        return J({"ok": True})

    # ---------------------------------------------------------------- contas
    async def accounts(request: Request):
        out = {}
        for p in ("instagram", "tiktok"):
            a = db.get_secret(f"account:{p}")
            out[p] = {"connected": bool(a), "username": (a or {}).get("username"), "handle": db.get(f"handle_{p}")}
        out["tiktok_oauth_ready"] = bool(s.tiktok_client_key and s.public_base_url)
        return J(out)

    async def connect_instagram(request: Request):
        from .. import http
        d = await body(request)
        tok, uid = str(d.get("access_token", "")).strip(), str(d.get("ig_user_id", "")).strip()
        if not tok or not uid.isdigit():
            raise ValueError("informe access_token e ig_user_id (numérico)")
        r = http.need_ok(http.request("GET", f"{s.graph_base}/{uid}", params={"fields": "username", "access_token": tok}), "Instagram")
        uname = r.json().get("username")
        db.put_secret("account:instagram", {"access_token": tok, "ig_user_id": uid, "username": uname})
        db.event("info", "accounts", f"Instagram conectado: @{uname}")
        return J({"ok": True, "username": uname})

    async def disconnect(request: Request):
        p = request.path_params["platform"]
        if p not in ("instagram", "tiktok"):
            raise ValueError("plataforma inválida")
        db.del_secret(f"account:{p}")
        return J({"ok": True})

    async def tiktok_start(request: Request):
        if not (s.tiktok_client_key and s.public_base_url):
            raise PermanentError("configure TIKTOK_CLIENT_KEY/SECRET e PUBLIC_BASE_URL no .env")
        state = secrets.token_urlsafe(24)
        db.set("oauth_state_tiktok", {"s": state, "exp": time.time() + 600})
        return J({"url": authorize_url(s.tiktok_client_key, f"{s.public_base_url}/api/oauth/tiktok/callback", state, s.tiktok_auth_url)})

    async def tiktok_callback(request: Request):
        from .. import http
        if not current_user(request):
            return Response("não autenticado", status_code=401)
        st = db.get("oauth_state_tiktok") or {}
        if request.query_params.get("state") != st.get("s") or time.time() > st.get("exp", 0):
            return Response("state inválido/expirado", status_code=400)
        db.set("oauth_state_tiktok", {})
        code = request.query_params.get("code")
        if not code:
            return Response("autorização negada", status_code=400)
        r = http.request("POST", f"{s.tiktok_api}/v2/oauth/token/", data={
            "client_key": s.tiktok_client_key, "client_secret": s.tiktok_client_secret, "code": code, "grant_type": "authorization_code",
            "redirect_uri": f"{s.public_base_url}/api/oauth/tiktok/callback"}, headers={"Content-Type": "application/x-www-form-urlencoded"})
        j = r.json() if r.content else {}
        if "access_token" not in j:
            db.event("error", "accounts", "Falha OAuth TikTok: " + redact(str(j)[:200]))
            return Response("Falha ao obter token do TikTok", status_code=400)
        acct = {"access_token": j["access_token"], "refresh_token": j.get("refresh_token"), "open_id": j.get("open_id"),
                "expires_at": time.time() + int(j.get("expires_in", 86400))}
        try:
            ui = http.request("GET", f"{s.tiktok_api}/v2/user/info/", params={"fields": "open_id,display_name,username"},
                              headers={"Authorization": f"Bearer {acct['access_token']}"}).json()
            acct["username"] = ((ui.get("data") or {}).get("user") or {}).get("username")
        except Exception:
            pass
        db.put_secret("account:tiktok", acct)
        db.event("info", "accounts", "TikTok conectado")
        return Response(status_code=302, headers={"Location": "/#config"})

    # ---------------------------------------------------------------- upload seguro
    async def upload(request: Request):
        if request.headers.get("x-authorized") != "1":
            raise ValueError("confirme que o vídeo é seu/autorizado")
        name = os.path.basename(request.query_params.get("name", "video.mp4"))
        ext = Path(name).suffix.lower()
        if ext not in VIDEO_EXT:
            raise ValueError("extensão não permitida (mp4, mov, mkv, webm, m4v)")
        cap = s.max_upload_mb * 1024 * 1024
        if not eng.st.has_space(cap // 4):
            raise ValueError("espaço em disco insuficiente")
        tmp = Path(tempfile.mkstemp(dir=s.production_dir / "uploads", suffix=".part")[1])
        n = 0
        try:
            with open(tmp, "wb") as f:
                async for chunk in request.stream():
                    n += len(chunk)
                    if n > cap:
                        raise ValueError(f"arquivo maior que {s.max_upload_mb} MB")
                    f.write(chunk)
            info = probe(tmp)                       # valida de verdade (não confia em extensão/mime)
            if info["duration"] < 5:
                raise ValueError("vídeo curto demais")
        except PermanentError as e:
            tmp.unlink(missing_ok=True)
            raise ValueError(f"arquivo inválido: {e}")
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        src = db.one("SELECT id FROM sources WHERE platform='upload'")
        sid = src["id"] if src else db.x("INSERT INTO sources(platform,name,ref,enabled,authorized,created) VALUES('upload','Uploads','upload',0,1,?)", (time.time(),))
        cid = db.x("INSERT INTO contents(source_id,ext_id,kind,title,status,duration,discovered) VALUES(?,?,?,?,?,?,?)",
                   (sid, f"up-{secrets.token_hex(6)}", "upload", name[:100], "downloaded", info["duration"], time.time()))
        dest = eng.st.download_path(cid, ext.lstrip("."))
        os.replace(tmp, dest)
        db.x("UPDATE contents SET local_path=?, fingerprint=? WHERE id=?", (str(dest), quick_fingerprint(dest, info["duration"]), cid))
        eng.q.enqueue("process", {"content_id": cid}, content_id=cid, dedupe_key=f"process:{cid}", priority=3)
        return J({"ok": True, "content_id": cid, "duration": info["duration"]})

    # ---------------------------------------------------------------- real test, stats, backup
    async def realtest_start(request: Request):
        d = await body(request)
        plat = d.get("platform")
        if plat not in (None, "tiktok", "instagram"):
            raise ValueError("plataforma inválida")
        return J({"id": rt.start(plat)})

    async def realtest_latest(request: Request):
        return J(rt.latest() or {})

    async def realtest_confirm(request: Request):
        rt.confirm(int(request.path_params["rid"]))
        return J({"ok": True})

    async def stats(request: Request):
        rows = db.q("SELECT platform, strftime('%Y-%m-%d', published_at, 'unixepoch') d, COUNT(*) n FROM publications WHERE status='published' "
                    "AND published_at>? GROUP BY platform, d ORDER BY d", (time.time() - 14 * 86400,))
        _, info = learning.learned_weights(db)
        return J({"published_by_day": rows, "learning": info,
                  "best_hours": {p: learning.best_hours(db, p, s.tz) for p in ("instagram", "tiktok")},
                  "metrics_note": "TikTok/Instagram só informam o que as APIs oficiais permitem; posts privados do TikTok (app não auditado) não têm métricas."})

    async def backup_create(request: Request):
        p = eng.st.backup_db()
        return J({"name": p.name})

    async def backup_list(request: Request):
        return J(sorted([{"name": f.name, "size": f.stat().st_size} for f in s.backup_dir.glob("xhakinha-*.db")], key=lambda x: x["name"], reverse=True))

    async def backup_get(request: Request):
        name = request.path_params["name"]
        f = s.backup_dir / name
        if not re.fullmatch(r"xhakinha-[0-9\-]+\.db", name) or not f.is_file():
            return J({"error": "não encontrado"}, 404)
        return FileResponse(f, filename=name)

    async def public_media(request: Request):
        """URL pública TEMPORÁRIA e assinada (HMAC, expira) usada pelo Instagram para baixar o vídeo."""
        pid = verify_media_token(s.secret_key, request.path_params["token"])
        if not pid:
            return Response("forbidden", status_code=403)
        p = db.one("SELECT file_path, status FROM publications WHERE id=?", (pid,))
        if not p or not p["file_path"] or p["status"] in ("published", "cancelled") or not Path(p["file_path"]).is_file() or not eng.st.inside_managed(Path(p["file_path"])):
            return Response("not found", status_code=404)
        return FileResponse(p["file_path"], media_type="video/mp4")

    async def health(request: Request):
        try:
            version = db.get("schema_version")
            return J({
                "ok": True,
                "database": "ok",
                "backend": db.backend,
                "schema_version": version,
            })
        except Exception:
            return J({"ok": False, "database": "error"}, 503)

    async def index(request: Request):
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def sw(request: Request):
        return FileResponse(STATIC / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    G = guard
    routes = [
        Route("/", index), Route("/sw.js", sw), Route("/api/health", health),
        Route("/api/login", login, methods=["POST"]), Route("/api/logout", logout, methods=["POST"]), Route("/api/me", me),
        Route("/api/status", G(status)), Route("/api/control", G(control), methods=["POST"]),
        Route("/api/sources", G(sources), methods=["GET", "POST"]),
        Route("/api/sources/{sid:int}", G(source_item), methods=["PATCH", "DELETE"]),
        Route("/api/sources/{sid:int}/test", G(source_test), methods=["POST"]),
        Route("/api/contents", G(contents)), Route("/api/clips", G(clips)),
        Route("/api/clips/{cid:int}/video", G(clip_video)), Route("/api/clips/{cid:int}/{act}", G(clip_action), methods=["POST"]),
        Route("/api/publications", G(publications)), Route("/api/publications/{pid:int}/{act}", G(pub_action), methods=["POST"]),
        Route("/api/jobs", G(jobs)), Route("/api/jobs/{jid:int}/retry", G(job_retry), methods=["POST"]),
        Route("/api/logs", G(logs)), Route("/api/attempts", G(attempts)),
        Route("/api/settings", G(settings_api), methods=["GET", "PUT"]),
        Route("/api/accounts", G(accounts)), Route("/api/accounts/instagram", G(connect_instagram), methods=["POST"]),
        Route("/api/accounts/{platform}", G(disconnect), methods=["DELETE"]),
        Route("/api/oauth/tiktok/start", G(tiktok_start)), Route("/api/oauth/tiktok/callback", tiktok_callback),
        Route("/api/upload", G(upload), methods=["PUT"]),
        Route("/api/realtest", G(realtest_start), methods=["POST"]), Route("/api/realtest/latest", G(realtest_latest)),
        Route("/api/realtest/{rid:int}/confirm", G(realtest_confirm), methods=["POST"]),
        Route("/api/stats", G(stats)),
        Route("/api/backup", G(backup_create), methods=["POST"]), Route("/api/backups", G(backup_list)),
        Route("/api/backups/{name}", G(backup_get)),
        Route("/media/{token}.mp4", public_media),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]

    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.update({
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()"})
        if s.cookie_secure:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return resp

    from starlette.middleware.base import BaseHTTPMiddleware
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        if run_engine:
            eng.start()
        yield
        if run_engine:
            eng.stop()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(BaseHTTPMiddleware, dispatch=security_headers)
    app.state.engine = eng
    return app
