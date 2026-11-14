"""Descrição + hashtags (TEXTO DA PUBLICAÇÃO — nunca desenhado no vídeo).
Usa LLM local (Ollama) se configurado; senão, modelo de texto determinístico. Sempre informa a origem."""
from __future__ import annotations

import json
import re

from . import http

TAGS = {
    "risada/piada": ["humor", "engracado", "risadas"], "grito/susto": ["susto", "reacao", "inesperado"],
    "reação/surpresa": ["reacao", "surpresa", "momentoengracado"], "fala marcante": ["fraseforte", "resenha", "papo"],
    "ação/movimento": ["acao", "momentos", "viral"], "momento": ["momentos", "clipes", "melhoresmomentos"],
}
OPENERS = {
    "risada/piada": "Impossível não rir desse momento 😂", "grito/susto": "Esse susto ninguém esperava 😱",
    "reação/surpresa": "A reação mais inesperada do dia 👀", "fala marcante": "Essa fala ficou marcada 🔥",
    "ação/movimento": "Olha o que aconteceu aqui 🎬", "momento": "Um dos melhores momentos da transmissão ✨",
}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower().encode("ascii", "ignore").decode())[:30]


def _llm(url: str, model: str, category: str, transcript: str, source: str) -> str | None:
    prompt = ("Escreva UMA descrição curta (máx. 140 caracteres), em português do Brasil, para um clipe vertical. "
              f"Categoria: {category}. Trecho falado: \"{transcript[:600]}\". Não invente fatos, não cite nomes que não "
              "estejam no trecho, sem hashtags, sem aspas. Responda só com a descrição.")
    try:
        r = http.request("POST", f"{url}/api/generate", json={"model": model, "prompt": prompt, "stream": False,
                                                              "options": {"temperature": 0.7, "num_predict": 80}}, timeout=90)
        if r.status_code == 200:
            t = (r.json().get("response") or "").strip().strip('"')
            return t[:200] or None
    except Exception:
        return None
    return None


def generate(category: str, transcript: str, source_name: str, platform: str, *, ollama_url: str = "",
             ollama_model: str = "", credit: bool = True) -> dict:
    ai = _llm(ollama_url, ollama_model, category, transcript, source_name) if ollama_url else None
    text = ai or OPENERS.get(category, OPENERS["momento"])
    tags = list(TAGS.get(category, TAGS["momento"]))
    if source_name and slug(source_name):
        tags.insert(0, slug(source_name))
    tags += ["fyp"] if platform == "tiktok" else ["reels"]
    seen, out = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    out = out[:5]
    credit_line = f"\n\nCréditos: {source_name}" if credit and source_name else ""
    desc = f"{text}{credit_line}\n\n" + " ".join("#" + t for t in out)
    return {"text": text, "hashtags": out, "description": desc[:2200], "generated_by": "ollama" if ai else "modelo-de-texto"}
