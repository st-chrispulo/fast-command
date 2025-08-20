from commands.base_command import BaseCommand
from auth.db import SessionLocal
from fastapi import HTTPException
from pydantic import BaseModel, field_validator, model_validator
from pathlib import Path
import os
from typing import Optional

try:
    from openai import OpenAI
except ImportError:
    raise HTTPException(status_code=500, detail="openai package not installed")

TEMPLATE_ROOT = Path.cwd() / "template-nextjs"

class TransformTemplatePayload(BaseModel):
    template_name: Optional[str] = None
    code: Optional[str] = None
    instructions: str
    model: str = "gpt-4o-mini"

    @field_validator("template_name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("template_name must not be empty when provided")
        if any(ch in v for ch in r"\/:*?<>|"):
            raise ValueError("template_name contains invalid characters")
        return v

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("code must not be empty when provided")
        return v

    @field_validator("instructions")
    @classmethod
    def validate_instructions(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("instructions must not be empty")
        return v

    @model_validator(mode="after")
    def check_source_inputs(self):
        if not self.code and not self.template_name:
            raise ValueError("Either 'code' or 'template_name' must be provided")
        return self

def _ci_get(base: Path, name: str):
    lname = name.lower()
    for p in base.iterdir():
        if p.name.lower() == lname:
            return p
    return None

def _read_template_source(template_name: str) -> str:
    t = template_name.strip()
    root = TEMPLATE_ROOT
    if not root.exists() or not root.is_dir():
        raise HTTPException(status_code=500, detail=f"Template root not found: {root}")

    file = None
    for ext in ("tsx", "jsx"):
        cand = _ci_get(root, f"{t}.{ext}")
        if cand and cand.is_file():
            file = cand
            break

    if not file:
        d = _ci_get(root, t)
        if d and d.is_dir():
            for idxname in ("index.tsx", "index.jsx"):
                idx = _ci_get(d, idxname)
                if idx and idx.is_file():
                    file = idx
                    break

    if file:
        return file.read_text(encoding="utf-8")

    raise HTTPException(
        status_code=404,
        detail=(
            f"Template source not found for '{template_name}' (case-insensitive). "
            f"Checked {t}.tsx/.jsx and {t}/index.tsx/.jsx"
        ),
    )

def _build_prompt(instructions: str, full_source: str) -> list:
    sys = (
        "You are a precise React + Tailwind UI refactorer. Modify ONLY JSX markup/classes to satisfy styling requests. "
        "Do not change logic, props, imports/exports, file structure, or add libraries/assets. "
        "Preserve all dynamic expressions and types. Keep minimal diffs without restructuring DOM. "
        "Apply requested styles to the main exported component, resolving conflicts simply. "
        "Map casual color names to Tailwind, defaulting to 500; use inline style only if no utility exists. "
        "Output the full source file as plain text, no code fences or comments."
    )

    usr = (
        "User instructions:\n"
        f"{instructions}\n\n"
        "Here is the full component source file. Apply the changes only to the JSX while preserving everything else:\n\n"
        f"{full_source}\n\n"
        "Return the full, updated source file."
    )
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": usr},
    ]

def _strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t

class TransformTemplateRenderCommand(BaseCommand):
    name = "transform_template_render"
    schema = TransformTemplatePayload
    require_auth = False

    def run(self, payload: TransformTemplatePayload):
        _ = SessionLocal()
        try:
            if payload.code:
                src = payload.code
                template_id = payload.template_name or "<inline-code>"
            else:
                src = _read_template_source(payload.template_name or "")
                template_id = payload.template_name

            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            messages = _build_prompt(payload.instructions, src)

            rsp = client.chat.completions.create(
                model=payload.model,
                messages=messages,
                temperature=0.2,
            )
            content = rsp.choices[0].message.content if rsp.choices else ""
            content = _strip_code_fences(content or "")
            if not content.strip():
                raise HTTPException(status_code=502, detail="Model returned empty content")
            return {
                "status": "ok",
                "template": template_id,
                "model": payload.model,
                "code": content,
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
