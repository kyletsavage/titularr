"""TMDB (The Movie Database) as a title source.

Try a search against the real API (needs your TMDB "API Read Access Token"):

    uv run python -m titularr.sources.tmdb "christmas" [--adult exclude|include|only]

with the token in the TMDB_API_TOKEN environment variable.
"""

import argparse
import logging
import os
import sys
import time
from collections.abc import Iterator
from types import TracebackType
from typing import Any, Self, get_args

import httpx2
from pydantic import BaseModel, ValidationError

from titularr.models import AdultMode, Candidate

log = logging.getLogger(__name__)

BASE_URL = "https://api.themoviedb.org/3"
TIMEOUT_SECONDS = 10.0
# TMDB never serves more than 500 pages (20 results each), even when total_pages says more.
MAX_PAGES = 500
MAX_RETRIES = 3
DEFAULT_RETRY_AFTER_SECONDS = 1.0


class TmdbError(Exception):
    """TMDB returned an error, or a response we couldn't understand."""


class _MovieResult(BaseModel):
    # TMDB omits fields for sparse entries (e.g. no votes yet), so only id and title are required.
    id: int
    title: str
    original_title: str | None = None
    release_date: str | None = None  # "YYYY-MM-DD", or "" when unknown
    original_language: str | None = None
    vote_average: float | None = None
    vote_count: int | None = None
    adult: bool | None = None

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


class _SearchPage(BaseModel):
    page: int
    total_pages: int
    total_results: int
    # Validated one by one in search_movies, so a single bad entry is skipped, not fatal.
    results: list[dict[str, Any]]


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
    """Searches TMDB. Use as a context manager so the connection is closed."""

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
        """Yield every movie TMDB returns for `query`, fetching page after page.

        `adult` controls adult titles: "exclude" (default), "include", or "only".
        TMDB itself only supports include on/off, so "only" fetches everything and
        keeps the results TMDB marks as adult.
        """
        include_adult = adult != "exclude"
        page = 1
        while True:
            data = self._get_page("/search/movie", query, page, include_adult)
            if page == 1 and data.total_pages > MAX_PAGES:
                log.warning(
                    "TMDB reports %d pages for %r; only the first %d can be fetched",
                    data.total_pages,
                    query,
                    MAX_PAGES,
                )
            for raw in data.results:
                try:
                    result = _MovieResult.model_validate(raw)
                except ValidationError as e:
                    log.warning("skipping TMDB result without a usable id/title: %s", e)
                    continue
                if adult == "only" and result.adult is not True:
                    continue
                yield result.to_candidate()
            if page >= min(data.total_pages, MAX_PAGES):
                return
            page += 1

    def _get_page(self, path: str, query: str, page: int, include_adult: bool) -> _SearchPage:
        params = {"query": query, "page": page, "include_adult": str(include_adult).lower()}
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


class _WarningCounter(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m titularr.sources.tmdb", description="Search TMDB for movies."
    )
    parser.add_argument("query", help="text to search for")
    parser.add_argument(
        "--adult",
        choices=get_args(AdultMode),
        default="exclude",
        help="adult titles: exclude (default), include, or only",
    )
    args = parser.parse_args(argv)

    token = os.environ.get("TMDB_API_TOKEN")
    if not token:
        print("Set TMDB_API_TOKEN to your TMDB API Read Access Token.", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    # Titles can contain characters the terminal's encoding can't show (e.g. a Windows
    # console redirected to a file); replace them instead of crashing.
    sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]

    warnings = _WarningCounter()
    log.addHandler(warnings)
    count = 0
    status = 0
    try:
        with TmdbClient(token) as tmdb:
            for movie in tmdb.search_movies(args.query, adult=args.adult):
                year = movie.year if movie.year is not None else "?"
                votes = movie.vote_count if movie.vote_count is not None else "?"
                print(f"{movie.title} ({year})  tmdb:{movie.tmdb_id}  votes:{votes}")
                count += 1
    except (TmdbError, httpx2.HTTPError) as e:
        print(f"Error: {e}", file=sys.stderr)
        status = 1
    finally:
        log.removeHandler(warnings)
    # Printed even after an error, so a partial run still shows how far it got.
    print(f"{_plural(count, 'result')}, {_plural(warnings.count, 'warning')}")
    return status


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


if __name__ == "__main__":
    sys.exit(main())
