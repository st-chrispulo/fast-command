from __future__ import annotations

import os
from typing import Set

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.core.lifespan import lifespan
from app.core.logging import get_logger
from app.core.settings import settings
from commands.invoker import command_registry
from app.api.router import api_router


logger = get_logger("solitud")


def register_static_uploads(app: FastAPI) -> None:
    """Mount static upload directories declared by registered commands."""
    mounted_paths: Set[str] = set()

    for command in command_registry:
        upload_dir = getattr(command, "upload_dir", None)
        static_mount = getattr(command, "static_mount", None)

        if not upload_dir or not static_mount:
            continue

        if static_mount in mounted_paths:
            continue

        os.makedirs(upload_dir, exist_ok=True)
        app.mount(static_mount, StaticFiles(directory=upload_dir), name=static_mount.strip("/"))
        mounted_paths.add(static_mount)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title=settings.app_name,
        description=settings.app_description,
        version=settings.app_version,
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        lifespan=lifespan,
    )

    app.add_middleware(GZipMiddleware, minimum_size=settings.gzip_minimum_size)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if settings.static_enabled:
        register_static_uploads(app)

    # app.include_router(router)

    app.include_router(api_router, prefix="/api")
    @app.get("/healthz", tags=["system"])
    def healthz() -> dict:
        return {"ok": True}

    return app


app = create_app()
logger.info("Application initialized.")
