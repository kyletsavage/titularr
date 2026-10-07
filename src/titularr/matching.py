"""Title matching: keep only the candidates whose title really matches.

Sources search fuzzily (TMDB's "christmas" search also returns "Secret Santa"), so
every candidate is checked locally against the user's term or pattern.
"""

import re
import unicodedata
from typing import Literal

from titularr.models import Candidate

MatchMode = Literal["substring", "word", "regex"]
"""How a title is compared: anywhere in it, as a whole word/phrase, or by regex."""


def _normalize(text: str) -> str:
    """Fold case and accents and collapse whitespace, so "Noël" compares equal to "noel".

    Only letters Unicode decomposes into "letter + accent" are folded; letters such as
    "ø", "æ" and "ł" are distinct letters and stay as they are.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(without_accents.casefold().split())


class TitleMatcher:
    """Decides whether a candidate's title (or original title) matches.

    - "substring": the term appears anywhere, ignoring case and accents.
    - "word": the term appears as a whole word or phrase, ignoring case and accents.
    - "regex": a Python regular expression, case-insensitive, searched anywhere in the
      title unless anchored with ^ or $. Accents are not folded.
    """

    def __init__(self, mode: MatchMode, value: str) -> None:
        if not value.strip():
            raise ValueError("the match text must not be empty")
        self.mode = mode
        self.value = value
        if mode == "regex":
            try:
                self._pattern = re.compile(value, re.IGNORECASE)
            except re.error as e:
                raise ValueError(f"invalid regex {value!r}: {e}") from e
        elif mode == "word":
            self._pattern = re.compile(rf"\b{re.escape(_normalize(value))}\b")
        else:
            self._term = _normalize(value)

    def matches(self, candidate: Candidate) -> bool:
        titles = [candidate.title]
        if candidate.original_title:
            titles.append(candidate.original_title)
        return any(self._matches_text(t) for t in titles)

    def _matches_text(self, title: str) -> bool:
        if self.mode == "regex":
            return self._pattern.search(title) is not None
        if self.mode == "word":
            return self._pattern.search(_normalize(title)) is not None
        return self._term in _normalize(title)
