"""Tests for the run history file."""

import sqlite3
from contextlib import closing

import pytest

from titularr.history import SCHEMA_VERSION, History, HistoryError, RecordedTitle
from titularr.models import Candidate


def candidate(tmdb_id, title, kind="movie", year=2000):
    return Candidate(
        source="tmdb",
        kind=kind,
        tmdb_id=tmdb_id,
        title=title,
        original_title=None,
        year=year,
        original_language=None,
        vote_average=None,
        vote_count=None,
        adult=None,
    )


def test_open_creates_the_file_and_tables(tmp_path):
    path = tmp_path / "titularr.db"
    with History.open(path) as history:
        assert history.recent_runs() == []

    assert path.exists()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_reopening_keeps_what_was_recorded(tmp_path):
    path = tmp_path / "titularr.db"
    with History.open(path) as history:
        history.start_run("search", "christmas", None)
    with History.open(path) as history:
        assert [run.id for run in history.recent_runs()] == [1]


def test_a_run_and_its_titles_round_trip(tmp_path):
    with History.open(tmp_path / "titularr.db") as history:
        run_id = history.start_run("run", "christmas", "holiday-movies")
        history.record(run_id, candidate(2, "Christmastime", year=2012), "added")
        history.record(run_id, candidate(4, "Adult Christmas", year=None), "failed", "HTTP 500")
        history.record(run_id, candidate(10, "Christmas Wars", kind="series"), "not_found")
        history.finish_run(run_id)

        run = history.get_run(run_id)
        assert run.command == "run"
        assert (run.query_name, run.query_text) == ("holiday-movies", "christmas")
        assert run.finished_at is not None and run.finished_at >= run.started_at
        assert run.counts == {
            ("movie", "added"): 1,
            ("movie", "failed"): 1,
            ("series", "not_found"): 1,
        }
        assert history.run_titles(run_id) == [
            RecordedTitle("movie", 2, "Christmastime", 2012, "added", None),
            RecordedTitle("movie", 4, "Adult Christmas", None, "failed", "HTTP 500"),
            RecordedTitle("series", 10, "Christmas Wars", 2000, "not_found", None),
        ]


def test_an_unfinished_run_has_no_finish_time(tmp_path):
    with History.open(tmp_path / "titularr.db") as history:
        run_id = history.start_run("search", "christmas", None)
        assert history.get_run(run_id).finished_at is None


def test_recent_runs_are_newest_first_and_limited(tmp_path):
    with History.open(tmp_path / "titularr.db") as history:
        for _ in range(3):
            history.start_run("search", "christmas", None)
        assert [run.id for run in history.recent_runs(limit=2)] == [3, 2]


def test_unknown_run(tmp_path):
    with History.open(tmp_path / "titularr.db") as history:
        assert history.get_run(7) is None
        assert history.run_titles(7) == []


def test_a_file_that_isnt_a_database_is_an_error(tmp_path):
    path = tmp_path / "titularr.db"
    path.write_bytes(b"not a database, just some text that is long enough" * 4)
    with pytest.raises(HistoryError, match="can't use run history"):
        History.open(path)


def test_a_newer_schema_is_refused(tmp_path):
    path = tmp_path / "titularr.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(HistoryError, match="written by a newer Titularr"):
        History.open(path)


def test_a_missing_folder_is_an_error(tmp_path):
    with pytest.raises(HistoryError, match="can't open run history"):
        History.open(tmp_path / "missing" / "titularr.db")
