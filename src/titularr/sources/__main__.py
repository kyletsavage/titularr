"""Dev command: search the sources for movies and series and print the titles kept.

    uv run python -m titularr.sources "christmas" [--kind movie|series|both]
        [--match substring|word | --regex PATTERN] [--adult exclude|include|only]
        [--year-min YEAR] [--year-max YEAR] [--language CODE ...]
        [--min-rating N] [--min-votes N] [--keep-unknown] [--show-dropped]

The search text is always what TMDB searches for. Matching then keeps only titles that
contain it (substring, the default), contain it as a whole word (--match word), or
match --regex PATTERN instead. Filters then narrow the matching titles; see --help.

Needs your TMDB "API Read Access Token" in the TMDB_API_TOKEN environment variable.
Temporary: phase 3's real CLI replaces this.
"""

import argparse
import logging
import os
import sys
from typing import get_args

import httpx2
from pydantic import ValidationError

from titularr.filters import Filters
from titularr.matching import TitleMatcher
from titularr.models import AdultMode, Candidate
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
    filters = parser.add_argument_group("filters (applied to matching titles)")
    filters.add_argument("--year-min", type=int, metavar="YEAR", help="earliest year to keep")
    filters.add_argument("--year-max", type=int, metavar="YEAR", help="latest year to keep")
    languages = filters.add_mutually_exclusive_group()
    languages.add_argument(
        "--language",
        action="append",
        metavar="CODE",
        help="keep only this original language, e.g. ja (repeat for several; default: en)",
    )
    languages.add_argument(
        "--any-language",
        action="store_true",
        help="keep titles in any original language",
    )
    filters.add_argument("--min-rating", type=float, metavar="N", help="minimum TMDB rating, 0-10")
    filters.add_argument("--min-votes", type=int, metavar="N", help="minimum TMDB vote count")
    filters.add_argument(
        "--keep-unknown",
        action="store_true",
        help="keep titles whose value for a filter is unknown (dropped by default)",
    )
    parser.add_argument(
        "--show-dropped",
        action="store_true",
        help="also list titles that didn't match or were filtered out, with the reason",
    )
    args = parser.parse_args(argv)

    # Invalid input exits with status 2 here, before any request is made.
    try:
        if args.regex is not None:
            matcher = TitleMatcher("regex", args.regex)
        else:
            matcher = TitleMatcher(args.match, args.query)
    except ValueError as e:
        parser.error(str(e))
    settings = {
        "year_min": args.year_min,
        "year_max": args.year_max,
        "min_rating": args.min_rating,
        "min_votes": args.min_votes,
        "keep_unknown": args.keep_unknown,
    }
    # Leaving "languages" out keeps Filters' default (English only).
    if args.any_language:
        settings["languages"] = None
    elif args.language is not None:
        settings["languages"] = frozenset(args.language)
    try:
        keep = Filters(**settings)
    except ValidationError as e:
        parser.error(_describe(e))

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
    kept = dict.fromkeys(kinds, 0)
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
                    if not matcher.matches(c):
                        dropped = " (no match)"
                    elif reasons := keep.reasons(c):
                        matched[kind] += 1
                        dropped = f" (filtered: {'; '.join(reasons)})"
                    else:
                        matched[kind] += 1
                        kept[kind] += 1
                        dropped = ""
                    if not dropped or args.show_dropped:
                        print(_describe_candidate(c) + dropped)
    except (TmdbError, httpx2.HTTPError) as e:
        print(f"Error: {e}", file=sys.stderr)
        status = 1
    finally:
        package_log.removeHandler(warnings)

    # Printed even after an error, so a partial run still shows how far it got.
    parts = []
    for kind in kinds:
        one, many = _KIND_WORDS[kind]
        part = f"{_plural(found[kind], one, many)} found, {matched[kind]} matched"
        if keep.active:
            part += f", {kept[kind]} kept"
        parts.append(part)
    parts.append(_plural(warnings.count, "warning", "warnings"))
    print("; ".join(parts))
    return status


def _describe_candidate(c: Candidate) -> str:
    def shown(value: object) -> str:
        return "?" if value is None else str(value)

    rating = "?" if c.vote_average is None else f"{c.vote_average:.1f}"
    return (
        f"[{c.kind}] {c.title} ({shown(c.year)})  tmdb:{c.tmdb_id}  "
        f"lang:{shown(c.original_language)}  rating:{rating}  votes:{shown(c.vote_count)}"
    )


def _describe(error: ValidationError) -> str:
    """Pydantic's validation errors, reworded as one short line."""
    messages = []
    for err in error.errors():
        message = err["msg"].removeprefix("Value error, ")
        field = ".".join(str(part) for part in err["loc"])
        messages.append(f"{field}: {message}" if field else message)
    return "; ".join(messages)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


if __name__ == "__main__":
    sys.exit(main())
