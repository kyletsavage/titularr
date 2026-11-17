"""Tests for the service layer: previews as data, shared by the CLI and the API."""

import logging
import threading

import pytest

from titularr.arr import ArrError, SonarrLibrary
from titularr.config import load_config
from titularr.filters import Filters
from titularr.models import Candidate
from titularr.pipeline import Query
from titularr.service import collect_warnings, preview
from titularr.sources.tmdb import TmdbError


def candidate(tmdb_id, title, kind="movie", votes=100):
    return Candidate(
        source="tmdb",
        kind=kind,
        tmdb_id=tmdb_id,
        title=title,
        original_title=None,
        year=2001,
        original_language="en",
        vote_average=7.0,
        vote_count=votes,
        adult=False,
    )


class FakeSource:
    """A TitleSource with canned results; `fail_after` raises a TMDB error after that
    many movies, as a search that stops part-way."""

    def __init__(self, movies=(), series=(), fail_after=None):
        self.movies = list(movies)
        self.series = list(series)
        self.fail_after = fail_after

    def search_movies(self, query, *, adult="exclude"):
        for i, movie in enumerate(self.movies):
            if i == self.fail_after:
                raise TmdbError("TMDB returned HTTP 503")
            yield movie

    def search_series(self, query, *, adult="exclude"):
        yield from self.series

    def movie_runtime(self, tmdb_id):
        return None


class FakeArr:
    """Stands in for RadarrClient and SonarrClient: a context manager with a library."""

    def __init__(self, ids=(), without_tmdb_id=(), error=None):
        self.ids = frozenset(ids)
        self.without_tmdb_id = tuple(without_tmdb_id)
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
        return SonarrLibrary(tmdb_ids=self.ids, titles_without_tmdb_id=self.without_tmdb_id)


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A config with Radarr and Sonarr; `config(sonarr_key=...)` can make Sonarr unusable."""
    monkeypatch.setenv("RADARR_KEY", "r")

    def make(sonarr_key="s"):
        text = (
            "sources:\n  tmdb:\n    token: t\n"
            "radarr:\n  url: http://radarr:7878\n  api_key: ${RADARR_KEY}\n"
            f"sonarr:\n  url: http://sonarr:8989\n  api_key: {sonarr_key}\n"
        )
        path = tmp_path / "config.yaml"
        path.write_bytes(text.encode("utf-8"))
        return load_config(path)

    return make


CHRISTMAS = Query(text="christmas", kinds=("movie",), filters=Filters(min_votes=50))
MOVIES = [
    candidate(1, "A Christmas Story"),
    candidate(2, "Christmas Vacation"),
    candidate(3, "Christmastime Short", votes=4),
    candidate(4, "Elf"),
]


def test_preview_returns_every_decision_with_reasons(config):
    result = preview(CHRISTMAS, FakeSource(MOVIES), config(), radarr=FakeArr(ids={2}))

    assert [(d.candidate.tmdb_id, d.status, d.reasons) for d in result.decisions] == [
        (1, "kept", ()),
        (2, "in_library", ()),
        (3, "filtered", ("votes 4 < 50",)),
        (4, "no_match", ()),
    ]
    assert [c.tmdb_id for c in result.kept] == [1]
    counts = result.summary.counts["movie"]
    assert (counts.found, counts.matched, counts.kept, counts.in_library) == (4, 3, 1, 1)
    assert result.library.checks("movie") and not result.library.checks("series")
    assert result.error is None
    assert result.warnings == []


def test_preview_without_config_checks_no_library():
    result = preview(CHRISTMAS, FakeSource(MOVIES), None)

    assert [d.status for d in result.decisions] == ["kept", "kept", "filtered", "no_match"]
    assert not result.library.checks("movie")


def test_preview_collects_the_warnings(config, monkeypatch):
    monkeypatch.delenv("UNSET_SONARR_KEY", raising=False)
    query = Query(text="christmas", kinds=("movie", "series"))
    result = preview(
        query, FakeSource(), config(sonarr_key="${UNSET_SONARR_KEY}"), radarr=FakeArr()
    )

    assert result.warnings == [
        (
            "sonarr is in the config but can't be used (UNSET_SONARR_KEY is not set); "
            "series won't be checked against your library"
        )
    ]


def test_preview_warns_about_sonarr_series_without_tmdb_ids(config):
    query = Query(text="christmas", kinds=("series",))
    sonarr = FakeArr(ids={10}, without_tmdb_id=["Old Show"])
    result = preview(query, FakeSource(), config(), sonarr=sonarr)

    assert result.warnings == [
        "1 series in Sonarr has no TMDB ID and can't be recognized in results: Old Show"
    ]


def test_tmdb_error_gives_a_partial_result(config):
    source = FakeSource(MOVIES, fail_after=2)
    result = preview(CHRISTMAS, source, config(), radarr=FakeArr())

    assert result.error == "TMDB returned HTTP 503"
    assert [d.candidate.tmdb_id for d in result.decisions] == [1, 2]
    assert result.summary.counts["movie"].found == 2


def test_arr_error_is_raised_before_searching(config):
    source = FakeSource(MOVIES, fail_after=0)  # would fail if it were searched
    with pytest.raises(ArrError, match="401"):
        preview(CHRISTMAS, source, config(), radarr=FakeArr(error="Radarr returned HTTP 401"))


def test_warnings_from_other_threads_are_not_collected():
    log = logging.getLogger("titularr.test")
    with collect_warnings() as warnings:
        log.warning("mine")
        other = threading.Thread(target=lambda: log.warning("someone else's"))
        other.start()
        other.join()
    log.warning("after the block")

    assert warnings == ["mine"]
