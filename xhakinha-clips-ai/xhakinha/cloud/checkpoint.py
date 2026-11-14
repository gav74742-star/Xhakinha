"""Checkpoints persistentes entre nuvens: resultados por pedaço, endereçados por hash do conteúdo.
A fonte da verdade é o Drive (não a RAM nem o disco temporário de um executor). O coordenador guarda só
um resumo pequeno para acelerar; ao retomar, a listagem do Drive manda."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .drive_storage import DriveBackend


def chunk_name(kind: str, t0: float, t1: float) -> str:
    return f"{kind}-{int(round(t0 * 1000)):010d}-{int(round(t1 * 1000)):010d}.json"


class ChunkStore:
    def __init__(self, drive: DriveBackend, content_key: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{8,128}", content_key):
            raise ValueError("content_key inválida (use o hash do conteúdo)")
        self.drive, self.base = drive, f"checkpoints/{content_key}"

    def path(self, kind: str, t0: float, t1: float) -> str:
        return f"{self.base}/{chunk_name(kind, t0, t1)}"

    def has(self, kind, t0, t1) -> bool:
        return self.drive.stat(self.path(kind, t0, t1)) is not None

    def load(self, kind, t0, t1):
        d = self.drive.read_json(self.path(kind, t0, t1))
        return None if d is None else d["result"]

    def save(self, kind, t0, t1, result, by: str = "") -> bool:
        """Primeiro a gravar vence (resultado é determinístico para a mesma entrada). Devolve True se gravou agora."""
        p = self.path(kind, t0, t1)
        if self.drive.stat(p):
            return False
        self.drive.write_json(p, {"kind": kind, "t0": t0, "t1": t1, "result": result})
        return True

    def done(self, kind: str) -> list[tuple[float, float]]:
        out = []
        for f in self.drive.listdir(self.base):
            m = re.fullmatch(rf"{re.escape(kind)}-(\d{{10}})-(\d{{10}})\.json", Path(f["path"]).name)
            if m:
                out.append((int(m[1]) / 1000, int(m[2]) / 1000))
        return sorted(out)
