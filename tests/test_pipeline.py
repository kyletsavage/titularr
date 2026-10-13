import pytest
from pydantic import ValidationError

from titularr.exclusions import Exclusions
from titularr.filters import Filters
from titularr.models import Candidate, Decision
from titularr.pipeline import Query, Summary, decide, run


def candidate(tmdb_id=1, title="A Christmas Story", kind="movie", language="en", votes=2900):
    return Candidate(
        source="tmdb",
        kind=kind,
        tmdb_id=tmdb_id,
        title=title,
        original_title=None,
        year=1983,
        original_language=language,
        vote_average=7.3,
        vote_count=votes,
        adult=False,
    )


class FakeSource:
    """A TitleSource with canned results that records every search it's asked for."""

    def __init__(self, movies=(), series=()):
        self.movies = list(movies)
        self.series = list(series)
        self.calls = []

    def search_movies(self, query, *, adult="exclude"):
        self.calls.append(("movie", query, adult))
        yield from self.movies

    def search_series(self, query, *, adult="exclude"):
        self.calls.append(("series", query, adult))
        yield from self.series


# --- Query ---


def test_query_defaults():
    query = Query(text="christmas")
    assert query.kinds == ("movie", "series")
    assert query.match == "substring"
    assert query.pattern is None
    assert query.adult == "exclude"
    assert query.filters.languages == {"en"}
    assert not query.exclusions.active


@pytest.mark.parametrize("text", ["", "   "])
def test_query_text_must_not_be_empty(text):
    with pytest.raises(ValidationError):
        Query(text=text)


def test_query_needs_at_least_one_kind():
    with pytest.raises(ValidationError):
        Query(text="x", kinds=())


def test_query_kinds_drop_repeats_and_keep_order():
    assert Query(text="x", kinds=("series", "movie", "series")).kinds == ("series", "movie")


def test_regex_needs_a_pattern():
    with pytest.raises(ValidationError, match='pattern is required when match is "regex"'):
        Query(text="dracula", match="regex")


def test_pattern_needs_regex_mode():
    with pytest.raises(ValidationError, match='pattern is only used when match is "regex"'):
        Query(text="dracula", pattern="^dracula")


def test_invalid_regex_is_a_validation_error():
    with pytest.raises(ValidationError, match="invalid regex"):
        Query(text="dracula", match="regex", pattern="(unclosed")


def test_query_matches_text_or_pattern():
    assert Query(text="christmas").matches(candidate(title="Christmastime"))
    assert not Query(text="christmas", match="word").matches(candidate(title="Christmastime"))
    regex = Query(text="christmas", match="regex", pattern="^a ")
    assert regex.matches(candidate(title="A Christmas Story"))
    assert not regex.matches(candidate(title="Christmastime"))


# --- decide ---


def test_decide_kept():
    assert decide(Query(text="christmas"), candidate()) == Decision(
        candidate=candidate(), status="kept"
    )


def test_decide_no_match_skips_exclusions_and_filters():
    query = Query(text="christmas", exclusions=Exclusions(titles=("santa",)))
    decision = decide(query, candidate(title="Secret Santa", language="fr"))
    assert decision.status == "no_match"
    assert decision.reasons == ()


def test_decide_excluded_beats_filter_reasons():
    query = Query(
        text="christmas",
        filters=Filters(min_votes=5000),
        exclusions=Exclusions(ids={"movie:1"}),
    )
    decision = decide(query, candidate(tmdb_id=1, votes=10))
    assert decision.status == "excluded"
    assert decision.reasons == ("id movie:1",)


def test_decide_filtered_reports_every_reason():
    query = Query(text="christmas", filters=Filters(languages={"ja"}, min_votes=5000))
    decision = decide(query, candidate())
    assert decision.status == "filtered"
    assert decision.reasons == ("language en not in ja", "votes 2900 < 5000")


# --- run ---


def test_run_searches_each_kind_in_order_with_the_query_settings():
    source = FakeSource(
        movies=[candidate(1), candidate(2, title="Elf")],
        series=[candidate(3, kind="series", title="Christmas Wars")],
    )
    decisions = list(run(Query(text="christmas", adult="include"), source))

    assert source.calls == [("movie", "christmas", "include"), ("series", "christmas", "include")]
    assert [(d.candidate.tmdb_id, d.status) for d in decisions] == [
        (1, "kept"),
        (2, "no_match"),
        (3, "kept"),
    ]


def test_run_only_searches_requested_kinds():
    source = FakeSource(movies=[candidate()], series=[candidate(kind="series")])
    list(run(Query(text="christmas", kinds=("series",)), source))
    assert [call[0] for call in source.calls] == ["series"]


def test_run_streams_lazily():
    source = FakeSource(movies=[candidate(1), candidate(2)], series=[candidate(3, kind="series")])
    decisions = run(Query(text="christmas"), source)

    assert next(decisions).candidate.tmdb_id == 1
    assert source.calls == [("movie", "christmas", "exclude")]  # series not searched yet
    list(decisions)
    assert [call[0] for call in source.calls] == ["movie", "series"]


# --- Summary ---


def test_summary_counts_each_status_per_kind():
    query = Query(
        text="christmas",
        filters=Filters(min_votes=100),
        exclusions=Exclusions(titles=("carol",)),
    )
    source = FakeSource(
        movies=[
            candidate(1),  # kept
            candidate(2, title="A Christmas Carol"),  # excluded
            candidate(3, votes=5),  # filtered
            candidate(4, title="Elf"),  # no match
        ],
        series=[candidate(5, kind="series")],  # kept
    )
    summary = Summary(kinds=query.kinds)
    for decision in run(query, source):
        summary.add(decision)

    movies, series = summary.counts["movie"], summary.counts["series"]
    assert (movies.found, movies.matched, movies.excluded, movies.kept) == (4, 3, 1, 1)
    assert (series.found, series.matched, series.excluded, series.kept) == (1, 1, 0, 1)


def test_summary_lists_requested_kinds_even_when_empty():
    summary = Summary(kinds=("series",))
    assert list(summary.counts) == ["series"]
    assert summary.counts["series"].found == 0
