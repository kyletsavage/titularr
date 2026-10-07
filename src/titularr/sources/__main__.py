"""Dev command: search the sources for movies and series and print the matching titles.

    uv run python -m titularr.sources "christmas" [--kind movie|series|both]
        [--match substring|word | --regex PATTERN] [--adult exclude|include|only]
        [--show-unmatched]

The search text is always what TMDB searches for. Matching then keeps only titles that
contain it (substring, the default), contain it as a whole word (--match word), or
match --regex PATTERN instead.

Needs your TMDB "API Read Access Token" in the TMDB_API_TOKEN environment variable.
Temporary: phase 3's real CLI replaces this.
"""

import argparse
import logging
import os
import sys
from typing import get_args

import httpx2

from titularr.matching import TitleMatcher
from titularr.models import AdultMode
from titularr.sources.tmdb import TmdbClient, TmdbError

_KIND_WORDS = {"movie": ("movie", "movies"), "series": ("series", "series")}


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
    parser.add_argument("query", help="text to search TMDB for (always required)")
    parser.add_argument(
        "--kind",
        choices=["movie", "series", "both"],
        default="both",
        help="what to search for (default: both)",
    )
    how = parser.add_mutually_exclusive_group()
    how.add_argument(
        "--match",
        choices=["substring", "word"],
        default="substring",
        help="how the search text must appear in a title (default: substring)",
    )
    how.add_argument(
        "--regex",
        metavar="PATTERN",
        help="match titles against this regular expression instead (case-insensitive)",
    )
    parser.add_argument(
        "--adult",
        choices=get_args(AdultMode),
        default="exclude",
        help="adult titles: exclude (default), include, or only",
    )
    parser.add_argument(
        "--show-unmatched",
        action="store_true",
        help="also list titles TMDB returned that didn't match",
    )
    args = parser.parse_args(argv)

    try:
        if args.regex is not None:
            matcher = TitleMatcher("regex", args.regex)
        else:
            matcher = TitleMatcher(args.match, args.query)
    except ValueError as e:
        parser.error(str(e))  # exits with status 2, before any request is made

    token = os.environ.get("TMDB_API_TOKEN")
    if not token:
        print("Set TMDB_API_TOKEN to your TMDB API Read Access Token.", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    # Titles can contain characters the terminal's encoding can't show (e.g. a Windows
    # console redirected to a file); replace them instead of crashing.
    sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]

    kinds = ["movie", "series"] if args.kind == "both" else [args.kind]
    found = dict.fromkeys(kinds, 0)
    matched = dict.fromkeys(kinds, 0)
    warnings = _WarningCounter()
    package_log = logging.getLogger("titularr")
    package_log.addHandler(warnings)
    status = 0
    try:
        with TmdbClient(token) as tmdb:
            search = {"movie": tmdb.search_movies, "series": tmdb.search_series}
            for kind in kinds:
                for c in search[kind](args.query, adult=args.adult):
                    found[kind] += 1
                    is_match = matcher.matches(c)
                    if is_match:
                        matched[kind] += 1
                    if is_match or args.show_unmatched:
                        year = c.year if c.year is not None else "?"
                        votes = c.vote_count if c.vote_count is not None else "?"
                        suffix = "" if is_match else " (no match)"
                        print(
                            f"[{c.kind}] {c.title} ({year})  tmdb:{c.tmdb_id}  "
                            f"votes:{votes}{suffix}"
                        )
    except (TmdbError, httpx2.HTTPError) as e:
        print(f"Error: {e}", file=sys.stderr)
        status = 1
    finally:
        package_log.removeHandler(warnings)

    # Printed even after an error, so a partial run still shows how far it got.
    parts = []
    for kind in kinds:
        one, many = _KIND_WORDS[kind]
        parts.append(f"{_plural(found[kind], one, many)} found, {matched[kind]} matched")
    parts.append(_plural(warnings.count, "warning", "warnings"))
    print("; ".join(parts))
    return status


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


if __name__ == "__main__":
    sys.exit(main())
