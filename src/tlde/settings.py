"""Declarative configuration for tlde.

A single ``tlde.toml`` (overridable by env vars and CLI flags) configures the
target, input sources, ingestion, web research, the binary loop, concurrency,
and per-role models/providers. Precedence, highest first:

    CLI flags  >  environment (incl. .env)  >  tlde.toml  >  built-in defaults

The model and provider for each agent role are resolved by :func:`for_role`,
which replaces the old single global ``TLDE_MODEL`` override while keeping it
working as a backward-compatible global fallback.

Usage::

    from tlde import settings
    settings.init_settings(config_path="tlde.toml",
                           cli_overrides={"provider": "openrouter"})
    manager = AGENTS["firmware_emulation_manager"](**settings.for_role("firmware_emulation_manager"))
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from tlde.providers import KNOWN_PROVIDERS

# Map the agent-registry key (module name in tlde.agents) to its config role.
ROLE_BY_AGENT: dict[str, str] = {
    "firmware_emulation_manager": "manager",
    "fw_emu_eng": "engineer",
    "fw_verif_eng": "verifier",
    "emu_test_agg": "tester",
    "fw_failure_classifier": "failure_classifier",
}

# Roles that resolve to an LLM provider (embedder may also be the local "local").
LLM_ROLES = ("manager", "engineer", "verifier", "tester", "failure_classifier", "vision")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TargetConfig(BaseModel):
    board: str | None = None
    soc: str | None = None
    arch: str | None = None
    cpu: str | None = None


class SourcesConfig(BaseModel):
    reference_manual: str | None = None
    schematic: str | None = None
    svd: str | None = None
    dts: str | None = None
    header: str | None = None
    extra: list[str] = Field(default_factory=list)
    precedence: list[str] = Field(
        default_factory=lambda: [
            "svd", "dts", "vendor_header", "renode_upstream", "reference_manual",
        ]
    )


class ResearchConfig(BaseModel):
    mode: Literal["autonomous", "approval", "off"] = "autonomous"
    max_sources: int = 10
    allowlist: list[str] = Field(
        default_factory=lambda: [
            "nordicsemi.com",
            "developer.arm.com",
            "arm.com",
            "github.com/zephyrproject-rtos",
            "github.com/renode",
            "renode.io",
        ]
    )
    trust_tiers: dict[str, int] = Field(
        default_factory=lambda: {
            "vendor": 1, "arm_cmsis": 2, "zephyr": 2, "renode": 2,
            "standards": 3, "distributor": 4, "community": 5,
        }
    )


class IngestConfig(BaseModel):
    cache_dir: str = ".tlde_cache"
    strictness: Literal["fail_closed", "quarantine", "warn"] = "fail_closed"
    min_coverage: float = 0.7
    chunking: str = "table_aware"
    top_k: int = 12
    rerank: bool = True
    vision: Literal["auto", "on", "off"] = "auto"


class BinariesConfig(BaseModel):
    dir: str = "target_binaries"
    run_loop: bool = True
    max_attempts: int = 4
    virtual_time_budget_s: int = 30
    default_success_tier: int = 1
    renode_bin: str | None = None       # explicit path; else auto-discovered
    wall_timeout_s: int = 180           # hard wall-clock guard around a Renode run


class TestingConfig(BaseModel):
    samples_build: bool = True


class ConcurrencyConfig(BaseModel):
    max_parallel_units: int = 4
    verify_retries: int = 3

    @field_validator("max_parallel_units")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_parallel_units must be >= 1")
        return v


class ModelsConfig(BaseModel):
    manager: str = "claude-opus-4.6"
    engineer: str = "claude-sonnet-4.6"
    verifier: str = "claude-sonnet-4.6"
    tester: str = "claude-sonnet-4.6"
    failure_classifier: str = "claude-sonnet-4.6"
    vision: str = "claude-sonnet-4.6"
    embedder: str = "BAAI/bge-small-en-v1.5"          # ~130 MB, local
    reranker: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"  # ~80 MB (bge-reranker-base is 1.1 GB)


class ProvidersConfig(BaseModel):
    default: str = "github"
    manager: str | None = None
    engineer: str | None = None
    verifier: str | None = None
    tester: str | None = None
    failure_classifier: str | None = None
    vision: str | None = None
    embedder: str | None = None  # "local" or a BYOK provider name

    @field_validator("default", "manager", "engineer", "verifier", "tester",
                      "failure_classifier", "vision")
    @classmethod
    def _known_llm_provider(cls, v: str | None) -> str | None:
        if v is not None and v not in KNOWN_PROVIDERS:
            raise ValueError(
                f"unknown provider {v!r}; choose one of {sorted(KNOWN_PROVIDERS)}"
            )
        return v


class Settings(BaseModel):
    target: TargetConfig = Field(default_factory=TargetConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    binaries: BinariesConfig = Field(default_factory=BinariesConfig)
    testing: TestingConfig = Field(default_factory=TestingConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_settings: Settings | None = None
_cli_overrides: dict[str, str] = {}


def _load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Existing environment variables are NOT overwritten (explicit env wins, per
    the precedence rules). Lines starting with ``#`` and blanks are ignored.
    """
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge ``overlay`` onto ``base`` (overlay wins; lists replace)."""
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"[config] {path} is not valid TOML: {e}")


def init_settings(
    config_path: str | os.PathLike = "tlde.toml",
    cli_overrides: dict[str, str] | None = None,
    env_file: str | os.PathLike = ".env",
    base_path: str | os.PathLike = "tlde.toml",
) -> Settings:
    """Load .env, parse the project config, and cache the result.

    ``tlde.toml`` (``base_path``) is the base; a different ``--config`` file is
    deep-merged **on top** of it (so e.g. an OpenRouter profile only needs to set
    [providers]/[models] and still inherits [target]/[sources]). ``cli_overrides``
    may carry ``provider``/``model`` (a global override applied by :func:`for_role`).
    """
    global _settings, _cli_overrides
    _load_dotenv(env_file)
    _cli_overrides = {k: v for k, v in (cli_overrides or {}).items() if v}

    data: dict = {}
    base = Path(base_path)
    if base.is_file():
        data = _read_toml(base)

    overlay = Path(config_path) if config_path else None
    if overlay is not None and str(overlay) != str(base):
        if overlay.is_file():
            data = _deep_merge(data, _read_toml(overlay))
        else:
            raise SystemExit(f"[config] --config file not found: {overlay}")

    try:
        _settings = Settings(**data)
    except ValidationError as e:
        raise SystemExit(f"[config] invalid settings:\n{e}")
    return _settings


def get_settings() -> Settings:
    """Return the cached settings, loading defaults lazily if needed."""
    global _settings
    if _settings is None:
        _settings = init_settings()
    return _settings


def for_role(agent_key: str, s: Settings | None = None) -> dict:
    """Resolve constructor kwargs (model, provider) for an agent registry key.

    Precedence — model:    CLI --model  > TLDE_<ROLE>_MODEL  > TLDE_MODEL  > tlde.toml [models].<role>
    Precedence — provider: CLI --provider > TLDE_<ROLE>_PROVIDER > TLDE_PROVIDER
                           > tlde.toml [providers].<role> > tlde.toml [providers].default

    Pass ``s`` to resolve against a specific Settings object (else the cached one).
    """
    s = s or get_settings()
    role = ROLE_BY_AGENT.get(agent_key, agent_key)
    rolu = role.upper()

    model = (
        _cli_overrides.get("model")
        or os.environ.get(f"TLDE_{rolu}_MODEL")
        or os.environ.get("TLDE_MODEL")
        or getattr(s.models, role, None)
    )
    provider = (
        _cli_overrides.get("provider")
        or os.environ.get(f"TLDE_{rolu}_PROVIDER")
        or os.environ.get("TLDE_PROVIDER")
        or getattr(s.providers, role, None)
        or s.providers.default
    )

    kwargs: dict = {}
    if model:
        kwargs["model"] = model
    if provider:
        kwargs["provider"] = provider
    return kwargs
