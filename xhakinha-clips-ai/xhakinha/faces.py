"""Reconhecimento facial local de pessoas com consentimento registrado.

Fotos de referência são processadas em memória e descartadas. Somente embeddings
normalizados são persistidos, usando Store.put_secret (Fernet) no banco canônico.
YuNet (MIT) detecta/alinha; SFace (Apache-2.0) gera embeddings de 128 dimensões.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np


MODEL_FILES = {
    "yunet": {
        "name": "face_detection_yunet_2023mar.onnx",
        "url": "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        "size": 232589,
        "license": "MIT",
    },
    "sface": {
        "name": "face_recognition_sface_2021dec.onnx",
        "url": "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "sha256": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        "size": 38696353,
        "license": "Apache-2.0",
    },
}
MODEL_VERSION = "opencv-zoo-yunet-2023mar+sface-2021dec"
CONSENT_VERSION = "xh-face-consent-v1"
EMBEDDING_SIZE = 128
DEFAULT_THRESHOLD = 0.50
MIN_MATCH_FRAMES = 2


class FaceRecognitionError(ValueError):
    """Modelo ou imagem de referência inválidos; falhar fechado."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_model(model_dir: Path, spec: dict) -> Path:
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    destination = model_dir / spec["name"]
    if (
        destination.is_file()
        and destination.stat().st_size == spec["size"]
        and _sha256(destination) == spec["sha256"]
    ):
        return destination

    destination.unlink(missing_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=destination.name + ".", suffix=".part", dir=model_dir
    )
    os.close(fd)
    temporary = Path(temp_name)
    digest = hashlib.sha256()
    size = 0
    try:
        request = urllib.request.Request(
            spec["url"], headers={"User-Agent": "Xhakinha-Clips-AI/1.0"}
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            with temporary.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > spec["size"]:
                        raise FaceRecognitionError(
                            "arquivo do modelo excedeu o tamanho esperado"
                        )
                    digest.update(chunk)
                    output.write(chunk)
        if size != spec["size"] or digest.hexdigest() != spec["sha256"]:
            raise FaceRecognitionError(
                "hash/tamanho do modelo facial não confere; download bloqueado"
            )
        temporary.replace(destination)
        return destination
    except FaceRecognitionError:
        raise
    except Exception as exc:
        raise FaceRecognitionError(
            "não foi possível obter o modelo facial verificado"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


class FaceModel:
    def __init__(self, model_dir: Path):
        self.model_dir = Path(model_dir)
        self.detector = None
        self.recognizer = None

    def load(self) -> None:
        if self.detector is not None and self.recognizer is not None:
            return
        yunet = _ensure_model(self.model_dir, MODEL_FILES["yunet"])
        sface = _ensure_model(self.model_dir, MODEL_FILES["sface"])
        try:
            self.detector = cv2.FaceDetectorYN_create(
                str(yunet), "", (320, 320), 0.85, 0.30, 5000
            )
            self.recognizer = cv2.FaceRecognizerSF_create(str(sface), "")
        except Exception as exc:
            self.detector = None
            self.recognizer = None
            raise FaceRecognitionError(
                "OpenCV não conseguiu carregar YuNet/SFace"
            ) from exc

    def _faces(self, image):
        height, width = image.shape[:2]
        self.detector.setInputSize((width, height))
        _, found = self.detector.detect(image)
        return found

    @staticmethod
    def _normalise(feature) -> list[float]:
        vector = np.asarray(feature, dtype=np.float32).reshape(-1)
        if vector.size != EMBEDDING_SIZE or not np.isfinite(vector).all():
            raise FaceRecognitionError("embedding facial inválido")
        norm = float(np.linalg.norm(vector))
        if not norm > 1e-8:
            raise FaceRecognitionError("embedding facial vazio")
        return (vector / norm).astype(np.float32).tolist()

    def reference_embedding(self, image_bytes: bytes) -> list[float]:
        if not image_bytes or len(image_bytes) > 5 * 1024 * 1024:
            raise FaceRecognitionError("foto vazia ou maior que 5 MB")
        self.load()
        image = cv2.imdecode(
            np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR
        )
        if image is None or image.size == 0:
            raise FaceRecognitionError("arquivo de imagem inválido")
        height, width = image.shape[:2]
        if not min(height, width) >= 120 or max(height, width) > 12000:
            raise FaceRecognitionError("resolução da foto fora do limite")

        found = self._faces(image)
        if found is None or len(found) != 1:
            raise FaceRecognitionError(
                "a foto precisa mostrar exatamente um rosto"
            )
        face = found[0]
        face_width = int(round(float(face[2])))
        face_height = int(round(float(face[3])))
        confidence = float(face[14])
        if (
            not confidence >= 0.85
            or not min(face_width, face_height) >= 70
            or not face_width * face_height >= 0.015 * width * height
        ):
            raise FaceRecognitionError(
                "rosto pequeno ou pouco nítido; envie uma foto frontal e clara"
            )
        try:
            aligned = self.recognizer.alignCrop(image, face)
            return self._normalise(self.recognizer.feature(aligned))
        except FaceRecognitionError:
            raise
        except Exception as exc:
            raise FaceRecognitionError(
                "não foi possível extrair o rosto da foto"
            ) from exc

    def frame_embedding(self, image) -> list[list[float]]:
        self.load()
        found = self._faces(image)
        if found is None or len(found) == 0:
            return []
        embeddings = []
        for face in found[:8]:
            try:
                if not float(face[14]) >= 0.80:
                    continue
                face_width = int(round(float(face[2])))
                face_height = int(round(float(face[3])))
                if not min(face_width, face_height) >= 48:
                    continue
                aligned = self.recognizer.alignCrop(image, face)
                embeddings.append(
                    self._normalise(self.recognizer.feature(aligned))
                )
            except FaceRecognitionError:
                continue
            except Exception:
                continue
        return embeddings


def active_profiles(db, now: float | None = None) -> list[dict]:
    """Carrega apenas perfis consentidos e não revogados; vetores só em memória."""
    now = float(now if now is not None else time.time())
    rows = db.q(
        "SELECT id,name,threshold,consent_expires,reference_count "
        "FROM face_profiles "
        "WHERE revoked_at IS NULL AND (consent_expires IS NULL OR consent_expires>?) "
        "ORDER BY id",
        (now,),
    )
    profiles = []
    for row in rows:
        secret = db.get_secret(f"face_profile:{row['id']}")
        vectors = secret.get("embeddings") if isinstance(secret, dict) else None
        if not isinstance(vectors, list):
            continue
        clean = []
        for item in vectors:
            try:
                vector = np.asarray(item, dtype=np.float32).reshape(-1)
                norm = float(np.linalg.norm(vector))
                if (
                    vector.size == EMBEDDING_SIZE
                    and np.isfinite(vector).all()
                    and norm > 1e-8
                ):
                    clean.append((vector / norm).astype(np.float32))
            except Exception:
                continue
        if clean:
            profiles.append(
                {
                    "id": int(row["id"]),
                    "name": str(row["name"]),
                    "threshold": float(row["threshold"] or DEFAULT_THRESHOLD),
                    "embeddings": clean,
                }
            )
    return profiles


def match_interval(
    video_path: Path,
    start: float,
    end: float,
    profiles: list[dict],
    model_dir: Path,
    *,
    sample_every: float = 1.5,
    model: FaceModel | None = None,
) -> list[dict]:
    """Busca pessoas autorizadas em vários quadros; sem perfil ativo, não processa."""
    if not profiles:
        return []
    if not float(end) > float(start):
        raise FaceRecognitionError("intervalo de vídeo inválido")

    face_model = model or FaceModel(model_dir)
    face_model.load()
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise FaceRecognitionError("não foi possível abrir vídeo para reconhecimento")

    hits = {int(profile["id"]): [] for profile in profiles}
    try:
        timestamp = max(0.0, float(start))
        stop_at = max(timestamp, float(end))
        step = max(0.75, float(sample_every))
        while not timestamp >= stop_at:
            capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
            ok, frame = capture.read()
            if ok and frame is not None:
                for feature in face_model.frame_embedding(frame):
                    vector = np.asarray(feature, dtype=np.float32)
                    for profile in profiles:
                        best_score = max(
                            float(np.dot(vector, reference))
                            for reference in profile["embeddings"]
                        )
                        if best_score >= float(profile["threshold"]):
                            hits[int(profile["id"])].append(
                                (timestamp, best_score)
                            )
            timestamp += step
    finally:
        capture.release()

    matches = []
    for profile in profiles:
        values = hits[int(profile["id"])]
        unique_times = {round(timestamp, 1) for timestamp, _ in values}
        if not len(unique_times) >= MIN_MATCH_FRAMES:
            continue
        scores = sorted((score for _, score in values), reverse=True)
        confidence = float(np.mean(scores[: min(3, len(scores))]))
        matches.append(
            {
                "profile_id": int(profile["id"]),
                "name": str(profile["name"]),
                "confidence": round(confidence, 4),
                "frames": len(unique_times),
                "model": MODEL_VERSION,
            }
        )
    return sorted(matches, key=lambda item: item["confidence"], reverse=True)


def append_reference(db, profile_id: int, embedding: list[float]) -> int:
    """Armazena uma referência cifrada; a fotografia original não é guardada."""
    profile_id = int(profile_id)
    if db.one(
        "SELECT id FROM face_profiles WHERE id=? AND revoked_at IS NULL",
        (profile_id,),
    ) is None:
        raise FaceRecognitionError("perfil inexistente ou revogado")
    secret_name = f"face_profile:{profile_id}"
    current = db.get_secret(secret_name) or {"embeddings": []}
    vectors = current.get("embeddings") if isinstance(current, dict) else []
    if not isinstance(vectors, list):
        vectors = []
    if len(vectors) >= 20:
        raise FaceRecognitionError(
            "limite de 20 fotos de referência por pessoa atingido"
        )
    vectors.append(FaceModel._normalise(embedding))
    db.put_secret(
        secret_name, {"embeddings": vectors, "model": MODEL_VERSION}
    )
    db.x(
        "UPDATE face_profiles SET reference_count=?,updated=? "
        "WHERE id=? AND revoked_at IS NULL",
        (len(vectors), time.time(), profile_id),
    )
    return len(vectors)
