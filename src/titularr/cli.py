"""The `titularr` command: search TMDB and preview what each query would keep.

Every command is a preview unless given --apply: then the kept movies are added to
Radarr and the kept series to Sonarr, after asking first (--yes skips asking).
"""

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, NoReturn

import httpx2
import typer
from pydantic import ValidationError

from titularr.arr import (
    AddResult,
    ArrError,
    RadarrAdd,
    RadarrAddOptions,
    RadarrClient,
    SonarrAdd,
    SonarrAddOptions,
    SonarrClient,
)
from titularr.config import ArrSettings, Config, ConfigError, load_config, resolve_config_path
from titularr.exclusions import Exclusions
from titularr.filters import Filters
from titularr.models import AdultMode, Candidate, Decision, Kind
from titularr.pipeline import Library, Query, Summary, run
from titularr.sources.tmdb import TmdbClient, TmdbError

app = typer.Typer(
    help="Find every movie and series whose title matches your keywords, and add them to "
    "Radarr and Sonarr with --apply.",
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
ApplyOption = Annotated[
    bool,
    typer.Option(
        "--apply",
        help="Add the kept titles to Radarr and Sonarr, after asking. Without it, nothing "
        "is changed.",
    ),
]
YesOption = Annotated[bool, typer.Option("--yes", help="With --apply: add without asking first.")]


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
    apply: ApplyOption = False,
    yes: YesOption = False,
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
    _preview(query, token, show_dropped, loaded, apply=apply, yes=yes)


@app.command(name="run")
def run_saved(
    name: Annotated[str, typer.Argument(help="Name of a query saved in the config file.")],
    config: ConfigOption = None,
    show_dropped: ShowDroppedOption = False,
    apply: ApplyOption = False,
    yes: YesOption = False,
) -> None:
    """Run a query saved in the config file."""
    loaded = _load_config(config, required=True)
    assert loaded is not None
    try:
        query = loaded.query(name)
    except ConfigError as e:
        _usage_error(str(e))
    _preview(
        query,
        loaded.sources.tmdb.token,
        show_dropped,
        loaded,
        apply=apply,
        yes=yes,
        query_name=name,
    )


@app.command()
def validate(config: ConfigOption = None) -> None:
    """Check the config file without contacting TMDB, Radarr or Sonarr."""
    path = resolve_config_path(config)
    loaded = _load_config(path, required=True)
    assert loaded is not None
    for warning in loaded.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    names = sorted(loaded.queries)
    rules = len(loaded.exclusions.ids) + len(loaded.exclusions.titles)
    rules += len(loaded.exclusions.regexes)
    listed = f" ({', '.join(names)})" if names else ""
    apps = [name for name, s in (("Radarr", loaded.radarr), ("Sonarr", loaded.sonarr)) if s]
    print(
        f"{path} is valid: {_plural(len(names), 'saved query', 'saved queries')}{listed}, "
        f"{_plural(rules, 'global exclusion rule', 'global exclusion rules')}, "
        f"library check: {', '.join(apps) or 'none'}"
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


def _preview(
    query: Query,
    token: str,
    show_dropped: bool,
    config: Config | None,
    *,
    apply: bool = False,
    yes: bool = False,
    query_name: str | None = None,
) -> None:
    """Run the query, print the kept (and optionally dropped) titles, then a summary.

    With `apply`, then add the kept titles to Radarr and Sonarr (asking first, unless
    `yes`). `query_name` is the saved query being run, whose add settings apply.
    """
    targets: list[_Target] = []
    if apply:
        if not yes and not _can_prompt():
            _usage_error("--apply asks before adding, but nobody can answer here; add --yes")
        targets = [_target(kind, config, query_name) for kind in query.kinds]

    # Titles can contain characters the terminal's encoding can't show (e.g. a Windows
    # console redirected to a file); replace them instead of crashing.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")

    summary = Summary(kinds=query.kinds)
    kept: list[Candidate] = []
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
    library = Library()
    try:
        # Fetched once, before searching, so a bad URL or API key fails fast.
        library = _load_library(query, config)
        for target in targets:
            # Checked before searching too, so a wrong profile or folder fails fast.
            target.prepare()
        with TmdbClient(token) as tmdb:
            for decision in run(query, tmdb, library):
                summary.add(decision)
                if decision.status == "kept":
                    kept.append(decision.candidate)
                if decision.status == "kept" or show_dropped:
                    print(_describe_candidate(decision.candidate) + _why_dropped(decision))
    except ArrError as e:
        # Before any TMDB request, so there's no summary to show.
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(1) from e
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
        if query.filters.active or excludes or library.checks(kind):
            part += f", {counts.kept} kept"
        if library.checks(kind):
            part += f", {counts.in_library} in library"
        parts.append(part)
    parts.append(_plural(warnings.count, "warning", "warnings"))
    print("; ".join(parts))
    if status:
        if apply:
            print("Nothing was added, because the search didn't finish.", file=sys.stderr)
        raise typer.Exit(status)
    if apply:
        _apply(kept, targets, yes)


@dataclass
class _Target:
    """Where kept titles of one kind are added: Radarr for movies, Sonarr for series."""

    kind: Kind
    connection: ArrSettings
    add: RadarrAdd | SonarrAdd
    # Set by prepare(), once the settings are checked against the app.
    options: RadarrAddOptions | SonarrAddOptions | None = None

    @property
    def app(self) -> str:
        return "Radarr" if self.kind == "movie" else "Sonarr"

    def client(self) -> RadarrClient | SonarrClient:
        client = RadarrClient if self.kind == "movie" else SonarrClient
        return client(self.connection.url, self.connection.api_key)

    # The client, settings and options always belong to the same app: all come from `kind`.

    def prepare(self) -> None:
        with self.client() as client:
            self.options = client.prepare(self.add)

    def add_one(self, client: RadarrClient | SonarrClient, tmdb_id: int) -> AddResult:
        if self.kind == "movie":
            return client.add_movie(tmdb_id, self.options)
        return client.add_series(tmdb_id, self.options)


def _target(kind: Kind, config: Config | None, query_name: str | None) -> _Target:
    """Where --apply adds titles of this kind. Missing settings are usage errors."""
    section = "radarr" if kind == "movie" else "sonarr"
    connection = getattr(config, section) if config else None
    add: RadarrAdd | SonarrAdd | None = None
    if config is not None:
        add = config.radarr_add(query_name) if kind == "movie" else config.sonarr_add(query_name)
    if connection is None or add is None:
        _usage_error(
            f"--apply needs a usable {section} section in the config to add {_KIND_WORDS[kind][1]}"
        )
    missing = [key for key in ("quality_profile", "root_folder") if not getattr(add, key)]
    if missing:
        needed = " and ".join(f"{section}.{key}" for key in missing)
        _usage_error(f"--apply needs {needed} in the config")
    return _Target(kind, connection, add)


def _apply(kept: list[Candidate], targets: list[_Target], yes: bool) -> None:
    """Add the kept titles, after asking unless `yes`. One line per title, then a
    summary per app."""
    batches = [(t, [c for c in kept if c.kind == t.kind]) for t in targets]
    batches = [(target, titles) for target, titles in batches if titles]
    if not batches:
        print("Nothing to add.")
        return

    question = " and ".join(
        f"{_plural(len(titles), *_KIND_WORDS[target.kind])} to {target.app}"
        # Tags change which release profiles apply, so they're named before adding.
        + (f" (tags: {', '.join(target.add.tags)})" if target.add.tags else "")
        for target, titles in batches
    )
    if not yes and not typer.confirm(f"Add {question}?", err=True):
        print("Nothing added.")
        return

    summaries = []
    any_failed = False
    for target, titles in batches:
        counts = dict.fromkeys(("added", "exists", "not_found", "failed"), 0)
        with target.client() as client:
            for title in titles:
                described = _describe_candidate(title)
                try:
                    result = target.add_one(client, title.tmdb_id)
                except ArrError as e:
                    # One title failing doesn't stop the rest.
                    counts["failed"] += 1
                    print(f"failed: {described}: {e}")
                    continue
                counts[result] += 1
                if result == "added":
                    print(f"added: {described}")
                elif result == "exists":
                    print(f"already in {target.app}: {described}")
                else:
                    print(f"not found in {target.app}: {described}")
        summary = f"{target.app}: {counts['added']} added, {counts['exists']} already there"
        # Only Sonarr can fail to find a title (a series it has no TVDB entry for).
        if target.kind == "series":
            summary += f", {counts['not_found']} not found"
        summaries.append(summary + f", {counts['failed']} failed")
        any_failed = any_failed or counts["failed"] > 0
    for summary in summaries:
        print(summary)
    if any_failed:
        raise typer.Exit(1)


def _can_prompt() -> bool:
    """Whether someone can answer a question (stdin is a terminal, not a script or pipe)."""
    return sys.stdin.isatty()


def _load_library(query: Query, config: Config | None) -> Library:
    """What's already in Radarr/Sonarr, for the kinds this query searches."""
    if config is None:
        return Library()
    log = logging.getLogger("titularr")
    for warning in config.warnings:
        # Only mention an app whose kind this query actually searches.
        app_kind = "movie" if warning.startswith("radarr") else "series"
        if app_kind in query.kinds:
            log.warning(warning)

    movie_ids = series_ids = None
    if "movie" in query.kinds and config.radarr:
        with RadarrClient(config.radarr.url, config.radarr.api_key) as radarr:
            movie_ids = frozenset(radarr.library_tmdb_ids())
    if "series" in query.kinds and config.sonarr:
        with SonarrClient(config.sonarr.url, config.sonarr.api_key) as sonarr:
            sonarr_library = sonarr.library()
        series_ids = sonarr_library.tmdb_ids
        if missing := sonarr_library.titles_without_tmdb_id:
            shown = ", ".join(missing[:10])
            more = f" and {len(missing) - 10} more" if len(missing) > 10 else ""
            log.warning(
                "%s in Sonarr %s no TMDB ID and can't be recognized in results: %s%s",
                _plural(len(missing), "series", "series"),
                "has" if len(missing) == 1 else "have",
                shown,
                more,
            )
    return Library(movie_ids=movie_ids, series_ids=series_ids)


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
    if decision.status == "in_library":
        return " (in library)"
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
