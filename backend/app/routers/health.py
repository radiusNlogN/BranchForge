"""Liveness endpoint."""

from fastapi import APIRouter

from app import __version__
from app.schemas import HealthRead

router = APIRouter(tags=["health"])


@router.get("/api/health", response_model=HealthRead)
def health() -> HealthRead:
    """Report that the API process is up.

    Intentionally does not touch the database, so it stays useful for answering
    "is the server running?" separately from "is the schema migrated?".
    """
    return HealthRead(status="ok", service="branchforge-backend", version=__version__)
