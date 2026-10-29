"""The guided run's questions. Answers that are settings (keys, provider, model) are
saved to .env, so the next run only asks what changes per video."""

import importlib.util
import os
import shutil
from dataclasses import dataclass

from dotenv import load_dotenv, set_key

from explainer import llm, prompts, ui
from explainer.config import DEFAULT_ANTHROPIC_MODEL, LLM_PROVIDERS, ROOT
from explainer.prompts import Option
from explainer.urls import parse_video_id

ENV_PATH = ROOT / ".env"

_TYPE_IT = "__type__"


@dataclass
class Plan:
    url: str
    transcript: str
    ghost: bool


def ask(reconfigure: bool) -> Plan:
    if not ENV_PATH.exists():
        shutil.copy(ROOT / ".env.example", ENV_PATH)
    load_dotenv(ENV_PATH)

    ui.intro("New blog post")
    _secret("YOUTUBE_API_KEY", "YouTube Data API key", reconfigure)
    model = _llm(reconfigure)

    url = prompts.text("YouTube video URL", validate=_check_url)
    transcript = _transcript()

    # The blog is always saved to generated_blogs/; Ghost is on top of that.
    ghost = prompts.confirm("Also send it to Ghost as a draft?", default=bool(_env("GHOST_ADMIN_API_KEY")))
    if ghost:
        _ghost(reconfigure)

    plan = Plan(url, transcript, ghost)
    prompts.done("Ready to go", f"{parse_video_id(url)} · {model} · {transcript} · {'generated_blogs/ + Ghost' if ghost else 'generated_blogs/'}")
    if not prompts.confirm("Start generating?"):
        raise KeyboardInterrupt
    return plan


# --- settings ---------------------------------------------------------------

def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _save(name: str, value: str) -> None:
    set_key(ENV_PATH, name, value, quote_mode="never")
    os.environ[name] = value


def _secret(name: str, label: str, reconfigure: bool, validate=None) -> None:
    if _env(name) and not reconfigure:
        prompts.done(label, f"{ui.mask(_env(name))}  (saved)")
        return
    _save(name, prompts.text(label, validate=validate or _required, secret=True, shown_as=ui.mask))


def _llm(reconfigure: bool) -> str:
    """Provider, its key and the model; returns the model."""
    saved = _env("LLM_PROVIDER") or "anthropic"
    provider = prompts.select("LLM provider", [Option(name, label) for name, (label, _) in LLM_PROVIDERS.items()], saved)
    label, key_env = LLM_PROVIDERS[provider]
    switched = provider != saved
    _save("LLM_PROVIDER", provider)

    if provider == "custom" and (reconfigure or not _env("LLM_BASE_URL")):
        _save("LLM_BASE_URL", prompts.text("Base URL of the OpenAI-compatible API (ends in /v1)", validate=_required))
    if key_env:
        _secret(key_env, f"{label} API key", reconfigure)

    saved_model = "" if switched else _env("LLM_MODEL")
    if saved_model and not reconfigure and prompts.confirm(f"Keep using {saved_model}?"):
        return saved_model
    model = _model(provider, label, saved_model or (DEFAULT_ANTHROPIC_MODEL if provider == "anthropic" else ""))
    _save("LLM_MODEL", model)
    return model


def _model(provider: str, label: str, default: str) -> str:
    key_env = LLM_PROVIDERS[provider][1]
    try:
        with ui.spinner(f"Loading {label} models"):
            models = llm.list_models(provider, _env(key_env) if key_env else "", _env("LLM_BASE_URL"))
    except Exception as exc:
        prompts.done(f"Couldn't list {label} models", f"{type(exc).__name__}: {exc}")
        models = []
    if not models:
        return prompts.text(f"{label} model name", default=default, validate=_required)

    options = [Option(m, m) for m in models] + [Option(_TYPE_IT, "Type a model name…")]
    model = prompts.select("Model", options, default if default in models else models[0])
    return prompts.text(f"{label} model name", validate=_required) if model == _TYPE_IT else model


def _transcript() -> str:
    whisper = importlib.util.find_spec("faster_whisper") is not None
    needs_whisper = "" if whisper else "needs: pip install -e '.[whisper]'"
    saved = _env("TRANSCRIPT_METHOD") or "captions"
    return prompts.select("Transcript source", [
        Option("captions", "YouTube captions", "fast and free; the video needs captions"),
        Option("whisper", "Whisper", "transcribes on this machine; slower, works without captions", needs_whisper),
        Option("auto", "Auto", "captions when the video has them, otherwise Whisper", needs_whisper),
    ], saved if whisper or saved == "captions" else "captions")


def _ghost(reconfigure: bool) -> None:
    if _env("GHOST_ADMIN_API_KEY") and not reconfigure:
        prompts.done("Ghost", f"{_env('GHOST_URL')} · {ui.mask(_env('GHOST_ADMIN_API_KEY'))}  (saved)")
        return
    _save("GHOST_URL", prompts.text("Ghost URL", default=_env("GHOST_URL") or "http://localhost:2368", validate=_required))
    _save("GHOST_ADMIN_API_KEY", prompts.text("Ghost Admin API key", validate=_check_admin_key, secret=True, shown_as=ui.mask))
    content_key = prompts.text("Ghost Content API key (optional, Enter to skip)", secret=True,
                               shown_as=lambda v: ui.mask(v) if v else "(skipped)")
    if content_key:
        _save("GHOST_CONTENT_API_KEY", content_key)


# --- validation -------------------------------------------------------------

def _required(value: str) -> bool | str:
    return bool(value.strip()) or "Required"


def _check_url(value: str) -> bool | str:
    try:
        parse_video_id(value.strip())
        return True
    except ValueError:
        return "That isn't a YouTube video URL"


def _check_admin_key(value: str) -> bool | str:
    return ":" in value or "The Admin API key looks like <id>:<secret>"
