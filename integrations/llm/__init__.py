"""Provider-agnostic LLM access used across the codebase.

Pick a provider via env (``LLM_PROVIDER``) and a model via ``LLM_MODEL``.
Per use-case overrides take precedence, e.g. ``LLM_PROVIDER_PARTNER_SUMMARIZER``.

Today: OpenAI is the only working backend. Anthropic is structured but raises
NotImplementedError until the ``anthropic`` package is added to requirements.
"""

from integrations.llm.factory import get_llm
from integrations.llm.provider import LLMImage, LLMProvider, LLMResult

__all__ = ["LLMProvider", "LLMResult", "LLMImage", "get_llm"]
