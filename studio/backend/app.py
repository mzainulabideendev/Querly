"""Querly Studio local FastAPI application."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from querly_studio.compiler.pipeline import CHECK_ONLY, COMPILE, CompilerPipeline
from querly_studio.connections import ConnectionService
from querly_studio.execution import ExecutionService, RunRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("querly_studio")

PIPELINE = CompilerPipeline()
EXECUTION = ExecutionService(connections=ConnectionService())


class CompileRequest(BaseModel):
    source: str
    dialect: str = "sqlite"


class CheckRequest(BaseModel):
    source: str


class RunRequestBody(BaseModel):
    connectionId: str
    statements: list[Any] = []
    parameters: dict[str, Any] = {}
    maxRows: int = 500
    timeoutMs: int = 15000
    readOnly: bool | None = None
    confirmDestructive: bool = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Querly Studio backend starting")
    yield
    EXECUTION.connections.close_all()


app = FastAPI(title="Querly Studio API", lifespan=lifespan)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": "0.10.0"}


@app.post("/compiler/check")
async def compiler_check(request: CheckRequest) -> dict[str, Any]:
    result = PIPELINE.check(request.source, file_name="<memory>")
    return result.to_dict()


@app.post("/compiler/compile")
async def compiler_compile(request: CompileRequest) -> dict[str, Any]:
    result = PIPELINE.compile(
        request.source,
        file_name="<memory>",
        options=None,
    )
    return result.to_dict()


@app.post("/execution/run")
async def execution_run(request: RunRequestBody) -> dict[str, Any]:
    run_request = RunRequest(
        connection_id=request.connectionId,
        statements=request.statements,
        parameters=request.parameters,
        max_rows=request.maxRows,
        timeout_ms=request.timeoutMs,
        read_only=request.readOnly,
        confirm_destructive=request.confirmDestructive,
    )
    result = EXECUTION.run(run_request)
    return result.public()


@app.get("/connections/profiles")
async def list_profiles() -> list[dict[str, Any]]:
    from querly_studio.connections import ConnectionService, ProfileStore

    service = ConnectionService()
    profiles = service.store.list()
    return [profile.to_public() for profile in profiles]


@app.get("/capabilities")
async def capabilities() -> dict[str, Any]:
    return {
        "verified": ["sqlite"],
        "unverified": ["all"],
        "adapters": EXECUTION.connections.registry.list(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
