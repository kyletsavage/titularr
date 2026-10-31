import json
from dataclasses import replace

import httpx2
import pytest

from titularr.arr import ArrError, RadarrAdd, RadarrAddOptions, RadarrClient, SonarrClient

API_KEY = "secret-api-key"


def client_for(cls, handler, url="http://radarr:7878"):
    requests = []

    def record(request):
        requests.append(request)
        return handler(request)

    return cls(url, API_KEY, transport=httpx2.MockTransport(record)), requests


def test_radarr_library_tmdb_ids():
    movies = [{"tmdbId": 10719, "title": "Elf"}, {"tmdbId": 850, "title": "A Christmas Story"}]
    client, requests = client_for(RadarrClient, lambda r: httpx2.Response(200, json=movies))

    assert client.library_tmdb_ids() == {10719, 850}
    (request,) = requests
    assert str(request.url) == "http://radarr:7878/api/v3/movie"
    assert request.headers["X-Api-Key"] == API_KEY


def test_radarr_skips_movies_without_a_tmdb_id():
    movies = [{"tmdbId": 1}, {"tmdbId": 0}, {"title": "no id at all"}]
    client, _ = client_for(RadarrClient, lambda r: httpx2.Response(200, json=movies))
    assert client.library_tmdb_ids() == {1}


@pytest.mark.parametrize("url", ["http://host/radarr", "http://host/radarr/"])
def test_base_path_is_kept(url):
    client, requests = client_for(RadarrClient, lambda r: httpx2.Response(200, json=[]), url=url)
    client.library_tmdb_ids()
    assert str(requests[0].url) == "http://host/radarr/api/v3/movie"


def test_sonarr_library_lists_series_without_a_tmdb_id():
    series = [
        {"tmdbId": 1399, "tvdbId": 121361, "title": "Game of Thrones"},
        {"tmdbId": 0, "tvdbId": 1, "title": "Zeta Show"},
        {"tvdbId": 2, "title": "Alpha Show"},  # no tmdbId field at all
        {"tmdbId": None, "tvdbId": 3},  # no title either
    ]
    client, requests = client_for(
        SonarrClient, lambda r: httpx2.Response(200, json=series), url="http://sonarr:8989"
    )

    library = client.library()
    assert library.tmdb_ids == {1399}
    assert library.titles_without_tmdb_id == ("Alpha Show", "Zeta Show", "tvdb:3")
    assert str(requests[0].url) == "http://sonarr:8989/api/v3/series"
    assert requests[0].headers["X-Api-Key"] == API_KEY


def test_unauthorized_names_the_app_without_leaking_the_key():
    client, _ = client_for(RadarrClient, lambda r: httpx2.Response(401))

    with pytest.raises(ArrError) as excinfo:
        client.library_tmdb_ids()
    message = str(excinfo.value)
    assert message == "Radarr returned HTTP 401: check the api_key in your config"
    assert API_KEY not in message


def test_error_message_from_the_app_is_included():
    client, _ = client_for(
        SonarrClient, lambda r: httpx2.Response(500, json={"message": "Database is locked"})
    )
    with pytest.raises(ArrError, match="Sonarr returned HTTP 500: Database is locked"):
        client.library()


def test_unreachable_app_is_an_arr_error():
    def refuse(request):
        raise httpx2.ConnectError("connection refused", request=request)

    client, _ = client_for(RadarrClient, refuse)
    with pytest.raises(ArrError, match="can't reach Radarr at http://radarr:7878/"):
        client.library_tmdb_ids()


def test_non_json_response_is_an_arr_error():
    client, _ = client_for(RadarrClient, lambda r: httpx2.Response(200, text="<html>login</html>"))
    with pytest.raises(ArrError, match="isn't JSON"):
        client.library_tmdb_ids()


# --- adding movies to Radarr ---

PROFILES = [{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}]
FOLDERS = [{"path": "/movies"}, {"path": "/kids/"}]


def fake_radarr(post=lambda r: httpx2.Response(201, json={})):
    """A Radarr handler with two quality profiles and two root folders. `post` answers
    the add request."""

    def handler(request):
        path = request.url.path
        if path == "/api/v3/qualityprofile":
            return httpx2.Response(200, json=PROFILES)
        if path == "/api/v3/rootfolder":
            return httpx2.Response(200, json=FOLDERS)
        if path == "/api/v3/movie/lookup/tmdb":
            tmdb_id = int(request.url.params["tmdbId"])
            return httpx2.Response(200, json={"tmdbId": tmdb_id, "title": "Elf", "year": 2003})
        if path == "/api/v3/movie" and request.method == "POST":
            return post(request)
        return httpx2.Response(404)

    return handler


def test_prepare_finds_profile_ignoring_case_and_folder_ignoring_trailing_slash():
    client, _ = client_for(RadarrClient, fake_radarr())
    add = RadarrAdd(quality_profile="hd-1080P", root_folder="/kids", search_on_add=True)

    assert client.prepare(add) == RadarrAddOptions(
        quality_profile_id=4,
        root_folder="/kids/",  # Radarr's own spelling
        monitored=True,
        minimum_availability="released",
        search_on_add=True,
    )


def test_prepare_lists_profiles_when_one_is_unknown():
    client, _ = client_for(RadarrClient, fake_radarr())
    add = RadarrAdd(quality_profile="4K", root_folder="/movies")

    with pytest.raises(ArrError, match=r"no quality profile named '4K' \(it has: Any, HD-1080p\)"):
        client.prepare(add)


def test_prepare_lists_root_folders_when_one_is_unknown():
    client, _ = client_for(RadarrClient, fake_radarr())
    add = RadarrAdd(quality_profile="Any", root_folder="/Movies")  # paths keep their case

    with pytest.raises(ArrError, match=r"no root folder '/Movies' \(it has: /kids/, /movies\)"):
        client.prepare(add)


OPTIONS = RadarrAddOptions(
    quality_profile_id=4,
    root_folder="/movies",
    monitored=False,
    minimum_availability="announced",
    search_on_add=False,
)


def test_add_movie_sends_the_lookup_back_with_the_settings():
    client, requests = client_for(RadarrClient, fake_radarr())

    assert client.add_movie(10719, OPTIONS) is True
    lookup, post = requests
    assert str(lookup.url) == "http://radarr:7878/api/v3/movie/lookup/tmdb?tmdbId=10719"
    assert post.method == "POST"
    assert json.loads(post.content) == {
        "tmdbId": 10719,
        "title": "Elf",
        "year": 2003,
        "qualityProfileId": 4,
        "rootFolderPath": "/movies",
        "monitored": False,
        "minimumAvailability": "announced",
        "addOptions": {"searchForMovie": False, "monitor": "none"},
    }


def test_monitored_movie_is_added_as_movie_only():
    client, requests = client_for(RadarrClient, fake_radarr())
    client.add_movie(1, replace(OPTIONS, monitored=True, search_on_add=True))

    body = json.loads(requests[-1].content)
    assert body["monitored"] is True
    assert body["addOptions"] == {"searchForMovie": True, "monitor": "movieOnly"}


def test_add_movie_already_in_radarr_returns_false():
    exists = [
        {
            "propertyName": "TmdbId",
            "errorMessage": "This movie has already been added",
            "errorCode": "MovieExistsValidator",
        }
    ]
    client, _ = client_for(RadarrClient, fake_radarr(lambda r: httpx2.Response(400, json=exists)))
    assert client.add_movie(10719, OPTIONS) is False


def test_other_validation_errors_are_reported():
    errors = [
        {"errorMessage": "Path is not writable", "errorCode": "FolderWritableValidator"},
        {"errorMessage": "Invalid Path"},
    ]
    client, _ = client_for(RadarrClient, fake_radarr(lambda r: httpx2.Response(400, json=errors)))

    with pytest.raises(ArrError) as excinfo:
        client.add_movie(10719, OPTIONS)
    assert str(excinfo.value) == "Radarr returned HTTP 400: Path is not writable; Invalid Path"
    assert excinfo.value.codes == ("FolderWritableValidator",)
