#!/usr/bin/env python3
"""Gera o .env com SEGREDOS FORTES (nunca usa valor padrão). Rode uma vez: python scripts/setup.py"""
import secrets
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
env, example = root / ".env", root / ".env.example"
if env.exists() and "--force" not in sys.argv:
    sys.exit(".env já existe (use --force para recriar). Nada foi alterado.")
text = example.read_text()
text = text.replace("SECRET_KEY=", f"SECRET_KEY={secrets.token_urlsafe(48)}", 1)
env.write_text(text)
env.chmod(0o600)
print("OK: .env criado com SECRET_KEY forte (permissão 600). Agora edite o domínio e as chaves de API no .env.")
