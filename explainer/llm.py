"""Thin wrapper over the Anthropic SDK used by the generate stage."""

import logging

import anthropic
from anthropic.types.beta import BetaMessage, BetaMessageParam

log = logging.getLogger(__name__)

# On a safety-classifier decline, the API re-runs the request on Anthropic's
# recommended fallback model instead of returning a refusal.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    pass


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def call(
    *,
    model: str,
    system: str,
    messages: list[BetaMessageParam],
    effort: str,
    max_tokens: int = 64000,
    json_schema: dict | None = None,
) -> BetaMessage:
    """Stream one request and return the final message. Raises LLMError on refusal or truncation."""
    output_config: dict = {"effort": effort}
    if json_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": json_schema}

    # Haiku has no server-side fallback: "default" would silently do nothing there.
    fallback = {} if model.startswith("claude-haiku") else {"betas": [_FALLBACK_BETA], "fallbacks": "default"}
    with _client().beta.messages.stream(
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
    return message


def text_of(message: BetaMessage) -> str:
    return "".join(block.text for block in message.content if block.type == "text")
