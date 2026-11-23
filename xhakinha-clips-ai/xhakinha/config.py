"""Configuração. Nenhum segredo padrão no código: SECRET_KEY é obrigatória e validada."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

WEAK_SECRETS = {
    "", "changeme", "change-me", "secret", "dev", "dev-secret", "password", "admin",
    "troque-esta-chave", "your-secret-key", "supersecret", "default",
}


class ConfigError(RuntimeError):
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        raise ConfigError(f"{name} precisa ser um número inteiro")


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        raise ConfigError(f"{name} precisa ser um número")


@dataclass
class Settings:
    secret_key: str
    data_dir: Path
    timezone: str = "America/Sao_Paulo"
    env: str = "production"
    public_base_url: str = ""          # ex.: https://xhakinha.duckdns.org (obrigatório p/ Instagram)
    cookie_secure: bool = True
    max_workers: int = 4
    min_free_gb: float = 3.0
    max_disk_gb: float = 0.0            # 0 = sem teto próprio (usa só min_free_gb)
    max_upload_mb: int = 2048
    # APIs (todas opcionais; sem chave => conector fica "não configurado")
    youtube_api_key: str = ""
    youtube_daily_quota: int = 9000     # margem de segurança sobre os 10.000 padrão
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    ollama_url: str = ""                # ex.: http://ollama:11434 (opcional)
    ollama_model: str = "qwen2.5:3b"
    whisper_model: str = "small"
    whisper_enabled: bool = True
    # endpoints (sobrescrevíveis para testes com servidor falso)
    youtube_base: str = "https://www.googleapis.com/youtube/v3"
    youtube_rss: str = "https://www.youtube.com/feeds/videos.xml"
    twitch_api: str = "https://api.twitch.tv/helix"
    twitch_oauth: str = "https://id.twitch.tv/oauth2/token"
    tiktok_api: str = "https://open.tiktokapis.com"
    tiktok_auth_url: str = "https://www.tiktok.com/v2/auth/authorize/"
    graph_base: str = "https://graph.facebook.com/v21.0"
    # banco canônico compartilhado (Turso/libSQL). Vazio => SQLite local (comportamento anterior, também é o plano de rollback)
    turso_url: str = ""
    turso_token: str = ""
    state_backend: str = "auto"         # auto | sqlite | turso
    # retentativas
    retry_base_s: float = 20.0
    retry_cap_s: float = 3600.0
    retry_max_attempts: int = 6
    extra: dict = field(default_factory=dict)

    @property
    def use_turso(self) -> bool:
        if self.state_backend == "sqlite":
            return False
        return bool(self.turso_url and self.turso_token)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    # diretórios -------------------------------------------------------
    @property
    def production_dir(self) -> Path:
        return self.data_dir / "production"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "xhakinha.db"

    def validate(self) -> "Settings":
        s = self.secret_key.strip()
        if s.lower() in WEAK_SECRETS or len(s) < 32:
            raise ConfigError(
                "SECRET_KEY ausente, padrão ou curta demais (mínimo 32 caracteres). "
                "Rode: python scripts/setup.py  (gera .env com segredo forte)."
            )
        if self.env == "production" and self.public_base_url and not self.public_base_url.startswith("https://"):
            raise ConfigError("PUBLIC_BASE_URL precisa começar com https:// em produção.")
        if self.state_backend not in ("auto", "sqlite", "turso"):
            raise ConfigError("XH_STATE_BACKEND deve ser auto, sqlite ou turso")
        if self.state_backend == "turso" and not (self.turso_url and self.turso_token):
            raise ConfigError("XH_STATE_BACKEND=turso exige TURSO_DATABASE_URL e TURSO_AUTH_TOKEN")
        if self.turso_url and not self.turso_url.startswith(("libsql://", "https://", "wss://", "http://127.0.0.1", "http://localhost")):
            raise ConfigError("TURSO_DATABASE_URL deve começar com libsql:// (copie 'Database URL' do painel do Turso)")
        try:
            ZoneInfo(self.timezone)
        except Exception:
            raise ConfigError(f"TIMEZONE inválido: {self.timezone}")
        return self

    def ensure_dirs(self) -> None:
        for p in (self.production_dir / "downloads", self.production_dir / "work",
                  self.production_dir / "uploads", self.production_dir / "ready",
                  self.clips_dir, self.backup_dir):
            p.mkdir(parents=True, exist_ok=True)


def load_settings(validate: bool = True) -> Settings:
    s = Settings(
        secret_key=_env("SECRET_KEY"),
        data_dir=Path(_env("DATA_DIR", "./data")).resolve(),
        timezone=_env("TIMEZONE", "America/Sao_Paulo"),
        env=_env("XH_ENV", "production"),
        public_base_url=_env("PUBLIC_BASE_URL").rstrip("/"),
        cookie_secure=_env("COOKIE_SECURE", "1") != "0",
        max_workers=_int("MAX_WORKERS", 4),
        min_free_gb=_float("MIN_FREE_GB", 3.0),
        max_disk_gb=_float("MAX_DISK_GB", 0.0),
        max_upload_mb=_int("MAX_UPLOAD_MB", 2048),
        youtube_api_key=_env("YOUTUBE_API_KEY"),
        youtube_daily_quota=_int("YOUTUBE_DAILY_QUOTA", 9000),
        twitch_client_id=_env("TWITCH_CLIENT_ID"),
        twitch_client_secret=_env("TWITCH_CLIENT_SECRET"),
        tiktok_client_key=_env("TIKTOK_CLIENT_KEY"),
        tiktok_client_secret=_env("TIKTOK_CLIENT_SECRET"),
        ollama_url=_env("OLLAMA_URL").rstrip("/"),
        ollama_model=_env("OLLAMA_MODEL", "qwen2.5:3b"),
        whisper_model=_env("WHISPER_MODEL", "small"),
        whisper_enabled=_env("WHISPER_ENABLED", "1") != "0",
        graph_base=(_env("GRAPH_BASE") or "https://graph.facebook.com/v21.0").rstrip("/"),
        turso_url=_env("TURSO_DATABASE_URL"),
        turso_token=_env("TURSO_AUTH_TOKEN"),
        state_backend=(_env("XH_STATE_BACKEND") or "auto").lower(),
        retry_base_s=_float("RETRY_BASE_S", 20.0),
        retry_cap_s=_float("RETRY_CAP_S", 3600.0),
        retry_max_attempts=_int("RETRY_MAX_ATTEMPTS", 6),
    )
    for attr, env in (("youtube_base", "YOUTUBE_BASE"), ("youtube_rss", "YOUTUBE_RSS"),
                      ("twitch_api", "TWITCH_API"), ("twitch_oauth", "TWITCH_OAUTH"),
                      ("tiktok_api", "TIKTOK_API")):
        if _env(env):
            setattr(s, attr, _env(env).rstrip("/"))
    return s.validate() if validate else s


_SECRET_PAT = re.compile(
    r"(?i)(access_token|refresh_token|client_secret|api[_-]?key|key|token|password|senha|authorization)"
    r"([\"'\s:=]+)(bearer\s+)?([A-Za-z0-9._\-~+/=]{6,})"
)


def redact(text: str) -> str:
    """Remove segredos de qualquer texto antes de logar."""
    if not text:
        return text
    return _SECRET_PAT.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3) or ''}***", str(text))
