"""Executado pelo notebook Kaggle: copia os Secrets do Kaggle (Add-ons → Secrets) para variáveis de ambiente
e chama o runner. Nenhum segredo é impresso."""
from __future__ import annotations

import importlib
from importlib import metadata
import os
import subprocess
import sys


LIBSQL_REQUIREMENT = "libsql==0.1.11"


def ensure_libsql_driver() -> None:
    """Confere o driver Turso e tenta reparar a instalação se estiver ausente."""
    def validate_driver() -> None:
        importlib.import_module("libsql")
        installed = metadata.version("libsql")
        if installed != "0.1.11":
            raise RuntimeError(
                f"versão libsql inesperada: {installed}; esperada: 0.1.11"
            )

    try:
        validate_driver()
        print("[Kaggle] Driver libsql 0.1.11: OK")
        return
    except Exception as exc:
        original_error = exc
        print("[Kaggle] Driver libsql ausente/incompatível; reparando instalação...")

    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "--disable-pip-version-check", "--no-cache-dir", "-q",
            LIBSQL_REQUIREMENT,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "")[-1200:]
        raise RuntimeError(
            f"Falha ao instalar {LIBSQL_REQUIREMENT} "
            f"(pip exit {result.returncode}): {detail}"
        ) from original_error

    importlib.invalidate_caches()
    try:
        validate_driver()
    except Exception as exc:
        raise RuntimeError(
            "O pip terminou, mas o driver libsql continua sem importar "
            "na versão esperada."
        ) from exc

    print("[Kaggle] Driver libsql 0.1.11 instalado e validado.")


SECRET_NAMES = ["SECRET_KEY", "XH_COORD_URL", "XH_COORD_TOKEN", "GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET",
                "GDRIVE_REFRESH_TOKEN", "YOUTUBE_API_KEY", "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET",
                "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN", "XH_STATE_BACKEND", "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "PUBLIC_BASE_URL"]


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
    ensure_libsql_driver()
    os.environ.setdefault("DATA_DIR", "/kaggle/working/xh-data")
    os.environ.setdefault("XH_ENV", "production")
    os.environ.setdefault("XH_STATE_BACKEND", "turso")
    os.environ.setdefault("COOKIE_SECURE", "1")
    from .runner import main as run
    run()


if __name__ == "__main__":
    main()
