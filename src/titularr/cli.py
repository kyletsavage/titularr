"""The `titularr` command: search TMDB and preview what each query would keep.

Every command is a preview: nothing is added anywhere. Adding to Radarr/Sonarr comes
later and will need an explicit --apply.
"""

import logging
import os
import sys
from pathlib import Path
from typing import Annotated, Literal, NoReturn

import httpx2
import typer
from pydantic import ValidationError

from titularr.config import Config, ConfigError, load_config, resolve_config_path
from titularr.exclusions import Exclusions
from titularr.filters import Filters
from titularr.models import AdultMode, Candidate, Decision
from titularr.pipeline import Query, Summary, run
from titularr.sources.tmdb import TmdbClient, TmdbError

app = typer.Typer(
    help="Find every movie and series whose title matches your keywords (preview only).",
    no_args_is_help=True,
    add_completion=False,
)

_KIND_WORDS = {"movie": ("movie", "movies"), "series": ("series", "series")}

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        help="Config file. Default: $TITULARR_CONFIG, then ./config.yaml.",
        show_default=False,
    ),
]
ShowDroppedOption = Annotated[
    bool,
    typer.Option(
        "--show-dropped",
        help="Also list titles that didn't match, were excluded or were filtered out.",
    ),
]


@app.command()
def search(
    text: Annotated[str, typer.Argument(help="Text to search TMDB for.")],
    kind: Annotated[
        Literal["movie", "series", "both"], typer.Option(help="What to search for.")
    ] = "both",
    match: Annotated[
        Literal["substring", "word"],
        typer.Option(help="How the search text must appear in a title."),
    ] = "substring",
    regex: Annotated[
        str | None,
        typer.Option(
            metavar="PATTERN", help="Match titles against this regex instead (case-insensitive)."
        ),
    ] = None,
    adult: Annotated[AdultMode, typer.Option(help="Adult titles.")] = "exclude",
    year_min: Annotated[
        int | None, typer.Option(help="Earliest year to keep.", rich_help_panel="Filters")
    ] = None,
    year_max: Annotated[
        int | None, typer.Option(help="Latest year to keep.", rich_help_panel="Filters")
    ] = None,
    language: Annotated[
        list[str] | None,
        typer.Option(
            metavar="CODE",
            help="Keep only this original language, e.g. ja. Repeatable. Default: en.",
            rich_help_panel="Filters",
        ),
    ] = None,
    any_language: Annotated[
        bool,
        typer.Option(help="Keep titles in any original language.", rich_help_panel="Filters"),
    ] = False,
    min_rating: Annotated[
        float | None, typer.Option(help="Minimum TMDB rating, 0-10.", rich_help_panel="Filters")
    ] = None,
    min_votes: Annotated[
        int | None, typer.Option(help="Minimum TMDB vote count.", rich_help_panel="Filters")
    ] = None,
    keep_unknown: Annotated[
        bool,
        typer.Option(
            help="Keep titles whose value for a filter is unknown (dropped by default).",
            rich_help_panel="Filters",
        ),
    ] = False,
    exclude_id: Annotated[
        list[str] | None,
        typer.Option(
            metavar="KIND:ID",
            help="Never keep this TMDB ID, e.g. movie:1399. Repeatable.",
            rich_help_panel="Exclusions",
        ),
    ] = None,
    exclude_title: Annotated[
        list[str] | None,
        typer.Option(
            metavar="TEXT",
            help="Never keep titles containing this text. Repeatable.",
            rich_help_panel="Exclusions",
        ),
    ] = None,
    exclude_regex: Annotated[
        list[str] | None,
        typer.Option(
            metavar="PATTERN",
            help="Never keep titles matching this regex. Repeatable.",
            rich_help_panel="Exclusions",
        ),
    ] = None,
    config: ConfigOption = None,
    show_dropped: ShowDroppedOption = False,
) -> None:
    """Search TMDB once, with the rules given as options.

    Uses the config file's token and global exclusions when a config file exists;
    otherwise the TMDB_API_TOKEN environment variable.
    """
    # Typer has no mutually exclusive options, so these pairs are checked here.
    if regex is not None and match != "substring":
        _usage_error("--match and --regex can't be combined")
    if language and any_language:
        _usage_error("--language and --any-language can't be combined")

    loaded = _load_config(config, required=False)

    filter_settings: dict[str, object] = {
        "year_min": year_min,
        "year_max": year_max,
        "min_rating": min_rating,
        "min_votes": min_votes,
        "keep_unknown": keep_unknown,
    }
    # Leaving "languages" out keeps Filters' default (English only).
    if any_language:
        filter_settings["languages"] = None
    elif language:
        filter_settings["languages"] = frozenset(language)
    try:
        query = Query(
            text=text,
            kinds=("movie", "series") if kind == "both" else (kind,),
            match="regex" if regex is not None else match,
            pattern=regex,
            adult=adult,
            filters=Filters(**filter_settings),
            exclusions=Exclusions(
                ids=frozenset(exclude_id or ()),
                titles=tuple(exclude_title or ()),
                regexes=tuple(exclude_regex or ()),
            ),
            # The config's exclusions apply to every search, ad-hoc ones included.
            global_exclusions=loaded.exclusions if loaded else Exclusions(),
        )
    except ValidationError as e:
        _usage_error(_describe(e))

    token = loaded.sources.tmdb.token if loaded else os.environ.get("TMDB_API_TOKEN")
    if not token:
        _usage_error("set TMDB_API_TOKEN to your TMDB API Read Access Token, or use a config file")
    _preview(query, token, show_dropped)


@app.command(name="run")
def run_saved(
    name: Annotated[str, typer.Argument(help="Name of a query saved in the config file.")],
    config: ConfigOption = None,
    show_dropped: ShowDroppedOption = False,
) -> None:
    """Run a query saved in the config file."""
    loaded = _load_config(config, required=True)
    assert loaded is not None
    try:
        query = loaded.query(name)
    except ConfigError as e:
        _usage_error(str(e))
    _preview(query, loaded.sources.tmdb.token, show_dropped)


@app.command()
def validate(config: ConfigOption = None) -> None:
    """Check the config file without contacting TMDB."""
    path = resolve_config_path(config)
    loaded = _load_config(path, required=True)
    assert loaded is not None
    names = sorted(loaded.queries)
    rules = len(loaded.exclusions.ids) + len(loaded.exclusions.titles)
    rules += len(loaded.exclusions.regexes)
    listed = f" ({', '.join(names)})" if names else ""
    print(
        f"{path} is valid: {_plural(len(names), 'saved query', 'saved queries')}{listed}, "
        f"{_plural(rules, 'global exclusion rule', 'global exclusion rules')}"
    )


def _load_config(explicit: Path | None, *, required: bool) -> Config | None:
    """Load the config. If it isn't required, a missing default config file is fine."""
    path = resolve_config_path(explicit)
    if not required and explicit is None and not path.exists():
        return None
    try:
        return load_config(path)
    except ConfigError as e:
        _usage_error(str(e))


def _preview(query: Query, token: str, show_dropped: bool) -> None:
    """Run the query, print the kept (and optionally dropped) titles, then a summary."""
    # Titles can contain characters the terminal's encoding can't show (e.g. a Windows
    # console redirected to a file); replace them instead of crashing.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")

    summary = Summary(kinds=query.kinds)
    # Handlers are attached for this run only (not the global root logger), so they
    # always write to the current stderr and never pile up between runs.
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    warnings = _WarningCounter()
    package_log = logging.getLogger("titularr")
    package_log.addHandler(console)
    package_log.addHandler(warnings)
    status = 0
    try:
        with TmdbClient(token) as tmdb:
            for decision in run(query, tmdb):
                summary.add(decision)
                if decision.status == "kept" or show_dropped:
                    print(_describe_candidate(decision.candidate) + _why_dropped(decision))
    except (TmdbError, httpx2.HTTPError) as e:
        print(f"Error: {e}", file=sys.stderr)
        status = 1
    finally:
        package_log.removeHandler(warnings)
        package_log.removeHandler(console)

    # Printed even after an error, so a partial run still shows how far it got.
    excludes = any(layer.active for _, layer in query.exclusion_layers())
    parts = []
    for kind, counts in summary.counts.items():
        one, many = _KIND_WORDS[kind]
        part = f"{_plural(counts.found, one, many)} found, {counts.matched} matched"
        if excludes:
            part += f", {counts.excluded} excluded"
        if query.filters.active or excludes:
            part += f", {counts.kept} kept"
        parts.append(part)
    parts.append(_plural(warnings.count, "warning", "warnings"))
    print("; ".join(parts))
    if status:
        raise typer.Exit(status)


class _WarningCounter(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1


def _usage_error(message: str) -> NoReturn:
    """Report a problem with the input or config (exit status 2), before any request."""
    print(f"Error: {message}", file=sys.stderr)
    raise typer.Exit(2)


def _why_dropped(decision: Decision) -> str:
    if decision.status == "no_match":
        return " (no match)"
    if decision.status == "excluded":
        return f" (excluded: {decision.reasons[0]})"
    if decision.status == "filtered":
        return f" (filtered: {'; '.join(decision.reasons)})"
    return ""


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
