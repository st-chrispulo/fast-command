"""Pick the right LLMProvider based on env config.

Env precedence (most specific wins):
  1. ``LLM_PROVIDER_<USE_CASE>`` / ``LLM_MODEL_<USE_CASE>``
  2. ``LLM_PROVIDER`` / ``LLM_MODEL`` (global default)
  3. Hard-coded default: openai / gpt-4o-mini

Use cases used in this codebase:
  - PARTNER_SUMMARIZER
  - PARTNER_PART6
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from integrations.llm.provider import LLMProvider

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("integrations.llm.factory")
except Exception:
    logger = logging.getLogger(__name__)


_PROVIDER_DEFAULT = "openai"
_MODEL_DEFAULTS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
}


def _resolve(use_case: Optional[str], key: str) -> Optional[str]:
    if use_case:
        v = os.environ.get(f"{key}_{use_case.upper()}")
        if v:
            return v.strip()
    v = os.environ.get(key)
    return v.strip() if v else None


def get_llm(use_case: Optional[str] = None) -> LLMProvider:
    """Return the configured provider for the given use-case."""
    provider_name = (_resolve(use_case, "LLM_PROVIDER") or _PROVIDER_DEFAULT).lower()
    model = _resolve(use_case, "LLM_MODEL") or _MODEL_DEFAULTS.get(provider_name)

    if provider_name == "openai":
        from integrations.llm.openai_provider import OpenAIProvider

        logger.debug("get_llm use_case=%s provider=openai model=%s", use_case, model)
        return OpenAIProvider(default_model=model)

    if provider_name == "anthropic":
        from integrations.llm.anthropic_provider import AnthropicProvider

        logger.debug("get_llm use_case=%s provider=anthropic model=%s", use_case, model)
        return AnthropicProvider(default_model=model)

    raise RuntimeError(
        f"Unknown LLM_PROVIDER '{provider_name}'. Supported: 'openai', 'anthropic'."
    )


__all__ = ["get_llm"]
