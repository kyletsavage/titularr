"""Tests for the HTTP API (`titularr serve`)."""

import httpx2
import pytest
from fastapi.testclient import TestClient

from titularr import cli
from titularr.api import create_app
from titularr.arr import ArrError, SonarrLibrary
from titularr.history import History
from titularr.models import Candidate
from titularr.sources.tmdb import TmdbClient

KEY = "test-api-key"
AUTH = {"X-Api-Key": KEY}

MOVIES = [
    {
        "id": 1,
        "title": "A Christmas Story",
        "release_date": "1983-11-18",
        "vote_count": 2900,
        "original_language": "en",
    },
    {
        "id": 2,
        "title": "Christmastime",
        "release_date": "2012-12-01",
        "vote_count": 4,
        "original_language": "en",
    },
    {
        "id": 3,
        "title": "Secret Santa",
        "release_date": "2015-12-04",
        "vote_count": 300,
        "original_language": "en",
    },
    {
        "id": 5,
        "title": "Christmas Vacation",
        "release_date": "1989-12-01",
        "vote_count": 900,
        "original_language": "en",
    },
]
SERIES = [
    {"id": 10, "name": "Christmas Wars", "first_air_date": "2022-12-13", "original_language": "en"}
]

CONFIG = """\
sources:
  tmdb:
    token: ${TMDB_TOKEN}
radarr:
  url: http://radarr:7878
  api_key: radarr-key
queries:
  christmas:
    text: christmas
    kinds: [movie]
    filters:
      min_votes: 50
    scheduled: true
  christmas-both:
    text: christmas
"""


def page(results):
    return {"page": 1, "total_pages": 1, "total_results": len(results), "results": results}


def tmdb_answers(request):
    """Fake TMDB: the movie and series lists above, and no runtimes."""
    if request.url.path.startswith("/3/movie/"):
        return httpx2.Response(200, json={"runtime": 0})
    if request.url.path.endswith("/movie"):
        return httpx2.Response(200, json=page(MOVIES))
    return httpx2.Response(200, json=page(SERIES))


def series_fail(request):
    """Fake TMDB whose movie search works and series search fails."""
    if request.url.path.endswith("/tv"):
        return httpx2.Response(503)
    return tmdb_answers(request)


class FakeArr:
    """Stands in for RadarrClient and SonarrClient: a context manager with a library."""

    def __init__(self, ids=(), error=None):
        self.ids = frozenset(ids)
        self.error = error

    def __call__(self, url, key):
        return self

    def __enter__(self):
        if self.error:
            raise ArrError(self.error)
        return self

    def __exit__(self, *exc):
        return None

    def library_tmdb_ids(self):
        return set(self.ids)

    def library(self):
        return SonarrLibrary(tmdb_ids=self.ids, titles_without_tmdb_id=())


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.setenv("TMDB_TOKEN", "tmdb-token")
    path = tmp_path / "config.yaml"
    path.write_bytes(CONFIG.encode("utf-8"))
    return path


def client(config_path, tmdb=tmdb_answers, radarr=None):
    app = create_app(
        config_path,
        KEY,
        tmdb=lambda token: TmdbClient(token, transport=httpx2.MockTransport(tmdb)),
        radarr=radarr or FakeArr(ids={5}),
    )
    return TestClient(app)


def test_health_needs_no_key(config_path):
    response = client(config_path).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "path", ["/api/queries", "/api/queries/christmas/preview", "/api/history", "/api/history/1"]
)
@pytest.mark.parametrize("headers", [{}, {"X-Api-Key": "wrong"}])
def test_routes_need_the_api_key(config_path, path, headers):
    response = client(config_path).get(path, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "missing or wrong X-Api-Key header"}


def test_lists_saved_queries_without_the_secrets(config_path, monkeypatch):
    monkeypatch.delenv("TMDB_TOKEN")
    response = client(config_path).get("/api/queries", headers=AUTH)

    assert response.status_code == 200
    queries = response.json()
    assert [q["name"] for q in queries] == ["christmas", "christmas-both"]
    settings = queries[0]["settings"]
    assert settings["text"] == "christmas"
    assert settings["kinds"] == ["movie"]
    assert settings["filters"]["min_votes"] == 50
    assert settings["scheduled"] is True
    assert "global_exclusions" not in settings


def test_preview_gives_every_decision_with_counts(config_path):
    response = client(config_path).get("/api/queries/christmas/preview", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "christmas"
    assert [(d["candidate"]["tmdb_id"], d["status"], d["reasons"]) for d in body["decisions"]] == [
        (1, "kept", []),
        (2, "filtered", ["votes 4 < 50"]),
        (3, "no_match", []),
        (5, "in_library", []),
    ]
    assert body["decisions"][0]["candidate"]["title"] == "A Christmas Story"
    assert body["counts"] == {
        "movie": {"found": 4, "matched": 3, "excluded": 0, "kept": 1, "in_library": 1}
    }
    assert body["library_checked"] == {"movie": True}
    assert body["warnings"] == []
    assert body["error"] is None


def test_preview_of_an_unknown_query(config_path):
    response = client(config_path).get("/api/queries/easter/preview", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["detail"] == (
        "no saved query named 'easter' (available: christmas, christmas-both)"
    )


def test_tmdb_error_gives_the_partial_preview(config_path):
    response = client(config_path, tmdb=series_fail).get(
        "/api/queries/christmas-both/preview", headers=AUTH
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"].startswith("TMDB returned HTTP 503")
    assert [d["candidate"]["kind"] for d in body["decisions"]] == ["movie"] * 4
    assert body["counts"]["series"]["found"] == 0
    # The query searches series too, and no Sonarr is configured.
    assert body["library_checked"] == {"movie": True, "series": False}


def test_radarr_error_is_a_bad_gateway(config_path):
    radarr = FakeArr(error="Radarr returned HTTP 401: Unauthorized")
    response = client(config_path, radarr=radarr).get(
        "/api/queries/christmas/preview", headers=AUTH
    )
    assert response.status_code == 502
    assert response.json() == {"detail": "Radarr returned HTTP 401: Unauthorized"}


def test_config_edits_show_up_without_a_restart(config_path):
    api = client(config_path)
    assert len(api.get("/api/queries", headers=AUTH).json()) == 2

    text = CONFIG + "  easter:\n    text: easter\n"
    config_path.write_bytes(text.encode("utf-8"))
    assert [q["name"] for q in api.get("/api/queries", headers=AUTH).json()][-1] == "easter"

    config_path.write_bytes(b"queries: [not, a, mapping]\n")
    response = api.get("/api/queries", headers=AUTH)
    assert response.status_code == 500
    assert "config.yaml" in response.json()["detail"]


def test_history_without_a_file(config_path, tmp_path):
    api = client(config_path)
    assert api.get("/api/history", headers=AUTH).json() == []
    assert api.get("/api/history/1", headers=AUTH).status_code == 404
    assert not (tmp_path / "titularr.db").exists()  # looking doesn't create the file


def recorded_title(tmdb_id, title, kind="movie"):
    return Candidate(
        source="tmdb",
        kind=kind,
        tmdb_id=tmdb_id,
        title=title,
        original_title=None,
        year=1983,
        original_language="en",
        vote_average=7.0,
        vote_count=100,
        adult=False,
    )


def test_history_lists_runs_and_shows_one(config_path, tmp_path):
    with History.open(tmp_path / "titularr.db") as history:
        first = history.start_run("run", "christmas", "christmas")
        history.record(first, recorded_title(1, "A Christmas Story"), "added")
        history.record(first, recorded_title(2, "Christmastime"), "failed", "HTTP 500")
        history.finish_run(first)
        history.start_run("search", "easter", None)  # stopped before finishing

    api = client(config_path)
    runs = api.get("/api/history", headers=AUTH).json()
    assert [(r["id"], r["command"], r["query_name"]) for r in runs] == [
        (2, "search", None),
        (1, "run", "christmas"),
    ]
    assert runs[0]["finished_at"] is None
    assert runs[1]["counts"] == [
        {"kind": "movie", "result": "added", "count": 1},
        {"kind": "movie", "result": "failed", "count": 1},
    ]
    assert len(api.get("/api/history?limit=1", headers=AUTH).json()) == 1
    assert api.get("/api/history?limit=0", headers=AUTH).status_code == 422

    run = api.get("/api/history/1", headers=AUTH).json()
    assert run["query_text"] == "christmas"
    assert [(t["tmdb_id"], t["result"], t["error"]) for t in run["titles"]] == [
        (1, "added", None),
        (2, "failed", "HTTP 500"),
    ]
    assert api.get("/api/history/9", headers=AUTH).status_code == 404


# --- titularr serve ---


@pytest.fixture
def no_server(monkeypatch):
    """Stand in for uvicorn.run; returns the calls it got."""
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    return calls


def test_serve_needs_an_api_key(config_path, no_server, monkeypatch):
    from typer.testing import CliRunner

    monkeypatch.delenv("TITULARR_API_KEY", raising=False)
    result = CliRunner().invoke(cli.app, ["serve", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "set TITULARR_API_KEY" in result.stderr
    assert no_server == []


def test_serve_checks_the_config_then_starts(config_path, no_server, monkeypatch):
    from typer.testing import CliRunner

    monkeypatch.setenv("TITULARR_API_KEY", KEY)
    runner = CliRunner()

    monkeypatch.delenv("TMDB_TOKEN")
    result = runner.invoke(cli.app, ["serve", "--config", str(config_path)])
    assert result.exit_code == 2
    assert "TMDB_TOKEN" in result.stderr
    assert no_server == []

    monkeypatch.setenv("TMDB_TOKEN", "tmdb-token")
    result = runner.invoke(cli.app, ["serve", "--config", str(config_path), "--port", "9000"])
    assert result.exit_code == 0
    assert no_server == [{"host": "127.0.0.1", "port": 9000}]
