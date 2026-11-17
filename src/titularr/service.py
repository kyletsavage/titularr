"""What Titularr does, as functions that return data: shared by the CLI and the API.

Nothing here prints, asks, or exits. Problems are exceptions or fields of the result,
and each interface decides how to show them.
"""

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx2

from titularr.arr import RadarrClient, SonarrClient
from titularr.config import Config
from titularr.models import Candidate, Decision
from titularr.pipeline import Library, Query, Summary, TitleSource, run
from titularr.sources.tmdb import TmdbError

log = logging.getLogger("titularr")


@dataclass
class PreviewResult:
    """What one query found, and what was decided about each title.

    `error` is set when the search stopped part-way (a TMDB or network error): the
    decisions and counts are then the partial ones up to that point.
    """

    decisions: list[Decision]
    summary: Summary
    library: Library
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def kept(self) -> list[Candidate]:
        """The titles that would be added."""
        return [d.candidate for d in self.decisions if d.status == "kept"]


class _ThreadWarnings(logging.Handler):
    """Keeps the warning messages logged by one thread, so overlapping calls (the
    API serves several at once) each get only their own."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.thread = threading.get_ident()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread == self.thread:
            self.messages.append(record.getMessage())


@contextmanager
def collect_warnings() -> Iterator[list[str]]:
    """Collect the warnings Titularr logs in this thread while the block runs."""
    handler = _ThreadWarnings()
    log.addHandler(handler)
    try:
        yield handler.messages
    finally:
        log.removeHandler(handler)


def load_library(
    query: Query,
    config: Config | None,
    *,
    radarr: Callable[[str, str], RadarrClient] = RadarrClient,
    sonarr: Callable[[str, str], SonarrClient] = SonarrClient,
) -> Library:
    """What's already in Radarr/Sonarr, for the kinds this query searches.

    `radarr` and `sonarr` make the clients (url, API key); tests pass fakes. Raises
    ArrError if an app can't be reached or rejects the API key.
    """
    if config is None:
        return Library()
    for warning in config.warnings:
        # Only mention an app whose kind this query actually searches.
        app_kind = "movie" if warning.startswith("radarr") else "series"
        if app_kind in query.kinds:
            log.warning(warning)

    movie_ids = series_ids = None
    if "movie" in query.kinds and config.radarr:
        with radarr(config.radarr.url, config.radarr.api_key) as client:
            movie_ids = frozenset(client.library_tmdb_ids())
    if "series" in query.kinds and config.sonarr:
        with sonarr(config.sonarr.url, config.sonarr.api_key) as client:
            sonarr_library = client.library()
        series_ids = sonarr_library.tmdb_ids
        if missing := sonarr_library.titles_without_tmdb_id:
            shown = ", ".join(missing[:10])
            more = f" and {len(missing) - 10} more" if len(missing) > 10 else ""
            log.warning(
                "%d series in Sonarr %s no TMDB ID and can't be recognized in results: %s%s",
                len(missing),
                "has" if len(missing) == 1 else "have",
                shown,
                more,
            )
    return Library(movie_ids=movie_ids, series_ids=series_ids)


def search(
    query: Query,
    source: TitleSource,
    library: Library,
    on_decision: Callable[[Decision], None] | None = None,
) -> PreviewResult:
    """Search `source` and decide about every title. `on_decision` is called with each
    decision as it arrives (the CLI prints them live). A TMDB or network error stops
    the search and is returned in `error`, with the decisions made so far."""
    result = PreviewResult(decisions=[], summary=Summary(kinds=query.kinds), library=library)
    try:
        for decision in run(query, source, library):
            result.decisions.append(decision)
            result.summary.add(decision)
            if on_decision is not None:
                on_decision(decision)
    except (TmdbError, httpx2.HTTPError) as e:
        result.error = str(e)
    return result


def preview(
    query: Query,
    source: TitleSource,
    config: Config | None,
    *,
    radarr: Callable[[str, str], RadarrClient] = RadarrClient,
    sonarr: Callable[[str, str], SonarrClient] = SonarrClient,
) -> PreviewResult:
    """Load the library, then search: everything a preview shows, with the warnings
    logged along the way. Raises ArrError (before searching) like `load_library`."""
    with collect_warnings() as warnings:
        library = load_library(query, config, radarr=radarr, sonarr=sonarr)
        result = search(query, source, library)
    result.warnings = list(warnings)
    return result
