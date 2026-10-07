"""Tests for the dev command, `python -m titularr.sources`."""

import httpx2
import pytest

from titularr.sources import __main__ as cli
from titularr.sources import tmdb

TOKEN = "test-token-123"

MOVIES = [
    {
        "id": 1,
        "title": "A Christmas Story",
        "release_date": "1983-11-18",
        "original_language": "en",
        "vote_average": 7.3,
        "vote_count": 2900,
    },
    {
        "id": 2,
        "title": "Christmastime",
        "release_date": "2012-12-01",
        "original_language": "en",
        "vote_average": 5.0,
        "vote_count": 4,
    },
    {"id": 3, "title": "Secret Santa", "release_date": "2015-12-04", "original_language": "en"},
    {"id": 4, "title": "Adult Christmas", "release_date": "2010-01-01", "adult": True},
]
SERIES = [
    {
        "id": 10,
        "name": "Christmas Wars",
        "first_air_date": "2022-12-13",
        "original_language": "en",
        "vote_average": 6.4,
        "vote_count": 85,
    },
    {"id": 11, "name": "Holiday Baking", "first_air_date": "2015-05-05"},
]

STORY = "[movie] A Christmas Story (1983)  tmdb:1  lang:en  rating:7.3  votes:2900"
CHRISTMASTIME = "[movie] Christmastime (2012)  tmdb:2  lang:en  rating:5.0  votes:4"
ADULT = "[movie] Adult Christmas (2010)  tmdb:4  lang:?  rating:?  votes:?"
WARS = "[series] Christmas Wars (2022)  tmdb:10  lang:en  rating:6.4  votes:85"
BAKING = "[series] Holiday Baking (2015)  tmdb:11  lang:?  rating:?  votes:?"


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


# --- searching and matching ---


def test_default_searches_both_kinds_and_matches_substring(fake_tmdb, capsys):
    status, out = run(capsys, "christmas")

    assert status == 0
    assert [r.url.path for r in fake_tmdb] == ["/3/search/movie", "/3/search/tv"]
    assert all(r.url.params["include_adult"] == "false" for r in fake_tmdb)
    assert all(r.url.params["query"] == "christmas" for r in fake_tmdb)
    # English only by default, so "Adult Christmas" (language unknown) isn't kept.
    assert out == [
        STORY,
        CHRISTMASTIME,
        WARS,
        "4 movies found, 3 matched, 2 kept; 2 series found, 1 matched, 1 kept; 0 warnings",
    ]


def test_match_word_drops_partial_words(fake_tmdb, capsys):
    status, out = run(capsys, "christmas", "--kind", "movie", "--match", "word", "--any-language")

    assert status == 0
    assert out == [STORY, ADULT, "4 movies found, 2 matched; 0 warnings"]


def test_regex_replaces_the_search_text_for_matching(fake_tmdb, capsys):
    status, out = run(capsys, "christmas", "--kind", "movie", "--regex", "^christmas")

    assert status == 0
    assert fake_tmdb[0].url.params["query"] == "christmas"  # still what TMDB searches
    assert out == [CHRISTMASTIME, "4 movies found, 1 matched, 1 kept; 0 warnings"]


@pytest.mark.parametrize(
    ("kind", "expected_paths"),
    [("movie", ["/3/search/movie"]), ("series", ["/3/search/tv"])],
)
def test_kind_searches_only_one(fake_tmdb, capsys, kind, expected_paths):
    status, _ = run(capsys, "christmas", "--kind", kind)

    assert status == 0
    assert [r.url.path for r in fake_tmdb] == expected_paths


def test_adult_only_applies_to_both_kinds(fake_tmdb, capsys):
    status, out = run(capsys, "christmas", "--adult", "only", "--any-language")

    assert status == 0
    assert all(r.url.params["include_adult"] == "true" for r in fake_tmdb)
    assert out == [ADULT, "1 movie found, 1 matched; 0 series found, 0 matched; 0 warnings"]


# --- filters ---


def test_filters_add_a_kept_count(fake_tmdb, capsys):
    status, out = run(capsys, "christmas", "--min-votes", "50")

    assert status == 0
    assert out == [
        STORY,
        WARS,
        "4 movies found, 3 matched, 1 kept; 2 series found, 1 matched, 1 kept; 0 warnings",
    ]


def test_year_and_language_filters(fake_tmdb, capsys):
    args = ["christmas", "--kind", "movie", "--year-min", "1980", "--year-max", "1999"]
    status, out = run(capsys, *args, "--language", "en")

    assert status == 0
    assert out == [STORY, "4 movies found, 3 matched, 1 kept; 0 warnings"]


def test_min_rating_filter(fake_tmdb, capsys):
    status, out = run(capsys, "christmas", "--kind", "series", "--min-rating", "6")

    assert status == 0
    assert out == [WARS, "2 series found, 1 matched, 1 kept; 0 warnings"]


def test_keep_unknown(fake_tmdb, capsys):
    status, out = run(capsys, "christmas", "--kind", "movie", "--min-votes", "50", "--keep-unknown")

    assert status == 0
    assert out == [STORY, ADULT, "4 movies found, 3 matched, 2 kept; 0 warnings"]


def test_show_dropped_gives_reasons(fake_tmdb, capsys):
    status, out = run(capsys, "christmas", "--kind", "movie", "--min-votes", "50", "--show-dropped")

    assert status == 0
    assert out == [
        STORY,
        CHRISTMASTIME + " (filtered: votes 4 < 50)",
        "[movie] Secret Santa (2015)  tmdb:3  lang:en  rating:?  votes:? (no match)",
        ADULT + " (filtered: language unknown; votes unknown)",
        "4 movies found, 3 matched, 1 kept; 0 warnings",
    ]


def test_show_dropped_lists_non_matches(fake_tmdb, capsys):
    status, out = run(capsys, "christmas", "--kind", "series", "--show-dropped")

    assert status == 0
    assert out == [WARS, BAKING + " (no match)", "2 series found, 1 matched, 1 kept; 0 warnings"]


# --- language default ---


def test_default_language_is_english(fake_tmdb, capsys):
    status, out = run(capsys, "christmas", "--kind", "movie", "--show-dropped")

    assert status == 0
    assert out[2:4] == [
        "[movie] Secret Santa (2015)  tmdb:3  lang:en  rating:?  votes:? (no match)",
        ADULT + " (filtered: language unknown)",
    ]


def test_language_replaces_the_default(fake_tmdb, capsys):
    status, out = run(capsys, "christmas", "--kind", "movie", "--language", "ja", "--show-dropped")

    assert status == 0
    assert out[0] == STORY + " (filtered: language en not in ja)"
    assert out[-1] == "4 movies found, 3 matched, 0 kept; 0 warnings"


def test_any_language_turns_the_language_filter_off(fake_tmdb, capsys):
    status, out = run(capsys, "christmas", "--kind", "movie", "--any-language")

    assert status == 0
    assert out == [STORY, CHRISTMASTIME, ADULT, "4 movies found, 3 matched; 0 warnings"]


def test_language_and_any_language_cannot_be_combined(fake_tmdb, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["christmas", "--language", "en", "--any-language"])

    assert excinfo.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err
    assert fake_tmdb == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--year-min", "2000", "--year-max", "1990"], "year_min (2000) is after year_max (1990)"),
        (["--min-rating", "11"], "min_rating: Input should be less than or equal to 10"),
        (["--min-votes", "-5"], "min_votes: Input should be greater than or equal to 0"),
        (["--language", ""], "language codes must not be empty"),
        (["--year-min", "soon"], "invalid int value"),
    ],
)
def test_invalid_filters_are_rejected_before_any_request(fake_tmdb, capsys, args, message):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["christmas", *args])

    assert excinfo.value.code == 2
    assert message in capsys.readouterr().err
    assert fake_tmdb == []


# --- exclusions ---


def test_exclusions_add_an_excluded_count(fake_tmdb, capsys):
    status, out = run(capsys, "christmas", "--exclude-id", "movie:1", "--exclude-title", "wars")

    assert status == 0
    assert out == [
        CHRISTMASTIME,
        (
            "4 movies found, 3 matched, 1 excluded, 1 kept; "
            "2 series found, 1 matched, 1 excluded, 0 kept; 0 warnings"
        ),
    ]


def test_excluded_id_needs_the_right_kind(fake_tmdb, capsys):
    status, out = run(capsys, "christmas", "--kind", "movie", "--exclude-id", "series:1")

    assert status == 0
    assert out[0] == STORY
    assert out[-1] == "4 movies found, 3 matched, 0 excluded, 2 kept; 0 warnings"


def test_exclude_regex(fake_tmdb, capsys):
    status, out = run(capsys, "christmas", "--kind", "movie", "--exclude-regex", "^a ")

    assert status == 0
    assert out == [CHRISTMASTIME, "4 movies found, 3 matched, 1 excluded, 1 kept; 0 warnings"]


def test_exclusion_takes_priority_over_filter_reasons(fake_tmdb, capsys):
    args = ["christmas", "--kind", "movie", "--min-votes", "50", "--show-dropped"]
    status, out = run(capsys, *args, "--exclude-title", "christmastime")

    assert status == 0
    assert out == [
        STORY,
        CHRISTMASTIME + ' (excluded: title "christmastime")',
        "[movie] Secret Santa (2015)  tmdb:3  lang:en  rating:?  votes:? (no match)",
        ADULT + " (filtered: language unknown; votes unknown)",
        "4 movies found, 3 matched, 1 excluded, 1 kept; 0 warnings",
    ]


def test_exclusions_alone_show_kept_count(fake_tmdb, capsys):
    status, out = run(
        capsys, "christmas", "--kind", "movie", "--any-language", "--exclude-id", "movie:4"
    )

    assert status == 0
    assert out == [
        STORY,
        CHRISTMASTIME,
        "4 movies found, 3 matched, 1 excluded, 2 kept; 0 warnings",
    ]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--exclude-id", "1399"], "expected movie:<id> or series:<id>"),
        (["--exclude-id", "tv:1399"], "expected movie:<id> or series:<id>"),
        (["--exclude-title", " "], "must not be empty"),
        (["--exclude-regex", "(unclosed"], "invalid regex"),
    ],
)
def test_invalid_exclusions_are_rejected_before_any_request(fake_tmdb, capsys, args, message):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["christmas", *args])

    assert excinfo.value.code == 2
    assert message in capsys.readouterr().err
    assert fake_tmdb == []


# --- errors and argument handling ---


def test_warnings_are_counted(monkeypatch, capsys):
    def handler(request):
        results = [{"id": 1, "title": "Elf"}, {"id": 2}] if "movie" in request.url.path else []
        return httpx2.Response(
            200, json={"page": 1, "total_pages": 1, "total_results": 2, "results": results}
        )

    install_fake_tmdb(monkeypatch, handler)

    status, out = run(capsys, "elf", "--any-language")
    assert status == 0
    assert out[-1] == "1 movie found, 1 matched; 0 series found, 0 matched; 1 warning"


def test_error_still_prints_summary(monkeypatch, capsys):
    install_fake_tmdb(monkeypatch, lambda request: httpx2.Response(401))

    assert cli.main(["christmas"]) == 1
    captured = capsys.readouterr()
    assert "Error: TMDB returned HTTP 401" in captured.err
    assert captured.out.splitlines() == [
        "0 movies found, 0 matched, 0 kept; 0 series found, 0 matched, 0 kept; 0 warnings"
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
