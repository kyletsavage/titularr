"""Dev command: search the sources for movies and series and print every result.

    uv run python -m titularr.sources "christmas" [--kind movie|series|both]
                                                  [--adult exclude|include|only]

Needs your TMDB "API Read Access Token" in the TMDB_API_TOKEN environment variable.
Temporary: phase 3's real CLI replaces this.
"""

import argparse
import logging
import os
import sys
from typing import get_args

import httpx2

from titularr.models import AdultMode
from titularr.sources.tmdb import TmdbClient, TmdbError


class _WarningCounter(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m titularr.sources", description="Search TMDB for movies and series."
    )
    parser.add_argument("query", help="text to search for")
    parser.add_argument(
        "--kind",
        choices=["movie", "series", "both"],
        default="both",
        help="what to search for (default: both)",
    )
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
    package_log = logging.getLogger("titularr")
    package_log.addHandler(warnings)
    counts = {"movie": 0, "series": 0}
    status = 0
    try:
        with TmdbClient(token) as tmdb:
            searches = []
            if args.kind in ("movie", "both"):
                searches.append(tmdb.search_movies(args.query, adult=args.adult))
            if args.kind in ("series", "both"):
                searches.append(tmdb.search_series(args.query, adult=args.adult))
            for results in searches:
                for c in results:
                    year = c.year if c.year is not None else "?"
                    votes = c.vote_count if c.vote_count is not None else "?"
                    print(f"[{c.kind}] {c.title} ({year})  tmdb:{c.tmdb_id}  votes:{votes}")
                    counts[c.kind] += 1
    except (TmdbError, httpx2.HTTPError) as e:
        print(f"Error: {e}", file=sys.stderr)
        status = 1
    finally:
        package_log.removeHandler(warnings)

    # Printed even after an error, so a partial run still shows how far it got.
    summary = []
    if args.kind in ("movie", "both"):
        summary.append(_plural(counts["movie"], "movie", "movies"))
    if args.kind in ("series", "both"):
        summary.append(_plural(counts["series"], "series", "series"))
    summary.append(_plural(warnings.count, "warning", "warnings"))
    print(", ".join(summary))
    return status


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


if __name__ == "__main__":
    sys.exit(main())
