"""Clients for Radarr and Sonarr: which titles are already in your library, and (for
Radarr so far) adding movies to it.

Both apps serve the same v3 API, authenticated with an X-Api-Key header. Only
`RadarrClient.add_movie` changes anything; everything else reads.
"""

from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, Self

import httpx2
from pydantic import BaseModel, ConfigDict

# Whole libraries come back in one response, which can take a while for large ones.
TIMEOUT_SECONDS = 30.0

MinimumAvailability = Literal["announced", "inCinemas", "released"]
"""When Radarr considers a movie available to download (Radarr's own names)."""


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


@dataclass(frozen=True)
class RadarrAddOptions:
    """`RadarrAdd` checked against Radarr, with the quality profile as its ID."""

    quality_profile_id: int
    root_folder: str
    monitored: bool
    minimum_availability: MinimumAvailability
    search_on_add: bool


@dataclass(frozen=True)
class SonarrLibrary:
    tmdb_ids: frozenset[int]
    # Series Sonarr has no TMDB ID for; they can't be recognized in TMDB results.
    titles_without_tmdb_id: tuple[str, ...]


class _ArrClient:
    app = "the app"

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


class RadarrClient(_ArrClient):
    app = "Radarr"

    def library_tmdb_ids(self) -> set[int]:
        """The TMDB IDs of every movie in Radarr."""
        return {movie["tmdbId"] for movie in self._get("api/v3/movie") if movie.get("tmdbId")}

    def prepare(self, add: RadarrAdd) -> RadarrAddOptions:
        """Check the add settings against Radarr: the profile and root folder must exist.

        Raises ArrError naming the choices Radarr has when one doesn't. Both must be set
        (callers report a missing one as a config problem first).
        """
        if add.quality_profile is None or add.root_folder is None:
            raise ValueError("quality_profile and root_folder are needed to add movies")

        profiles = {p["name"]: p["id"] for p in self._get("api/v3/qualityprofile")}
        profile_id = _find(profiles, add.quality_profile, str.casefold)
        if profile_id is None:
            raise ArrError(
                f"Radarr has no quality profile named {add.quality_profile!r} "
                f"(it has: {', '.join(sorted(profiles)) or 'none'})"
            )

        folders = {f["path"]: f["path"] for f in self._get("api/v3/rootfolder")}
        root_folder = _find(folders, add.root_folder, _folder_key)
        if root_folder is None:
            raise ArrError(
                f"Radarr has no root folder {add.root_folder!r} "
                f"(it has: {', '.join(sorted(folders)) or 'none'})"
            )

        return RadarrAddOptions(
            quality_profile_id=profile_id,
            root_folder=root_folder,
            monitored=add.monitored,
            minimum_availability=add.minimum_availability,
            search_on_add=add.search_on_add,
        )

    def add_movie(self, tmdb_id: int, options: RadarrAddOptions) -> bool:
        """Add the movie with this TMDB ID. False if Radarr already has it.

        The movie is looked up in Radarr first, and that same data is sent back with
        the add settings filled in, as Radarr's own web UI does.
        """
        movie = self._get("api/v3/movie/lookup/tmdb", {"tmdbId": tmdb_id})
        movie.update(
            qualityProfileId=options.quality_profile_id,
            rootFolderPath=options.root_folder,
            monitored=options.monitored,
            minimumAvailability=options.minimum_availability,
            addOptions={
                "searchForMovie": options.search_on_add,
                "monitor": "movieOnly" if options.monitored else "none",
            },
        )
        try:
            self._post("api/v3/movie", movie)
        except ArrError as e:
            # Added since the library was read, e.g. by hand while the preview ran.
            if "MovieExistsValidator" in e.codes:
                return False
            raise
        return True


class SonarrClient(_ArrClient):
    app = "Sonarr"

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
