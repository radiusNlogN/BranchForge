"""Liveness endpoint."""

from fastapi import APIRouter

from app import __version__
from app.config import settings
from app.schemas import HealthRead

router = APIRouter(tags=["health"])


@router.get("/api/health", response_model=HealthRead)
def health() -> HealthRead:
    """Report that the API process is up, and whether this deployment has a dispatcher.

    Intentionally does not touch the database, so it stays useful for answering
    "is the server running?" separately from "is the schema migrated?".
    `dispatcher_available` is read from configuration; it does not check that a
    dispatcher process is alive.
    """
    return HealthRead(
        status="ok",
        service="branchforge-backend",
        version=__version__,
        dispatcher_available=settings.dispatcher_available,
    )
