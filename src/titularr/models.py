"""Data types shared across Titularr."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

AdultMode = Literal["exclude", "include", "only"]
"""Whether a search leaves out adult titles (the default), includes them, or returns only them."""

Kind = Literal["movie", "series"]
"""What a title is: a movie (added to Radarr) or a series (added to Sonarr)."""

Status = Literal["kept", "no_match", "excluded", "filtered", "in_library"]
"""What a run decided about a title. "kept" means it would be added."""


class Candidate(BaseModel):
    """A title found by a source, before any matching or filtering.

    `None` means the source didn't say (e.g. no votes recorded), not zero.
    """

    model_config = ConfigDict(frozen=True)

    source: Literal["tmdb"]
    kind: Kind
    tmdb_id: int
    title: str
    original_title: str | None
    year: int | None
    original_language: str | None
    vote_average: float | None
    vote_count: int | None
    adult: bool | None


class Decision(BaseModel):
    """What a run decided about one candidate, and why.

    `reasons` is empty for "kept" and "no_match", holds the exclusion rule that applied
    for "excluded", and every failed check for "filtered".
    """

    model_config = ConfigDict(frozen=True)

    candidate: Candidate
    status: Status
    reasons: tuple[str, ...] = ()
