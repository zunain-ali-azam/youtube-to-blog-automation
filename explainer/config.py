"""Loads settings from the environment (and `.env` at the project root)."""

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, field_validator

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"
GLOSSARY_PATH = ROOT / "config" / "glossary.txt"

# Used when LLM_PROVIDER is anthropic and LLM_MODEL is empty; other providers need LLM_MODEL.
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"

# LLM_PROVIDER value -> (name shown in the guided run, env var holding its API key).
# Ollama runs locally and needs no key.
LLM_PROVIDERS = {
    "anthropic": ("Anthropic (Claude)", "ANTHROPIC_API_KEY"),
    "openai": ("OpenAI", "OPENAI_API_KEY"),
    "gemini": ("Google Gemini", "GEMINI_API_KEY"),
    "groq": ("Groq", "GROQ_API_KEY"),
    "deepseek": ("DeepSeek", "DEEPSEEK_API_KEY"),
    "openrouter": ("OpenRouter", "OPENROUTER_API_KEY"),
    "mistral": ("Mistral", "MISTRAL_API_KEY"),
    "ollama": ("Ollama (local)", None),
    "custom": ("Other OpenAI-compatible API", "LLM_API_KEY"),
}


class ConfigError(Exception):
    pass


class Settings(BaseModel):
    youtube_api_key: str = ""
    youtube_update_description: bool = False

    transcript_method: Literal["captions", "whisper", "auto"] = "captions"
    whisper_model: str = "small"
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8"

    llm_provider: str = "anthropic"  # a key of LLM_PROVIDERS
    # Filled from the provider's own key variable (GEMINI_API_KEY, ...), see load_settings.
    llm_api_key: str = ""
    llm_model: str = ""
    llm_base_url: str = ""

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
    # Every finished blog is copied here as <slug>.md, whether or not it also goes to Ghost.
    blogs_dir: Path = ROOT / "generated_blogs"

    @field_validator("llm_provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        if v not in LLM_PROVIDERS:
            raise ValueError(f"LLM_PROVIDER must be one of: {', '.join(LLM_PROVIDERS)}")
        return v

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
    provider = values.get("llm_provider", "anthropic")
    key_env = LLM_PROVIDERS.get(provider, (None, None))[1]
    values["llm_api_key"] = os.environ.get(key_env, "").strip() if key_env else ""
    if provider == "anthropic" and not values.get("llm_model"):
        # ANTHROPIC_MODEL is what older .env files call it.
        values["llm_model"] = os.environ.get("ANTHROPIC_MODEL", "").strip() or DEFAULT_ANTHROPIC_MODEL
    return Settings(**values)


def load_glossary() -> list[str]:
    if not GLOSSARY_PATH.exists():
        return []
    lines = GLOSSARY_PATH.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")
