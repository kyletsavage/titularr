"""Clients for Radarr and Sonarr: which titles are already in your library, and adding
movies and series to it.

Both apps serve the same v3 API, authenticated with an X-Api-Key header. Only
`RadarrClient.add_movie` and `SonarrClient.add_series` change anything; everything
else reads.
"""

from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Annotated, Any, Literal, Self

import httpx2
from pydantic import AfterValidator, BaseModel, ConfigDict

# Whole libraries come back in one response, which can take a while for large ones.
TIMEOUT_SECONDS = 30.0

MinimumAvailability = Literal["announced", "inCinemas", "released"]
"""When Radarr considers a movie available to download (Radarr's own names)."""

SonarrMonitor = Literal[
    "all",
    "future",
    "missing",
    "existing",
    "firstSeason",
    "lastSeason",
    "pilot",
    "recent",
    "monitorSpecials",
    "unmonitorSpecials",
    "none",
]
"""Which episodes Sonarr monitors when a series is added (Sonarr's own names)."""

SeriesType = Literal["standard", "daily", "anime"]

AddResult = Literal["added", "exists", "not_found"]
"""What happened to one title: added, already in the app, or not found by the app."""


def _unique_tags(tags: tuple[str, ...]) -> tuple[str, ...]:
    # The apps store tags in lowercase, so "Anime" and "anime" are the same tag.
    return tuple(dict.fromkeys(tag.strip().lower() for tag in tags))


Tags = Annotated[tuple[str, ...], AfterValidator(_unique_tags)]
"""Tag names, as the app shows them. In Radarr and Sonarr, a title's tags decide which
release profiles, indexers, download clients and delay profiles apply to it, so they
must already exist: they're never created."""


class ArrError(Exception):
    """Radarr or Sonarr couldn't be reached, or returned an error.

    `codes` holds the app's validation error codes, if it sent any.
    """

    def __init__(self, message: str, codes: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.codes = codes


class RadarrAdd(BaseModel):
    """How movies are added to Radarr, as written in the config.

    The quality profile is a name and the root folder a path, as Radarr shows them;
    `RadarrClient.prepare` checks both against Radarr. Both are only needed to add.
    """

    # extra="forbid": a typo like "root_folders" in a config file is an error, not ignored.
    model_config = ConfigDict(frozen=True, extra="forbid")

    quality_profile: str | None = None
    root_folder: str | None = None
    monitored: bool = True
    minimum_availability: MinimumAvailability = "released"
    search_on_add: bool = False
    tags: Tags = ()


@dataclass(frozen=True)
class RadarrAddOptions:
    """`RadarrAdd` checked against Radarr, with the quality profile and tags as IDs."""

    quality_profile_id: int
    root_folder: str
    monitored: bool
    minimum_availability: MinimumAvailability
    search_on_add: bool
    tag_ids: tuple[int, ...] = ()


class SonarrAdd(BaseModel):
    """How series are added to Sonarr, as written in the config.

    Like `RadarrAdd`, the quality profile is a name and the root folder a path, and both
    are only needed to add. The other defaults match Sonarr's own "Add Series" screen,
    except that searching on add is off.
    """

    # extra="forbid": a typo like "series_types" in a config file is an error, not ignored.
    model_config = ConfigDict(frozen=True, extra="forbid")

    quality_profile: str | None = None
    root_folder: str | None = None
    monitor: SonarrMonitor = "all"
    season_folder: bool = True
    series_type: SeriesType = "standard"
    monitor_new_items: Literal["all", "none"] = "all"
    search_on_add: bool = False
    tags: Tags = ()


@dataclass(frozen=True)
class SonarrAddOptions:
    """`SonarrAdd` checked against Sonarr, with the quality profile and tags as IDs."""

    quality_profile_id: int
    root_folder: str
    monitor: SonarrMonitor
    season_folder: bool
    series_type: SeriesType
    monitor_new_items: Literal["all", "none"]
    search_on_add: bool
    tag_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class SonarrLibrary:
    tmdb_ids: frozenset[int]
    # Series Sonarr has no TMDB ID for; they can't be recognized in TMDB results.
    titles_without_tmdb_id: tuple[str, ...]


class _ArrClient:
    app = "the app"
    # The validation error code the app sends when a title is already in it.
    exists_code = ""

    def __init__(
        self, url: str, api_key: str, *, transport: httpx2.BaseTransport | None = None
    ) -> None:
        # The trailing slash keeps a base path (e.g. http://host/radarr) when joining.
        self._client = httpx2.Client(
            base_url=url.rstrip("/") + "/",
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
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

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("POST", path, json=body)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx2.HTTPError as e:
            raise ArrError(f"can't reach {self.app} at {self._client.base_url}: {e}") from e
        if response.is_error:
            raise ArrError(_error_message(self.app, response), _error_codes(response))
        try:
            return response.json()
        except ValueError as e:
            raise ArrError(f"{self.app} returned a response that isn't JSON") from e

    def _profile_and_folder(self, profile: str | None, folder: str | None) -> tuple[int, str]:
        """The ID of the quality profile named `profile`, and the app's own spelling of
        root folder `folder`.

        Raises ArrError naming the choices the app has when one doesn't exist. Both must
        be set (callers report a missing one as a config problem first).
        """
        if profile is None or folder is None:
            raise ValueError(f"quality_profile and root_folder are needed to add to {self.app}")

        profiles = {p["name"]: p["id"] for p in self._get("api/v3/qualityprofile")}
        profile_id = _find(profiles, profile, str.casefold)
        if profile_id is None:
            raise ArrError(
                f"{self.app} has no quality profile named {profile!r} "
                f"(it has: {', '.join(sorted(profiles)) or 'none'})"
            )

        folders = {f["path"]: f["path"] for f in self._get("api/v3/rootfolder")}
        root_folder = _find(folders, folder, _folder_key)
        if root_folder is None:
            raise ArrError(
                f"{self.app} has no root folder {folder!r} "
                f"(it has: {', '.join(sorted(folders)) or 'none'})"
            )
        return profile_id, root_folder

    def _tag_ids(self, names: tuple[str, ...]) -> tuple[int, ...]:
        """The IDs of the tags with these names. Raises ArrError naming the app's tags
        when one doesn't exist."""
        if not names:
            return ()
        tags = {t["label"]: t["id"] for t in self._get("api/v3/tag")}
        ids = []
        for name in names:
            tag_id = _find(tags, name, str.casefold)
            if tag_id is None:
                raise ArrError(
                    f"{self.app} has no tag named {name!r} "
                    f"(it has: {', '.join(sorted(tags)) or 'none'}); tags aren't created, "
                    "since they decide which release profiles apply"
                )
            ids.append(tag_id)
        return tuple(ids)

    def _add(self, path: str, body: dict[str, Any]) -> AddResult:
        """POST a title to add; "exists" if the app already has it."""
        try:
            self._post(path, body)
        except ArrError as e:
            # Added since the library was read, e.g. by hand while the preview ran.
            if self.exists_code in e.codes:
                return "exists"
            raise
        return "added"


class RadarrClient(_ArrClient):
    app = "Radarr"
    exists_code = "MovieExistsValidator"

    def library_tmdb_ids(self) -> set[int]:
        """The TMDB IDs of every movie in Radarr."""
        return {movie["tmdbId"] for movie in self._get("api/v3/movie") if movie.get("tmdbId")}

    def prepare(self, add: RadarrAdd) -> RadarrAddOptions:
        """Check the add settings against Radarr: the profile, root folder and tags
        must exist."""
        profile_id, root_folder = self._profile_and_folder(add.quality_profile, add.root_folder)
        return RadarrAddOptions(
            quality_profile_id=profile_id,
            root_folder=root_folder,
            monitored=add.monitored,
            minimum_availability=add.minimum_availability,
            search_on_add=add.search_on_add,
            tag_ids=self._tag_ids(add.tags),
        )

    def add_movie(self, tmdb_id: int, options: RadarrAddOptions) -> AddResult:
        """Add the movie with this TMDB ID: "added", or "exists" if Radarr has it.

        The movie is looked up in Radarr first, and that same data is sent back with
        the add settings filled in, as Radarr's own web UI does.
        """
        movie = self._get("api/v3/movie/lookup/tmdb", {"tmdbId": tmdb_id})
        movie.update(
            qualityProfileId=options.quality_profile_id,
            rootFolderPath=options.root_folder,
            monitored=options.monitored,
            minimumAvailability=options.minimum_availability,
            tags=list(options.tag_ids),
            addOptions={
                "searchForMovie": options.search_on_add,
                "monitor": "movieOnly" if options.monitored else "none",
            },
        )
        return self._add("api/v3/movie", movie)


class SonarrClient(_ArrClient):
    app = "Sonarr"
    exists_code = "SeriesExistsValidator"

    def prepare(self, add: SonarrAdd) -> SonarrAddOptions:
        """Check the add settings against Sonarr: the profile, root folder and tags
        must exist."""
        profile_id, root_folder = self._profile_and_folder(add.quality_profile, add.root_folder)
        return SonarrAddOptions(
            quality_profile_id=profile_id,
            root_folder=root_folder,
            monitor=add.monitor,
            season_folder=add.season_folder,
            series_type=add.series_type,
            monitor_new_items=add.monitor_new_items,
            search_on_add=add.search_on_add,
            tag_ids=self._tag_ids(add.tags),
        )

    def add_series(self, tmdb_id: int, options: SonarrAddOptions) -> AddResult:
        """Add the series with this TMDB ID: "added", "exists" if Sonarr has it, or
        "not_found" if Sonarr can't find it.

        Sonarr adds series by TVDB ID, so the series is looked up by its TMDB ID first
        (Sonarr's own "tmdb:" search term). Only a result with that exact TMDB ID and a
        TVDB ID counts; that data is sent back with the add settings filled in.
        """
        results = self._get("api/v3/series/lookup", {"term": f"tmdb:{tmdb_id}"})
        series = next((s for s in results if s.get("tmdbId") == tmdb_id and s.get("tvdbId")), None)
        if series is None:
            return "not_found"
        series.update(
            qualityProfileId=options.quality_profile_id,
            rootFolderPath=options.root_folder,
            monitored=options.monitor != "none",
            seasonFolder=options.season_folder,
            seriesType=options.series_type,
            monitorNewItems=options.monitor_new_items,
            tags=list(options.tag_ids),
            addOptions={
                "monitor": options.monitor,
                "searchForMissingEpisodes": options.search_on_add,
                "searchForCutoffUnmetEpisodes": False,
            },
        )
        return self._add("api/v3/series", series)

    def library(self) -> SonarrLibrary:
        """The TMDB IDs of every series in Sonarr, plus the series that have none."""
        ids: set[int] = set()
        missing: list[str] = []
        for series in self._get("api/v3/series"):
            if series.get("tmdbId"):
                ids.add(series["tmdbId"])
            else:
                missing.append(series.get("title") or f"tvdb:{series.get('tvdbId', '?')}")
        return SonarrLibrary(frozenset(ids), tuple(sorted(missing)))


def _find[T](choices: dict[str, T], wanted: str, key: Callable[[str], str]) -> T | None:
    """The value whose name equals `wanted` once both are passed through `key`."""
    for name, value in choices.items():
        if key(name) == key(wanted):
            return value
    return None


def _folder_key(path: str) -> str:
    # "/movies/" and "/movies" are the same folder. Case is kept: Linux paths are
    # case-sensitive.
    return path.rstrip("/\\") or path


def _json_body(response: httpx2.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _error_codes(response: httpx2.Response) -> tuple[str, ...]:
    """The codes in a validation error response, which lists every check that failed."""
    body = _json_body(response)
    if not isinstance(body, list):
        return ()
    return tuple(e["errorCode"] for e in body if isinstance(e, dict) and e.get("errorCode"))


def _error_message(app: str, response: httpx2.Response) -> str:
    detail = None
    body = _json_body(response)
    if isinstance(body, dict):
        detail = body.get("message") or body.get("error")
    elif isinstance(body, list):
        # A validation error response: one entry per failed check.
        messages = [e.get("errorMessage") for e in body if isinstance(e, dict)]
        detail = "; ".join(m for m in messages if m) or None
    if response.status_code == 401:
        detail = detail or "check the api_key in your config"
    return f"{app} returned HTTP {response.status_code}" + (f": {detail}" if detail else "")
