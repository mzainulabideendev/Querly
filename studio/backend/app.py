"""Querly Studio local FastAPI application."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from oq.converter.schema_conversion import ConversionProfile, SchemaConverter
from oq.ir import Safety
from pydantic import BaseModel
from querly_studio import __version__
from querly_studio.compiler.pipeline import CompilerPipeline, PipelineOptions
from querly_studio.connections import ConnectionService as _ConnectionService
ConnectionService = _ConnectionService
from querly_studio.execution import ExecutionService, RunRequest, explain_statement, sample_table
from querly_studio.metadata import inspect_sqlite

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("querly_studio")

PIPELINE = CompilerPipeline()
SERVICE = ConnectionService()
EXECUTION = ExecutionService(connections=SERVICE)

# Keep in sync with DIALECTS in studio/frontend/src/components/Layout.tsx.
SUPPORTED_DIALECTS = ("sqlite", "postgres", "mysql", "sqlserver", "oracle", "clickhouse")


class CompileRequest(BaseModel):
    source: str
    dialect: str = "sqlite"


class CheckRequest(BaseModel):
    source: str


class RunRequestBody(BaseModel):
    connectionId: str
    source: str | None = None
    dialect: str = "sqlite"
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


app = FastAPI(title="Querly Studio API", lifespan=lifespan)


def _resolve_static_dir() -> Path | None:
    """Locate the built frontend in dev and in a frozen PyInstaller bundle."""
    candidates: list[Path] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        # ``--add-data "studio/frontend/dist:<dest>"`` extracts to this folder.
        candidates.append(Path(meipass) / "static")

    here = Path(__file__).resolve().parent
    candidates.extend(
        [
            here / "static",
            here / ".." / "frontend" / "dist",
            here.parent / "frontend" / "dist",
        ]
    )

    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate.resolve()
    return None


static_dir = _resolve_static_dir()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__}


@app.get("/compiler/dialects")
async def compiler_dialects() -> dict[str, Any]:
    return {"dialects": list(SUPPORTED_DIALECTS), "default": "sqlite"}


@app.post("/compiler/check")
async def compiler_check(request: CheckRequest) -> dict[str, Any]:
    result = PIPELINE.check(request.source, file_name="<memory>")
    return result.to_dict()


@app.post("/compiler/compile")
async def compiler_compile(request: CompileRequest) -> dict[str, Any]:
    result = PIPELINE.compile(
        request.source,
        file_name="<memory>",
        options=PipelineOptions(dialect=request.dialect),
    )
    return result.to_dict()


def _coerce_statement(item: Any) -> Any:
    """Turn a statement received over JSON back into an attribute object."""
    if not isinstance(item, dict):
        return item

    safety = item.get("safety")
    return SimpleNamespace(
        id=item.get("id", "stmt"),
        kind=item.get("kind", "select"),
        sql=item.get("sql", ""),
        safety=Safety.model_validate(safety) if isinstance(safety, dict) else Safety(),
        parameters=item.get("parameters") or [],
        parameter_values=item.get("parameterValues") or [],
        warnings=item.get("warnings") or [],
        source_name=item.get("sourceName"),
    )


@app.post("/execution/run")
async def execution_run(request: RunRequestBody) -> dict[str, Any]:
    statements = [_coerce_statement(item) for item in request.statements]

    if request.source is not None:
        compilation = PIPELINE.compile(
            request.source,
            file_name="<memory>",
            options=PipelineOptions(dialect=request.dialect),
        )
        if not compilation.ok:
            return {
                "ok": False,
                "connectionId": request.connectionId,
                "statements": [],
                "needsConfirmation": False,
                "error": "compilation failed",
                "diagnostics": compilation.diagnostics,
            }
        statements = list(compilation.compiled)

    run_request = RunRequest(
        connection_id=request.connectionId,
        statements=statements,
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
    service = SERVICE
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
    service = SERVICE
    profile = service.create_profile(request.model_dump())
    return profile.to_public()


@app.patch("/connections/profiles/{profile_id}")
async def update_profile(profile_id: str, request: ProfileUpdateRequest) -> dict[str, Any]:
    service = SERVICE
    profile = service.update_profile(profile_id, request.model_dump(exclude_unset=True))
    return profile.to_public()


@app.delete("/connections/profiles/{profile_id}")
async def delete_profile(profile_id: str) -> dict[str, str]:
    service = SERVICE
    service.delete_profile(profile_id)
    return {"status": "deleted"}


@app.post("/connections/profiles/{profile_id}/test")
async def test_profile(profile_id: str) -> dict[str, Any]:
    service = SERVICE
    return service.test(profile_id)


@app.post("/connections/profiles/{profile_id}/connect")
async def connect_profile(profile_id: str) -> dict[str, Any]:
    service = SERVICE
    live = service.connect(profile_id)
    return live.public()


@app.delete("/connections/profiles/{profile_id}/disconnect")
async def disconnect_profile(profile_id: str) -> dict[str, str]:
    service = SERVICE
    service.disconnect(profile_id)
    return {"status": "disconnected"}


@app.get("/connections/live")
async def list_live() -> list[dict[str, Any]]:
    service = SERVICE
    return service.list_live()


@app.get("/connections/profiles/{profile_id}/capabilities")
async def profile_capabilities(profile_id: str) -> dict[str, Any]:
    service = SERVICE
    return service.capabilities_for(profile_id)


# -- Schema Explorer Endpoints --


@app.get("/connections/profiles/{profile_id}/schema")
async def list_schema(profile_id: str) -> dict[str, Any]:
    service = SERVICE
    live = service.get_live(profile_id)
    connection = live.connection

    if live.adapter_id != "sqlite":
        raise HTTPException(status_code=400, detail="Schema inspection only implemented for SQLite")

    schema = inspect_sqlite(connection)
    return schema.model_dump(mode="json")


@app.get("/connections/profiles/{profile_id}/schema/{table_name}")
async def get_table(profile_id: str, table_name: str) -> dict[str, Any]:
    service = SERVICE
    live = service.get_live(profile_id)
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
    service = SERVICE
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
        raise HTTPException(status_code=400, detail=f"Invalid schema IR: {e!s}") from e

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

    result = converter.convert(
        source_schema, target_capabilities, target_dialect=request.targetDialect
    )
    return {
        "source_schema": result.source_schema.model_dump(mode="json"),
        "target_schema": result.target_schema.model_dump(mode="json"),
        "compatibility_report": result.compatibility_report,
        "migration_plan": [a.model_dump(mode="json") if hasattr(a, "model_dump") else asdict(a) for a in (result.migration_plan or [])],
        "conversion_status": result.conversion_status.value,
        "profile": result.profile.value,
        "findings": result.findings,
        "warnings": result.warnings,
    }


@app.get("/connections/profiles/{profile_id}/sample/{table_name}")
async def get_sample(profile_id: str, table_name: str, limit: int = 100) -> dict[str, Any]:
    service = SERVICE
    live = service.get_live(profile_id)
    return sample_table(live, table_name, limit=limit)


# Serve the compiled frontend, if present. These routes are registered last so
# that the SPA catch-all cannot shadow the API routes above.
if static_dir is not None:
    logger.info("Serving static files from %s", static_dir)
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def serve_index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str) -> FileResponse:
        file_path = static_dir / full_path
        if file_path.is_file():
            return FileResponse(file_path)
        api_prefixes = ("api/", "compiler/", "connections/", "execution/", "converter/")
        if full_path == "health" or full_path.startswith(api_prefixes):
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(static_dir / "index.html")

else:
    logger.warning("Built frontend not found; static files will not be served.")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
