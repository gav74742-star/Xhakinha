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
        if len(raw) > MAX_REFERENCE_BYTES:
            raise FaceRecognitionError("a foto deve ter no máximo 5 MB")
        return model.reference_embedding(raw)

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
            return JSONResponse(rows)

        form = await request.form()
        name = str(form.get("name", "")).strip()
        scope = str(form.get("scope", "")).strip()
        evidence = str(form.get("evidence", "")).strip()
        expiry_text = str(form.get("expires", "")).strip()
        accepted = str(form.get("consent_confirmed", "")).lower() in (
            "1", "true", "on", "yes"
        )
        if not 1 &lt;= len(name) &lt;= 80:
            raise FaceRecognitionError("informe um nome de identificação")
        if not 12 &lt;= len(scope) &lt;= 1000:
            raise FaceRecognitionError("descreva a finalidade e as fontes autorizadas")
        if not 3 &lt;= len(evidence) &lt;= 500:
            raise FaceRecognitionError(
                "registre uma referência da autorização (documento, mensagem ou contrato)"
            )
        if not accepted:
            raise FaceRecognitionError(
                "confirme que existe autorização expressa para uso biométrico"
            )

        expiry = None
        if expiry_text:
            try:
                expiry = datetime.combine(
                    date.fromisoformat(expiry_text), day_time.max
                ).timestamp()
            except ValueError as exc:
                raise FaceRecognitionError("data de validade inválida") from exc
            if not expiry &gt; time.time():
                raise FaceRecognitionError("a autorização já está vencida")

        embedding = await read_reference(form)
        now = time.time()
        creator = current_user(request) or "admin"
        profile_id = db.x(
            "INSERT INTO face_profiles("
            "name,consent_version,consent_scope,consent_evidence,consent_recorded,"
            "consent_by,consent_expires,revoked_at,threshold,reference_count,created,updated"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                name,
                CONSENT_VERSION,
                scope,
                evidence,
                now,
                creator,
                expiry,
                None,
                DEFAULT_THRESHOLD,
                0,
                now,
                now,
            ),
        )
        try:
            count = append_reference(db, profile_id, embedding)
        except Exception:
            db.del_secret(f"face_profile:{profile_id}")
            db.x("DELETE FROM face_profiles WHERE id=?", (profile_id,))
            raise
        db.event(
            "info",
            "faces",
            f"Perfil facial autorizado cadastrado (id {profile_id})",
            profile_id=profile_id,
            consent_by=creator,
            consent_version=CONSENT_VERSION,
        )
        return JSONResponse(
            {"ok": True, "id": profile_id, "reference_count": count},
            status_code=201,
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
                and float(profile["consent_expires"]) &lt;= now
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
            "/api/faces/{pid:int}",
            guard(revoke),
            methods=["DELETE"],
        ),
    ]
