"""OpenAI-compatible LLM client for the read-only copilot (V1.2.2 Phase C1).

Provider abstraction so a weaker model can be swapped for a stronger one
without prompt hacks. Two providers are configured for eval:

- ``deepseek``     -> base https://api.deepseek.com/v1, model deepseek-v4-flash
- ``deepseek-pro`` -> base https://api.deepseek.com/v1, model deepseek-v4-pro

Config comes from the environment (``COPILOT_LLM_*``), never from code or git:

- ``COPILOT_LLM_BASE_URL`` / ``COPILOT_LLM_MODEL`` / ``COPILOT_LLM_TIMEOUT`` —
  default provider (also ``COPILOT_LLM_PROVIDER``, default ``deepseek``).
- ``COPILOT_LLM_DEEPSEEK_*`` / ``COPILOT_LLM_DEEPSEEK_PRO_*`` — per-provider
  overrides (``_BASE_URL``, ``_MODEL``).
- ``COPILOT_LLM_API_KEY`` — the API key; falls back to ``DEEPSEEK_API_KEY``.

The interface is a thin OpenAI-compatible ``chat/completions`` wrapper over
httpx, so adding another OpenAI-compatible provider (DashScope/qwen, ...) is a
one-line registry entry. Errors are typed and fail-closed: timeouts and 5xx
never yield partial/fabricated text.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

ENV_PREFIX = "COPILOT_LLM_"
DEFAULT_PROVIDER = "deepseek"
# Reasoning models (deepseek-v4-*) burn tokens/time on reasoning_content;
# 120s covers slow cold starts without hiding provider errors.
DEFAULT_TIMEOUT_SECONDS = 120.0

# Default registry (env vars override per provider). The interface is
# OpenAI-compatible, so adding e.g. qwen/dashscope is just a new entry.
PROVIDER_BASE_URLS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com/v1",
    "deepseek-pro": "https://api.deepseek.com/v1",
    # C1.1 fast non-reasoning lane (same key, deepseek's chat model): wired
    # for Hermes' latency comparison — NOT the default for any surface yet.
    "deepseek-chat": "https://api.deepseek.com/v1",
}
PROVIDER_MODELS: dict[str, str] = {
    "deepseek": "deepseek-v4-flash",
    "deepseek-pro": "deepseek-v4-pro",
    "deepseek-chat": "deepseek-chat",
}

# Reasoning kind per provider (for docs/comparison; routing is centralized).
PROVIDER_KINDS: dict[str, str] = {
    "deepseek": "reasoning",
    "deepseek-pro": "reasoning",
    "deepseek-chat": "non-reasoning",
}

# Centralized provider profile map (Requirement 6): which profile uses which
# provider. TODAY = None (deterministic-first, NO LLM on the critical path);
# EXPLAIN = fast non-reasoning; ASK = strong. Env-tunable per profile via
# ``COPILOT_LLM_PROFILE_<PROFILE>`` (e.g. COPILOT_LLM_PROFILE_ASK).
# Do NOT switch a profile's default without eval evidence (Hermes owns the
# model-latency comparison; defaults stay conservative).
PROVIDER_PROFILES: dict[str, str | None] = {
    "TODAY": None,
    "EXPLAIN": "deepseek-chat",
    "ASK": "deepseek-pro",
}


class LLMProviderError(RuntimeError):
    """Provider unreachable / server error / malformed response (fail-closed)."""


class UnknownProviderError(LLMProviderError):
    """Requested provider name is not in the registry."""


class LLMTimeoutError(LLMProviderError):
    """The LLM call exceeded the configured timeout."""


@dataclass(frozen=True)
class LLMResult:
    """Normalized completion result from any provider."""

    text: str
    model: str
    provider: str
    latency_ms: int
    version: str | None = None


@dataclass(frozen=True)
class ProviderConfig:
    """Resolved configuration for one provider call."""

    name: str
    base_url: str
    api_key: str
    model: str
    timeout: float


def _env(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value is not None and value.strip() else None


def _env_key(name: str) -> str:
    return name.upper().replace("-", "_")


def list_providers() -> list[str]:
    """Stable list of configured provider names."""
    return sorted(PROVIDER_MODELS)


def profile_provider(profile: str, requested: str | None = None) -> str | None:
    """Resolve the provider for a surface profile (centralized routing).

    ``requested`` (an explicit client-provided provider) always wins; the
    profile default comes from the env override ``COPILOT_LLM_PROFILE_<P>``
    then ``PROVIDER_PROFILES``. ``TODAY`` resolves to ``None`` (no LLM).
    """
    if requested is not None:
        return requested
    if profile not in PROVIDER_PROFILES:
        raise ValueError(f"unknown copilot provider profile {profile!r}")
    return _env(f"{ENV_PREFIX}PROFILE_{profile}") or PROVIDER_PROFILES[profile]


def provider_config(name: str | None = None) -> ProviderConfig:
    """Resolve provider config from env (secrets only from env, never git)."""
    resolved = name or _env(f"{ENV_PREFIX}PROVIDER") or DEFAULT_PROVIDER
    if resolved not in PROVIDER_MODELS:
        raise UnknownProviderError(
            f"unknown copilot LLM provider {resolved!r}; "
            f"known providers: {', '.join(list_providers())}"
        )
    suffix = _env_key(resolved)
    base_url = (
        _env(f"{ENV_PREFIX}{suffix}_BASE_URL")
        or _env(f"{ENV_PREFIX}BASE_URL")
        or PROVIDER_BASE_URLS[resolved]
    )
    model = (
        _env(f"{ENV_PREFIX}{suffix}_MODEL")
        or _env(f"{ENV_PREFIX}MODEL")
        or PROVIDER_MODELS[resolved]
    )
    api_key = (
        _env(f"{ENV_PREFIX}{suffix}_API_KEY")
        or _env(f"{ENV_PREFIX}API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or ""
    )
    timeout = float(_env(f"{ENV_PREFIX}TIMEOUT") or DEFAULT_TIMEOUT_SECONDS)
    return ProviderConfig(
        name=resolved,
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        model=model,
        timeout=timeout,
    )


def get_llm_client(provider: str | None = None) -> "LLMClient":
    """Build the default or named provider client (raises on unknown)."""
    return LLMClient(provider_config(provider))


class LLMClient:
    """OpenAI-compatible chat-completions client over httpx."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport

    @classmethod
    def from_env(cls, provider: str | None = None) -> "LLMClient":
        return cls(provider_config(provider))

    def complete(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.2,
        max_tokens: int = 700,
        response_format: dict | None = None,
    ) -> LLMResult:
        """POST /chat/completions and return the normalized completion."""
        if not self.config.api_key:
            raise LLMProviderError(
                f"copilot LLM provider {self.config.name!r} has no API key "
                "(set COPILOT_LLM_API_KEY or DEEPSEEK_API_KEY)"
            )
        url = f"{self.config.base_url}/chat/completions"
        payload: dict = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        try:
            with httpx.Client(timeout=self.config.timeout, transport=self._transport) as client:
                resp = client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"copilot LLM provider {self.config.name!r} timed out after "
                f"{self.config.timeout}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError(
                f"copilot LLM provider {self.config.name!r} unreachable: {exc}"
            ) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code >= 500:
            raise LLMProviderError(
                f"copilot LLM provider {self.config.name!r} server error: "
                f"HTTP {resp.status_code}"
            )
        if resp.status_code != 200:
            raise LLMProviderError(
                f"copilot LLM provider {self.config.name!r} error: "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            data = resp.json()
            text = data["choices"][0]["message"].get("content")
            returned_model = data.get("model") or self.config.model
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError(
                f"copilot LLM provider {self.config.name!r} returned a "
                "malformed response"
            ) from exc
        if not text or not str(text).strip():
            raise LLMProviderError(
                f"copilot LLM provider {self.config.name!r} returned empty "
                "completion content (reasoning model consumed the budget?)"
            )
        return LLMResult(
            text=text,
            model=self.config.model,
            provider=self.config.name,
            latency_ms=latency_ms,
            version=returned_model,
        )
