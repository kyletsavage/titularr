import pytest
from pydantic import ValidationError

from titularr.exclusions import Exclusions
from titularr.filters import Filters
from titularr.models import Candidate, Decision
from titularr.pipeline import Library, Query, Summary, decide, run


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
        self.runtimes = {}
        self.calls = []

    def search_movies(self, query, *, adult="exclude"):
        self.calls.append(("movie", query, adult))
        yield from self.movies

    def search_series(self, query, *, adult="exclude"):
        self.calls.append(("series", query, adult))
        yield from self.series

    def movie_runtime(self, tmdb_id):
        self.calls.append(("runtime", tmdb_id))
        return self.runtimes.get(tmdb_id)


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
    assert decision.reasons == ("query: id movie:1",)


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


# --- exclusion layers ---


def test_exclusions_say_which_layer_excluded_them():
    query = Query(
        text="christmas",
        global_exclusions=Exclusions(ids={"movie:1"}),
        exclusions=Exclusions(ids={"movie:2"}),
    )
    assert decide(query, candidate(1)).reasons == ("global: id movie:1",)
    assert decide(query, candidate(2)).reasons == ("query: id movie:2",)
    assert decide(query, candidate(3)).status == "kept"


def test_global_layer_is_checked_first():
    query = Query(
        text="christmas",
        global_exclusions=Exclusions(titles=("story",)),
        exclusions=Exclusions(ids={"movie:1"}),
    )
    assert decide(query, candidate(1)).reasons == ('global: title "story"',)


def test_empty_layers_exclude_nothing():
    query = Query(text="christmas")
    assert [label for label, _ in query.exclusion_layers()] == ["global", "query"]
    assert decide(query, candidate()).status == "kept"


def test_query_rejects_unknown_fields():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Query(text="christmas", filter={"min_votes": 5})


# --- library check ---


def test_kept_title_in_the_library_is_in_library():
    library = Library(movie_ids=frozenset({1}))
    assert decide(Query(text="christmas"), candidate(1), library).status == "in_library"
    assert decide(Query(text="christmas"), candidate(2), library).status == "kept"


def test_library_only_applies_to_the_matching_kind():
    library = Library(movie_ids=frozenset({1}))
    series = candidate(1, kind="series")
    assert decide(Query(text="christmas"), series, library).status == "kept"
    assert not library.checks("series")


def test_filtered_and_excluded_titles_keep_their_reasons_even_in_the_library():
    library = Library(movie_ids=frozenset({1, 2}))
    query = Query(
        text="christmas",
        filters=Filters(min_votes=5000),
        exclusions=Exclusions(ids={"movie:2"}),
    )
    assert decide(query, candidate(1), library).status == "filtered"
    assert decide(query, candidate(2), library).status == "excluded"


def test_unchecked_library_leaves_titles_kept():
    assert decide(Query(text="christmas"), candidate(1), Library()).status == "kept"
    assert decide(Query(text="christmas"), candidate(1)).status == "kept"


def test_run_and_summary_count_in_library():
    source = FakeSource(movies=[candidate(1), candidate(2)])
    query = Query(text="christmas", kinds=("movie",))
    summary = Summary(kinds=query.kinds)
    for decision in run(query, source, Library(movie_ids=frozenset({2}))):
        summary.add(decision)

    counts = summary.counts["movie"]
    assert (counts.found, counts.matched, counts.kept, counts.in_library) == (2, 2, 1, 1)


# --- runtime filter ---

RUNTIME_QUERY = Query(text="christmas", kinds=("movie",), filters=Filters(runtime_min=80))


class Lookups:
    """A runtime lookup that records which TMDB IDs it was asked for."""

    def __init__(self, runtimes):
        self.runtimes = runtimes
        self.asked = []

    def __call__(self, tmdb_id):
        self.asked.append(tmdb_id)
        return self.runtimes.get(tmdb_id)


def test_runtime_filter_drops_short_movies_and_keeps_the_rest():
    lookup = Lookups({1: 94, 2: 45})
    kept = decide(RUNTIME_QUERY, candidate(1), runtime=lookup)
    short = decide(RUNTIME_QUERY, candidate(2), runtime=lookup)

    assert kept.status == "kept"
    assert (short.status, short.reasons) == ("filtered", ("runtime 45 min < 80",))
    assert lookup.asked == [1, 2]


def test_runtime_is_only_looked_up_for_titles_that_would_be_added():
    query = Query(
        text="christmas",
        kinds=("movie",),
        filters=Filters(runtime_min=80, min_votes=100),
        exclusions=Exclusions(ids={"movie:2"}),
    )
    library = Library(movie_ids=frozenset({4}))
    lookup = Lookups({})
    statuses = [
        decide(query, c, library, lookup).status
        for c in (candidate(1, title="Elf"), candidate(2), candidate(3, votes=5), candidate(4))
    ]

    assert statuses == ["no_match", "excluded", "filtered", "in_library"]
    assert lookup.asked == []


def test_no_runtime_lookup_without_a_runtime_filter():
    source = FakeSource(movies=[candidate(1)])
    decisions = list(run(Query(text="christmas", kinds=("movie",)), source))

    assert [d.status for d in decisions] == ["kept"]
    assert ("runtime", 1) not in source.calls


def test_run_looks_up_runtimes_from_the_source():
    source = FakeSource(movies=[candidate(1), candidate(2)])
    source.runtimes = {1: 94}  # 2 unknown
    decisions = list(run(RUNTIME_QUERY, source))

    assert [(d.status, d.reasons) for d in decisions] == [
        ("kept", ()),
        ("filtered", ("runtime unknown",)),
    ]
    assert source.calls == [("movie", "christmas", "exclude"), ("runtime", 1), ("runtime", 2)]


def test_decide_with_a_runtime_filter_needs_a_lookup():
    with pytest.raises(ValueError, match="runtime lookup is needed"):
        decide(RUNTIME_QUERY, candidate(1))


@pytest.mark.parametrize("kinds", [("movie", "series"), ("series",)])
def test_runtime_filters_are_for_movies_only(kinds):
    with pytest.raises(ValidationError, match="runtime filters work for movies only"):
        Query(text="christmas", kinds=kinds, filters=Filters(runtime_max=180))
