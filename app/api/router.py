from __future__ import annotations

from fastapi import APIRouter

from app.api.v0.router import router

api_router = APIRouter()
api_router.include_router(router, prefix="/v0")

