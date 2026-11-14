"""Comandos de administração: create-admin, doctor."""
import getpass
import os
import sys

from . import auth
from .config import ConfigError, load_settings
from .db import Store


def main(argv=None):
    argv = argv or sys.argv[1:]
    try:
        s = load_settings()
    except ConfigError as e:
        sys.exit(f"ERRO: {e}")
    db = Store(s)
    cmd = argv[0] if argv else "help"
    if cmd == "create-admin":
        user = os.environ.get("ADMIN_USER") or input("Usuário do administrador: ").strip()
        pw = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Senha (mín. 10 caracteres): ")
        auth.create_user(db, user, pw)
        print(f"Administrador '{user}' criado/atualizado.")
    elif cmd == "doctor":
        from .analysis import face_detector_ok, whisper_available
        from .sources import ytdlp
        import shutil
        print("ffmpeg:", bool(shutil.which("ffmpeg")), "| yt-dlp:", ytdlp.check_available(), "| whisper:", whisper_available(),
              "| rosto:", face_detector_ok(), "| PUBLIC_BASE_URL:", s.public_base_url or "(vazio)")
    else:
        print("uso: python -m xhakinha.cli [create-admin|doctor]")


if __name__ == "__main__":
    main()
