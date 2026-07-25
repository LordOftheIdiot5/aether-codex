"""LLM factory — one place to switch between Claude, Grok and local models.

Every agent (Director included) gets its model through `get_llm()`, so adding a
new provider means adding one branch here and nothing else changes.
"""

from __future__ import annotations

import os

from .config import settings

# Sensible defaults per provider; override with AETHER_MODEL or per-Director.
DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "grok": "grok-4",
    "local": "llama3.1",
}


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Environment variable {name} is not set. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


def get_llm(provider: str | None = None, model: str | None = None,
            temperature: float | None = None):
    """Return a LangChain chat model for the requested provider.

    provider: "anthropic" (Claude), "grok"/"xai" (xAI), "local"/"ollama".
    Falls back to the AETHER_PROVIDER / AETHER_MODEL environment settings.
    """
    provider = (provider or settings.provider).lower()
    if provider == "xai":
        provider = "grok"
    if provider == "ollama":
        provider = "local"

    model = model or settings.model or DEFAULT_MODELS.get(provider)
    temperature = settings.temperature if temperature is None else temperature

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        _require_env("ANTHROPIC_API_KEY")
        # Claude Sonnet 5 rejects the temperature parameter (400: "temperature
        # is deprecated for this model") and runs adaptive thinking by default,
        # so we omit temperature and give max_tokens headroom for thinking.
        # max_retries handles transient 429/5xx/529 (overloaded) errors with
        # exponential backoff instead of killing a long project run.
        # 32k max_tokens: adaptive thinking + a long final answer (e.g. a
        # multi-contract audit write-up) can otherwise exhaust the budget and
        # return empty visible text on large inputs.
        return ChatAnthropic(model=model, max_tokens=32000, max_retries=6)

    if provider == "grok":
        # xAI exposes an OpenAI-compatible API, so we reuse langchain-openai.
        from langchain_openai import ChatOpenAI

        api_key = _require_env("XAI_API_KEY")
        return ChatOpenAI(
            model=model,
            temperature=temperature,
            api_key=api_key,
            base_url="https://api.x.ai/v1",
            max_retries=6,
        )

    if provider == "local":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=model,
            temperature=temperature,
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        )

    raise ValueError(
        f"Unknown provider '{provider}'. Use one of: anthropic, grok, local."
    )
