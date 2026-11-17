"""Filters: narrow matched titles by year, original language, rating, votes and runtime.

Every check that fails produces a short, human-readable reason, so a run can always
explain why a title was dropped.

Runtime isn't in search results, so it's checked separately (`runtime_reasons`), after
a lookup, and only for titles that pass everything else.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from titularr.models import Candidate

DEFAULT_LANGUAGES = frozenset({"en"})


class Filters(BaseModel):
    """Which titles to keep.

    By default only English-language titles are kept (`languages={"en"}`); pass
    `languages=None` (or "any") for any language. Every other filter is off unless set.

    When a filter is set but a title's value is unknown (e.g. TMDB has no vote count),
    the title is dropped unless `keep_unknown` is set.
    """

    # extra="forbid": a typo like "min_vote" in a config file is an error, not ignored.
    model_config = ConfigDict(frozen=True, extra="forbid")

    year_min: int | None = None
    year_max: int | None = None
    # TMDB original-language codes, e.g. "en", "ja"; None means any language. Not checked
    # against ISO 639-1, because TMDB uses a few non-standard codes (such as "cn").
    languages: frozenset[str] | None = DEFAULT_LANGUAGES
    min_rating: float | None = Field(default=None, ge=0, le=10)
    min_votes: int | None = Field(default=None, ge=0)
    # In minutes. Movies only: each check costs one TMDB request per title.
    runtime_min: int | None = Field(default=None, ge=1)
    runtime_max: int | None = Field(default=None, ge=1)
    keep_unknown: bool = False

    @field_validator("languages", mode="before")
    @classmethod
    def _any_language(cls, value: object) -> object:
        # "languages: any" reads better in a config file than "languages: null".
        if isinstance(value, str) and value.strip().lower() == "any":
            return None
        return value

    @field_validator("languages")
    @classmethod
    def _normalize_languages(cls, value: frozenset[str] | None) -> frozenset[str] | None:
        if value is None:
            return None
        codes = frozenset(code.strip().lower() for code in value)
        if "" in codes:
            raise ValueError("language codes must not be empty")
        return codes

    @model_validator(mode="after")
    def _check_year_range(self) -> "Filters":
        if (
            self.year_min is not None
            and self.year_max is not None
            and self.year_min > self.year_max
        ):
            raise ValueError(f"year_min ({self.year_min}) is after year_max ({self.year_max})")
        if (
            self.runtime_min is not None
            and self.runtime_max is not None
            and self.runtime_min > self.runtime_max
        ):
            raise ValueError(
                f"runtime_min ({self.runtime_min}) is more than runtime_max ({self.runtime_max})"
            )
        return self

    @property
    def active(self) -> bool:
        """Whether any filter is set."""
        return self.needs_runtime or any(
            v is not None
            for v in (self.year_min, self.year_max, self.languages, self.min_rating, self.min_votes)
        )

    @property
    def needs_runtime(self) -> bool:
        """Whether a runtime filter is set, so each title's runtime must be looked up."""
        return self.runtime_min is not None or self.runtime_max is not None

    def reasons(self, candidate: Candidate) -> list[str]:
        """Every reason this candidate fails the filters; an empty list means it passes."""
        reasons: list[str] = []
        year = candidate.year

        if self.year_min is not None or self.year_max is not None:
            if year is None:
                if not self.keep_unknown:
                    reasons.append("year unknown")
            elif self.year_min is not None and year < self.year_min:
                reasons.append(f"year {year} < {self.year_min}")
            elif self.year_max is not None and year > self.year_max:
                reasons.append(f"year {year} > {self.year_max}")

        if self.languages is not None:
            language = candidate.original_language
            if language is None:
                if not self.keep_unknown:
                    reasons.append("language unknown")
            elif language.lower() not in self.languages:
                allowed = ", ".join(sorted(self.languages))
                reasons.append(f"language {language} not in {allowed}")

        if self.min_rating is not None:
            rating = candidate.vote_average
            if rating is None:
                if not self.keep_unknown:
                    reasons.append("rating unknown")
            elif rating < self.min_rating:
                reasons.append(f"rating {rating:.1f} < {self.min_rating:.1f}")

        if self.min_votes is not None:
            votes = candidate.vote_count
            if votes is None:
                if not self.keep_unknown:
                    reasons.append("votes unknown")
            elif votes < self.min_votes:
                reasons.append(f"votes {votes} < {self.min_votes}")

        return reasons

    def runtime_reasons(self, runtime: int | None) -> list[str]:
        """Why a title with this runtime (minutes; None or 0 if unknown) fails the
        runtime filters; an empty list means it passes."""
        if not self.needs_runtime:
            return []
        if not runtime:  # TMDB gives 0 when it has no runtime
            return [] if self.keep_unknown else ["runtime unknown"]
        if self.runtime_min is not None and runtime < self.runtime_min:
            return [f"runtime {runtime} min < {self.runtime_min}"]
        if self.runtime_max is not None and runtime > self.runtime_max:
            return [f"runtime {runtime} min > {self.runtime_max}"]
        return []
