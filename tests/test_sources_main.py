"""Tests for the dev command, `python -m titularr.sources`."""

import httpx2
import pytest

from titularr.sources import __main__ as cli
from titularr.sources import tmdb

TOKEN = "test-token-123"

MOVIES = [
    {"id": 1, "title": "Elf", "release_date": "2003-10-09", "vote_count": 5214, "adult": False},
    {"id": 2, "title": "Adult Movie", "release_date": "2010-01-01", "vote_count": 3, "adult": True},
]
SERIES = [
    {"id": 10, "name": "Christmas Wars", "first_air_date": "2022-12-13", "adult": False},
    {"id": 11, "name": "Adult Series", "first_air_date": "2015-05-05", "adult": True},
]


@pytest.fixture
def fake_tmdb(monkeypatch):
    """Point the command at fake TMDB data; returns the requests it makes."""
    requests = []

    def handler(request):
        requests.append(request)
        results = MOVIES if request.url.path.endswith("/movie") else SERIES
        return httpx2.Response(
            200, json={"page": 1, "total_pages": 1, "total_results": 2, "results": results}
        )

    real_client = tmdb.TmdbClient
    monkeypatch.setenv("TMDB_API_TOKEN", TOKEN)
    monkeypatch.setattr(
        cli, "TmdbClient", lambda token: real_client(token, transport=httpx2.MockTransport(handler))
    )
    return requests


def paths(requests):
    return [r.url.path for r in requests]


def test_default_searches_both_kinds(fake_tmdb, capsys):
    assert cli.main(["christmas"]) == 0

    assert paths(fake_tmdb) == ["/3/search/movie", "/3/search/tv"]
    assert all(r.url.params["include_adult"] == "false" for r in fake_tmdb)
    assert capsys.readouterr().out.splitlines() == [
        "[movie] Elf (2003)  tmdb:1  votes:5214",
        "[movie] Adult Movie (2010)  tmdb:2  votes:3",
        "[series] Christmas Wars (2022)  tmdb:10  votes:?",
        "[series] Adult Series (2015)  tmdb:11  votes:?",
        "2 movies, 2 series, 0 warnings",
    ]


@pytest.mark.parametrize(
    ("kind", "expected_paths", "summary"),
    [
        ("movie", ["/3/search/movie"], "2 movies, 0 warnings"),
        ("series", ["/3/search/tv"], "2 series, 0 warnings"),
    ],
)
def test_kind_searches_only_one(fake_tmdb, capsys, kind, expected_paths, summary):
    assert cli.main(["christmas", "--kind", kind]) == 0

    assert paths(fake_tmdb) == expected_paths
    assert capsys.readouterr().out.splitlines()[-1] == summary


def test_adult_only_applies_to_both_kinds(fake_tmdb, capsys):
    assert cli.main(["christmas", "--adult", "only"]) == 0

    assert all(r.url.params["include_adult"] == "true" for r in fake_tmdb)
    assert capsys.readouterr().out.splitlines() == [
        "[movie] Adult Movie (2010)  tmdb:2  votes:3",
        "[series] Adult Series (2015)  tmdb:11  votes:?",
        "1 movie, 1 series, 0 warnings",
    ]


def test_warnings_are_counted(monkeypatch, capsys):
    def handler(request):
        results = [{"id": 1, "title": "Elf"}, {"id": 2}] if "movie" in request.url.path else []
        return httpx2.Response(
            200, json={"page": 1, "total_pages": 1, "total_results": 2, "results": results}
        )

    real_client = tmdb.TmdbClient
    monkeypatch.setenv("TMDB_API_TOKEN", TOKEN)
    monkeypatch.setattr(
        cli, "TmdbClient", lambda token: real_client(token, transport=httpx2.MockTransport(handler))
    )

    assert cli.main(["christmas"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "1 movie, 0 series, 1 warning"


def test_error_still_prints_summary(monkeypatch, capsys):
    real_client = tmdb.TmdbClient
    monkeypatch.setenv("TMDB_API_TOKEN", TOKEN)
    monkeypatch.setattr(
        cli,
        "TmdbClient",
        lambda token: real_client(
            token, transport=httpx2.MockTransport(lambda r: httpx2.Response(401))
        ),
    )

    assert cli.main(["christmas"]) == 1
    captured = capsys.readouterr()
    assert "Error: TMDB returned HTTP 401" in captured.err
    assert captured.out.splitlines() == ["0 movies, 0 series, 0 warnings"]


@pytest.mark.parametrize("args", [["x", "--kind", "tv"], ["x", "--adult", "sometimes"]])
def test_rejects_invalid_choices(fake_tmdb, capsys, args):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(args)
    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_requires_token(monkeypatch, capsys):
    monkeypatch.delenv("TMDB_API_TOKEN", raising=False)
    assert cli.main(["christmas"]) == 2
    assert "TMDB_API_TOKEN" in capsys.readouterr().err
