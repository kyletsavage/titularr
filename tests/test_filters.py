import pytest
from pydantic import ValidationError

from titularr.filters import Filters
from titularr.models import Candidate


def candidate(year=2003, language="en", rating=7.1, votes=1200):
    return Candidate(
        source="tmdb",
        kind="movie",
        tmdb_id=1,
        title="Elf",
        original_title=None,
        year=year,
        original_language=language,
        vote_average=rating,
        vote_count=votes,
        adult=None,
    )


def test_default_keeps_only_english():
    filters = Filters()
    assert filters.languages == {"en"}
    assert filters.active
    assert filters.reasons(candidate(language="en")) == []
    assert filters.reasons(candidate(language="fr")) == ["language fr not in en"]
    assert filters.reasons(candidate(language=None)) == ["language unknown"]


def test_any_language_with_nothing_else_keeps_everything():
    filters = Filters(languages=None)
    assert not filters.active
    assert filters.reasons(candidate(language="fr")) == []
    assert filters.reasons(candidate(year=None, language=None, rating=None, votes=None)) == []


# --- year ---


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (1939, ["year 1939 < 1940"]),
        (1940, []),  # bounds are inclusive
        (1999, []),
        (2000, ["year 2000 > 1999"]),
    ],
)
def test_year_range(year, expected):
    assert Filters(year_min=1940, year_max=1999).reasons(candidate(year=year)) == expected


def test_year_min_only_and_max_only():
    assert Filters(year_min=2000).reasons(candidate(year=1990)) == ["year 1990 < 2000"]
    assert Filters(year_max=2000).reasons(candidate(year=2010)) == ["year 2010 > 2000"]


# --- language ---


def test_language_allow_list():
    filters = Filters(languages={"en", "ja"})
    assert filters.reasons(candidate(language="ja")) == []
    assert filters.reasons(candidate(language="fr")) == ["language fr not in en, ja"]


def test_language_codes_are_case_insensitive():
    assert Filters(languages={"EN"}).reasons(candidate(language="en")) == []
    assert Filters(languages={"en"}).reasons(candidate(language="EN")) == []


def test_nonstandard_language_codes_are_allowed():
    # TMDB uses "cn" for Cantonese, which isn't an ISO 639-1 code.
    assert Filters(languages={"cn"}).reasons(candidate(language="cn")) == []


# --- rating and votes ---


def test_min_rating():
    filters = Filters(min_rating=6)
    assert filters.reasons(candidate(rating=6.0)) == []
    assert filters.reasons(candidate(rating=5.2)) == ["rating 5.2 < 6.0"]


def test_min_votes():
    filters = Filters(min_votes=500)
    assert filters.reasons(candidate(votes=500)) == []
    assert filters.reasons(candidate(votes=12)) == ["votes 12 < 500"]


# --- several reasons, unknown values ---


def test_every_failed_check_is_reported_in_order():
    filters = Filters(year_min=2010, languages={"ja"}, min_rating=8, min_votes=5000)
    assert filters.reasons(candidate()) == [
        "year 2003 < 2010",
        "language en not in ja",
        "rating 7.1 < 8.0",
        "votes 1200 < 5000",
    ]


def test_unknown_values_are_dropped_by_default():
    filters = Filters(year_min=1940, languages={"en"}, min_rating=5, min_votes=100)
    unknown = candidate(year=None, language=None, rating=None, votes=None)
    assert filters.reasons(unknown) == [
        "year unknown",
        "language unknown",
        "rating unknown",
        "votes unknown",
    ]


def test_keep_unknown_keeps_unknown_values_but_not_known_failures():
    filters = Filters(min_votes=100, min_rating=5, keep_unknown=True)
    assert filters.reasons(candidate(votes=None, rating=None)) == []
    assert filters.reasons(candidate(votes=12, rating=None)) == ["votes 12 < 100"]


def test_unknown_value_for_an_unset_filter_is_fine():
    filters = Filters(languages=None, min_votes=100)
    assert filters.reasons(candidate(year=None, language=None)) == []


# --- validation ---


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"year_min": 2000, "year_max": 1990}, "year_min .2000. is after year_max .1990."),
        ({"min_rating": 11}, "less than or equal to 10"),
        ({"min_rating": -1}, "greater than or equal to 0"),
        ({"min_votes": -5}, "greater than or equal to 0"),
        ({"languages": {"en", " "}}, "language codes must not be empty"),
    ],
)
def test_invalid_filters_are_rejected(kwargs, message):
    with pytest.raises(ValidationError, match=message):
        Filters(**kwargs)


def test_active_when_any_filter_is_set():
    assert Filters(languages=None, min_votes=0).active
    assert Filters(languages={"ja"}).active
    assert not Filters(languages=None, keep_unknown=True).active
