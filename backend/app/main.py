"""FastAPI application factory."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config import Settings, settings
from app.routers import health, runs

DESCRIPTION = (
    "BranchForge investigates GitHub issues with competing agent-generated fixes. "
    "This milestone covers run intake and persistence only: runs are validated and "
    "stored in the `pending` state, and are never executed."
)


def create_app(app_settings: Settings | None = None) -> FastAPI:
    app_settings = app_settings or settings

    app = FastAPI(
        title="BranchForge API",
        description=DESCRIPTION,
        version=__version__,
    )

    # Allowed origins come from configuration (CORS_ORIGINS), not a hardcoded list.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    app.include_router(health.router)
    app.include_router(runs.router)
    return app


app = create_app()
