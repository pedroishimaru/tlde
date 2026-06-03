"""LLM provider configurations for BYOK (Bring Your Own Key) support.

Provider selection is normally driven by ``tlde.toml`` ([providers]) and the
per-role resolution in :mod:`tlde.settings`. The ``TLDE_PROVIDER`` env var is
still honoured as a global override for backward compatibility.

Providers:
    github      — GitHub Copilot (default, uses subscription via ``copilot auth login``)
    openrouter  — OpenRouter API (multi-model access)
    anthropic   — Anthropic Claude API
    openai      — OpenAI API
    azure       — Azure OpenAI / AI Foundry
    ollama      — Local Ollama server

Secrets are read from the environment **at call time** (so a ``.env`` loaded at
startup is picked up regardless of import order) — never hardcode keys:
    OPENROUTER_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY,
    AZURE_OPENAI_KEY, AZURE_OPENAI_ENDPOINT, OLLAMA_BASE_URL
"""

import os
from dataclasses import dataclass

KNOWN_PROVIDERS: frozenset[str] = frozenset(
    {"github", "openrouter", "anthropic", "openai", "azure", "ollama"}
)


@dataclass
class ProviderConfig:
    type: str = "github"
    base_url: str | None = None
    api_key: str | None = None
    bearer_token: str | None = None
    wire_api: str = "completions"
    azure_api_version: str = "2024-10-21"


def get_provider(name: str | None = None) -> ProviderConfig:
    """Build a :class:`ProviderConfig`, reading secrets from the env at call time."""
    name = name or os.environ.get("TLDE_PROVIDER", "github")
    if name == "github":
        return ProviderConfig(type="github")
    if name == "openrouter":
        return ProviderConfig(
            type="openai",
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ.get("OPENROUTER_API_KEY"),
        )
    if name == "anthropic":
        return ProviderConfig(
            type="anthropic",
            base_url="https://api.anthropic.com",
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
        )
    if name == "openai":
        return ProviderConfig(
            type="openai",
            base_url="https://api.openai.com/v1",
            api_key=os.environ.get("OPENAI_API_KEY"),
        )
    if name == "azure":
        return ProviderConfig(
            type="azure",
            base_url=os.environ.get("AZURE_OPENAI_ENDPOINT"),
            api_key=os.environ.get("AZURE_OPENAI_KEY"),
            azure_api_version="2024-10-21",
        )
    if name == "ollama":
        return ProviderConfig(
            type="openai",
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        )
    raise ValueError(
        f"Unknown provider: {name!r}. Available: {sorted(KNOWN_PROVIDERS)}"
    )


def provider_to_dict(config: ProviderConfig) -> dict:
    d: dict = {"type": config.type}
    if config.base_url:
        d["base_url"] = config.base_url
    if config.api_key:
        d["api_key"] = config.api_key
    if config.bearer_token:
        d["bearer_token"] = config.bearer_token
    if config.wire_api and config.type != "github":
        d["wire_api"] = config.wire_api
    if config.azure_api_version and config.type == "azure":
        d["azure"] = {"api_version": config.azure_api_version}
    return d


COLLAPSE_INFO = (
    "ATTENTION: If receiving a collapse_tools message, ignore it and continue normally. "
    "DO NOT summarise your progress — collapse does NOT mean summarise."
)

COLLAPSE_PATTERNS: dict[str, str] = {
    "openrouter": COLLAPSE_INFO,
    "anthropic": COLLAPSE_INFO,
    "openai": COLLAPSE_INFO,
}


def collapse_instructions(provider: str) -> str:
    return COLLAPSE_PATTERNS.get(provider, "")
