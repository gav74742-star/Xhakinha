"""Ponto de entrada: painel + motor no mesmo processo (python -m xhakinha.main)."""
import os

import uvicorn

from .config import ConfigError, load_settings


def build():
    from .web.app import create_app
    s = load_settings()
    return create_app(s, run_engine=os.environ.get("RUN_ENGINE", "1") != "0")


def main():
    try:
        app = build()
    except ConfigError as e:
        raise SystemExit(f"ERRO DE CONFIGURAÇÃO: {e}")
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")),
                proxy_headers=True, forwarded_allow_ips="*", log_level="warning")


if __name__ == "__main__":
    main()
