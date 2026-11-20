"""The HTTP API: list saved queries, preview one, and read the run history.

Read-only: nothing here adds to Radarr or Sonarr. Every route under /api except
/api/health needs the `X-Api-Key` header. Start it with `titularr serve`; the
interactive docs are at /docs.

The config file is read on every request, so edits show up without a restart. Routes
are plain functions, which FastAPI runs in a thread pool, so previews can overlap.
"""

import secrets
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from titularr import service
from titularr.arr import ArrError, RadarrClient, SonarrClient
from titularr.config import Config, ConfigError, load_config
from titularr.history import Command, History, HistoryError, RecordedTitle, Result, Run
from titularr.models import Decision, Kind
from titularr.sources.tmdb import TmdbClient


class Health(BaseModel):
    status: str


class SavedQueryInfo(BaseModel):
    """A saved query: its name, and its settings with the defaults filled in."""

    name: str
    settings: dict[str, Any]


class Counts(BaseModel):
    """How many titles of one kind were found, matched the title check, and of those,
    were excluded, kept, or already in the library (the rest were filtered out)."""

    found: int
    matched: int
    excluded: int
    kept: int
    in_library: int


class Preview(BaseModel):
    """What a saved query finds and would add. `error` is set when the search stopped
    part-way (a TMDB or network error); everything else is then up to that point."""

    query: str
    decisions: list[Decision]
    counts: dict[Kind, Counts]
    library_checked: dict[Kind, bool]
    warnings: list[str]
    error: str | None


class ResultCount(BaseModel):
    kind: Kind
    result: Result
    count: int


class RunInfo(BaseModel):
    """One recorded --apply run. `finished_at` is null if it stopped before finishing."""

    id: int
    started_at: datetime
    finished_at: datetime | None
    command: Command
    query_name: str | None
    query_text: str
    counts: list[ResultCount]

    @classmethod
    def of(cls, run: Run) -> "RunInfo":
        counts = [
            ResultCount(kind=kind, result=result, count=count)
            for (kind, result), count in sorted(run.counts.items())
        ]
        return cls(**{**asdict(run), "counts": counts})


class RunDetail(RunInfo):
    """A run and what it did with each title, in order."""

    titles: list[RecordedTitle]


def create_app(
    config_path: Path,
    api_key: str,
    *,
    tmdb: Callable[[str], TmdbClient] = TmdbClient,
    radarr: Callable[[str, str], RadarrClient] = RadarrClient,
    sonarr: Callable[[str, str], SonarrClient] = SonarrClient,
) -> FastAPI:
    """The API app. `api_key` is what the X-Api-Key header must be. `tmdb`, `radarr`
    and `sonarr` make the clients; tests pass fakes."""
    expected_key = api_key.encode("utf-8")
    key_header = APIKeyHeader(name="X-Api-Key", auto_error=False)

    def check_key(key: Annotated[str | None, Security(key_header)]) -> None:
        if key is None or not secrets.compare_digest(key.encode("utf-8"), expected_key):
            raise HTTPException(401, "missing or wrong X-Api-Key header")

    def config(*, need_secrets: bool) -> Config:
        return load_config(config_path, need_secrets=need_secrets)

    app = FastAPI(
        title="Titularr",
        summary="Preview saved title queries and read the run history (read-only).",
    )

    @app.exception_handler(ConfigError)
    @app.exception_handler(HistoryError)
    def server_problem(request: Request, error: Exception) -> JSONResponse:
        # A config file that broke after startup, or an unusable history file.
        return JSONResponse(status_code=500, content={"detail": str(error)})

    @app.get("/api/health", tags=["status"])
    def health() -> Health:
        """Whether the service is up. Needs no API key."""
        return Health(status="ok")

    api = APIRouter(prefix="/api", dependencies=[Depends(check_key)])

    @api.get("/queries", tags=["queries"])
    def list_queries() -> list[SavedQueryInfo]:
        """The queries saved in the config file."""
        # Listing contacts nothing, so the TMDB/Radarr/Sonarr secrets needn't be set.
        loaded = config(need_secrets=False)
        return [
            SavedQueryInfo(
                name=name,
                settings=saved.model_dump(mode="json", exclude={"global_exclusions"}),
            )
            for name, saved in loaded.queries.items()
        ]

    @api.get(
        "/queries/{name}/preview",
        tags=["queries"],
        responses={
            404: {"description": "No saved query by that name"},
            502: {"description": "Radarr or Sonarr can't be used"},
        },
    )
    def preview(name: str) -> Preview:
        """Search for a saved query's titles and say what would be added, and why each
        other title was dropped. Changes nothing."""
        loaded = config(need_secrets=True)
        try:
            query = loaded.query(name)
        except ConfigError as e:
            raise HTTPException(404, str(e)) from e
        try:
            with tmdb(loaded.sources.tmdb.token) as source:
                result = service.preview(query, source, loaded, radarr=radarr, sonarr=sonarr)
        except ArrError as e:
            raise HTTPException(502, str(e)) from e
        return Preview(
            query=name,
            decisions=result.decisions,
            counts={kind: Counts(**asdict(c)) for kind, c in result.summary.counts.items()},
            library_checked={kind: result.library.checks(kind) for kind in query.kinds},
            warnings=result.warnings,
            error=result.error,
        )

    @api.get("/history", tags=["history"])
    def list_runs(limit: Annotated[int, Query(ge=1, le=200)] = 20) -> list[RunInfo]:
        """The latest --apply runs, newest first."""
        path = config(need_secrets=False).history_path
        if not path.exists():
            return []
        with History.open(path) as recorded:
            return [RunInfo.of(run) for run in recorded.recent_runs(limit)]

    @api.get(
        "/history/{run_id}",
        tags=["history"],
        responses={404: {"description": "No run with that ID"}},
    )
    def get_run(run_id: int) -> RunDetail:
        """One run, with what it did with each title."""
        path = config(need_secrets=False).history_path
        if not path.exists():
            raise HTTPException(404, f"no run #{run_id} in the run history")
        with History.open(path) as recorded:
            run = recorded.get_run(run_id)
            if run is None:
                raise HTTPException(404, f"no run #{run_id} in the run history")
            titles = recorded.run_titles(run_id)
        return RunDetail(**RunInfo.of(run).model_dump(), titles=titles)

    app.include_router(api)
    return app
