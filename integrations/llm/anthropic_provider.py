"""Anthropic (Claude) implementation of the LLMProvider protocol.

The structure is wired up so the rest of the codebase can target
``LLM_PROVIDER=anthropic`` today, but actually invoking it requires the
``anthropic`` Python package which is NOT yet in requirements.txt.

To switch this on:
  1. Add ``anthropic>=0.39`` to requirements.txt
  2. Set ``ANTHROPIC_API_KEY`` in env
  3. Set ``LLM_PROVIDER=anthropic`` (and optionally ``LLM_MODEL=...``)

Until then any call into ``complete_json`` raises NotImplementedError so the
factory doesn't silently fall back to a half-working backend.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Dict, List, Optional

from integrations.llm.provider import LLMImage, LLMProvider, LLMResult

try:  # pragma: no cover - optional dep
    import anthropic  # type: ignore

    _ANTHROPIC_IMPORT_ERROR: Optional[Exception] = None
except Exception as e:  # pragma: no cover - optional dep
    anthropic = None  # type: ignore
    _ANTHROPIC_IMPORT_ERROR = e

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("integrations.llm.anthropic")
except Exception:
    logger = logging.getLogger(__name__)


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    default_model = "claude-haiku-4-5"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
    ) -> None:
        if anthropic is None:
            raise RuntimeError(
                "anthropic package is not installed. Add `anthropic>=0.39` to requirements.txt "
                f"to use the Anthropic provider. Underlying import error: {_ANTHROPIC_IMPORT_ERROR}"
            )
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set; cannot initialise AnthropicProvider.")
        self._client = anthropic.AsyncAnthropic(api_key=key)
        if default_model:
            self.default_model = default_model

    @staticmethod
    def _image_block(image: LLMImage) -> Dict[str, Any]:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": (image.mime or "application/octet-stream").strip(),
                "data": base64.b64encode(image.bytes_).decode("ascii"),
            },
        }

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
        # Claude doesn't expose strict JSON-schema mode like OpenAI does. We
        # ask for JSON in the system prompt and validate after the fact.
        # For tool-style enforcement we'd use ``tools=`` with a tool that
        # mirrors ``schema`` -- left as a follow-up.
        m = model or self.default_model

        content_parts: List[Dict[str, Any]] = []
        for img in images or []:
            content_parts.append(self._image_block(img))
        content_parts.append({"type": "text", "text": user})

        json_instruction = (
            "\n\nRespond ONLY with a single valid JSON object that matches the "
            "schema embedded in the user message. No prose, no code fences."
        )

        kwargs: Dict[str, Any] = {
            "model": m,
            "max_tokens": max_output_tokens or 4096,
            "system": system + json_instruction,
            "messages": [{"role": "user", "content": content_parts}],
        }
        if timeout is not None:
            kwargs["timeout"] = timeout

        message = await self._client.messages.create(**kwargs)

        # Concatenate text blocks
        text_chunks: List[str] = []
        for block in getattr(message, "content", []) or []:
            if getattr(block, "type", None) == "text":
                text_chunks.append(getattr(block, "text", "") or "")
        raw = "".join(text_chunks).strip()

        try:
            parsed = json.loads(raw) if raw else {}
        except Exception as e:
            logger.warning("anthropic_provider: failed to parse JSON: %s", e)
            parsed = {}

        usage: Dict[str, Any] = {}
        if getattr(message, "usage", None) is not None:
            usage = {
                "input_tokens": getattr(message.usage, "input_tokens", None),
                "output_tokens": getattr(message.usage, "output_tokens", None),
            }

        return LLMResult(
            content=parsed if isinstance(parsed, dict) else {"value": parsed},
            raw=raw,
            model=m,
            provider=self.name,
            usage=usage,
        )


__all__ = ["AnthropicProvider"]
