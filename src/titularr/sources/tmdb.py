"""TMDB (The Movie Database) as a title source, for both movies and series.

To try it against the real API, see `titularr search --help`.
"""

import datetime
import logging
import time
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

import httpx2
from pydantic import BaseModel, ValidationError

from titularr.models import AdultMode, Candidate

log = logging.getLogger(__name__)

BASE_URL = "https://api.themoviedb.org/3"
TIMEOUT_SECONDS = 10.0
# TMDB never serves more than 500 pages (20 results each), even when total_pages says more.
MAX_PAGES = 500
RESULTS_PER_PAGE = 20
# TMDB's paging is unreliable for large result sets: some titles come back on two pages
# and others on none, the same way every time. Small result sets page correctly, so when
# a search comes back short it is repeated one release year at a time.
FIRST_YEAR = 1874  # the oldest surviving film footage
YEARS_AHEAD = 3  # announced titles can be dated a few years out
_YEAR_PARAMS = {"/search/movie": "primary_release_year", "/search/tv": "first_air_date_year"}
MAX_RETRIES = 3
DEFAULT_RETRY_AFTER_SECONDS = 1.0


class TmdbError(Exception):
    """TMDB returned an error, or a response we couldn't understand."""


class _Result(BaseModel):
    """Fields shared by movie and series results. Only `id` (and the name) are required,
    because TMDB omits fields for sparse entries (e.g. no votes yet)."""

    id: int
    original_language: str | None = None
    vote_average: float | None = None
    vote_count: int | None = None
    adult: bool | None = None

    def to_candidate(self) -> Candidate:
        raise NotImplementedError


class _MovieResult(_Result):
    title: str
    original_title: str | None = None
    release_date: str | None = None  # "YYYY-MM-DD", or "" when unknown

    def to_candidate(self) -> Candidate:
        return Candidate(
            source="tmdb",
            kind="movie",
            tmdb_id=self.id,
            title=self.title,
            original_title=self.original_title,
            year=_year(self.release_date),
            original_language=self.original_language,
            vote_average=self.vote_average,
            vote_count=self.vote_count,
            adult=self.adult,
        )


class _SeriesResult(_Result):
    # TMDB calls these name/original_name/first_air_date for TV, where movies use
    # title/original_title/release_date.
    name: str
    original_name: str | None = None
    first_air_date: str | None = None  # "YYYY-MM-DD", or "" when unknown

    def to_candidate(self) -> Candidate:
        return Candidate(
            source="tmdb",
            kind="series",
            tmdb_id=self.id,
            title=self.name,
            original_title=self.original_name,
            year=_year(self.first_air_date),
            original_language=self.original_language,
            vote_average=self.vote_average,
            vote_count=self.vote_count,
            adult=self.adult,
        )


class _SearchPage(BaseModel):
    page: int
    total_pages: int
    total_results: int
    # Validated one by one in _pages, so a single bad entry is skipped, not fatal.
    results: list[dict[str, Any]]


@dataclass
class _SearchState:
    """What one search has returned so far, across its normal and year-by-year parts.

    Every raw result counts, including ones skipped later (no title, not adult in
    "only" mode), because TMDB's total_results counts them too.
    """

    seen_ids: set[int] = field(default_factory=set)
    id_less: int = 0  # entries without a usable id can't be de-duplicated
    cap_warned: bool = False

    @property
    def returned(self) -> int:
        return len(self.seen_ids) + self.id_less


def _years() -> range:
    """Every release year a year-by-year search covers."""
    this_year = datetime.datetime.now(datetime.UTC).year  # UTC: same answer on any machine
    return range(FIRST_YEAR, this_year + YEARS_AHEAD + 1)


def _year(date: str | None) -> int | None:
    if date and len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


def _retry_after(response: httpx2.Response) -> float:
    try:
        return max(0.0, float(response.headers["Retry-After"]))
    except (KeyError, ValueError):
        return DEFAULT_RETRY_AFTER_SECONDS


def _error_message(response: httpx2.Response) -> str:
    try:
        message = response.json().get("status_message")
    except (ValueError, AttributeError):
        message = None
    return f"TMDB returned HTTP {response.status_code}" + (f": {message}" if message else "")


class TmdbClient:
    """Searches TMDB. Use as a context manager so the connection is closed.

    For every search, `adult` controls adult titles: "exclude" (default), "include", or
    "only". TMDB itself only supports include on/off, so "only" fetches everything and
    keeps the results TMDB marks as adult.
    """

    def __init__(self, token: str, *, transport: httpx2.BaseTransport | None = None) -> None:
        self._client = httpx2.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def search_movies(self, query: str, *, adult: AdultMode = "exclude") -> Iterator[Candidate]:
        """Yield every movie TMDB returns for `query`, fetching page after page."""
        return self._search("/search/movie", _MovieResult, query, adult)

    def search_series(self, query: str, *, adult: AdultMode = "exclude") -> Iterator[Candidate]:
        """Yield every TV series TMDB returns for `query`, fetching page after page."""
        return self._search("/search/tv", _SeriesResult, query, adult)

    def _search(
        self, path: str, result_model: type[_Result], query: str, adult: AdultMode
    ) -> Iterator[Candidate]:
        state = _SearchState()
        target = yield from self._pages(path, result_model, query, adult, state)
        if state.returned >= target:
            return

        # TMDB's paging dropped some titles. Small result sets page correctly, so search
        # again one release year at a time; only titles not seen yet are yielded.
        log.info(
            "TMDB returned %d of %d results for %r at %s; searching year by year",
            state.returned,
            target,
            query,
            path,
        )
        before = state.returned
        year_param = _YEAR_PARAMS[path]
        for year in _years():
            yield from self._pages(path, result_model, query, adult, state, {year_param: year})
        log.info("year-by-year search found %d more", state.returned - before)

        if state.returned < target:
            log.warning(
                "expected %d results for %r at %s but only %d were returned, even year by "
                "year; %d may be missing",
                target,
                query,
                path,
                state.returned,
                target - state.returned,
            )

    def _pages(
        self,
        path: str,
        result_model: type[_Result],
        query: str,
        adult: AdultMode,
        state: _SearchState,
        extra: dict[str, Any] | None = None,
    ) -> Generator[Candidate, None, int]:
        """Yield the new titles from every page of one search; return how many TMDB says
        exist (limited to what its page cap can serve)."""
        include_adult = adult != "exclude"
        year_split = bool(extra)
        page = 1
        while True:
            data = self._get_page(path, query, page, include_adult, extra)
            if data.total_pages > MAX_PAGES and not state.cap_warned:
                log.warning(
                    "TMDB reports %d pages for %r at %s; only the first %d can be fetched",
                    data.total_pages,
                    query,
                    path,
                    MAX_PAGES,
                )
                state.cap_warned = True
            for raw in data.results:
                raw_id = raw.get("id")
                if isinstance(raw_id, int) and not isinstance(raw_id, bool):
                    if raw_id in state.seen_ids:
                        continue  # TMDB repeats titles across pages
                    state.seen_ids.add(raw_id)
                elif year_split:
                    continue  # already counted and warned about in the normal search
                else:
                    state.id_less += 1
                try:
                    result = result_model.model_validate(raw)
                except ValidationError as e:
                    log.warning("skipping TMDB result without a usable id/title: %s", e)
                    continue
                if adult == "only" and result.adult is not True:
                    continue
                yield result.to_candidate()
            if page >= min(data.total_pages, MAX_PAGES):
                return min(data.total_results, MAX_PAGES * RESULTS_PER_PAGE)
            page += 1

    def _get_page(
        self,
        path: str,
        query: str,
        page: int,
        include_adult: bool,
        extra: dict[str, Any] | None = None,
    ) -> _SearchPage:
        params = {
            "query": query,
            "page": page,
            "include_adult": str(include_adult).lower(),
            **(extra or {}),
        }
        try:
            return _SearchPage.model_validate(self._get(path, params))
        except ValidationError as e:
            raise TmdbError(f"unexpected response from TMDB {path}: {e}") from e

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        for attempt in range(MAX_RETRIES + 1):
            response = self._client.get(path, params=params)
            if response.status_code != 429 or attempt == MAX_RETRIES:
                break
            delay = _retry_after(response)
            log.info("TMDB rate limit hit; retrying in %.1fs", delay)
            time.sleep(delay)
        if response.is_error:
            raise TmdbError(_error_message(response))
        return response.json()
