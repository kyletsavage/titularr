import httpx2
import pytest

from titularr.arr import ArrError, RadarrClient, SonarrClient

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
