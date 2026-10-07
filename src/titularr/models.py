"""Data types shared across Titularr."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

AdultMode = Literal["exclude", "include", "only"]
"""Whether a search leaves out adult titles (the default), includes them, or returns only them."""


class Candidate(BaseModel):
    """A title found by a source, before any matching or filtering.

    `None` means the source didn't say (e.g. no votes recorded), not zero.
    """

    model_config = ConfigDict(frozen=True)

    source: Literal["tmdb"]
    kind: Literal["movie"]
    tmdb_id: int
    title: str
    original_title: str | None
    year: int | None
    original_language: str | None
    vote_average: float | None
    vote_count: int | None
    adult: bool | None
