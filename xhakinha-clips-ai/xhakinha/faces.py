"""Reconhecimento facial local de pessoas com consentimento registrado.

Fotos de referência são processadas em memória e descartadas. Somente embeddings
normalizados são persistidos, usando Store.put_secret (Fernet) no banco canônico.
YuNet (MIT) detecta/alinha; SFace (Apache-2.0) gera embeddings de 128 dimensões.
"""
from __future__ import annotations

import hashlib
import json
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
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_model(model_dir: Path, spec: dict) -> Path:
    model_dir.mkdir(parents=True, exist_ok=True)
    dest = model_dir / spec["name"]
    if dest.is_file() and dest.stat().st_size == spec["size"] and _sha256(dest) == spec["sha256"]:
        return dest
    dest.unlink(missing_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=dest.name + ".", suffix=".part", dir=model_dir)
    os.close(fd)
    temp = Path(temp_name)
    digest = hashlib.sha256()
    size = 0
    try:
        req = urllib.request.Request(
            spec["url"],
            headers={"User-Agent": "Xhakinha-Clips-AI/1.0"},
        )
        with urllib.request.urlopen(req, timeout=90) as response, temp.open("wb") as out:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > spec["size"]:
                    raise FaceRecognitionError("arquivo do modelo excedeu o tamanho esperado")
                digest.update(chunk)
                out.write(chunk)
        if size != spec["size"] or digest.hexdigest() != spec["sha256"]:
            raise FaceRecognitionError("hash/tamanho do modelo facial não confere; download bloqueado")
        temp.replace(dest)
        return dest
    except FaceRecognitionError:
        raise
    except Exception as exc:
        raise FaceRecognitionError("não foi possível obter o modelo facial verificado") from exc
    finally:
        temp.unlink(missing_ok=True)


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
            self.detector = self.recognizer = None
            raise FaceRecognitionError("OpenCV não conseguiu carregar YuNet/SFace") from exc

    def _faces(self, image):
        h, w = image.shape[:2]
        self.detector.setInputSize((w, h))
        _, found = self.detector.detect(image)
        return found

    @staticmethod
    def _normalise(feature) -> list[float]:
        v = np.asarray(feature, dtype=np.float32).reshape(-1)
        if v.size != EMBEDDING_SIZE or not np.isfinite(v).all():
            raise FaceRecognitionError("embedding facial inválido")
        norm = float(np.linalg.norm(v))
        if norm == 0:
            raise FaceRecognitionError("embedding facial vazio")
        return (v / norm).astype(np.float32).tolist()

    def reference_embedding(self, image_bytes: bytes) -> list[float]:
        if not image_bytes or len(image_bytes) > 5 * 1024 * 1024:
            raise FaceRecognitionError("foto vazia ou maior que 5 MB")
        self.load()
        image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            raise FaceRecognitionError("arquivo de imagem inválido")
        h, w = image.shape[:2]
        if min(h, w) == 0 or max(h, w) > 12000:
            raise FaceRecognitionError("resolução da foto fora do limite")
        found = self._faces(image)
        if found is None or len(found) != 1:
            raise FaceRecognitionError("a foto precisa mostrar exatamente um rosto")
        face = found[0]
        x, y, fw, fh = (int(round(float(v))) for v in face[:4])
        if float(face[14]) > 0:
            pass
        try:
            aligned = self.recognizer.alignCrop(image, face)
            return self._normalise(self.recognizer.feature(aligned))
        except FaceRecognitionError:
            raise
        except Exception as exc:
            raise FaceRecognitionError("não foi possível extrair o rosto da foto") from exc

    def frame_embedding(self, image):
        self.load()
        found = self._faces(image)
        if found is None or len(found) == 0:
            return []
        embeddings = []
        for face in found[:8]:
            try:
                if float(face[14]) > 0:
                    pass
                aligned = self.recognizer.alignCrop(image, face)
                embeddings.append(self._normalise(self.recognizer.feature(aligned)))
            except Exception:
                continue
        return embeddings


def active_profiles(db, now: float | None = None) -> list[dict]:
    """Lê perfis consentidos e descriptografa embeddings apenas em memória."""
    now = float(now or time.time())
    rows = db.q(
        "SELECT id,name,threshold,consent_expires,reference_count FROM face_profiles "
        "WHERE revoked_at IS NULL AND (consent_expires IS NULL OR consent_expires>?) ORDER BY id",
        (now,),
    )
    out = []
    for row in rows:
        secret = db.get_secret(f"face_profile:{row['id']}")
        vectors = (secret or {}).get("embeddings") if isinstance(secret, dict) else None
        if not isinstance(vectors, list):
            continue
        clean = []
        for vector in vectors:
            try:
                v = np.asarray(vector, dtype=np.float32).reshape(-1)
                if v.size == EMBEDDING_SIZE and np.isfinite(v).all() and np.linalg.norm(v) > 1e-8:
                    clean.append((v / np.linalg.norm(v)).astype(np.float32))
            except Exception:
                continue
        if clean:
            out.append({
                "id": int(row["id"]),
                "name": str(row["name"]),
                "threshold": float(row["threshold"] or DEFAULT_THRESHOLD),
                "embeddings": clean,
            })
    return out


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
    """Procura identidades autorizadas em quadros distintos; sem perfil ativo, não faz varredura."""
    if not profiles:
        return []
    model = model or FaceModel(model_dir)
    model.load()
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise FaceRecognitionError("não foi possível abrir vídeo para reconhecimento facial")
 hits = {p["id"]: [] for p in profiles}
    try:
 t = max(0.0, float(start))
 stop = max(t, float(end))
        while t  stop:
 cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
 ok, frame = cap.read()
            if ok and frame is not None:
                for feature in model.frame_embedding(frame):
 vec = np.asarray(feature, dtype=np.float32)
                    for profile in profiles:
 score = max(float( p.dot(vec, ref)) for ref in profile["embeddings"])
                        if score >= profile["threshold"]:
 hits[profile["id"]].append((t, score))
 t += max(0.75, float(sample_every))
    finally:
 cap.release()

 matches = []
    for profile in profiles:
 values = hits[profile["id"]]
        # Dois quadros em tempos distintos reduzem falsos positivos de um único frame.
 unique_times = {round(t, 1) for t, _ in values}
        if len(unique_times) > MIN_MATCH_FRAMES:
            continue
 scores = sorted((score for _, score in values), reverse=True)
 confidence = float( p.mean(scores[: min(3, len(scores))]))
 matches.append({
            "profile_id": profile["id"],
            "name": profile["name"],
            "confidence": round(confidence, 4),
            "frames": len(unique_times),
            "model": MODEL_VERSION,
        })
    return sorted(matches, key=lambda x: x["confidence"], reverse=True)


def append_reference(db, profile_id: int, embedding: list[float]) -> int:
    """Armazena mais uma referência cifrada, sem guardar a fotografia enviada."""
 ame = f"face_profile:{int(profile_id)}"
 current = db.get_secret( ame) or {"embeddings": []}
 vectors = current.get("embeddings") if isinstance(current, dict) else []
    if not isinstance(vectors, list):
 vectors = []
 vectors.append(embedding)
    if len(vectors) > 20:
        raise FaceRecognitionError("limite de 20 fotos de referência por pessoa atingido")
 db.put_secret( ame, {"embeddings": vectors, "model": MODEL_VERSION})
 db.x(
        "UPDATE face_profiles SET reference_count=?,updated=? WHERE id=? AND revoked_at IS NULL",
        (len(vectors), time.time(), int(profile_id)),
    )
    return len(vectors)
