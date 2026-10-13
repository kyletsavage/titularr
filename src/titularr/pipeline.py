"""The pipeline: search a source, then decide what to keep and why.

For every candidate a source returns, the decision is made in a fixed order:
match the title, then check exclusions, then filters. Interfaces (the CLI now, an API
and web UI later) build a `Query`, call `run`, and present the decisions.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from titularr.exclusions import Exclusions
from titularr.filters import Filters
from titularr.matching import MatchMode, TitleMatcher
from titularr.models import AdultMode, Candidate, Decision, Kind


class TitleSource(Protocol):
    """Somewhere titles are searched for. `TmdbClient` is one."""

    def search_movies(self, query: str, *, adult: AdultMode = "exclude") -> Iterator[Candidate]: ...

    def search_series(self, query: str, *, adult: AdultMode = "exclude") -> Iterator[Candidate]: ...


class Query(BaseModel):
    """One search and the rules for what to keep.

    `text` is what the source searches for. Titles are matched against `text` too
    (as a substring or whole word), unless `match` is "regex": then `pattern` is
    required and is what titles are matched against.

    `exclusions` are this query's own; `global_exclusions` come from the config and
    apply to every query. Both are checked, and each reason says which one applied.
    """

    # extra="forbid": a typo like "filter" in a config file is an error, not ignored.
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1)
    kinds: tuple[Kind, ...] = Field(default=("movie", "series"), min_length=1)
    match: MatchMode = "substring"
    pattern: str | None = None
    adult: AdultMode = "exclude"
    filters: Filters = Filters()
    exclusions: Exclusions = Exclusions()
    global_exclusions: Exclusions = Exclusions()

    _matcher: TitleMatcher = PrivateAttr()

    @field_validator("text")
    @classmethod
    def _check_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("the search text must not be empty")
        return value

    @field_validator("kinds")
    @classmethod
    def _unique_kinds(cls, value: tuple[Kind, ...]) -> tuple[Kind, ...]:
        return tuple(dict.fromkeys(value))  # drop repeats, keep order

    @model_validator(mode="after")
    def _build_matcher(self) -> Self:
        if self.match == "regex" and self.pattern is None:
            raise ValueError('a pattern is required when match is "regex"')
        if self.match != "regex" and self.pattern is not None:
            raise ValueError('a pattern is only used when match is "regex"')
        value = self.pattern if self.match == "regex" else self.text
        self._matcher = TitleMatcher(self.match, value)  # raises for an invalid regex
        return self

    def matches(self, candidate: Candidate) -> bool:
        return self._matcher.matches(candidate)

    def exclusion_layers(self) -> list[tuple[str, Exclusions]]:
        """Exclusion lists in the order they're checked, each with its label.

        The blacklist (SQLite + web UI, later) will be added here, first.
        """
        return [("global", self.global_exclusions), ("query", self.exclusions)]


@dataclass(frozen=True)
class Library:
    """TMDB IDs already in Radarr (movies) and Sonarr (series).

    None for a kind means its library wasn't checked (that app isn't configured).
    """

    movie_ids: frozenset[int] | None = None
    series_ids: frozenset[int] | None = None

    def checks(self, kind: Kind) -> bool:
        return self.ids(kind) is not None

    def ids(self, kind: Kind) -> frozenset[int] | None:
        return self.movie_ids if kind == "movie" else self.series_ids

    def has(self, candidate: Candidate) -> bool:
        ids = self.ids(candidate.kind)
        return ids is not None and candidate.tmdb_id in ids


def decide(query: Query, candidate: Candidate, library: Library | None = None) -> Decision:
    """Match, then exclusions, then filters, then the library check.

    An excluded title isn't filtered, and only titles that would otherwise be kept are
    checked against the library: "kept" means it would be added.
    """
    if not query.matches(candidate):
        return Decision(candidate=candidate, status="no_match")
    for label, exclusions in query.exclusion_layers():
        if reason := exclusions.reason(candidate):
            return Decision(candidate=candidate, status="excluded", reasons=(f"{label}: {reason}",))
    if reasons := query.filters.reasons(candidate):
        return Decision(candidate=candidate, status="filtered", reasons=tuple(reasons))
    if library is not None and library.has(candidate):
        return Decision(candidate=candidate, status="in_library")
    return Decision(candidate=candidate, status="kept")


def run(query: Query, source: TitleSource, library: Library | None = None) -> Iterator[Decision]:
    """Search for each kind in turn and yield a decision per candidate as it arrives."""
    for kind in query.kinds:
        search = source.search_movies if kind == "movie" else source.search_series
        for candidate in search(query.text, adult=query.adult):
            yield decide(query, candidate, library)


@dataclass
class KindCounts:
    found: int = 0
    matched: int = 0
    excluded: int = 0
    kept: int = 0
    in_library: int = 0


@dataclass
class Summary:
    """Running counts per kind. Every matched title is excluded, filtered, in the
    library, or kept."""

    kinds: tuple[Kind, ...] = ("movie", "series")
    counts: dict[Kind, KindCounts] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for kind in self.kinds:
            self.counts.setdefault(kind, KindCounts())

    def add(self, decision: Decision) -> None:
        counts = self.counts.setdefault(decision.candidate.kind, KindCounts())
        counts.found += 1
        if decision.status != "no_match":
            counts.matched += 1
        if decision.status == "excluded":
            counts.excluded += 1
        elif decision.status == "kept":
            counts.kept += 1
        elif decision.status == "in_library":
            counts.in_library += 1
