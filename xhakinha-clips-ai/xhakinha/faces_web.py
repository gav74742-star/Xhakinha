"""Rotas autenticadas para cadastro, referência adicional e revogação facial."""
from __future__ import annotations

import time
from datetime import date, datetime, time as day_time
from pathlib import Path

from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .faces import (
    CONSENT_VERSION,
    DEFAULT_THRESHOLD,
    FaceModel,
    FaceRecognitionError,
    append_reference,
)


MAX_REFERENCE_BYTES = 5 * 1024 * 1024


def build_face_routes(settings, db, guard, current_user):
    """Cria as rotas do módulo sem expor embeddings nem armazenar as fotos."""
    model = FaceModel(Path(settings.data_dir) / "models")

    async def read_reference(form):
        upload = form.get("photo")
        if not isinstance(upload, UploadFile):
            raise FaceRecognitionError("selecione uma foto de referência")
        raw = await upload.read(MAX_REFERENCE_BYTES + 1)
        if not raw:
            raise FaceRecognitionError("a foto está vazia")
        if len(raw) not in range(1, MAX_REFERENCE_BYTES + 1):
            raise FaceRecognitionError("a foto deve ter no máximo 5 MB")
        return model.reference_embedding(raw)

    def authorized_source_ids(form):
        raw_ids = form.getlist("source_ids")
        if not raw_ids:
            raw = str(form.get("source_ids", "")).strip()
            raw_ids = [raw] if raw else []
        if not raw_ids or len(raw_ids) > 100:
            raise FaceRecognitionError("selecione de 1 a 100 fontes autorizadas")
        try:
            ids = sorted({int(str(value).strip()) for value in raw_ids})
        except (TypeError, ValueError):
            raise FaceRecognitionError("identificador de fonte inválido")
        if not ids or any(value <= 0 for value in ids):
            raise FaceRecognitionError("identificador de fonte inválido")

        marks = ",".join("?" for _ in ids)
        rows = db.q(
            f"SELECT id FROM sources WHERE id IN ({marks}) "
            "AND enabled=1 AND authorized=1",
            tuple(ids),
        )
        if {int(row["id"]) for row in rows} != set(ids):
            raise FaceRecognitionError(
                "todas as fontes precisam existir, estar ativas e autorizadas"
            )
        return ids

    def link_sources(profile_id, source_ids):
        now = time.time()
        for source_id in source_ids:
            db.x(
                "INSERT OR IGNORE INTO face_profile_sources"
                "(profile_id,source_id,created) VALUES(?,?,?)",
                (profile_id, source_id, now),
            )

    async def collection(request: Request):
        if request.method == "GET":
            now = time.time()
            rows = db.q(
                "SELECT id,name,consent_scope,consent_evidence,consent_by,"
                "consent_recorded,consent_expires,revoked_at,reference_count "
                "FROM face_profiles ORDER BY id DESC"
            )
            for row in rows:
                row["active"] = (
                    row["revoked_at"] is None
                    and (
                        row["consent_expires"] is None
                        or float(row["consent_expires"]) > now
                    )
                )
                row["source_ids"] = [
                    int(item["source_id"])
                    for item in db.q(
                        "SELECT source_id FROM face_profile_sources "
                        "WHERE profile_id=? ORDER BY source_id",
                        (row["id"],),
                    )
                ]
            return JSONResponse(rows)

        form = await request.form()
        name = str(form.get("name", "")).strip()
        scope = str(form.get("scope", "")).strip()
        evidence = str(form.get("evidence", "")).strip()
        expiry_text = str(form.get("expires", "")).strip()
        accepted = str(form.get("consent_confirmed", "")).lower() in (
            "1", "true", "on", "yes"
        )
        sources_confirmed = str(form.get("sources_confirmed", "")).lower() in (
            "1", "true", "on", "yes"
        )
        if len(name) not in range(1, 81):
            raise FaceRecognitionError("informe um nome de identificação")
        if len(scope) not in range(12, 1001):
            raise FaceRecognitionError("descreva a finalidade e as fontes autorizadas")
        if len(evidence) not in range(3, 501):
            raise FaceRecognitionError(
                "registre uma referência da autorização (documento, mensagem ou contrato)"
            )
        if not accepted:
            raise FaceRecognitionError(
                "confirme que existe autorização expressa para uso biométrico"
            )
        if not sources_confirmed:
            raise FaceRecognitionError(
                "confirme que a autorização abrange as fontes selecionadas"
            )

        source_ids = authorized_source_ids(form)
        expiry = None
        if expiry_text:
            try:
                expiry_date = date.fromisoformat(expiry_text)
                days_remaining = (expiry_date - date.today()).days
                if days_remaining not in range(1, 365001):
                    raise FaceRecognitionError("a autorização já está vencida")
                expiry = datetime.combine(
                    expiry_date, day_time.max
                ).timestamp()
            except ValueError as exc:
                raise FaceRecognitionError("data de validade inválida") from exc

        embedding = await read_reference(form)
        now = time.time()
        creator = current_user(request)
        if not creator:
            return JSONResponse({"error": "não autenticado"}, status_code=401)

        profile_id = db.x(
            "INSERT INTO face_profiles("
            "name,consent_version,consent_scope,consent_evidence,consent_recorded,"
            "consent_by,consent_expires,revoked_at,threshold,reference_count,created,updated"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                name, CONSENT_VERSION, scope, evidence, now, creator,
                expiry, None, DEFAULT_THRESHOLD, 0, now, now,
            ),
        )
        try:
            link_sources(profile_id, source_ids)
            count = append_reference(db, profile_id, embedding)
        except Exception:
            db.x(
                "DELETE FROM face_profile_sources WHERE profile_id=?",
                (profile_id,),
            )
            db.del_secret(f"face_profile:{profile_id}")
            db.x("DELETE FROM face_profiles WHERE id=?", (profile_id,))
            raise

        db.event(
            "info", "faces",
            f"Perfil facial cadastrado com fontes vinculadas (id {profile_id})",
            profile_id=profile_id, consent_by=creator,
            consent_version=CONSENT_VERSION, source_ids=source_ids,
        )
        return JSONResponse(
            {
                "ok": True, "id": profile_id,
                "reference_count": count, "source_ids": source_ids,
            },
            status_code=201,
        )

    async def add_sources(request: Request):
        profile_id = int(request.path_params["pid"])
        now = time.time()
        profile = db.one(
            "SELECT id,consent_expires,revoked_at FROM face_profiles WHERE id=?",
            (profile_id,),
        )
        if (
            not profile
            or profile["revoked_at"] is not None
            or (
                profile["consent_expires"] is not None
                and float(profile["consent_expires"]) <= now
            )
        ):
            raise FaceRecognitionError("perfil inexistente, vencido ou revogado")

        form = await request.form()
        confirmed = str(form.get("sources_confirmed", "")).lower() in (
            "1", "true", "on", "yes"
        )
        if not confirmed:
            raise FaceRecognitionError(
                "confirme que a autorização abrange as novas fontes selecionadas"
            )

        source_ids = authorized_source_ids(form)
        link_sources(profile_id, source_ids)
        all_sources = [
            int(row["source_id"])
            for row in db.q(
                "SELECT source_id FROM face_profile_sources "
                "WHERE profile_id=? ORDER BY source_id",
                (profile_id,),
            )
        ]
        db.event(
            "info", "faces",
            f"Fontes autorizadas associadas ao perfil facial {profile_id}",
            profile_id=profile_id, source_ids=source_ids,
        )
        return JSONResponse(
            {"ok": True, "id": profile_id, "source_ids": all_sources}
        )

    async def add_reference(request: Request):
        profile_id = int(request.path_params["pid"])
        profile = db.one(
            "SELECT id,consent_expires,revoked_at FROM face_profiles WHERE id=?",
            (profile_id,),
        )
        now = time.time()
        if (
            not profile
            or profile["revoked_at"] is not None
            or (
                profile["consent_expires"] is not None
                and float(profile["consent_expires"]) <= now
            )
        ):
            raise FaceRecognitionError("perfil inexistente, vencido ou revogado")
        form = await request.form()
        accepted = str(form.get("same_person_confirmed", "")).lower() in (
            "1", "true", "on", "yes"
        )
        if not accepted:
            raise FaceRecognitionError(
                "confirme que a foto mostra a mesma pessoa autorizada"
            )
        embedding = await read_reference(form)
        count = append_reference(db, profile_id, embedding)
        db.event(
            "info",
            "faces",
            f"Referência facial adicional cadastrada (perfil {profile_id})",
            profile_id=profile_id,
        )
        return JSONResponse(
            {"ok": True, "id": profile_id, "reference_count": count}
        )

    async def revoke(request: Request):
        profile_id = int(request.path_params["pid"])
        now = time.time()
        changed = db.x(
            "UPDATE face_profiles SET revoked_at=?,updated=? "
            "WHERE id=? AND revoked_at IS NULL",
            (now, now, profile_id),
        )
        db.del_secret(f"face_profile:{profile_id}")
        db.event(
            "info",
            "faces",
            f"Consentimento facial revogado (perfil {profile_id})",
            profile_id=profile_id,
        )
        return JSONResponse({"ok": True, "revoked": bool(changed)})

    return [
        Route("/api/faces", guard(collection), methods=["GET", "POST"]),
        Route(
            "/api/faces/{pid:int}/references",
            guard(add_reference),
            methods=["POST"],
        ),
        Route(
            "/api/faces/{pid:int}/sources",
            guard(add_sources),
            methods=["POST"],
        ),
        Route(
            "/api/faces/{pid:int}",
            guard(revoke),
            methods=["DELETE"],
        ),
        ]
