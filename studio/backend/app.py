"""Querly Studio local FastAPI application."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from querly_studio.compiler.pipeline import CHECK_ONLY, COMPILE, CompilerPipeline
from querly_studio.connections import ConnectionService, ProfileStore
from querly_studio.execution import ExecutionService, RunRequest, explain_statement, sample_table
from querly_studio.metadata import inspect_sqlite
from oq.converter.schema_conversion import SchemaConverter, ConversionProfile

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


class ExplainRequestBody(BaseModel):
    connectionId: str
    sql: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Querly Studio backend starting")
    yield
    EXECUTION.connections.close_all()


from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI(title="Querly Studio API", lifespan=lifespan)

# Mount static files for the frontend if they exist
static_dir = Path(__file__).parent / ".." / "frontend" / "dist"
if not static_dir.exists():
    # Fallback for development if frontend is sibling to backend
    static_dir = Path(__file__).parent.parent / "frontend" / "dist"

if static_dir.exists():
    logger.info(f"Serving static files from {static_dir}")
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/")
    async def serve_index():
        return FileResponse(static_dir / "index.html")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # Serve static files directly if they exist
        file_path = static_dir / full_path
        if file_path.is_file():
            return FileResponse(file_path)
        # Fallback to index.html for SPA routing (only if not an /api call)
        if not full_path.startswith("api/"):
             return FileResponse(static_dir / "index.html")
        raise HTTPException(status_code=404, detail="Not found")
else:
    logger.warning(f"Static directory {static_dir} not found. Frontend will not be served.")

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


# -- Connection Management Endpoints --

class ProfileCreateRequest(BaseModel):
    name: str
    adapter: str = "sqlite"
    host: str | None = None
    port: int | None = None
    database: str | None = None
    user: str | None = None
    path: str | None = None
    sslmode: str | None = None
    options: dict[str, str] = {}
    readOnly: bool = False
    password: str | None = None
    createIfMissing: bool = False


class ProfileUpdateRequest(BaseModel):
    name: str | None = None
    path: str | None = None
    readOnly: bool | None = None
    password: str | None = None


@app.post("/connections/profiles")
async def create_profile(request: ProfileCreateRequest) -> dict[str, Any]:
    service = ConnectionService()
    profile = service.create_profile(request.model_dump())
    return profile.to_public()


@app.patch("/connections/profiles/{profile_id}")
async def update_profile(profile_id: str, request: ProfileUpdateRequest) -> dict[str, Any]:
    service = ConnectionService()
    profile = service.update_profile(profile_id, request.model_dump(exclude_unset=True))
    return profile.to_public()


@app.delete("/connections/profiles/{profile_id}")
async def delete_profile(profile_id: str) -> dict[str, str]:
    service = ConnectionService()
    service.delete_profile(profile_id)
    return {"status": "deleted"}


@app.post("/connections/profiles/{profile_id}/test")
async def test_profile(profile_id: str) -> dict[str, Any]:
    service = ConnectionService()
    return service.test(profile_id)


@app.post("/connections/profiles/{profile_id}/connect")
async def connect_profile(profile_id: str) -> dict[str, Any]:
    service = ConnectionService()
    live = service.connect(profile_id)
    return live.public()


@app.delete("/connections/profiles/{profile_id}/disconnect")
async def disconnect_profile(profile_id: str) -> dict[str, str]:
    service = ConnectionService()
    service.disconnect(profile_id)
    return {"status": "disconnected"}


@app.get("/connections/live")
async def list_live() -> list[dict[str, Any]]:
    service = ConnectionService()
    return service.list_live()


@app.get("/connections/profiles/{profile_id}/capabilities")
async def profile_capabilities(profile_id: str) -> dict[str, Any]:
    service = ConnectionService()
    return service.capabilities_for(profile_id)


# -- Schema Explorer Endpoints --

@app.get("/connections/profiles/{profile_id}/schema")
async def list_schema(profile_id: str) -> dict[str, Any]:
    service = ConnectionService()
    live = service.get_live(profile_id)
    adapter = live.adapter
    connection = live.connection

    if live.adapter_id != "sqlite":
        raise HTTPException(status_code=400, detail="Schema inspection only implemented for SQLite")

    schema = inspect_sqlite(connection)
    return schema.model_dump(mode="json")


@app.get("/connections/profiles/{profile_id}/schema/{table_name}")
async def get_table(profile_id: str, table_name: str) -> dict[str, Any]:
    service = ConnectionService()
    live = service.get_live(profile_id)
    adapter = live.adapter
    connection = live.connection

    if live.adapter_id != "sqlite":
        raise HTTPException(status_code=400, detail="Schema inspection only implemented for SQLite")

    schema = inspect_sqlite(connection)
    table = schema.tables.get(table_name)
    if table is None:
        raise HTTPException(status_code=404, detail=f"Table {table_name} not found")
    return table.model_dump(mode="json")


# -- Execution Plan & Sampling Endpoints --

@app.post("/execution/explain")
async def execution_explain(request: ExplainRequestBody) -> dict[str, Any]:
    service = ConnectionService()
    live = service.get_live(request.connectionId)
    return explain_statement(live, request.sql)


class ConverterRequest(BaseModel):
    sourceSchema: str
    sourceDialect: str = "sqlite"
    targetDialect: str = "postgres"


@app.post("/converter/convert")
async def converter_convert(request: ConverterRequest) -> dict[str, Any]:
    from oq.ir import SchemaDocument
    from oq.runtime.contract import TypeSupport, TypeSupportLevel

    # Parse the source schema IR
    try:
        source_schema = SchemaDocument.model_validate_json(request.sourceSchema)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid schema IR: {str(e)}")

    # Build target capabilities based on target dialect
    target_capabilities = {
        "integer": TypeSupport(support=TypeSupportLevel.EXACT, native="INTEGER"),
        "text": TypeSupport(support=TypeSupportLevel.EXACT, native="TEXT"),
        "timestamp": TypeSupport(support=TypeSupportLevel.COMPATIBLE, native="DATETIME"),
    }

    profile = ConversionProfile.COMPATIBLE
    converter = SchemaConverter(
        profile=profile,
        source_adapter=request.sourceDialect,
        target_adapter=request.targetDialect,
    )

    result = converter.convert(source_schema, target_capabilities, target_dialect=request.targetDialect)
    return result.model_dump(mode="json")


@app.get("/connections/profiles/{profile_id}/sample/{table_name}")
async def get_sample(profile_id: str, table_name: str, limit: int = 100) -> dict[str, Any]:
    service = ConnectionService()
    live = service.get_live(profile_id)
    return sample_table(live, table_name, limit=limit)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
