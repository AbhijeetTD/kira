"""LLM client — Ollama (local, OpenAI-compatible API)."""
from __future__ import annotations

import logging

from openai import AsyncOpenAI, APIError, APIConnectionError

from backend.config import settings

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    """Lazy-initialise the Ollama async client."""
    global _client  # noqa: PLW0603
    if _client is None:
        _client = AsyncOpenAI(
            base_url=settings.ollama_base_url,
            api_key="ollama",   # Ollama ignores the key; SDK requires a non-empty value
            timeout=120.0,      # local models can be slow on first token
        )
        logger.info("LLM client → Ollama at %s (model: %s)", settings.ollama_base_url, settings.ollama_model)
    return _client


async def generate(prompt: str, system: str = "") -> str:
    """Send a chat request to Ollama and return the full response text."""
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        response = await _get_client().chat.completions.create(
            model=settings.ollama_model,
            messages=messages,
            max_tokens=2048,
        )
        return response.choices[0].message.content or ""
    except APIConnectionError as exc:
        logger.error("Ollama connection failed: %s", exc)
        raise RuntimeError(f"Ollama unreachable at {settings.ollama_base_url}: {exc}") from exc
    except APIError as exc:
        logger.error("Ollama API error: %s", exc)
        raise RuntimeError(f"Ollama API error: {exc}") from exc


async def check_health() -> bool:
    """Return True if Ollama is reachable and the model responds."""
    try:
        response = await _get_client().chat.completions.create(
            model=settings.ollama_model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
        )
        return bool(response.choices)
    except Exception:  # noqa: BLE001
        return False
