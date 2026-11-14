"""Worker extra (sem painel). Rode quantos quiser: python -m xhakinha.worker. Compartilham a mesma fila (SQLite WAL)."""
import signal
import time

from .config import ConfigError, load_settings
from .engine import Engine


def main():
    try:
        s = load_settings()
    except ConfigError as e:
        raise SystemExit(f"ERRO DE CONFIGURAÇÃO: {e}")
    e = Engine(s)
    e.start(workers=True, scheduler_on=False)
    stop = []
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
    while not stop:
        time.sleep(1)
    e.stop()


if __name__ == "__main__":
    main()
