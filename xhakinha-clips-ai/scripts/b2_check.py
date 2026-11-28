"""Verifica o Backblaze B2 REAL com o mesmo backend que o Xhakinha usa. Só toca em objetos tmp/b2check-*, que são apagados no fim.
Variáveis (nunca no chat/código): B2_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET e B2_REGION (ex.: us-east-005) ou B2_ENDPOINT_URL.
  python scripts/b2_check.py
Imprime no fim uma linha:  RESULTADO: PASSOU   ou   RESULTADO: FALHOU <motivo>. Não imprime chaves."""
from __future__ import annotations

import os
import secrets
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run(backend, out=print) -> bool:
    from xhakinha.cloud.drive_storage import IntegrityError, sha256_file
    steps: list[tuple[str, bool, str]] = []
    tag = secrets.token_hex(4)
    key = f"tmp/b2check-{tag}.bin"
    key_big = f"tmp/b2check-{tag}-b.bin"
    created: list[str] = []

    def step(name, fn):
        try:
            steps.append((name, True, str(fn() or "")))
        except Exception as e:                                       # noqa: BLE001
            steps.append((name, False, f"{type(e).__name__}: {str(e)[:160]}"))

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "a.bin"
        src.write_bytes(os.urandom(300_000))
        got = td / "got.bin"

        def quota():
            q = backend.quota()
            return f"usado={q['usage'] / 1e6:.1f}MB limite_config={q['limit'] / 1e9:.1f}GB"

        def up():
            r = backend.upload(src, key); created.append(key)
            return f"enviado {r['size']} bytes"

        def stat():
            s = backend.stat(key)
            assert s and s["size"] == src.stat().st_size and s["sha256"] == sha256_file(src), s
            return "tamanho e SHA256 conferem"

        def down():
            backend.download(key, got)
            assert sha256_file(got) == sha256_file(src)
            assert not got.with_suffix(got.suffix + ".part").exists()
            return "download íntegro, sem .part"

        def idem():
            r = backend.upload(src, key)
            assert r.get("reused") is True, r
            return "reenvio idêntico não duplica"

        def no_overwrite():
            other = td / "b.bin"; other.write_bytes(b"conteudo diferente")
            try:
                backend.upload(other, key)
            except IntegrityError:
                return "sobrescrita recusada"
            raise AssertionError("sobrescreveu objeto existente")

        def listing():
            names = [x["path"] for x in backend.listdir("tmp")]
            assert key in names, names[:5]
            return f"{len(names)} objeto(s) em tmp/"

        def missing():
            assert backend.stat(f"tmp/nao-existe-{tag}") is None
            return "objeto ausente retorna None"

        def refuse_protected():
            try:
                backend.delete("originals/qualquer.mp4")
            except PermissionError:
                return "apagar fora de prefixos permitidos é recusado"
            raise AssertionError("permitiu apagar original")

        def multipart():
            big = td / "big.bin"
            with big.open("wb") as f:
                for _ in range(int(backend.multipart_threshold // 1_000_000) + 2):   # só passa do limiar
                    f.write(os.urandom(1_000_000))
            backend.upload(big, key_big); created.append(key_big)
            backend.download(key_big, td / "bigdl.bin")
            assert sha256_file(big) == sha256_file(td / "bigdl.bin")
            return f"multipart de {big.stat().st_size // 1_000_000} MB íntegro"

        for n, f in (("quota/listagem", quota), ("upload", up), ("stat", stat), ("download", down), ("idempotência", idem),
                     ("sem sobrescrita", no_overwrite), ("listagem", listing), ("ausente", missing),
                     ("proteção de originais", refuse_protected)):
            step(n, f)
        if os.environ.get("B2_CHECK_MULTIPART", "1") == "1":
            step("multipart (arquivo grande)", multipart)
        else:
            steps.append(("multipart (arquivo grande)", True, "PULADO (B2_CHECK_MULTIPART=0)"))

        def cleanup():
            for k in created:
                backend.delete(k)
            for k in created:
                assert backend.stat(k) is None
            return "objetos de teste removidos"
        step("limpeza", cleanup)

    for n, ok, d in steps:
        out(f"  [{'OK ' if ok else 'FALHOU'}] {n}: {d}")
    bad = [n for n, ok, _ in steps if not ok]
    out("RESULTADO: PASSOU" if not bad else "RESULTADO: FALHOU " + ", ".join(bad))
    return not bad


def main() -> int:
    need = ["B2_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET"]
    miss = [n for n in need if not os.environ.get(n)] + ([] if os.environ.get("B2_REGION") or os.environ.get("B2_ENDPOINT_URL") else ["B2_REGION"])
    if miss:
        print("Faltam variáveis:", ", ".join(miss)); print("RESULTADO: FALHOU configuração"); return 2
    try:
        import boto3  # noqa: F401
    except ImportError:
        print("Rode: pip install 'boto3>=1.35,<2'"); print("RESULTADO: FALHOU boto3 ausente"); return 2
    os.environ["XH_STORAGE_BACKEND"] = "b2"
    from xhakinha.cloud.runner import drive_from_env
    try:
        backend = drive_from_env(os.environ)
    except Exception as e:                                          # noqa: BLE001
        print(f"Não consegui criar o backend: {type(e).__name__}: {str(e)[:160]}"); print("RESULTADO: FALHOU backend"); return 1
    return 0 if run(backend) else 1


if __name__ == "__main__":
    sys.exit(main())
