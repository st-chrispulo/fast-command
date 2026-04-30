"""OpenAI implementation of the LLMProvider protocol.

Uses ``response_format={"type": "json_schema", ...}`` so the model is
constrained to a JSON shape. Vision is supported by including ``image_url``
content parts encoded as base64 data URLs (no GCS exposure to OpenAI).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

from integrations.llm.provider import LLMImage, LLMProvider, LLMResult

try:
    from logger import logger as _app_logger

    logger = _app_logger.getChild("integrations.llm.openai")
except Exception:
    logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    name = "openai"
    default_model = "gpt-4o-mini"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
    ) -> None:
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set; cannot initialise OpenAIProvider.")
        self._client = AsyncOpenAI(api_key=key)
        if default_model:
            self.default_model = default_model

    @staticmethod
    def _data_url(image: LLMImage) -> str:
        b64 = base64.b64encode(image.bytes_).decode("ascii")
        mime = (image.mime or "application/octet-stream").strip()
        return f"data:{mime};base64,{b64}"

    @classmethod
    def _build_user_content(
        cls,
        user: str,
        images: Optional[List[LLMImage]],
    ) -> Any:
        if not images:
            return user

        parts: List[Dict[str, Any]] = [{"type": "text", "text": user}]
        for img in images:
            parts.append({
                "type": "image_url",
                "image_url": {"url": cls._data_url(img), "detail": "auto"},
            })
        return parts

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
        m = model or self.default_model
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": self._build_user_content(user, images)},
        ]

        kwargs: Dict[str, Any] = {
            "model": m,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "schema": schema,
                    "strict": False,
                },
            },
        }
        if max_output_tokens is not None:
            kwargs["max_tokens"] = max_output_tokens

        client = self._client
        if timeout is not None:
            client = self._client.with_options(timeout=timeout)

        completion = await client.chat.completions.create(**kwargs)
        raw = (completion.choices[0].message.content or "").strip()

        try:
            parsed = json.loads(raw) if raw else {}
        except Exception as e:
            logger.warning("openai_provider: failed to parse JSON: %s", e)
            parsed = {}

        usage: Dict[str, Any] = {}
        if getattr(completion, "usage", None) is not None:
            usage = {
                "prompt_tokens": getattr(completion.usage, "prompt_tokens", None),
                "completion_tokens": getattr(completion.usage, "completion_tokens", None),
                "total_tokens": getattr(completion.usage, "total_tokens", None),
            }

        return LLMResult(
            content=parsed if isinstance(parsed, dict) else {"value": parsed},
            raw=raw,
            model=m,
            provider=self.name,
            usage=usage,
        )


__all__ = ["OpenAIProvider"]
