"""Testa o Turso REAL com o mesmo adaptador que o Xhakinha usa. Não cria nada do projeto: só uma tabela xh_probe_* que é apagada.
Rode no Termux ou no Kaggle (precisa de `pip install libsql`).
  export TURSO_DATABASE_URL='libsql://SEU-BANCO.turso.io'   # painel do Turso > Database URL
  export TURSO_AUTH_TOKEN='...'                              # token com permissão de ESCRITA (não "read-only")
  python scripts/turso_check.py
Não imprime o token nem a URL completa."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xhakinha.turso_tools import probe  # noqa: E402


def main() -> int:
    url, tok = os.environ.get("TURSO_DATABASE_URL", ""), os.environ.get("TURSO_AUTH_TOKEN", "")
    if not url or not tok:
        print("Defina TURSO_DATABASE_URL e TURSO_AUTH_TOKEN (veja o cabeçalho deste arquivo).")
        return 2
    try:
        import libsql  # noqa: F401
    except ImportError:
        print("Pacote ausente. Rode:  pip install libsql")
        return 2
    return 0 if probe(url, tok) else 1


if __name__ == "__main__":
    sys.exit(main())
