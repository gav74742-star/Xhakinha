"""Gerencia o banco Turso do Xhakinha. Precisa de SECRET_KEY, TURSO_DATABASE_URL, TURSO_AUTH_TOKEN (token com ESCRITA).
  python scripts/turso_migrate.py check                  # mostra tabelas e contagens (não altera nada)
  python scripts/turso_migrate.py init                   # cria o esquema (idempotente, não apaga nada)
  python scripts/turso_migrate.py import ARQUIVO.db      # copia um SQLite local para o Turso VAZIO; recusa se já houver dados
Nunca imprime segredos."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("check", "init", "import"):
        print(__doc__)
        return 2
    from xhakinha import turso_tools
    from xhakinha.config import load_settings
    from xhakinha.db import Store
    s = load_settings()
    s.state_backend = "turso"
    s.validate()
    print("Banco:", turso_tools.mask(s.turso_url))
    if argv[0] == "check":
        from xhakinha import dbdriver
        c = dbdriver.LibsqlConn(s.turso_url, s.turso_token)
        c_ = turso_tools.counts(c)
        print("(vazio: 0 tabelas)" if not c_ else "")
        for t, n in sorted(c_.items()):
            print(f"  {t}: {n}")
        return 0
    if argv[0] == "init":
        st = turso_tools.init_schema(lambda: Store(s))
        print("Esquema pronto. Tabelas:", len(turso_tools.counts(st.conn)))
        return 0
    if len(argv) < 2 or not Path(argv[1]).is_file():
        print("Informe o caminho de um arquivo .db existente.")
        return 2
    st = turso_tools.init_schema(lambda: Store(s))
    turso_tools.import_sqlite(st.conn, argv[1])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
