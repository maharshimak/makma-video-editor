"""Runtime configuration.

Defaults are chosen so that `uvicorn makma.main:app` works on a bare machine
with no Docker, no Postgres and no API keys. Every default is overridable
through the environment for real deployments.
"""
from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Secrets that ship with the source and therefore protect nothing.
DEFAULT_SECRETS = {
    "dev-insecure-change-me",
    "change-me-to-a-long-random-string",
    "changeme",
    "secret",
}


class UnsafeConfiguration(RuntimeError):
    """Production configuration that would leave the instance open."""


def _discover(binary: str) -> str:
    """Prefer the vendored static build, then PATH."""
    vendored = REPO_ROOT / "tools" / "ffmpeg" / "bin" / f"{binary}.exe"
    if vendored.exists():
        return str(vendored)
    vendored_nix = REPO_ROOT / "tools" / "ffmpeg" / "bin" / binary
    if vendored_nix.exists():
        return str(vendored_nix)
    found = shutil.which(binary)
    return found or binary


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"), env_prefix="MAKMA_", extra="ignore"
    )

    app_name: str = "Mak'ma Editz"
    #: development | test | production. Production fails closed.
    environment: str = "development"
    debug: bool = True

    # --- storage -------------------------------------------------------
    data_dir: Path = REPO_ROOT / "data"
    storage_backend: str = "local"      # local | s3
    s3_bucket: str | None = None
    s3_endpoint: str | None = None
    s3_region: str = "us-east-1"

    # --- database ------------------------------------------------------
    database_url: str = ""

    # --- jobs ----------------------------------------------------------
    redis_url: str | None = None
    inline_worker: bool = True          # run a worker thread inside the API
    worker_concurrency: int = 2

    # --- media ---------------------------------------------------------
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    proxy_height: int = 720
    proxy_crf: int = 26
    thumbnail_width: int = 320
    max_upload_mb: int = 8192

    # --- ai ------------------------------------------------------------
    anthropic_api_key: str | None = None
    director_model: str = "claude-sonnet-4-5-20250929"
    planner_mode: str = "auto"          # auto | llm | heuristic
    #: How many times a model may be asked to correct an invalid plan before
    #: the request falls back to the offline planner. Each retry is a real
    #: billed call, so this is deliberately small.
    plan_repair_attempts: int = 2
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    #: Model prices, as a path to a JSON file or inline JSON. Overrides the
    #: built-in table, which is only correct on the day it was written.
    model_pricing: str = ""

    # --- auth ----------------------------------------------------------
    secret_key: str = "dev-insecure-change-me"
    access_token_hours: int = 720
    dev_autologin: bool = True

    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    def model_post_init(self, __context) -> None:  # noqa: D105
        # A relative data directory is anchored to the repository, never to
        # the current working directory. `MAKMA_DATA_DIR=./data` otherwise
        # means a different database and a different storage tree depending on
        # where the process was started -- uploads land in one, the editor
        # reads the other, and the media simply is not there.
        data_dir = Path(self.data_dir).expanduser()
        if not data_dir.is_absolute():
            data_dir = (REPO_ROOT / data_dir).resolve()
        object.__setattr__(self, "data_dir", data_dir)

        if not self.ffmpeg_path:
            object.__setattr__(self, "ffmpeg_path", _discover("ffmpeg"))
        if not self.ffprobe_path:
            object.__setattr__(self, "ffprobe_path", _discover("ffprobe"))
        if not self.database_url:
            db_file = Path(self.data_dir) / "makma.db"
            object.__setattr__(
                self, "database_url", f"sqlite:///{db_file.as_posix()}"
            )
        for sub in ("storage", "tmp"):
            (Path(self.data_dir) / sub).mkdir(parents=True, exist_ok=True)

    # -- environment ----------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in ("production", "prod")

    @property
    def is_test(self) -> bool:
        return self.environment.strip().lower() in ("test", "testing")

    def unsafe_settings(self) -> list[str]:
        """Configuration that must not reach production, as readable problems.

        Returned rather than raised so callers can decide: startup refuses,
        while the capabilities endpoint reports. The checks are deliberately
        conservative -- a default that is merely *probably* fine still counts,
        because the cost of getting this wrong is a public instance anyone can
        sign into.
        """
        problems: list[str] = []

        if self.secret_key in DEFAULT_SECRETS:
            problems.append(
                "MAKMA_SECRET_KEY is still the built-in development value. It "
                "signs session tokens and media URLs, so anyone who knows the "
                "default can mint both. Set it to a long random string."
            )
        elif len(self.secret_key) < 32:
            problems.append(
                f"MAKMA_SECRET_KEY is only {len(self.secret_key)} characters. "
                "Use at least 32 from a cryptographic source."
            )

        if self.dev_autologin:
            problems.append(
                "MAKMA_DEV_AUTOLOGIN is on, which hands every unauthenticated "
                "request a shared account. Set it to false."
            )

        if self.debug:
            problems.append(
                "MAKMA_DEBUG is on, which returns exception text to clients. "
                "Set it to false."
            )

        if "*" in self.cors_list:
            problems.append(
                "MAKMA_CORS_ORIGINS allows any origin. List the origins that "
                "should be able to call this API."
            )
        return problems

    def enforce_production_safety(self) -> None:
        """Refuse to start a production process with development defaults."""
        if not self.is_production:
            return
        problems = self.unsafe_settings()
        if problems:
            raise UnsafeConfiguration(
                "Refusing to start in production with development defaults:\n\n"
                + "\n".join(f"  - {p}" for p in problems)
            )

    @property
    def storage_root(self) -> Path:
        return Path(self.data_dir) / "storage"

    @property
    def tmp_root(self) -> Path:
        return Path(self.data_dir) / "tmp"

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
