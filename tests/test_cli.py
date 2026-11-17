"""Tests for the `titularr` command (search, run, validate)."""

import json
import re
from importlib.metadata import entry_points

import httpx2
import pytest
from typer.testing import CliRunner

from titularr import cli
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

# Runtimes in minutes for /movie/{id}; others have none (TMDB answers 0).
RUNTIMES = {1: 94, 2: 45}

STORY = "[movie] A Christmas Story (1983)  tmdb:1  lang:en  rating:7.3  votes:2900"
CHRISTMASTIME = "[movie] Christmastime (2012)  tmdb:2  lang:en  rating:5.0  votes:4"
ADULT = "[movie] Adult Christmas (2010)  tmdb:4  lang:?  rating:?  votes:?"
WARS = "[series] Christmas Wars (2022)  tmdb:10  lang:en  rating:6.4  votes:85"
BAKING = "[series] Holiday Baking (2015)  tmdb:11  lang:?  rating:?  votes:?"

CONFIG = """\
sources:
  tmdb:
    token: config-token
exclusions:
  ids: [movie:2]
queries:
  christmas-movies:
    text: christmas
    kinds: [movie]
    match: word
    exclusions:
      titles: [story]
"""

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Run every test in an empty folder with no config, so no real config.yaml is used.

    A wide terminal keeps Typer's error boxes from wrapping messages mid-phrase.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TITULARR_CONFIG", raising=False)
    monkeypatch.setenv("COLUMNS", "200")


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
        if match := re.fullmatch(r"/3/movie/(\d+)", request.url.path):
            tmdb_id = int(match.group(1))
            return httpx2.Response(200, json={"id": tmdb_id, "runtime": RUNTIMES.get(tmdb_id, 0)})
        results = MOVIES if request.url.path.endswith("/movie") else SERIES
        return httpx2.Response(
            200,
            json={"page": 1, "total_pages": 1, "total_results": len(results), "results": results},
        )

    install_fake_tmdb(monkeypatch, handler)
    return requests


_ANSI_CODE = re.compile(r"\x1b\[[0-9;]*m")


def invoke(*args, input=None):
    """Run `titularr ARGS`, with `input` as what's typed; returns (exit code, stdout
    lines, stderr).

    Color codes are removed: Rich adds them when the environment asks for color (as
    CI does), and they can split text such as an option name in Typer's error box.
    """
    result = runner.invoke(cli.app, list(args), input=input)
    stdout = _ANSI_CODE.sub("", result.stdout)
    stderr = _ANSI_CODE.sub("", result.stderr)
    return result.exit_code, stdout.splitlines(), stderr


def write_config(path, text=CONFIG):
    path.write_bytes(text.encode("utf-8"))
    return path


# --- search: matching ---


def test_default_searches_both_kinds_and_matches_substring(fake_tmdb):
    status, out, _ = invoke("search", "christmas")

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


def test_match_word_drops_partial_words(fake_tmdb):
    status, out, _ = invoke(
        "search", "christmas", "--kind", "movie", "--match", "word", "--any-language"
    )

    assert status == 0
    assert out == [STORY, ADULT, "4 movies found, 2 matched; 0 warnings"]


def test_regex_replaces_the_search_text_for_matching(fake_tmdb):
    status, out, _ = invoke("search", "christmas", "--kind", "movie", "--regex", "^christmas")

    assert status == 0
    assert fake_tmdb[0].url.params["query"] == "christmas"  # still what TMDB searches
    assert out == [CHRISTMASTIME, "4 movies found, 1 matched, 1 kept; 0 warnings"]


@pytest.mark.parametrize(
    ("kind", "expected_paths"),
    [("movie", ["/3/search/movie"]), ("series", ["/3/search/tv"])],
)
def test_kind_searches_only_one(fake_tmdb, kind, expected_paths):
    status, _, _ = invoke("search", "christmas", "--kind", kind)

    assert status == 0
    assert [r.url.path for r in fake_tmdb] == expected_paths


def test_adult_only_applies_to_both_kinds(fake_tmdb):
    status, out, _ = invoke("search", "christmas", "--adult", "only", "--any-language")

    assert status == 0
    assert all(r.url.params["include_adult"] == "true" for r in fake_tmdb)
    assert out == [ADULT, "1 movie found, 1 matched; 0 series found, 0 matched; 0 warnings"]


# --- search: filters ---


def test_filters_add_a_kept_count(fake_tmdb):
    status, out, _ = invoke("search", "christmas", "--min-votes", "50")

    assert status == 0
    assert out == [
        STORY,
        WARS,
        "4 movies found, 3 matched, 1 kept; 2 series found, 1 matched, 1 kept; 0 warnings",
    ]


def test_year_and_language_filters(fake_tmdb):
    args = ["search", "christmas", "--kind", "movie", "--year-min", "1980", "--year-max", "1999"]
    status, out, _ = invoke(*args, "--language", "en")

    assert status == 0
    assert out == [STORY, "4 movies found, 3 matched, 1 kept; 0 warnings"]


def test_min_rating_filter(fake_tmdb):
    status, out, _ = invoke("search", "christmas", "--kind", "series", "--min-rating", "6")

    assert status == 0
    assert out == [WARS, "2 series found, 1 matched, 1 kept; 0 warnings"]


def test_keep_unknown(fake_tmdb):
    status, out, _ = invoke(
        "search", "christmas", "--kind", "movie", "--min-votes", "50", "--keep-unknown"
    )

    assert status == 0
    assert out == [STORY, ADULT, "4 movies found, 3 matched, 2 kept; 0 warnings"]


def test_show_dropped_gives_reasons(fake_tmdb):
    status, out, _ = invoke(
        "search", "christmas", "--kind", "movie", "--min-votes", "50", "--show-dropped"
    )

    assert status == 0
    assert out == [
        STORY,
        CHRISTMASTIME + " (filtered: votes 4 < 50)",
        "[movie] Secret Santa (2015)  tmdb:3  lang:en  rating:?  votes:? (no match)",
        ADULT + " (filtered: language unknown; votes unknown)",
        "4 movies found, 3 matched, 1 kept; 0 warnings",
    ]


def test_show_dropped_lists_non_matches(fake_tmdb):
    status, out, _ = invoke("search", "christmas", "--kind", "series", "--show-dropped")

    assert status == 0
    assert out == [WARS, BAKING + " (no match)", "2 series found, 1 matched, 1 kept; 0 warnings"]


def test_default_language_is_english(fake_tmdb):
    status, out, _ = invoke("search", "christmas", "--kind", "movie", "--show-dropped")

    assert status == 0
    assert out[2:4] == [
        "[movie] Secret Santa (2015)  tmdb:3  lang:en  rating:?  votes:? (no match)",
        ADULT + " (filtered: language unknown)",
    ]


def test_language_replaces_the_default(fake_tmdb):
    status, out, _ = invoke(
        "search", "christmas", "--kind", "movie", "--language", "ja", "--show-dropped"
    )

    assert status == 0
    assert out[0] == STORY + " (filtered: language en not in ja)"
    assert out[-1] == "4 movies found, 3 matched, 0 kept; 0 warnings"


def test_any_language_turns_the_language_filter_off(fake_tmdb):
    status, out, _ = invoke("search", "christmas", "--kind", "movie", "--any-language")

    assert status == 0
    assert out == [STORY, CHRISTMASTIME, ADULT, "4 movies found, 3 matched; 0 warnings"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--year-min", "2000", "--year-max", "1990"], "year_min (2000) is after year_max (1990)"),
        (["--min-rating", "11"], "min_rating: Input should be less than or equal to 10"),
        (["--min-votes", "-5"], "min_votes: Input should be greater than or equal to 0"),
        (["--language", ""], "language codes must not be empty"),
    ],
)
def test_invalid_filters_are_rejected_before_any_request(fake_tmdb, args, message):
    status, _, err = invoke("search", "christmas", *args)

    assert status == 2
    assert message in err
    assert fake_tmdb == []


# --- search: exclusions ---


def test_exclusions_add_an_excluded_count(fake_tmdb):
    status, out, _ = invoke(
        "search", "christmas", "--exclude-id", "movie:1", "--exclude-title", "wars"
    )

    assert status == 0
    assert out == [
        CHRISTMASTIME,
        (
            "4 movies found, 3 matched, 1 excluded, 1 kept; "
            "2 series found, 1 matched, 1 excluded, 0 kept; 0 warnings"
        ),
    ]


def test_excluded_id_needs_the_right_kind(fake_tmdb):
    status, out, _ = invoke("search", "christmas", "--kind", "movie", "--exclude-id", "series:1")

    assert status == 0
    assert out[0] == STORY
    assert out[-1] == "4 movies found, 3 matched, 0 excluded, 2 kept; 0 warnings"


def test_exclude_regex(fake_tmdb):
    status, out, _ = invoke("search", "christmas", "--kind", "movie", "--exclude-regex", "^a ")

    assert status == 0
    assert out == [CHRISTMASTIME, "4 movies found, 3 matched, 1 excluded, 1 kept; 0 warnings"]


def test_exclusion_takes_priority_over_filter_reasons(fake_tmdb):
    args = ["search", "christmas", "--kind", "movie", "--min-votes", "50", "--show-dropped"]
    status, out, _ = invoke(*args, "--exclude-title", "christmastime")

    assert status == 0
    assert out == [
        STORY,
        CHRISTMASTIME + ' (excluded: query: title "christmastime")',
        "[movie] Secret Santa (2015)  tmdb:3  lang:en  rating:?  votes:? (no match)",
        ADULT + " (filtered: language unknown; votes unknown)",
        "4 movies found, 3 matched, 1 excluded, 1 kept; 0 warnings",
    ]


def test_exclusions_alone_show_kept_count(fake_tmdb):
    status, out, _ = invoke(
        "search", "christmas", "--kind", "movie", "--any-language", "--exclude-id", "movie:4"
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
def test_invalid_exclusions_are_rejected_before_any_request(fake_tmdb, args, message):
    status, _, err = invoke("search", "christmas", *args)

    assert status == 2
    assert message in err
    assert fake_tmdb == []


# --- search: argument handling and errors ---


def test_invalid_regex_is_rejected_before_any_request(fake_tmdb):
    status, _, err = invoke("search", "dracula", "--regex", "(unclosed")

    assert status == 2
    assert "invalid regex" in err
    assert fake_tmdb == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--match", "word", "--regex", "^x"], "--match and --regex can't be combined"),
        (["--language", "en", "--any-language"], "--language and --any-language can't be combined"),
    ],
)
def test_options_that_cant_be_combined(fake_tmdb, args, message):
    status, _, err = invoke("search", "x", *args)

    assert status == 2
    assert message in err
    assert fake_tmdb == []


@pytest.mark.parametrize(
    ("args", "option"),
    [
        (["--kind", "tv"], "--kind"),
        (["--adult", "sometimes"], "--adult"),
        (["--match", "regex"], "--match"),
        (["--year-min", "soon"], "--year-min"),
    ],
)
def test_rejects_invalid_values(fake_tmdb, args, option):
    status, _, err = invoke("search", "x", *args)

    assert status == 2
    assert "Invalid value" in err
    assert option in err
    assert fake_tmdb == []


def test_warnings_are_counted_and_printed(monkeypatch):
    def handler(request):
        results = [{"id": 1, "title": "Elf"}, {"id": 2}] if "movie" in request.url.path else []
        return httpx2.Response(
            200,
            json={"page": 1, "total_pages": 1, "total_results": len(results), "results": results},
        )

    install_fake_tmdb(monkeypatch, handler)

    status, out, err = invoke("search", "elf", "--any-language")
    assert status == 0
    assert out[-1] == "1 movie found, 1 matched; 0 series found, 0 matched; 1 warning"
    assert "WARNING: skipping TMDB result without a usable id/title" in err


def test_error_still_prints_summary(monkeypatch):
    install_fake_tmdb(monkeypatch, lambda request: httpx2.Response(401))

    status, out, err = invoke("search", "christmas")
    assert status == 1
    assert "Error: TMDB returned HTTP 401" in err
    assert out == [
        "0 movies found, 0 matched, 0 kept; 0 series found, 0 matched, 0 kept; 0 warnings"
    ]


def test_search_requires_a_token_without_config(monkeypatch):
    monkeypatch.delenv("TMDB_API_TOKEN", raising=False)
    status, _, err = invoke("search", "christmas")

    assert status == 2
    assert "TMDB_API_TOKEN" in err


# --- search: which config it uses ---


def test_search_uses_config_yaml_in_the_current_folder(fake_tmdb, tmp_path):
    write_config(tmp_path / "config.yaml")
    status, out, _ = invoke("search", "christmas", "--kind", "movie", "--show-dropped")

    assert status == 0
    assert fake_tmdb[0].headers["Authorization"] == "Bearer config-token"
    assert CHRISTMASTIME + " (excluded: global: id movie:2)" in out


def test_search_uses_titularr_config_env_variable(fake_tmdb, tmp_path, monkeypatch):
    path = write_config(tmp_path / "elsewhere.yaml")
    monkeypatch.setenv("TITULARR_CONFIG", str(path))
    status, _, _ = invoke("search", "christmas", "--kind", "movie")

    assert status == 0
    assert fake_tmdb[0].headers["Authorization"] == "Bearer config-token"


def test_explicit_config_wins_over_config_yaml(fake_tmdb, tmp_path):
    write_config(tmp_path / "config.yaml", CONFIG.replace("config-token", "default-token"))
    explicit = write_config(tmp_path / "other.yaml", CONFIG.replace("config-token", "other-token"))
    status, _, _ = invoke("search", "christmas", "--kind", "movie", "--config", str(explicit))

    assert status == 0
    assert fake_tmdb[0].headers["Authorization"] == "Bearer other-token"


def test_search_with_a_broken_config_stops_before_any_request(fake_tmdb, tmp_path):
    write_config(tmp_path / "config.yaml", "sources: [oops\n")
    status, _, err = invoke("search", "christmas")

    assert status == 2
    assert "not valid YAML" in err
    assert fake_tmdb == []


# --- run ---


def test_run_saved_query_with_the_config_token(fake_tmdb, tmp_path):
    write_config(tmp_path / "config.yaml")
    status, out, _ = invoke("run", "christmas-movies")

    assert status == 0
    assert [r.url.path for r in fake_tmdb] == ["/3/search/movie"]
    assert all(r.headers["Authorization"] == "Bearer config-token" for r in fake_tmdb)
    # word match keeps "A Christmas Story" and "Adult Christmas"; the query's own
    # exclusion drops the first, the English default drops the second.
    assert out == ["4 movies found, 2 matched, 1 excluded, 0 kept; 0 warnings"]


def test_run_shows_which_layer_excluded(fake_tmdb, tmp_path):
    write_config(tmp_path / "config.yaml")
    status, out, _ = invoke("run", "christmas-movies", "--show-dropped")

    assert status == 0
    assert out[0] == STORY + ' (excluded: query: title "story")'


def test_run_with_explicit_config(fake_tmdb, tmp_path):
    path = write_config(tmp_path / "titularr.yaml")
    status, _, _ = invoke("run", "christmas-movies", "--config", str(path))

    assert status == 0
    assert fake_tmdb[0].headers["Authorization"] == "Bearer config-token"


def test_run_unknown_name_lists_available(fake_tmdb, tmp_path):
    write_config(tmp_path / "config.yaml")
    status, _, err = invoke("run", "nope")

    assert status == 2
    assert "no saved query named 'nope' (available: christmas-movies)" in err
    assert fake_tmdb == []


def test_run_needs_a_config_file(fake_tmdb):
    status, _, err = invoke("run", "christmas-movies")

    assert status == 2
    assert "config file not found: config.yaml" in err
    assert fake_tmdb == []


def test_run_config_error_names_the_field(fake_tmdb, tmp_path):
    bad = "sources:\n  tmdb:\n    token: x\nqueries:\n  q:\n    text: x\n    filters:\n      min_votes: -1\n"
    write_config(tmp_path / "config.yaml", bad)
    status, _, err = invoke("run", "q")

    assert status == 2
    assert "queries.q.filters.min_votes" in err
    assert fake_tmdb == []


# --- validate ---


def test_validate_summarizes_a_valid_config(fake_tmdb, tmp_path):
    write_config(tmp_path / "config.yaml")
    status, out, _ = invoke("validate")

    assert status == 0
    assert out == [
        (
            "config.yaml is valid: 1 saved query (christmas-movies), 1 global exclusion rule, "
            "library check: none, scheduled: none"
        )
    ]
    assert fake_tmdb == []  # never contacts TMDB


def test_validate_reports_errors(fake_tmdb, tmp_path):
    bad = "sources:\n  tmdb:\n    token: x\nqueries:\n  q:\n    text: x\n    filter: {}\n"
    path = write_config(tmp_path / "bad.yaml", bad)
    status, _, err = invoke("validate", "--config", str(path))

    assert status == 2
    assert "queries.q.filter: Extra inputs are not permitted" in err
    assert fake_tmdb == []


def test_validate_needs_a_config_file():
    status, _, err = invoke("validate")

    assert status == 2
    assert "config file not found" in err


# --- packaging ---


def test_help_lists_the_commands():
    status, out, _ = invoke("--help")
    text = "\n".join(out)

    assert status == 0
    for command in ("search", "run", "validate"):
        assert command in text


def test_titularr_console_script_is_installed():
    (script,) = [e for e in entry_points(group="console_scripts") if e.name == "titularr"]
    assert script.value == "titularr.cli:app"


# --- library check (Radarr / Sonarr) ---

ARR_CONFIG = """\
sources:
  tmdb:
    token: config-token
radarr:
  url: http://radarr:7878
  api_key: radarr-key
sonarr:
  url: http://sonarr:8989
  api_key: sonarr-key
"""


@pytest.fixture
def fake_arr(monkeypatch):
    """Fake Radarr (has movie 1) and Sonarr (has series 10, plus one without a TMDB ID).

    Returns the requests made to either app.
    """
    from titularr import arr

    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/movie"):
            return httpx2.Response(200, json=[{"tmdbId": 1, "title": "A Christmas Story"}])
        return httpx2.Response(
            200,
            json=[{"tmdbId": 10, "title": "Christmas Wars"}, {"tmdbId": 0, "title": "Old Show"}],
        )

    for name in ("RadarrClient", "SonarrClient"):
        real = getattr(arr, name)
        monkeypatch.setattr(
            cli,
            name,
            lambda url, key, real=real: real(url, key, transport=httpx2.MockTransport(handler)),
        )
    return requests


def test_titles_in_the_library_are_dropped_and_counted(fake_tmdb, fake_arr, tmp_path):
    write_config(tmp_path / "config.yaml", ARR_CONFIG)
    status, out, err = invoke("search", "christmas")

    assert status == 0
    assert [r.headers["X-Api-Key"] for r in fake_arr] == ["radarr-key", "sonarr-key"]
    assert out == [
        CHRISTMASTIME,
        (
            "4 movies found, 3 matched, 1 kept, 1 in library; "
            "2 series found, 1 matched, 0 kept, 1 in library; 1 warning"
        ),
    ]
    assert "1 series in Sonarr has no TMDB ID and can't be recognized in results: Old Show" in err


def test_show_dropped_marks_in_library(fake_tmdb, fake_arr, tmp_path):
    write_config(tmp_path / "config.yaml", ARR_CONFIG)
    status, out, _ = invoke("search", "christmas", "--kind", "movie", "--show-dropped")

    assert status == 0
    assert out[0] == STORY + " (in library)"


def test_only_the_searched_kinds_library_is_fetched(fake_tmdb, fake_arr, tmp_path):
    write_config(tmp_path / "config.yaml", ARR_CONFIG)
    status, out, err = invoke("search", "christmas", "--kind", "movie")

    assert status == 0
    assert [r.url.path for r in fake_arr] == ["/api/v3/movie"]
    assert "Sonarr" not in err  # no "no TMDB ID" warning for a movies-only search
    assert out[-1] == "4 movies found, 3 matched, 1 kept, 1 in library; 0 warnings"


def test_arr_error_stops_before_any_tmdb_request(fake_tmdb, monkeypatch, tmp_path):
    from titularr import arr

    real = arr.RadarrClient
    monkeypatch.setattr(
        cli,
        "RadarrClient",
        lambda url, key: real(
            url, key, transport=httpx2.MockTransport(lambda r: httpx2.Response(401))
        ),
    )
    write_config(tmp_path / "config.yaml", ARR_CONFIG)
    status, out, err = invoke("search", "christmas", "--kind", "movie")

    assert status == 1
    assert "Error: Radarr returned HTTP 401: check the api_key in your config" in err
    assert "radarr-key" not in err
    assert fake_tmdb == []
    assert out == []


def test_no_arr_config_means_no_library_check(fake_tmdb, fake_arr, tmp_path):
    write_config(tmp_path / "config.yaml")  # CONFIG has no radarr/sonarr sections
    status, out, _ = invoke("search", "christmas", "--kind", "movie", "--any-language")

    assert status == 0
    assert fake_arr == []
    assert "in library" not in out[-1]


def test_unusable_sonarr_section_warns_only_for_series(fake_tmdb, fake_arr, tmp_path, monkeypatch):
    monkeypatch.delenv("UNSET_SONARR_KEY", raising=False)
    text = ARR_CONFIG.replace("api_key: sonarr-key", "api_key: ${UNSET_SONARR_KEY}")
    write_config(tmp_path / "config.yaml", text)
    warning = "sonarr is in the config but can't be used (UNSET_SONARR_KEY is not set)"

    status, _, err = invoke("search", "christmas", "--kind", "movie")
    assert status == 0
    assert warning not in err

    status, out, err = invoke("search", "christmas", "--kind", "series")
    assert status == 0
    assert warning in err
    assert all(r.url.path != "/api/v3/series" for r in fake_arr)
    # No library check ("in library" missing), and the run still completes.
    assert out[-1] == "2 series found, 1 matched, 1 kept; 1 warning"


def test_validate_lists_library_checks_and_warnings(fake_arr, tmp_path, monkeypatch):
    monkeypatch.delenv("UNSET_SONARR_KEY", raising=False)
    text = ARR_CONFIG.replace("api_key: sonarr-key", "api_key: ${UNSET_SONARR_KEY}")
    write_config(tmp_path / "config.yaml", text)
    status, out, err = invoke("validate")

    assert status == 0
    assert out == [
        (
            "config.yaml is valid: 0 saved queries, 0 global exclusion rules, "
            "library check: Radarr, scheduled: none"
        )
    ]
    assert "WARNING: sonarr is in the config but can't be used" in err
    assert fake_arr == []  # validate never contacts the apps


# --- adding to Radarr and Sonarr (--apply) ---

APPLY_CONFIG = """\
sources:
  tmdb:
    token: config-token
radarr:
  url: http://radarr:7878
  api_key: radarr-key
  quality_profile: hd-1080p
  root_folder: /movies
sonarr:
  url: http://sonarr:8989
  api_key: sonarr-key
  quality_profile: HD-1080p
  root_folder: /tv
queries:
  kids:
    text: christmas
    kinds: [movie]
    filters:
      languages: any
      keep_unknown: true
    radarr:
      root_folder: /kids
  anime:
    text: christmas
    kinds: [series]
    sonarr:
      series_type: anime
"""


@pytest.fixture
def fake_radarr(monkeypatch):
    """A fake Radarr that has movie 1. Returns (requests made, add answers by TMDB ID).

    Adding succeeds unless an answer is set for that TMDB ID.
    """
    from titularr import arr

    requests = []
    answers = {}

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path == "/api/v3/movie" and request.method == "GET":
            return httpx2.Response(200, json=[{"tmdbId": 1, "title": "A Christmas Story"}])
        if path == "/api/v3/qualityprofile":
            return httpx2.Response(200, json=[{"id": 4, "name": "HD-1080p"}])
        if path == "/api/v3/tag":
            return httpx2.Response(200, json=[{"id": 3, "label": "anime"}])
        if path == "/api/v3/rootfolder":
            return httpx2.Response(200, json=[{"path": "/movies"}, {"path": "/kids"}])
        if path == "/api/v3/movie/lookup/tmdb":
            return httpx2.Response(200, json={"tmdbId": int(request.url.params["tmdbId"])})
        if path == "/api/v3/movie" and request.method == "POST":
            tmdb_id = json.loads(request.content)["tmdbId"]
            return answers.get(tmdb_id, httpx2.Response(201, json={}))
        return httpx2.Response(404)

    real = arr.RadarrClient
    monkeypatch.setattr(
        cli,
        "RadarrClient",
        lambda url, key: real(url, key, transport=httpx2.MockTransport(handler)),
    )
    return requests, answers


@pytest.fixture
def fake_sonarr(monkeypatch):
    """A fake Sonarr with an empty library. Returns (requests made, lookup results by
    TMDB ID, add answers by TMDB ID).

    A lookup finds the series (with TVDB ID 1000 + its TMDB ID) unless results are set
    for that TMDB ID; adding succeeds unless an answer is set.
    """
    from titularr import arr

    requests = []
    lookups = {}
    answers = {}

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path == "/api/v3/series" and request.method == "GET":
            return httpx2.Response(200, json=[])
        if path == "/api/v3/qualityprofile":
            return httpx2.Response(200, json=[{"id": 6, "name": "HD-1080p"}])
        if path == "/api/v3/tag":
            return httpx2.Response(200, json=[{"id": 3, "label": "anime"}])
        if path == "/api/v3/rootfolder":
            return httpx2.Response(200, json=[{"path": "/tv"}])
        if path == "/api/v3/series/lookup":
            tmdb_id = int(request.url.params["term"].removeprefix("tmdb:"))
            found = [{"tmdbId": tmdb_id, "tvdbId": 1000 + tmdb_id}]
            return httpx2.Response(200, json=lookups.get(tmdb_id, found))
        if path == "/api/v3/series" and request.method == "POST":
            tmdb_id = json.loads(request.content)["tmdbId"]
            return answers.get(tmdb_id, httpx2.Response(201, json={}))
        return httpx2.Response(404)

    real = arr.SonarrClient
    monkeypatch.setattr(
        cli,
        "SonarrClient",
        lambda url, key: real(url, key, transport=httpx2.MockTransport(handler)),
    )
    return requests, lookups, answers


def posted(requests):
    return [json.loads(r.content) for r in requests if r.method == "POST"]


def can_prompt(monkeypatch):
    monkeypatch.setattr(cli, "_can_prompt", lambda: True)


# Keeps Christmastime (tmdb:2) and Adult Christmas (tmdb:4); 1 is already in Radarr.
MOVIE_SEARCH = ("search", "christmas", "--kind", "movie", "--any-language", "--keep-unknown")


def test_preview_never_adds(fake_tmdb, fake_radarr, tmp_path):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    requests, _ = fake_radarr
    status, out, _ = invoke(*MOVIE_SEARCH)

    assert status == 0
    assert [r.url.path for r in requests] == ["/api/v3/movie"]  # the library check only
    assert out[-1] == "4 movies found, 3 matched, 2 kept, 1 in library; 0 warnings"


def test_apply_yes_adds_every_kept_movie(fake_tmdb, fake_radarr, tmp_path):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    requests, _ = fake_radarr
    status, out, _ = invoke(*MOVIE_SEARCH, "--apply", "--yes")

    assert status == 0
    assert out[-3:] == [
        f"added: {CHRISTMASTIME}",
        f"added: {ADULT}",
        "Radarr: 2 added, 0 already there, 0 failed",
    ]
    first, second = posted(requests)
    assert (first["tmdbId"], second["tmdbId"]) == (2, 4)
    assert first["qualityProfileId"] == 4
    assert first["rootFolderPath"] == "/movies"
    assert first["minimumAvailability"] == "released"
    assert first["addOptions"] == {"searchForMovie": False, "monitor": "movieOnly"}


def test_apply_asks_first_and_no_adds_nothing(fake_tmdb, fake_radarr, tmp_path, monkeypatch):
    can_prompt(monkeypatch)
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    requests, _ = fake_radarr
    status, out, err = invoke(*MOVIE_SEARCH, "--apply", input="n\n")

    assert status == 0
    assert "Add 2 movies to Radarr? [y/N]" in err
    assert out[-1] == "Nothing added."
    assert posted(requests) == []


def test_apply_asks_first_and_yes_adds(fake_tmdb, fake_radarr, tmp_path, monkeypatch):
    can_prompt(monkeypatch)
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    requests, _ = fake_radarr
    status, out, _ = invoke(*MOVIE_SEARCH, "--apply", input="y\n")

    assert status == 0
    assert out[-1] == "Radarr: 2 added, 0 already there, 0 failed"
    assert len(posted(requests)) == 2


def test_apply_without_yes_needs_someone_to_answer(fake_tmdb, fake_radarr, tmp_path):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    requests, _ = fake_radarr
    status, out, err = invoke(*MOVIE_SEARCH, "--apply")

    assert status == 2
    assert "nobody can answer here; add --yes" in err
    assert requests == [] and fake_tmdb == [] and out == []


def test_apply_names_why_the_radarr_section_cant_be_used(
    fake_tmdb, fake_radarr, tmp_path, monkeypatch
):
    monkeypatch.delenv("UNSET_RADARR_KEY", raising=False)
    text = APPLY_CONFIG.replace("api_key: radarr-key", "api_key: ${UNSET_RADARR_KEY}")
    write_config(tmp_path / "config.yaml", text)
    status, out, err = invoke(*MOVIE_SEARCH, "--apply", "--yes")

    assert status == 2
    assert (
        "--apply needs a usable radarr section in the config to add movies "
        "(radarr can't be used: UNSET_RADARR_KEY is not set)"
    ) in err
    requests, _ = fake_radarr
    assert requests == [] and fake_tmdb == [] and out == []


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (CONFIG, "--apply needs a radarr section in the config to add movies"),
        (
            APPLY_CONFIG.replace("  root_folder: /movies\n", ""),
            "--apply needs radarr.root_folder in the config",
        ),
        (
            APPLY_CONFIG.replace("  quality_profile: hd-1080p\n  root_folder: /movies\n", ""),
            "--apply needs radarr.quality_profile and radarr.root_folder in the config",
        ),
    ],
)
def test_apply_needs_radarr_settings(fake_tmdb, fake_radarr, tmp_path, config, message):
    write_config(tmp_path / "config.yaml", config)
    status, _, err = invoke(*MOVIE_SEARCH, "--apply", "--yes")

    assert status == 2
    assert message in err
    assert fake_tmdb == []


def test_unknown_profile_stops_before_searching(fake_tmdb, fake_radarr, tmp_path):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG.replace("hd-1080p", "4K"))
    requests, _ = fake_radarr
    status, out, err = invoke(*MOVIE_SEARCH, "--apply", "--yes")

    assert status == 1
    assert "Radarr has no quality profile named '4K' (it has: HD-1080p)" in err
    assert fake_tmdb == [] and out == [] and posted(requests) == []


def test_one_failure_doesnt_stop_the_rest(fake_tmdb, fake_radarr, tmp_path):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    _, answers = fake_radarr
    answers[2] = httpx2.Response(500, json={"message": "Database is locked"})
    status, out, _ = invoke(*MOVIE_SEARCH, "--apply", "--yes")

    assert status == 1
    assert out[-3:] == [
        f"failed: {CHRISTMASTIME}: Radarr returned HTTP 500: Database is locked",
        f"added: {ADULT}",
        "Radarr: 1 added, 0 already there, 1 failed",
    ]


def test_movie_added_meanwhile_counts_as_already_there(fake_tmdb, fake_radarr, tmp_path):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    _, answers = fake_radarr
    exists = [{"errorMessage": "already added", "errorCode": "MovieExistsValidator"}]
    answers[4] = httpx2.Response(400, json=exists)
    status, out, _ = invoke(*MOVIE_SEARCH, "--apply", "--yes")

    assert status == 0
    assert out[-2:] == [f"already in Radarr: {ADULT}", "Radarr: 1 added, 1 already there, 0 failed"]


BOTH_SEARCH = ("search", "christmas", "--any-language", "--keep-unknown")


def test_movies_and_series_are_added_after_one_question(
    fake_tmdb, fake_radarr, fake_sonarr, tmp_path, monkeypatch
):
    can_prompt(monkeypatch)
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    radarr_requests, _ = fake_radarr
    sonarr_requests, _, _ = fake_sonarr
    status, out, err = invoke(*BOTH_SEARCH, "--apply", input="y\n")

    assert status == 0
    assert err.count("[y/N]") == 1
    assert "Add 2 movies to Radarr and 1 series to Sonarr? [y/N]" in err
    assert out[-5:] == [
        f"added: {CHRISTMASTIME}",
        f"added: {ADULT}",
        f"added: {WARS}",
        "Radarr: 2 added, 0 already there, 0 failed",
        "Sonarr: 1 added, 0 already there, 0 not found, 0 failed",
    ]
    assert [body["tmdbId"] for body in posted(radarr_requests)] == [2, 4]
    (series,) = posted(sonarr_requests)
    assert series["tvdbId"] == 1010
    assert series["qualityProfileId"] == 6
    assert series["rootFolderPath"] == "/tv"
    assert series["seriesType"] == "standard"
    assert series["addOptions"] == {
        "monitor": "all",
        "searchForMissingEpisodes": False,
        "searchForCutoffUnmetEpisodes": False,
    }


def test_series_sonarr_cant_find_is_counted_not_failed(
    fake_tmdb, fake_radarr, fake_sonarr, tmp_path
):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    requests, lookups, _ = fake_sonarr
    lookups[10] = []
    status, out, _ = invoke(*BOTH_SEARCH, "--kind", "series", "--apply", "--yes")

    assert status == 0
    assert out[-2:] == [
        f"not found in Sonarr: {WARS}",
        "Sonarr: 0 added, 0 already there, 1 not found, 0 failed",
    ]
    assert posted(requests) == []


def test_series_added_meanwhile_and_failures(fake_tmdb, fake_radarr, fake_sonarr, tmp_path):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    _, _, answers = fake_sonarr
    answers[10] = httpx2.Response(500, json={"message": "Database is locked"})
    status, out, _ = invoke(*BOTH_SEARCH, "--apply", "--yes")

    # Radarr's adds still happen, and a Sonarr failure alone makes the exit status 1.
    assert status == 1
    assert out[-3:] == [
        f"failed: {WARS}: Sonarr returned HTTP 500: Database is locked",
        "Radarr: 2 added, 0 already there, 0 failed",
        "Sonarr: 0 added, 0 already there, 0 not found, 1 failed",
    ]

    exists = [{"errorMessage": "already added", "errorCode": "SeriesExistsValidator"}]
    answers[10] = httpx2.Response(400, json=exists)
    status, out, _ = invoke(*BOTH_SEARCH, "--kind", "series", "--apply", "--yes")
    assert status == 0
    assert out[-2:] == [
        f"already in Sonarr: {WARS}",
        "Sonarr: 0 added, 1 already there, 0 not found, 0 failed",
    ]


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            APPLY_CONFIG.replace(
                "sonarr:\n  url: http://sonarr:8989\n  api_key: sonarr-key\n"
                "  quality_profile: HD-1080p\n  root_folder: /tv\n",
                "",
            ),
            "--apply needs a sonarr section in the config to add series",
        ),
        (
            APPLY_CONFIG.replace("  root_folder: /tv\n", ""),
            "--apply needs sonarr.root_folder in the config",
        ),
    ],
)
def test_apply_needs_sonarr_settings_when_series_are_searched(
    fake_tmdb, fake_radarr, fake_sonarr, tmp_path, config, message
):
    write_config(tmp_path / "config.yaml", config)
    status, _, err = invoke(*BOTH_SEARCH, "--apply", "--yes")

    assert status == 2
    assert message in err
    assert fake_tmdb == []

    # A movies-only search doesn't need Sonarr at all.
    status, _, _ = invoke(*MOVIE_SEARCH, "--apply", "--yes")
    assert status == 0


def test_unknown_sonarr_folder_stops_before_searching(
    fake_tmdb, fake_radarr, fake_sonarr, tmp_path
):
    write_config(
        tmp_path / "config.yaml", APPLY_CONFIG.replace("root_folder: /tv", "root_folder: /TV")
    )
    radarr_requests, _ = fake_radarr
    status, out, err = invoke(*BOTH_SEARCH, "--apply", "--yes")

    assert status == 1
    assert "Sonarr has no root folder '/TV' (it has: /tv)" in err
    assert fake_tmdb == [] and out == [] and posted(radarr_requests) == []


def test_series_only_query_uses_its_overrides_and_never_contacts_radarr(
    fake_tmdb, fake_radarr, fake_sonarr, tmp_path
):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    radarr_requests, _ = fake_radarr
    sonarr_requests, _, _ = fake_sonarr
    status, out, _ = invoke("run", "anime", "--apply", "--yes")

    assert status == 0
    assert radarr_requests == []
    assert [body["seriesType"] for body in posted(sonarr_requests)] == ["anime"]
    assert out[-1] == "Sonarr: 1 added, 0 already there, 0 not found, 0 failed"


def test_nothing_to_add(fake_tmdb, fake_radarr, tmp_path):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    status, out, _ = invoke("search", "story", "--kind", "movie", "--apply", "--yes")

    assert status == 0
    assert out[-1] == "Nothing to add."


def test_saved_query_overrides_the_root_folder(fake_tmdb, fake_radarr, tmp_path):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    requests, _ = fake_radarr
    status, _, _ = invoke("run", "kids", "--apply", "--yes")

    assert status == 0
    assert {body["rootFolderPath"] for body in posted(requests)} == {"/kids"}


def test_search_error_means_nothing_is_added(fake_radarr, tmp_path, monkeypatch):
    install_fake_tmdb(monkeypatch, lambda r: httpx2.Response(500))
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    requests, _ = fake_radarr
    status, _, err = invoke(*MOVIE_SEARCH, "--apply", "--yes")

    assert status == 1
    assert "Nothing was added, because the search didn't finish." in err
    assert posted(requests) == []


# --- tags ---

TAGGED_CONFIG = APPLY_CONFIG.replace(
    "  root_folder: /tv\n", "  root_folder: /tv\n  tags: [Anime]\n"
)


def test_question_names_the_tags_and_titles_carry_them(
    fake_tmdb, fake_radarr, fake_sonarr, tmp_path, monkeypatch
):
    can_prompt(monkeypatch)
    write_config(tmp_path / "config.yaml", TAGGED_CONFIG)
    radarr_requests, _ = fake_radarr
    sonarr_requests, _, _ = fake_sonarr
    status, _, err = invoke(*BOTH_SEARCH, "--apply", input="y\n")

    assert status == 0
    assert "Add 2 movies to Radarr and 1 series to Sonarr (tags: anime)? [y/N]" in err
    assert [body["tags"] for body in posted(radarr_requests)] == [[], []]
    assert [body["tags"] for body in posted(sonarr_requests)] == [[3]]


def test_missing_tag_stops_before_searching(fake_tmdb, fake_radarr, fake_sonarr, tmp_path):
    write_config(tmp_path / "config.yaml", TAGGED_CONFIG.replace("[Anime]", "[anime, dubbed]"))
    sonarr_requests, _, _ = fake_sonarr
    status, out, err = invoke(*BOTH_SEARCH, "--apply", "--yes")

    assert status == 1
    assert "Sonarr has no tag named 'dubbed' (it has: anime)" in err
    assert fake_tmdb == [] and out == []
    assert all(r.method == "GET" for r in sonarr_requests)  # no tag was created


# --- run history ---


def test_apply_records_each_title_and_history_lists_it(
    fake_tmdb, fake_radarr, fake_sonarr, tmp_path
):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    _, _, answers = fake_sonarr
    answers[10] = httpx2.Response(500, json={"message": "Database is locked"})
    invoke(*BOTH_SEARCH, "--apply", "--yes")

    assert (tmp_path / "titularr.db").exists()
    status, out, _ = invoke("history")
    assert status == 0
    (line,) = out
    assert line.startswith("#1  ")
    assert line.endswith(
        '  search "christmas"  Radarr: 2 added, 0 already there, 0 failed; '
        "Sonarr: 0 added, 0 already there, 0 not found, 1 failed"
    )

    status, out, _ = invoke("history", "1")
    assert status == 0
    assert out[1:] == [
        "added: [movie] Christmastime (2012)  tmdb:2",
        "added: [movie] Adult Christmas (2010)  tmdb:4",
        (
            "failed: [series] Christmas Wars (2022)  tmdb:10: "
            "Sonarr returned HTTP 500: Database is locked"
        ),
    ]


def test_history_names_the_saved_query_and_lists_newest_first(
    fake_tmdb, fake_radarr, fake_sonarr, tmp_path
):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    invoke("run", "kids", "--apply", "--yes")
    invoke("run", "anime", "--apply", "--yes")

    status, out, _ = invoke("history")
    assert status == 0
    assert [line.split("  ")[0] for line in out] == ["#2", "#1"]
    assert "  run anime  Sonarr: 1 added" in out[0]
    assert "  run kids  Radarr: 2 added" in out[1]

    status, out, _ = invoke("history", "--limit", "1")
    assert [line.split("  ")[0] for line in out] == ["#2"]


def test_declining_or_previewing_records_nothing(fake_tmdb, fake_radarr, tmp_path, monkeypatch):
    can_prompt(monkeypatch)
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    invoke(*MOVIE_SEARCH)
    invoke(*MOVIE_SEARCH, "--apply", input="n\n")
    invoke("search", "story", "--kind", "movie", "--apply", "--yes")  # nothing to add

    status, out, _ = invoke("history")
    assert status == 0
    assert out == ["No runs recorded yet."]


def test_history_without_any_runs(tmp_path):
    write_config(tmp_path / "config.yaml")
    status, out, _ = invoke("history")

    assert status == 0
    assert out == ["No runs recorded yet."]
    assert not (tmp_path / "titularr.db").exists()  # looking doesn't create the file


def test_history_doesnt_need_the_secrets(fake_tmdb, fake_radarr, tmp_path, monkeypatch):
    # The secrets are set for the --apply run, then unset, as when running history by
    # hand where only the scheduler knows them.
    monkeypatch.setenv("SECRET_RADARR_KEY", "radarr-key")
    text = APPLY_CONFIG.replace("config-token", "${TMDB_API_TOKEN}").replace(
        "api_key: radarr-key", "api_key: ${SECRET_RADARR_KEY}"
    )
    write_config(tmp_path / "config.yaml", text)
    assert invoke(*MOVIE_SEARCH, "--apply", "--yes")[0] == 0

    monkeypatch.delenv("TMDB_API_TOKEN")
    monkeypatch.delenv("SECRET_RADARR_KEY")
    status, out, err = invoke("history")
    assert status == 0
    assert out[0].startswith("#1  ")
    assert err == ""

    # Commands that contact TMDB still refuse.
    status, _, err = invoke("run", "kids")
    assert status == 2
    assert "TMDB_API_TOKEN is not set" in err


def test_history_unknown_run(fake_tmdb, fake_radarr, tmp_path):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    invoke(*MOVIE_SEARCH, "--apply", "--yes")
    status, _, err = invoke("history", "9")

    assert status == 2
    assert "no run #9 in the run history" in err


def test_history_path_is_relative_to_the_config_folder(fake_tmdb, fake_radarr, tmp_path):
    folder = tmp_path / "conf"
    (folder / "data").mkdir(parents=True)
    config = write_config(folder / "titularr.yaml", APPLY_CONFIG + "history: data/runs.db\n")
    status, _, _ = invoke(*MOVIE_SEARCH, "--config", str(config), "--apply", "--yes")

    assert status == 0
    assert (folder / "data" / "runs.db").exists()
    assert not (tmp_path / "titularr.db").exists()
    status, out, _ = invoke("history", "--config", str(config))
    assert out[0].startswith("#1  ")


def test_unusable_history_path_stops_before_searching(fake_tmdb, fake_radarr, tmp_path):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG + "history: missing/runs.db\n")
    requests, _ = fake_radarr
    status, out, err = invoke(*MOVIE_SEARCH, "--apply", "--yes")

    assert status == 2
    assert "can't open run history" in err
    assert fake_tmdb == [] and out == [] and posted(requests) == []


# --- add limit (max_adds) ---


def test_apply_yes_adds_up_to_the_limit(fake_tmdb, fake_radarr, tmp_path):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    requests, _ = fake_radarr
    status, out, _ = invoke(*MOVIE_SEARCH, "--apply", "--yes", "--max-adds", "2")

    assert status == 0
    assert out[-1] == "Radarr: 2 added, 0 already there, 0 failed"
    assert len(posted(requests)) == 2


def test_apply_yes_over_the_limit_adds_nothing(fake_tmdb, fake_radarr, tmp_path):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    requests, _ = fake_radarr
    status, out, err = invoke(*MOVIE_SEARCH, "--apply", "--yes", "--max-adds", "1")

    assert status == 1
    assert (
        "Not adding: 2 titles to add is more than --max-adds (1). "
        "Preview the query, then narrow it or raise --max-adds." in err
    )
    # The titles that would have been added are still listed, for review.
    assert out[:2] == [CHRISTMASTIME, ADULT]
    assert posted(requests) == []
    assert invoke("history")[1] == ["No runs recorded yet."]


def test_limit_counts_movies_and_series_together(fake_tmdb, fake_radarr, fake_sonarr, tmp_path):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    radarr_requests, _ = fake_radarr
    sonarr_requests, _, _ = fake_sonarr
    status, _, err = invoke(*BOTH_SEARCH, "--apply", "--yes", "--max-adds", "2")

    assert status == 1
    assert "Not adding: 3 titles to add is more than --max-adds (2)." in err
    assert posted(radarr_requests) == [] and posted(sonarr_requests) == []


def test_limit_doesnt_apply_when_asking_first(fake_tmdb, fake_radarr, tmp_path, monkeypatch):
    can_prompt(monkeypatch)
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    requests, _ = fake_radarr
    status, out, _ = invoke(*MOVIE_SEARCH, "--apply", "--max-adds", "1", input="y\n")

    assert status == 0
    assert out[-1] == "Radarr: 2 added, 0 already there, 0 failed"
    assert len(posted(requests)) == 2


def test_saved_query_has_its_own_limit(fake_tmdb, fake_radarr, tmp_path):
    config = APPLY_CONFIG.replace(
        "      root_folder: /kids\n", "      root_folder: /kids\n    max_adds: 1\n"
    )
    write_config(tmp_path / "config.yaml", config)
    requests, _ = fake_radarr
    status, _, err = invoke("run", "kids", "--apply", "--yes")

    assert status == 1
    assert "Not adding: 2 titles to add is more than max_adds (1). " in err
    assert "raise max_adds." in err
    assert posted(requests) == []


def test_max_adds_must_be_at_least_one(fake_tmdb, tmp_path):
    config = APPLY_CONFIG.replace(
        "      root_folder: /kids\n", "      root_folder: /kids\n    max_adds: 0\n"
    )
    write_config(tmp_path / "config.yaml", config)
    status, _, err = invoke("validate")

    assert status == 2
    assert "queries.kids.max_adds" in err

    status, _, _ = invoke(*MOVIE_SEARCH, "--max-adds", "0")
    assert status == 2


# --- run --scheduled ---

SCHEDULED_CONFIG = (
    APPLY_CONFIG.replace("  kids:\n", "  kids:\n    scheduled: true\n").replace(
        "  anime:\n", "  anime:\n    scheduled: true\n"
    )
    + "  not-scheduled:\n    text: christmas\n    kinds: [movie]\n"
)


def headers(out):
    return [line for line in out if line.startswith("== ")]


def test_scheduled_runs_only_scheduled_queries_in_order(
    fake_tmdb, fake_radarr, fake_sonarr, tmp_path
):
    write_config(tmp_path / "config.yaml", SCHEDULED_CONFIG)
    status, out, _ = invoke("run", "--scheduled")

    assert status == 0
    assert headers(out) == ["== kids ==", "== anime =="]
    assert out[-1] == "Scheduled queries: 2 run, 0 failed"
    # A preview: nothing is added.
    radarr_requests, _ = fake_radarr
    sonarr_requests, _, _ = fake_sonarr
    assert posted(radarr_requests) == [] and posted(sonarr_requests) == []


def test_scheduled_apply_adds_and_records_each_query(fake_tmdb, fake_radarr, fake_sonarr, tmp_path):
    write_config(tmp_path / "config.yaml", SCHEDULED_CONFIG)
    status, out, _ = invoke("run", "--scheduled", "--apply", "--yes")

    assert status == 0
    assert "Radarr: 2 added, 0 already there, 0 failed" in out
    assert "Sonarr: 1 added, 0 already there, 0 not found, 0 failed" in out
    assert out[-1] == "Scheduled queries: 2 run, 0 failed"

    _, history_out, _ = invoke("history")
    assert [line.split("  ")[2] for line in history_out] == [
        "run anime",
        "run kids",
    ]


def test_one_failing_query_doesnt_stop_the_others(fake_tmdb, fake_radarr, fake_sonarr, tmp_path):
    write_config(tmp_path / "config.yaml", SCHEDULED_CONFIG)
    _, answers = fake_radarr
    answers[2] = httpx2.Response(500, json={"message": "Database is locked"})
    sonarr_requests, _, _ = fake_sonarr
    status, out, _ = invoke("run", "--scheduled", "--apply", "--yes")

    assert status == 1
    assert "Radarr: 1 added, 0 already there, 1 failed" in out
    assert len(posted(sonarr_requests)) == 1  # anime still ran after kids failed
    assert out[-1] == "Scheduled queries: 2 run, 1 failed"


def test_too_many_to_add_counts_as_failed(fake_tmdb, fake_radarr, fake_sonarr, tmp_path):
    config = SCHEDULED_CONFIG.replace("  kids:\n", "  kids:\n    max_adds: 1\n")
    write_config(tmp_path / "config.yaml", config)
    status, out, err = invoke("run", "--scheduled", "--apply", "--yes")

    assert status == 1
    assert "more than max_adds (1)" in err
    assert out[-1] == "Scheduled queries: 2 run, 1 failed"


@pytest.mark.parametrize("args", [("run",), ("run", "kids", "--scheduled")])
def test_run_needs_a_name_or_scheduled(fake_tmdb, tmp_path, args):
    write_config(tmp_path / "config.yaml", SCHEDULED_CONFIG)
    status, _, err = invoke(*args)

    assert status == 2
    assert "give the name of a saved query, or --scheduled (not both)" in err
    assert fake_tmdb == []


def test_no_scheduled_queries(fake_tmdb, tmp_path):
    write_config(tmp_path / "config.yaml", APPLY_CONFIG)
    status, out, _ = invoke("run", "--scheduled", "--apply", "--yes")

    assert status == 0
    assert out == ["No saved queries have scheduled: true."]
    assert fake_tmdb == []


def test_scheduled_apply_without_yes_needs_someone_to_answer(fake_tmdb, fake_radarr, tmp_path):
    write_config(tmp_path / "config.yaml", SCHEDULED_CONFIG)
    status, out, err = invoke("run", "--scheduled", "--apply")

    assert status == 2
    assert err.count("nobody can answer here; add --yes") == 1
    assert out == [] and fake_tmdb == []


def test_validate_lists_scheduled_queries(tmp_path):
    write_config(tmp_path / "config.yaml", SCHEDULED_CONFIG)
    status, out, _ = invoke("validate")

    assert status == 0
    assert out[0].endswith("library check: Radarr, Sonarr, scheduled: kids, anime")


# --- runtime filter ---


def test_search_runtime_min_drops_short_movies(fake_tmdb):
    status, out, _ = invoke(*MOVIE_SEARCH, "--runtime-min", "80", "--show-dropped")

    assert status == 0
    assert STORY in out
    assert f"{CHRISTMASTIME} (filtered: runtime 45 min < 80)" in out
    runtime_paths = [r.url.path for r in fake_tmdb if "/movie/" in r.url.path]
    assert runtime_paths  # one lookup per title that passed everything else
    assert len(runtime_paths) == len(set(runtime_paths))


def test_search_runtime_filter_needs_movies_only(fake_tmdb):
    status, _, err = invoke("search", "christmas", "--runtime-max", "180")

    assert status == 2
    assert "runtime filters work for movies only" in err
    assert fake_tmdb == []


def test_saved_query_with_runtime_filter_and_series_fails_validate(tmp_path):
    text = CONFIG + "  long-ones:\n    text: christmas\n    filters:\n      runtime_max: 180\n"
    write_config(tmp_path / "config.yaml", text)
    status, _, err = invoke("validate")

    assert status == 2
    assert "runtime filters work for movies only" in err
