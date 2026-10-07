"""Exclusions: titles that must never be kept, whatever else matches.

Rules are TMDB IDs ("movie:1399", "series:1399") and title patterns. Each excluded title
gets a short reason naming the rule that applied.
"""

import re

from pydantic import BaseModel, ConfigDict, PrivateAttr, field_validator

from titularr.matching import TitleMatcher
from titularr.models import Candidate

# TMDB numbers movies and series separately, so an ID needs its kind.
_ID_PATTERN = re.compile(r"(movie|series):(\d+)", re.IGNORECASE)


class Exclusions(BaseModel):
    """Never-keep rules. `Exclusions()` excludes nothing.

    - `ids`: "movie:<tmdb id>" or "series:<tmdb id>".
    - `titles`: excluded if the text appears anywhere in the title or original title,
      ignoring case and accents (the same rule as substring matching).
    - `regexes`: excluded if the regular expression matches (case-insensitive).
    """

    model_config = ConfigDict(frozen=True)

    ids: frozenset[str] = frozenset()
    titles: tuple[str, ...] = ()
    regexes: tuple[str, ...] = ()

    _title_matchers: list[TitleMatcher] = PrivateAttr(default_factory=list)
    _regex_matchers: list[TitleMatcher] = PrivateAttr(default_factory=list)

    @field_validator("ids")
    @classmethod
    def _normalize_ids(cls, value: frozenset[str]) -> frozenset[str]:
        normalized = set()
        for raw in value:
            match = _ID_PATTERN.fullmatch(raw.strip())
            if match is None:
                raise ValueError(f"invalid id {raw!r}: expected movie:<id> or series:<id>")
            kind, number = match.groups()
            normalized.add(f"{kind.lower()}:{int(number)}")
        return frozenset(normalized)

    @field_validator("titles")
    @classmethod
    def _check_titles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for text in value:
            TitleMatcher("substring", text)  # raises ValueError for an empty title
        return value

    @field_validator("regexes")
    @classmethod
    def _check_regexes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            TitleMatcher("regex", pattern)  # raises ValueError for an invalid regex
        return value

    def model_post_init(self, context: object) -> None:
        self._title_matchers = [TitleMatcher("substring", t) for t in self.titles]
        self._regex_matchers = [TitleMatcher("regex", r) for r in self.regexes]

    @property
    def active(self) -> bool:
        """Whether any exclusion rule is set."""
        return bool(self.ids or self.titles or self.regexes)

    def reason(self, candidate: Candidate) -> str | None:
        """The first rule that excludes this candidate, or None if it isn't excluded.

        Checked in order: IDs, then titles, then regexes.
        """
        key = f"{candidate.kind}:{candidate.tmdb_id}"
        if key in self.ids:
            return f"id {key}"
        for matcher in self._title_matchers:
            if matcher.matches(candidate):
                return f'title "{matcher.value}"'
        for matcher in self._regex_matchers:
            if matcher.matches(candidate):
                return f'regex "{matcher.value}"'
        return None
