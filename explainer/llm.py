"""LLM calls for the generate and code stages, for any provider.

Anthropic goes through its SDK. Every other provider goes through the OpenAI-style
`/chat/completions` endpoint, which OpenAI, Gemini, Groq, DeepSeek, OpenRouter,
Mistral and Ollama all serve; `LLM_BASE_URL` points it at any other compatible API.
"""

import json
import logging
import re

import anthropic
import httpx
from pydantic import BaseModel

from explainer import net
from explainer.config import LLM_PROVIDERS, ConfigError, Settings

log = logging.getLogger(__name__)

# On a safety-classifier decline, the API re-runs the request on Anthropic's
# recommended fallback model instead of returning a refusal.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Base URLs of the OpenAI-compatible providers.
BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "groq": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "mistral": "https://api.mistral.ai/v1",
    "ollama": "http://localhost:11434/v1",
}

# A long article can take minutes to generate.
_TIMEOUT = 600


class LLMError(Exception):
    pass


# Model ids that aren't chat models (embeddings, speech, images, moderation, ...).
_NOT_CHAT = re.compile(r"embed|tts|whisper|transcribe|audio|realtime|dall-e|image|moderation|guard|ocr|davinci|babbage|sora")


def list_models(provider: str, api_key: str, base_url: str = "") -> list[str]:
    """Chat model ids the provider offers to this key, newest first where the API says."""
    if provider == "anthropic":
        return [model.id for model in anthropic.Anthropic(api_key=api_key).models.list(limit=100)]
    base_url = base_url or BASE_URLS.get(provider, "")
    if not base_url:
        raise LLMError(f"LLM_PROVIDER={provider} needs LLM_BASE_URL")
    response = net.request(
        "GET",
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
    ).json()
    models = sorted(response.get("data", []), key=lambda m: m.get("created") or 0, reverse=True)
    # Gemini lists ids as "models/<id>"; the chat endpoint takes the bare id.
    ids = [m["id"].removeprefix("models/") for m in models]
    return list(dict.fromkeys(i for i in ids if not _NOT_CHAT.search(i.lower())))


def call(
    settings: Settings,
    *,
    system: str,
    messages: list[dict],
    effort: str,
    shape: type[BaseModel],
    max_tokens: int = 64000,
) -> str:
    """Send one request whose reply must be JSON matching `shape`; return the reply text.

    `messages` hold plain-string contents. Raises LLMError on refusal or truncation.
    """
    key_env = LLM_PROVIDERS[settings.llm_provider][1]
    if key_env and not settings.llm_api_key:
        raise ConfigError(f"Missing required config: {key_env} (LLM_PROVIDER is {settings.llm_provider}; set it in .env)")
    settings.require("llm_model")
    if settings.llm_provider == "anthropic":
        return _call_anthropic(settings, system, messages, effort, shape, max_tokens)
    return _call_openai_compatible(settings, system, messages, shape)


def _call_anthropic(settings, system, messages, effort, shape, max_tokens) -> str:
    output_config = {"effort": effort, "format": {"type": "json_schema", "schema": anthropic.transform_schema(shape)}}
    # Haiku has no server-side fallback: "default" would silently do nothing there.
    model = settings.llm_model
    fallback = {} if model.startswith("claude-haiku") else {"betas": [_FALLBACK_BETA], "fallbacks": "default"}
    client = anthropic.Anthropic(api_key=settings.llm_api_key)
    with client.beta.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
        output_config=output_config,
        **fallback,
    ) as stream:
        message = stream.get_final_message()

    usage = message.usage
    log.info(
        "LLM %s: %s in / %s out tokens, stop=%s (request %s)",
        # Streamed final messages don't carry _request_id; a log line must never lose a paid response.
        message.model, usage.input_tokens, usage.output_tokens, message.stop_reason,
        getattr(message, "_request_id", None),
    )
    if message.stop_reason == "refusal":
        details = message.stop_details
        raise LLMError(f"Model declined the request ({details.category if details else 'no category'})")
    if message.stop_reason == "max_tokens":
        raise LLMError(f"Output hit max_tokens={max_tokens} and was truncated")
    return "".join(block.text for block in message.content if block.type == "text")


def _call_openai_compatible(settings, system, messages, shape) -> str:
    base_url = settings.llm_base_url or BASE_URLS.get(settings.llm_provider)
    if not base_url:
        raise LLMError(f"LLM_PROVIDER={settings.llm_provider} needs LLM_BASE_URL")
    schema = shape.model_json_schema()
    # Not every compatible API enforces response_format, so the schema is in the prompt too.
    system = f"{system}\n\nReply with only a JSON object matching this JSON schema:\n{json.dumps(schema)}"
    body = {
        "model": settings.llm_model,
        "messages": [{"role": "system", "content": system}, *messages],
        "response_format": {"type": "json_schema", "json_schema": {"name": shape.__name__, "schema": schema, "strict": False}},
    }
    with httpx.Client(timeout=_TIMEOUT) as client:
        response = net.request(
            "POST",
            f"{base_url.rstrip('/')}/chat/completions",
            client=client,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {},
            json=body,
        ).json()

    choice = response["choices"][0]
    usage = response.get("usage") or {}
    log.info(
        "LLM %s: %s in / %s out tokens, stop=%s (request %s)",
        response.get("model", settings.llm_model), usage.get("prompt_tokens"), usage.get("completion_tokens"),
        choice.get("finish_reason"), response.get("id"),
    )
    if choice.get("finish_reason") == "length":
        raise LLMError("Output hit the model's token limit and was truncated")
    if choice["message"].get("refusal"):
        raise LLMError(f"Model declined the request ({choice['message']['refusal']})")
    return _strip_fences(choice["message"].get("content") or "")


def _strip_fences(text: str) -> str:
    """Some models wrap JSON in a ```json fence even when asked not to."""
    match = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, re.DOTALL)
    return match.group(1) if match else text
