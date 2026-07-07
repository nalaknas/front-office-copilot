"""FastAPI entry point for the Front Office Copilot API."""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from api.logging import configure_logging

configure_logging()

app = FastAPI(title="Front Office Copilot", version="0.1.0")


class HealthResponse(BaseModel):
    status: str


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")
