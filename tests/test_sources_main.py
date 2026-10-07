"""Tests for the dev command, `python -m titularr.sources`."""

import httpx2
import pytest

from titularr.sources import __main__ as cli
from titularr.sources import tmdb

TOKEN = "test-token-123"

MOVIES = [
    {"id": 1, "title": "A Christmas Story", "release_date": "1983-11-18", "vote_count": 2900},
    {"id": 2, "title": "Christmastime", "release_date": "2012-12-01", "vote_count": 4},
    {"id": 3, "title": "Secret Santa", "release_date": "2015-12-04", "vote_count": 12},
    {"id": 4, "title": "Adult Christmas", "release_date": "2010-01-01", "adult": True},
]
SERIES = [
    {"id": 10, "name": "Christmas Wars", "first_air_date": "2022-12-13"},
    {"id": 11, "name": "Holiday Baking", "first_air_date": "2015-05-05"},
]


def install_fake_tmdb(monkeypatch, handler):
    real_client = tmdb.TmdbClient
    monkeypatch.setenv("TMDB_API_TOKEN", TOKEN)
    monkeypatch.setattr(
        cli, "TmdbClient", lambda token: real_client(token, transport=httpx2.MockTransport(handler))
    )


@pytest.fixture
def fake_tmdb(monkeypatch):
    """Point the command at fake TMDB data; returns the requests it makes."""
    requests = []

    def handler(request):
        requests.append(request)
        results = MOVIES if request.url.path.endswith("/movie") else SERIES
        return httpx2.Response(
            200, json={"page": 1, "total_pages": 1, "total_results": 4, "results": results}
        )

    install_fake_tmdb(monkeypatch, handler)
    return requests


def run(capsys, *args):
    status = cli.main(list(args))
    return status, capsys.readouterr().out.splitlines()


def test_default_searches_both_kinds_and_matches_substring(fake_tmdb, capsys):
    status, out = run(capsys, "christmas")

    assert status == 0
    assert [r.url.path for r in fake_tmdb] == ["/3/search/movie", "/3/search/tv"]
    assert all(r.url.params["include_adult"] == "false" for r in fake_tmdb)
    assert all(r.url.params["query"] == "christmas" for r in fake_tmdb)
    assert out == [
        "[movie] A Christmas Story (1983)  tmdb:1  votes:2900",
        "[movie] Christmastime (2012)  tmdb:2  votes:4",
        "[movie] Adult Christmas (2010)  tmdb:4  votes:?",
        "[series] Christmas Wars (2022)  tmdb:10  votes:?",
        "4 movies found, 3 matched; 2 series found, 1 matched; 0 warnings",
    ]


def test_match_word_drops_partial_words(fake_tmdb, capsys):
    status, out = run(capsys, "christmas", "--kind", "movie", "--match", "word")

    assert status == 0
    assert out == [
        "[movie] A Christmas Story (1983)  tmdb:1  votes:2900",
        "[movie] Adult Christmas (2010)  tmdb:4  votes:?",
        "4 movies found, 2 matched; 0 warnings",
    ]


def test_regex_replaces_the_search_text_for_matching(fake_tmdb, capsys):
    status, out = run(capsys, "christmas", "--kind", "movie", "--regex", "^christmas")

    assert status == 0
    assert fake_tmdb[0].url.params["query"] == "christmas"  # still what TMDB searches
    assert out == [
        "[movie] Christmastime (2012)  tmdb:2  votes:4",
        "4 movies found, 1 matched; 0 warnings",
    ]


def test_show_unmatched_lists_everything(fake_tmdb, capsys):
    status, out = run(capsys, "christmas", "--kind", "series", "--show-unmatched")

    assert status == 0
    assert out == [
        "[series] Christmas Wars (2022)  tmdb:10  votes:?",
        "[series] Holiday Baking (2015)  tmdb:11  votes:? (no match)",
        "2 series found, 1 matched; 0 warnings",
    ]


@pytest.mark.parametrize(
    ("kind", "expected_paths"),
    [("movie", ["/3/search/movie"]), ("series", ["/3/search/tv"])],
)
def test_kind_searches_only_one(fake_tmdb, capsys, kind, expected_paths):
    status, _ = run(capsys, "christmas", "--kind", kind)

    assert status == 0
    assert [r.url.path for r in fake_tmdb] == expected_paths


def test_adult_only_applies_to_both_kinds(fake_tmdb, capsys):
    status, out = run(capsys, "christmas", "--adult", "only")

    assert status == 0
    assert all(r.url.params["include_adult"] == "true" for r in fake_tmdb)
    assert out == [
        "[movie] Adult Christmas (2010)  tmdb:4  votes:?",
        "1 movie found, 1 matched; 0 series found, 0 matched; 0 warnings",
    ]


def test_warnings_are_counted(monkeypatch, capsys):
    def handler(request):
        results = [{"id": 1, "title": "Elf"}, {"id": 2}] if "movie" in request.url.path else []
        return httpx2.Response(
            200, json={"page": 1, "total_pages": 1, "total_results": 2, "results": results}
        )

    install_fake_tmdb(monkeypatch, handler)

    status, out = run(capsys, "elf")
    assert status == 0
    assert out[-1] == "1 movie found, 1 matched; 0 series found, 0 matched; 1 warning"


def test_error_still_prints_summary(monkeypatch, capsys):
    install_fake_tmdb(monkeypatch, lambda request: httpx2.Response(401))

    assert cli.main(["christmas"]) == 1
    captured = capsys.readouterr()
    assert "Error: TMDB returned HTTP 401" in captured.err
    assert captured.out.splitlines() == [
        "0 movies found, 0 matched; 0 series found, 0 matched; 0 warnings"
    ]


def test_invalid_regex_is_rejected_before_any_request(fake_tmdb, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["dracula", "--regex", "(unclosed"])

    assert excinfo.value.code == 2
    assert "invalid regex" in capsys.readouterr().err
    assert fake_tmdb == []


def test_match_and_regex_cannot_be_combined(fake_tmdb, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["dracula", "--match", "word", "--regex", "^dracula"])

    assert excinfo.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [["x", "--kind", "tv"], ["x", "--adult", "sometimes"], ["x", "--match", "regex"]],
)
def test_rejects_invalid_choices(fake_tmdb, capsys, args):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(args)
    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_requires_token(monkeypatch, capsys):
    monkeypatch.delenv("TMDB_API_TOKEN", raising=False)
    assert cli.main(["christmas"]) == 2
    assert "TMDB_API_TOKEN" in capsys.readouterr().err
