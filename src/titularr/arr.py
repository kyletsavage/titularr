"""Read-only clients for Radarr and Sonarr: which titles are already in your library.

Both apps serve the same v3 API, authenticated with an X-Api-Key header. Titularr only
reads from them; nothing here changes either app.
"""

from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import httpx2

# Whole libraries come back in one response, which can take a while for large ones.
TIMEOUT_SECONDS = 30.0


class ArrError(Exception):
    """Radarr or Sonarr couldn't be reached, or returned an error."""


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

    def _get(self, path: str) -> Any:
        try:
            response = self._client.get(path)
        except httpx2.HTTPError as e:
            raise ArrError(f"can't reach {self.app} at {self._client.base_url}: {e}") from e
        if response.is_error:
            raise ArrError(_error_message(self.app, response))
        try:
            return response.json()
        except ValueError as e:
            raise ArrError(f"{self.app} returned a response that isn't JSON") from e


class RadarrClient(_ArrClient):
    app = "Radarr"

    def library_tmdb_ids(self) -> set[int]:
        """The TMDB IDs of every movie in Radarr."""
        return {movie["tmdbId"] for movie in self._get("api/v3/movie") if movie.get("tmdbId")}


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


def _error_message(app: str, response: httpx2.Response) -> str:
    detail = None
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = body.get("message") or body.get("error")
    except ValueError:
        pass
    if response.status_code == 401:
        detail = detail or "check the api_key in your config"
    return f"{app} returned HTTP {response.status_code}" + (f": {detail}" if detail else "")
