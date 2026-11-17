"""The `titularr` command: search TMDB and preview what each query would keep.

Every command is a preview unless given --apply: then the kept movies are added to
Radarr and the kept series to Sonarr, after asking first (--yes skips asking).
"""

import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, NoReturn

import typer
from pydantic import ValidationError

from titularr import service
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
from titularr.config import (
    DEFAULT_MAX_ADDS,
    ArrSettings,
    Config,
    ConfigError,
    load_config,
    resolve_config_path,
)
from titularr.exclusions import Exclusions
from titularr.filters import Filters
from titularr.history import History, HistoryError, RecordedTitle, Result, Run
from titularr.models import AdultMode, Candidate, Decision, Kind
from titularr.pipeline import Query
from titularr.sources.tmdb import TmdbClient

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
YesOption = Annotated[
    bool,
    typer.Option(
        "--yes",
        help="With --apply: add without asking first. More titles than the add limit adds none.",
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
    runtime_min: Annotated[
        int | None,
        typer.Option(
            min=1,
            help="Shortest runtime to keep, in minutes. Movies only; one TMDB request per title.",
            rich_help_panel="Filters",
        ),
    ] = None,
    runtime_max: Annotated[
        int | None,
        typer.Option(
            min=1,
            help="Longest runtime to keep, in minutes. Movies only; one TMDB request per title.",
            rich_help_panel="Filters",
        ),
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
    max_adds: Annotated[
        int,
        typer.Option(
            min=1,
            help="With --apply --yes: if more titles than this would be added, add none.",
        ),
    ] = DEFAULT_MAX_ADDS,
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
        "runtime_min": runtime_min,
        "runtime_max": runtime_max,
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
    _preview(
        query,
        token,
        show_dropped,
        loaded,
        apply=apply,
        yes=yes,
        max_adds=(max_adds, "--max-adds"),
    )


@app.command(name="run")
def run_saved(
    name: Annotated[
        str | None,
        typer.Argument(
            help="Name of a query saved in the config file. Leave out with --scheduled.",
            show_default=False,
        ),
    ] = None,
    scheduled: Annotated[
        bool,
        typer.Option(
            "--scheduled",
            help="Run every saved query that has 'scheduled: true', one after another.",
        ),
    ] = False,
    config: ConfigOption = None,
    show_dropped: ShowDroppedOption = False,
    apply: ApplyOption = False,
    yes: YesOption = False,
) -> None:
    """Run a query saved in the config file, or every scheduled one."""
    if (name is None) == (not scheduled):
        _usage_error("give the name of a saved query, or --scheduled (not both)")
    loaded = _load_config(config, required=True)
    assert loaded is not None
    if name is not None:
        try:
            loaded.query(name)  # an unknown name lists the saved ones
        except ConfigError as e:
            _usage_error(str(e))
        _run_saved_query(loaded, name, show_dropped, apply=apply, yes=yes)
        return

    names = [n for n, saved in loaded.queries.items() if saved.scheduled]
    if not names:
        print("No saved queries have scheduled: true.")
        return
    # Checked once here, rather than failing the same way for every query.
    if apply and not yes and not _can_prompt():
        _usage_error("--apply asks before adding, but nobody can answer here; add --yes")
    failed = 0
    for n in names:
        print(f"== {n} ==")
        try:
            _run_saved_query(loaded, n, show_dropped, apply=apply, yes=yes)
        except typer.Exit as e:
            # One query failing (a search error, a failed add, too many titles to add,
            # a missing setting) doesn't stop the others.
            if e.exit_code:
                failed += 1
    print(f"Scheduled queries: {len(names)} run, {failed} failed")
    if failed:
        raise typer.Exit(1)


def _run_saved_query(
    config: Config, name: str, show_dropped: bool, *, apply: bool, yes: bool
) -> None:
    _preview(
        config.query(name),
        config.sources.tmdb.token,
        show_dropped,
        config,
        apply=apply,
        yes=yes,
        query_name=name,
        max_adds=(config.queries[name].max_adds, "max_adds"),
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
    scheduled = [name for name, saved in loaded.queries.items() if saved.scheduled]
    print(
        f"{path} is valid: {_plural(len(names), 'saved query', 'saved queries')}{listed}, "
        f"{_plural(rules, 'global exclusion rule', 'global exclusion rules')}, "
        f"library check: {', '.join(apps) or 'none'}, "
        f"scheduled: {', '.join(scheduled) or 'none'}"
    )


@app.command()
def history(
    run_id: Annotated[
        int | None,
        typer.Argument(help="Show the titles of this run. Default: list recent runs."),
    ] = None,
    limit: Annotated[int, typer.Option(min=1, help="How many recent runs to list.")] = 20,
    config: ConfigOption = None,
) -> None:
    """Show what past --apply runs added to Radarr and Sonarr."""
    # Only reads the history file, so the TMDB/Radarr/Sonarr secrets needn't be set.
    loaded = _load_config(config, required=True, need_secrets=False)
    assert loaded is not None
    path = loaded.history_path
    if not path.exists():
        print("No runs recorded yet.")
        return
    try:
        with History.open(path) as recorded:
            if run_id is None:
                runs = recorded.recent_runs(limit)
                if not runs:
                    print("No runs recorded yet.")
                for run_ in runs:
                    print(_describe_run(run_))
                return
            run_ = recorded.get_run(run_id)
            if run_ is None:
                _usage_error(f"no run #{run_id} in the run history")
            print(_describe_run(run_))
            for title in recorded.run_titles(run_id):
                print(
                    _result_line(title.kind, title.result, _describe_recorded(title), title.error)
                )
    except HistoryError as e:
        _usage_error(str(e))


def _load_config(
    explicit: Path | None, *, required: bool, need_secrets: bool = True
) -> Config | None:
    """Load the config. If it isn't required, a missing default config file is fine."""
    path = resolve_config_path(explicit)
    if not required and explicit is None and not path.exists():
        return None
    try:
        return load_config(path, need_secrets=need_secrets)
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
    max_adds: tuple[int, str] = (DEFAULT_MAX_ADDS, "max_adds"),
) -> None:
    """Run the query, print the kept (and optionally dropped) titles, then a summary.

    With `apply`, then add the kept titles to Radarr and Sonarr (asking first, unless
    `yes`). `query_name` is the saved query being run, whose add settings apply.
    `max_adds` is the add limit for `yes` runs, and the setting's name for messages.
    """
    targets: list[_Target] = []
    if apply:
        if not yes and not _can_prompt():
            _usage_error("--apply asks before adding, but nobody can answer here; add --yes")
        targets = [_target(kind, config, query_name) for kind in query.kinds]
        # Checked before searching too, so nothing is ever added without a record.
        # (Any --apply has a config: _target needs one.)
        assert config is not None
        try:
            History.open(config.history_path).close()
        except HistoryError as e:
            _usage_error(str(e))

    # Titles can contain characters the terminal's encoding can't show (e.g. a Windows
    # console redirected to a file); replace them instead of crashing.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")

    def show(decision: Decision) -> None:
        if decision.status == "kept" or show_dropped:
            print(_describe_candidate(decision.candidate) + _why_dropped(decision))

    # Attached for this run only (not the global root logger), so it always writes to
    # the current stderr and never piles up between runs.
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    package_log = logging.getLogger("titularr")
    package_log.addHandler(console)
    try:
        with service.collect_warnings() as warnings:
            # Fetched once, before searching, so a bad URL or API key fails fast.
            library = service.load_library(query, config, radarr=RadarrClient, sonarr=SonarrClient)
            for target in targets:
                # Checked before searching too, so a wrong profile or folder fails fast.
                target.prepare()
            with TmdbClient(token) as tmdb:
                result = service.search(query, tmdb, library, on_decision=show)
    except ArrError as e:
        # Before any TMDB request, so there's no summary to show.
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(1) from e
    finally:
        package_log.removeHandler(console)
    if result.error is not None:
        print(f"Error: {result.error}", file=sys.stderr)

    # Printed even after an error, so a partial run still shows how far it got.
    excludes = any(layer.active for _, layer in query.exclusion_layers())
    parts = []
    for kind, counts in result.summary.counts.items():
        one, many = _KIND_WORDS[kind]
        part = f"{_plural(counts.found, one, many)} found, {counts.matched} matched"
        if excludes:
            part += f", {counts.excluded} excluded"
        if query.filters.active or excludes or result.library.checks(kind):
            part += f", {counts.kept} kept"
        if result.library.checks(kind):
            part += f", {counts.in_library} in library"
        parts.append(part)
    parts.append(_plural(len(warnings), "warning", "warnings"))
    print("; ".join(parts))
    if result.error is not None:
        if apply:
            print("Nothing was added, because the search didn't finish.", file=sys.stderr)
        raise typer.Exit(1)
    if apply:
        assert config is not None
        _apply(
            result.kept,
            targets,
            yes,
            history_path=config.history_path,
            query=query,
            name=query_name,
            max_adds=max_adds,
        )


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
        return _app_name(self.kind)

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
        titles = _KIND_WORDS[kind][1]
        if config is not None and (reason := config.unusable_reason(section)):
            # The section is in the file, but was left out (e.g. its API key isn't set).
            _usage_error(
                f"--apply needs a usable {section} section in the config to add {titles}"
                f" ({section} can't be used: {reason})"
            )
        _usage_error(f"--apply needs a {section} section in the config to add {titles}")
    missing = [key for key in ("quality_profile", "root_folder") if not getattr(add, key)]
    if missing:
        needed = " and ".join(f"{section}.{key}" for key in missing)
        _usage_error(f"--apply needs {needed} in the config")
    return _Target(kind, connection, add)


def _apply(
    kept: list[Candidate],
    targets: list[_Target],
    yes: bool,
    *,
    history_path: Path,
    query: Query,
    name: str | None,
    max_adds: tuple[int, str],
) -> None:
    """Add the kept titles, after asking unless `yes`. One line per title, then a
    summary per app. Each title's result is recorded in the run history; `name` is the
    saved query's (None for a one-off search).

    With `yes` nobody reviews the titles first, so if there are more than `max_adds`
    (the limit, and the setting's name), none are added and the exit status is 1.
    """
    batches = [(t, [c for c in kept if c.kind == t.kind]) for t in targets]
    batches = [(target, titles) for target, titles in batches if titles]
    if not batches:
        print("Nothing to add.")
        return

    limit, setting = max_adds
    total = sum(len(titles) for _, titles in batches)
    if yes and total > limit:
        # A run that asks first is reviewed by whoever answers, so it has no limit.
        print(
            f"Not adding: {_plural(total, 'title', 'titles')} to add is more than "
            f"{setting} ({limit}). Preview the query, then narrow it or raise {setting}.",
            file=sys.stderr,
        )
        raise typer.Exit(1)

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
    try:
        recorded = History.open(history_path)
    except HistoryError as e:
        # Checked before searching, so this only happens if the file changed since.
        print(f"Error: {e}; nothing was added", file=sys.stderr)
        raise typer.Exit(1) from e
    with recorded:
        run_id = recorded.start_run("run" if name is not None else "search", query.text, name)
        for target, titles in batches:
            counts: dict[Result, int] = dict.fromkeys(("added", "exists", "not_found", "failed"), 0)
            with target.client() as client:
                for title in titles:
                    error = None
                    try:
                        result: Result = target.add_one(client, title.tmdb_id)
                    except ArrError as e:
                        # One title failing doesn't stop the rest.
                        result, error = "failed", str(e)
                    recorded.record(run_id, title, result, error)
                    counts[result] += 1
                    print(_result_line(title.kind, result, _describe_candidate(title), error))
            summaries.append(_app_summary(target.kind, counts))
            any_failed = any_failed or counts["failed"] > 0
        recorded.finish_run(run_id)
    for summary in summaries:
        print(summary)
    if any_failed:
        raise typer.Exit(1)


def _app_name(kind: Kind) -> str:
    return "Radarr" if kind == "movie" else "Sonarr"


def _result_line(kind: Kind, result: Result, described: str, error: str | None) -> str:
    """One title's result, as --apply prints it and `history` shows it."""
    if result == "added":
        return f"added: {described}"
    if result == "exists":
        return f"already in {_app_name(kind)}: {described}"
    if result == "not_found":
        return f"not found in {_app_name(kind)}: {described}"
    return f"failed: {described}: {error}"


def _app_summary(kind: Kind, counts: Mapping[Result, int]) -> str:
    """How many titles one app got with each result, e.g. "Radarr: 2 added, ..."."""
    summary = f"{_app_name(kind)}: {counts.get('added', 0)} added, "
    summary += f"{counts.get('exists', 0)} already there"
    # Only Sonarr can fail to find a title (a series it has no TVDB entry for).
    if kind == "series":
        summary += f", {counts.get('not_found', 0)} not found"
    return summary + f", {counts.get('failed', 0)} failed"


def _describe_run(run_: Run) -> str:
    """One line per recorded run: when, which query, and what each app got."""
    started = run_.started_at.astimezone().strftime("%Y-%m-%d %H:%M")
    what = f"run {run_.query_name}" if run_.query_name else f'search "{run_.query_text}"'
    kinds: list[Kind] = [k for k in ("movie", "series") if any(k == c[0] for c in run_.counts)]
    results = "; ".join(
        _app_summary(kind, {r: n for (k, r), n in run_.counts.items() if k == kind})
        for kind in kinds
    )
    unfinished = "  (stopped before finishing)" if run_.finished_at is None else ""
    return f"#{run_.id}  {started}  {what}  {results}{unfinished}"


def _describe_recorded(title: RecordedTitle) -> str:
    year = "?" if title.year is None else title.year
    return f"[{title.kind}] {title.title} ({year})  tmdb:{title.tmdb_id}"


def _can_prompt() -> bool:
    """Whether someone can answer a question (stdin is a terminal, not a script or pipe)."""
    return sys.stdin.isatty()


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
