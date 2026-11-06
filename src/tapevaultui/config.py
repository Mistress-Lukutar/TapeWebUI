"""
File:   config.py
Brief:  Application settings via pydantic-settings (TAPE_* env prefix).
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.4.0
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """TapeWebUI settings, sourced from TAPE_* environment variables and .env."""

    model_config = SettingsConfigDict(
        env_prefix="TAPE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8091, ge=1, le=65535)  # 8090 is taken by OledWebUI
    data_dir: Path = Field(default=PROJECT_ROOT / "data")
    log_dir: Path = Field(default=PROJECT_ROOT / "data" / "logs")

    # ITDT-SE CLI: the only data path to the drive (see .agents/AGENTS.md).
    itdt_exe: Path = Field(default=Path("C:/Program Files/ITDT/itdt.exe"))
    device_name: str = Field(default="\\\\.\\tape0")
    force_generic_dd: bool = Field(default=True)
    itdt_timeout_s: int = Field(default=600, ge=1)
    # How long to wait before retrying when ITDT cannot open the device;
    # transient "Open failed" states were observed on this machine.
    itdt_open_retry_s: float = Field(default=2.0, ge=0.0)
    itdt_open_retries: int = Field(default=3, ge=0)

    # Drive status polling interval in seconds.
    drive_poll_interval: float = Field(default=15.0, ge=1.0)
    # Disable real ITDT subprocesses (tests / offline UI development).
    fake_transport: bool = Field(default=False)

    @property
    def jobs_dir(self) -> Path:
        """Directory with YAML job definitions."""
        return self.data_dir / "jobs"

    @property
    def catalog_db_path(self) -> Path:
        """Path to the SQLite catalog database."""
        return self.data_dir / "catalog.db"

    def ensure_dirs(self) -> None:
        """Create runtime data directories if missing."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
