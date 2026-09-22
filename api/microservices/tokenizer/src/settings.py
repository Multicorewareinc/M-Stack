"""Service configuration. Single Settings class, read from env (and optional .env)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    log_level: str = "INFO"

    # Registry key selected when a /tokenize request omits "tokenizer". Must name a
    # registered backend or the service fails to start (see main.py:create_app).
    tokenizer_default: str = "cl100k_base"
