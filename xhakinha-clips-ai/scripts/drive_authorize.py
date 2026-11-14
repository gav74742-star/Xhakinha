"""Obtém o REFRESH TOKEN do Google Drive pelo fluxo de dispositivo (funciona só com o celular: você digita um
código em google.com/device). Cliente OAuth deve ser do tipo "TVs e dispositivos de entrada limitada".
Uso:  GDRIVE_CLIENT_ID=... GDRIVE_CLIENT_SECRET=... python scripts/drive_authorize.py
Escopo: drive.file (o app só enxerga arquivos que ELE criou — mais seguro)."""
from __future__ import annotations

import os
import sys
import time

import requests

SCOPE = "https://www.googleapis.com/auth/drive.file"


def authorize(cid: str, secret: str, base: str = "https://oauth2.googleapis.com", sleep=time.sleep, out=print) -> str:
    r = requests.post(f"{base}/device/code", data={"client_id": cid, "scope": SCOPE}, timeout=30)
    r.raise_for_status()
    d = r.json()
    out(f"\n1) No celular abra: {d.get('verification_url', 'https://www.google.com/device')}\n2) Digite o código: {d['user_code']}\n3) Aceite o acesso ao Drive.\n")
    deadline = time.time() + int(d.get("expires_in", 900))
    interval = int(d.get("interval", 5))
    while time.time() < deadline:
        sleep(interval)
        t = requests.post(f"{base}/token", data={"client_id": cid, "client_secret": secret, "device_code": d["device_code"],
                                                 "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}, timeout=30)
        j = t.json()
        if "refresh_token" in j:
            return j["refresh_token"]
        err = j.get("error")
        if err == "slow_down":
            interval += 5
        elif err != "authorization_pending":
            raise SystemExit(f"Autorização falhou: {err}")
    raise SystemExit("Código expirou. Rode de novo.")


if __name__ == "__main__":
    tok = authorize(os.environ["GDRIVE_CLIENT_ID"], os.environ["GDRIVE_CLIENT_SECRET"], os.environ.get("GDRIVE_OAUTH_BASE", "https://oauth2.googleapis.com"))
    print("\nGDRIVE_REFRESH_TOKEN (guarde nos Secrets, nunca em arquivo público):\n" + tok)
