"""Loads settings from the environment (and `.env` at the project root)."""

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, field_validator

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"
GLOSSARY_PATH = ROOT / "config" / "glossary.txt"

DEFAULT_MODEL = "claude-opus-5-5"


class ConfigError(Exception):
    pass


class Settings(BaseModel):
    youtube_api_key: str = ""
    youtube_update_description: bool = False

    transcript_method: Literal["captions", "whisper", "auto"] = "captions"
    whisper_model: str = "small"
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8"

    anthropic_model: str = DEFAULT_MODEL

    ghost_url: str = "http://localhost:2368"
    ghost_admin_api_key: str = ""
    ghost_content_api_key: str = ""
    ghost_author_email: str = ""
    ghost_primary_tag: str = "video-explainer"
    # Where readers see the article (e.g. the Next.js blog); used for the canonical URL.
    site_url: str = ""

    github_token: str = ""
    slack_webhook_url: str = ""
    webhook_secret: str = ""

    data_dir: Path = ROOT / "data"

    @field_validator("ghost_url", "site_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    def require(self, *names: str) -> None:
        """Fail with a clear message if any of the named settings is empty."""
        missing = [n.upper() for n in names if not getattr(self, n)]
        if missing:
            raise ConfigError(f"Missing required config: {', '.join(missing)} (set it in .env)")


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    values = {}
    for name in Settings.model_fields:
        raw = os.environ.get(name.upper(), "").strip()
        # Empty values fall back to the defaults above.
        if raw:
            values[name] = raw
    return Settings(**values)


def load_glossary() -> list[str]:
    if not GLOSSARY_PATH.exists():
        return []
    lines = GLOSSARY_PATH.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")
