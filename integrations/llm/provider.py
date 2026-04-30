"""LLM provider Protocol + data classes.

A provider exposes ONE primary method, ``complete_json``, which:
  - takes a system prompt, user prompt, optional images,
  - constrains the output to a JSON schema,
  - returns a parsed dict + raw text + which model/provider answered.

Keep this surface small. Each concrete provider (OpenAI, Anthropic, ...) maps
its own SDK quirks underneath.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@dataclass
class LLMImage:
    """A single image attachment to be passed to a vision-capable model."""

    bytes_: bytes
    mime: str  # e.g. 'image/jpeg', 'image/png'
    filename: Optional[str] = None


@dataclass
class LLMResult:
    """Normalized output from any provider."""

    content: Dict[str, Any]
    raw: str
    model: str
    provider: str
    usage: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    """Provider Protocol -- structural typing, not inheritance."""

    name: str
    default_model: str

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: Dict[str, Any],
        schema_name: str = "response",
        model: Optional[str] = None,
        images: Optional[List[LLMImage]] = None,
        max_output_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> LLMResult:
        """Run a chat-style completion that MUST return JSON matching ``schema``.

        Args:
            system: System prompt.
            user: User prompt.
            schema: JSON schema the response must conform to.
            schema_name: Identifier for the schema (used by some providers,
                e.g. OpenAI requires a name on json_schema responses).
            model: Override the provider's default model name.
            images: Optional images to include alongside the user prompt.
            max_output_tokens: Cap on output length.
            timeout: Overall request timeout in seconds.

        Returns:
            Parsed dict in ``content``, plus the raw string response.
        """
        ...


__all__ = ["LLMImage", "LLMResult", "LLMProvider"]
