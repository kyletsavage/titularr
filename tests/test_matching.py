import pytest

from titularr.matching import TitleMatcher
from titularr.models import Candidate


def candidate(title, original_title=None):
    return Candidate(
        source="tmdb",
        kind="movie",
        tmdb_id=1,
        title=title,
        original_title=original_title,
        year=None,
        original_language=None,
        vote_average=None,
        vote_count=None,
        adult=None,
    )


def matches(mode, value, title, original_title=None):
    return TitleMatcher(mode, value).matches(candidate(title, original_title))


# --- substring (the default mode) ---


@pytest.mark.parametrize(
    "title",
    ["Christmas Vacation", "A CHRISTMAS STORY", "Christmastime in the City", "Elf's christmas"],
)
def test_substring_matches_anywhere_ignoring_case(title):
    assert matches("substring", "christmas", title)


def test_substring_rejects_titles_without_the_term():
    assert not matches("substring", "christmas", "Secret Santa")


@pytest.mark.parametrize(("term", "title"), [("noel", "Noël"), ("Noël", "NOEL"), ("cafe", "Café")])
def test_substring_ignores_accents(term, title):
    assert matches("substring", term, title)


def test_substring_collapses_whitespace():
    assert matches("substring", "star  trek", "Star   Trek: The Next Generation")


# --- word ---


@pytest.mark.parametrize("title", ["A Christmas Story", "Christmas's Eve", "Merry CHRISTMAS!"])
def test_word_matches_whole_words(title):
    assert matches("word", "christmas", title)


@pytest.mark.parametrize("title", ["Christmastime in the City", "Xmaschristmas"])
def test_word_rejects_partial_words(title):
    assert not matches("word", "christmas", title)


def test_word_matches_phrases():
    assert matches("word", "star trek", "Star Trek: Discovery")
    assert not matches("word", "star trek", "Starstruck Trekking")


def test_word_ignores_accents():
    assert matches("word", "noel", "Joyeux Noël")


def test_word_treats_regex_characters_literally():
    assert matches("word", "9.5", "Plan 9.5")
    assert not matches("word", "9.5", "Plan 945")


# --- regex ---


def test_regex_is_case_insensitive_and_searches_anywhere():
    assert matches("regex", "dracula", "Bram Stoker's DRACULA")


def test_regex_anchors_work():
    assert matches("regex", "^dracula", "Dracula 2000")
    assert not matches("regex", "^dracula", "Bram Stoker's Dracula")
    assert matches("regex", r"^dracula( \d+)?$", "Dracula 2000")
    assert not matches("regex", r"^dracula( \d+)?$", "Dracula Untold")


def test_regex_does_not_fold_accents():
    assert not matches("regex", "noel", "Noël")
    assert matches("regex", "noël", "NOËL")


def test_invalid_regex_raises():
    with pytest.raises(ValueError, match="invalid regex"):
        TitleMatcher("regex", "(unclosed")


# --- both titles, empty input ---


def test_matches_on_original_title():
    assert matches("word", "noel", "Christmas in Paris", original_title="Un Noël à Paris")


def test_missing_original_title_is_fine():
    assert not matches("substring", "noel", "Christmas in Paris", original_title=None)


@pytest.mark.parametrize("mode", ["substring", "word", "regex"])
@pytest.mark.parametrize("value", ["", "   "])
def test_empty_value_raises(mode, value):
    with pytest.raises(ValueError, match="must not be empty"):
        TitleMatcher(mode, value)
