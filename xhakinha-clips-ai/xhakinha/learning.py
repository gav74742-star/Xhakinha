"""Aprendizado por métricas REAIS (APIs oficiais). Sem amostras suficientes => não adapta (e diz isso)."""
from __future__ import annotations

import time
from datetime import datetime

import numpy as np

from .analysis import BASE_WEIGHTS, CRITERIA
from .db import Store

MIN_SAMPLES = 20
PRIORS = {  # PRIORS genéricos (suposição inicial, NÃO dado real). São substituídos por dados da própria conta.
    "instagram": {7: .3, 8: .4, 9: .4, 12: .8, 13: .6, 18: .9, 19: 1.0, 20: 1.0, 21: .9, 22: .6},
    "tiktok": {7: .3, 11: .5, 12: .8, 15: .5, 17: .7, 18: .9, 19: 1.0, 20: 1.0, 21: 1.0, 22: .8, 23: .5},
}


def engagement(m: dict) -> float:
    v = max(1, m.get("views") or 0)
    inter = (m.get("likes") or 0) + 2 * (m.get("comments") or 0) + 3 * (m.get("shares") or 0)
    rate = inter / v
    return float(np.log1p(v) * 0.4 + rate * 100 * 0.6)


def latest_metrics(db: Store) -> list[dict]:
    return db.q("""SELECT p.id pub_id, p.platform, p.published_at, c.scores, c.category, c.duration, m.views, m.likes, m.comments,
                   m.shares, m.retention FROM publications p JOIN clips c ON c.id=p.clip_id
                   JOIN metrics m ON m.id=(SELECT id FROM metrics WHERE publication_id=p.id ORDER BY ts DESC LIMIT 1)
                   WHERE p.status='published' AND m.views IS NOT NULL""")


def learned_weights(db: Store) -> tuple[dict, dict]:
    """Retorna (pesos, info). Mistura 70% base + 30% aprendido (correlação), limitado."""
    import json
    rows = latest_metrics(db)
    info = {"samples": len(rows), "active": False, "min_samples": MIN_SAMPLES}
    if len(rows) < MIN_SAMPLES:
        return dict(BASE_WEIGHTS), info
    X = np.array([[json.loads(r["scores"]).get(k, 0) for k in CRITERIA] for r in rows], float)
    y = np.array([engagement(r) for r in rows], float)
    if y.std() < 1e-9:
        return dict(BASE_WEIGHTS), info
    cors = []
    for j in range(X.shape[1]):
        cors.append(0.0 if X[:, j].std() < 1e-9 else float(np.corrcoef(X[:, j], y)[0, 1]))
    pos = np.clip(np.array(cors), 0.02, None)
    learned = pos / pos.sum()
    base = np.array([BASE_WEIGHTS[k] for k in CRITERIA])
    mix = 0.7 * base + 0.3 * learned
    mix = np.clip(mix, 0.03, 0.4)
    mix /= mix.sum()
    info.update(active=True, correlations=dict(zip(CRITERIA, np.round(cors, 3))))
    return dict(zip(CRITERIA, map(float, mix))), info


def category_multiplier(db: Store, category: str, platform: str) -> float:
    rows = [r for r in latest_metrics(db) if r["platform"] == platform]
    if len(rows) < MIN_SAMPLES:
        return 1.0
    allm = np.mean([engagement(r) for r in rows])
    cat = [engagement(r) for r in rows if r["category"] == category]
    if len(cat) < 5 or allm <= 0:
        return 1.0
    return float(np.clip(np.mean(cat) / allm, 0.8, 1.25))


def best_hours(db: Store, platform: str, tz, n: int = 12) -> dict:
    """Pontuação por hora local: prior (suposição) + dados reais com encolhimento bayesiano."""
    prior = PRIORS.get(platform, PRIORS["instagram"])
    rows = [r for r in latest_metrics(db) if r["platform"] == platform and r["published_at"]]
    by: dict[int, list[float]] = {}
    for r in rows:
        h = datetime.fromtimestamp(r["published_at"], tz).hour
        by.setdefault(h, []).append(engagement(r))
    mean_all = float(np.mean([engagement(r) for r in rows])) if rows else 1.0
    out = {}
    for h in range(7, 24):
        p = prior.get(h, 0.2)
        vals = by.get(h, [])
        k = 5.0
        data = (np.mean(vals) / mean_all) if vals and mean_all > 0 else p
        w = len(vals) / (len(vals) + k)
        out[h] = float(w * data + (1 - w) * p)
    return {"scores": out, "samples": len(rows), "source": "dados reais + prior" if rows else "prior genérico (sem dados ainda)"}
