"""Executado pelo notebook Kaggle: copia os Secrets do Kaggle (Add-ons → Secrets) para variáveis de ambiente
e chama o runner. Nenhum segredo é impresso."""
from __future__ import annotations

import os

SECRET_NAMES = ["SECRET_KEY", "XH_COORD_URL", "XH_COORD_TOKEN", "GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET",
                "GDRIVE_REFRESH_TOKEN", "YOUTUBE_API_KEY", "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET",
                "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "PUBLIC_BASE_URL"]


def load_kaggle_secrets() -> list[str]:
    try:
        from kaggle_secrets import UserSecretsClient          # só existe dentro do Kaggle
    except ImportError:
        return []
    cli, got = UserSecretsClient(), []
    for n in SECRET_NAMES:
        if n in os.environ:
            continue
        try:
            os.environ[n] = cli.get_secret(n)
            got.append(n)
        except Exception:
            pass
    return got


def main() -> None:
    got = load_kaggle_secrets()
    print("secrets carregados:", got)
    os.environ.setdefault("DATA_DIR", "/kaggle/working/xh-data")
    os.environ.setdefault("XH_ENV", "production")
    os.environ.setdefault("COOKIE_SECURE", "1")
    from .runner import main as run
    run()


if __name__ == "__main__":
    main()
