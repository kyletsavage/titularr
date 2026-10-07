import pytest
from pydantic import ValidationError

from titularr.exclusions import Exclusions
from titularr.models import Candidate


def candidate(kind="movie", tmdb_id=1399, title="War Horse", original_title=None):
    return Candidate(
        source="tmdb",
        kind=kind,
        tmdb_id=tmdb_id,
        title=title,
        original_title=original_title,
        year=2011,
        original_language="en",
        vote_average=7.0,
        vote_count=4000,
        adult=False,
    )


def test_empty_excludes_nothing():
    exclusions = Exclusions()
    assert not exclusions.active
    assert exclusions.reason(candidate()) is None


# --- IDs ---


def test_id_exclusion_respects_kind():
    exclusions = Exclusions(ids={"movie:1399"})
    assert exclusions.reason(candidate(kind="movie", tmdb_id=1399)) == "id movie:1399"
    assert exclusions.reason(candidate(kind="series", tmdb_id=1399)) is None
    assert exclusions.reason(candidate(kind="movie", tmdb_id=1400)) is None


@pytest.mark.parametrize("raw", ["Series:5", " series:5 ", "SERIES:005"])
def test_ids_are_normalized(raw):
    exclusions = Exclusions(ids={raw})
    assert exclusions.ids == {"series:5"}
    assert exclusions.reason(candidate(kind="series", tmdb_id=5)) == "id series:5"


@pytest.mark.parametrize("raw", ["1399", "tv:1399", "movie:abc", "movie:", "movie:12:3", ""])
def test_invalid_ids_are_rejected(raw):
    with pytest.raises(ValidationError, match="expected movie:<id> or series:<id>"):
        Exclusions(ids={raw})


# --- titles (substring) ---


@pytest.mark.parametrize("title", ["War Horse", "Warrior", "Star Wars", "THE WAR"])
def test_title_exclusion_is_substring_and_case_insensitive(title):
    assert Exclusions(titles=("war",)).reason(candidate(title=title)) == 'title "war"'


def test_title_exclusion_ignores_accents_and_checks_original_title():
    exclusions = Exclusions(titles=("noel",))
    assert exclusions.reason(candidate(title="Joyeux Noël")) == 'title "noel"'
    assert exclusions.reason(candidate(title="Paris", original_title="Un Noël à Paris"))


def test_title_exclusion_does_not_touch_other_titles():
    assert Exclusions(titles=("war",)).reason(candidate(title="Elf")) is None


# --- regexes ---


def test_regex_exclusion():
    exclusions = Exclusions(regexes=(r"^a ",))
    assert exclusions.reason(candidate(title="A Christmas Story")) == 'regex "^a "'
    assert exclusions.reason(candidate(title="Christmas, A Story")) is None


# --- order and validation ---


def test_first_rule_wins_in_order_ids_titles_regexes():
    exclusions = Exclusions(ids={"movie:1399"}, titles=("horse",), regexes=("^war",))
    assert exclusions.reason(candidate()) == "id movie:1399"
    assert exclusions.reason(candidate(tmdb_id=1)) == 'title "horse"'
    assert exclusions.reason(candidate(tmdb_id=1, title="War Games")) == 'regex "^war"'


def test_empty_title_is_rejected():
    with pytest.raises(ValidationError, match="must not be empty"):
        Exclusions(titles=("  ",))


def test_invalid_regex_is_rejected():
    with pytest.raises(ValidationError, match="invalid regex"):
        Exclusions(regexes=("(unclosed",))


def test_active_when_any_rule_is_set():
    assert Exclusions(ids={"movie:1"}).active
    assert Exclusions(titles=("x",)).active
    assert Exclusions(regexes=("x",)).active
