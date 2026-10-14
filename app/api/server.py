"""Запуск FastAPI рядом с ботом (тот же event loop)."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.api.auth import resolve_api_key
from app.api.routes import router
from app.config import get_settings
from app.store import Store

log = logging.getLogger("marketing.api")


def create_app(store: Store) -> FastAPI:
    app = FastAPI(
        title="Marketing API",
        version="1.0.0",
        description=(
            "Отчёты, таблицы и каталог Marketing-бота. "
            "Авторизация: заголовок X-API-Key или Authorization: Bearer <key>."
        ),
    )
    app.state.store = store

    @app.get("/")
    async def root():
        return RedirectResponse("/docs")

    @app.get("/health")
    async def public_health():
        settings = get_settings()
        out = {
            "ok": True,
            "service": "marketing-api",
            "version": "1.0.0",
            "docs": "/docs",
            "api_prefix": "/v1",
        }
        if settings.api_public_url:
            out["public_url"] = settings.api_public_url.rstrip("/")
        return out

    app.include_router(router, prefix="/v1")
    return app


async def start_api_server(store: Store) -> None:
    settings = get_settings()
    if not settings.api_enabled:
        return
    try:
        import uvicorn
    except ImportError:
        log.error("uvicorn не установлен: pip install fastapi uvicorn")
        return

    key = resolve_api_key()
    app = create_app(store)
    config = uvicorn.Config(
        app,
        host=settings.api_host,
        port=int(settings.api_port),
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    log.info(
        "API listening on http://%s:%s/docs (key file data/api_key.txt, len=%s)",
        settings.api_host,
        settings.api_port,
        len(key),
    )
    await server.serve()
