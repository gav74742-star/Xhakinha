#!/usr/bin/env python3
"""Sincroniza para o Android somente clipes finais ainda não publicados.

Autentica na API web existente do Xhakinha. Não guarda senha ou cookie em disco.
Arquivos são gravados como .part, conferidos por tamanho/SHA-256 e renomeados
atomicamente. Se o servidor aceitar HTTP Range, downloads interrompidos retomam.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

PENDING_STATES = ("awaiting", "scheduled", "retry_wait", "publish_failed")
HEADERS = {"X-Requested-With": "XHK"}
CHUNK_SIZE = 1024 * 1024


class AuthExpired(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_pending_status(status: str) -> bool:
    return str(status) in PENDING_STATES


def should_remove_on_phone(status: str) -> bool:
    # O backend marca o clipe como published após confirmar as publicações.
    return str(status) == "published"


def _api_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _check_base_url(base_url: str) -> str:
    base_url = base_url.strip().rstrip("/")
    parsed = urlsplit(base_url)
    if not parsed.hostname or parsed.scheme not in ("https", "http"):
        raise ValueError("Informe a URL raiz do painel web do Xhakinha")
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise ValueError("Por segurança, use HTTPS na URL pública do Xhakinha")
    return base_url


def login(base_url: str, username: str, password: str) -> requests.Session:
    session = requests.Session()
    response = session.post(
        _api_url(base_url, "/api/login"),
        json={"username": username, "password": password},
        headers=HEADERS,
        timeout=(15, 45),
    )
    if response.status_code != 200:
        try:
            detail = response.json().get("error", "falha de autenticação")
        except ValueError:
            detail = f"HTTP {response.status_code}"
        raise RuntimeError(f"Login recusado: {detail}")
    return session


def api_json(session: requests.Session, base_url: str, path: str, params=None):
    response = session.get(
        _api_url(base_url, path), params=params, headers=HEADERS, timeout=(15, 45)
    )
    if response.status_code == 401:
        raise AuthExpired("sessão do Xhakinha expirou")
    response.raise_for_status()
    return response.json()


def _preserve_bad_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(path.name + f".bad-{stamp}")
    counter = 1
    while target.exists():
        target = path.with_name(path.name + f".bad-{stamp}-{counter}")
        counter += 1
    os.replace(path, target)
    return target


def download_clip(
    session: requests.Session,
    base_url: str,
    clip: dict,
    out_dir: Path,
    min_free_bytes: int,
) -> str:
    clip_id = int(clip["id"])
    expected_hash = str(clip.get("sha256") or "").lower()
    expected_size = int(clip.get("size") or 0)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        return f"#{clip_id}: sem SHA-256 válido; mantido no servidor"
    if not clip.get("has_file"):
        return f"#{clip_id}: arquivo ainda não está disponível no servidor"

    target = out_dir / f"clip_{clip_id}.mp4"
    part = out_dir / f"clip_{clip_id}.mp4.part"
    if target.is_file():
        if sha256_file(target) == expected_hash and (not expected_size or target.stat().st_size == expected_size):
            return f"#{clip_id}: já está íntegro no celular"
        _preserve_bad_file(target)

    if part.exists() and expected_size and part.stat().st_size > expected_size:
        _preserve_bad_file(part)

    current_size = part.stat().st_size if part.exists() else 0
    disk_free = shutil.disk_usage(out_dir).free
    remaining = max(0, expected_size - current_size) if expected_size else 0
    if disk_free - remaining < min_free_bytes:
        return f"#{clip_id}: pausado para preservar {min_free_bytes / 1e9:.1f} GB livres"

    url = _api_url(base_url, f"/api/clips/{clip_id}/video")
    for attempt in range(2):
        start = part.stat().st_size if part.exists() else 0
        headers = dict(HEADERS)
        if start:
            headers["Range"] = f"bytes={start}-"
        with session.get(url, headers=headers, stream=True, timeout=(20, 180)) as response:
            if response.status_code == 401:
                raise AuthExpired("sessão do Xhakinha expirou")
            if response.status_code == 416 and start:
                part.unlink(missing_ok=True)
                continue
            if start and response.status_code == 206:
                content_range = response.headers.get("Content-Range", "")
                match = re.match(r"bytes\s+(\d+)-", content_range, re.IGNORECASE)
                if not match or int(match.group(1)) != start:
                    part.unlink(missing_ok=True)
                    continue
                mode = "ab"
            elif response.status_code == 200:
                # O servidor não aceitou Range: recomeça com segurança do zero.
                mode = "wb"
            else:
                response.raise_for_status()
                raise RuntimeError(f"resposta inesperada ao baixar #{clip_id}: HTTP {response.status_code}")

            with part.open(mode) as output:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        break
    else:
        raise RuntimeError(f"não foi possível retomar o download do clipe #{clip_id}")

    actual_size = part.stat().st_size
    if expected_size and actual_size != expected_size:
        raise RuntimeError(
            f"#{clip_id}: download incompleto ({actual_size} de {expected_size} bytes); .part preservado"
        )
    actual_hash = sha256_file(part)
    if actual_hash != expected_hash:
        bad = _preserve_bad_file(part)
        raise RuntimeError(f"#{clip_id}: SHA-256 divergente; arquivo isolado em {bad.name if bad else 'erro'}")
    os.replace(part, target)
    return f"#{clip_id}: baixado e verificado ({actual_size} bytes)"


def sync_once(session: requests.Session, base_url: str, out_dir: Path, min_free_bytes: int) -> None:
    # Remove do celular apenas clipes que o banco já marca como publicados.
    published = api_json(session, base_url, "/api/clips", {"status": "published", "limit": 1000})
    for row in published:
        if should_remove_on_phone(row.get("status")):
            clip_id = int(row["id"])
            for name in (f"clip_{clip_id}.mp4", f"clip_{clip_id}.mp4.part"):
                candidate = out_dir / name
                if candidate.is_file():
                    candidate.unlink()
                    print(f"#{clip_id}: cópia local removida após publicação confirmada")

    pending = {}
    for state in PENDING_STATES:
        rows = api_json(session, base_url, "/api/clips", {"status": state, "limit": 1000})
        for row in rows:
            if is_pending_status(row.get("status")):
                pending[int(row["id"])] = row

    if not pending:
        print("Nenhum clipe final pendente para baixar.")
        return
    for clip in sorted(pending.values(), key=lambda item: int(item["id"])):
        try:
            print(download_clip(session, base_url, clip, out_dir, min_free_bytes))
        except AuthExpired:
            raise
        except requests.RequestException as exc:
            print(f"#{clip['id']}: falha de rede; .part preservado para tentar novamente: {exc}")
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"#{clip['id']}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sincroniza clipes finais do Xhakinha para o Android")
    parser.add_argument("--url", default=os.environ.get("XH_MOBILE_URL"), help="URL raiz HTTPS do painel web")
    parser.add_argument("--user", default=os.environ.get("XH_MOBILE_USER"), help="usuário administrador do painel")
    parser.add_argument("--dir", dest="out_dir", help="pasta de destino no Android")
    parser.add_argument("--min-free-gb", type=float, default=5.0, help="espaço livre mínimo a preservar (padrão: 5 GB)")
    parser.add_argument("--watch", action="store_true", help="continua tentando a cada intervalo, inclusive após ficar offline")
    parser.add_argument("--interval", type=int, default=300, help="intervalo do modo --watch em segundos")
    args = parser.parse_args()

    base_url = _check_base_url(args.url or input("URL raiz HTTPS do painel web do Xhakinha: "))
    if args.out_dir:
        out_dir = Path(args.out_dir).expanduser().resolve()
    else:
        shared = Path.home() / "storage" / "shared"
        if not shared.exists():
            raise SystemExit("Execute 'termux-setup-storage' e permita o acesso aos arquivos antes de sincronizar.")
        out_dir = shared / "Movies" / "Xhakinha"
    out_dir.mkdir(parents=True, exist_ok=True)

    username = args.user or input("Usuário do painel Xhakinha: ").strip()
    min_free_bytes = max(0, int(args.min_free_gb * 1_000_000_000))
    session = None

    while True:
        try:
            if session is None:
                password = getpass.getpass("Senha do painel Xhakinha (não será salva): ")
                session = login(base_url, username, password)
                del password
                print("Login realizado. Senha e cookie ficam somente na memória desta execução.")
            sync_once(session, base_url, out_dir, min_free_bytes)
        except AuthExpired:
            print("Sessão expirada; será solicitado novo login.")
            session = None
            continue
        except requests.RequestException as exc:
            if not args.watch:
                print(f"Falha de conexão: {exc}", file=sys.stderr)
                return 2
            print(f"Servidor indisponível/offline: {exc}. Nova tentativa em {args.interval}s.")
        except KeyboardInterrupt:
            print("Sincronização interrompida pelo usuário.")
            return 0
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Erro: {exc}", file=sys.stderr)
            if not args.watch:
                return 2
        if not args.watch:
            return 0
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
