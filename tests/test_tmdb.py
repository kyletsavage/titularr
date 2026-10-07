import logging

import httpx2
import pytest

from titularr.sources import tmdb
from titularr.sources.tmdb import TmdbClient, TmdbError

TOKEN = "test-token-123"


def movie(id, title, release_date="2001-12-01", **overrides):
    return {
        "id": id,
        "title": title,
        "original_title": title,
        "release_date": release_date,
        "original_language": "en",
        "vote_average": 7.1,
        "vote_count": 1200,
        "popularity": 12.3,  # extra fields from TMDB are ignored
        **overrides,
    }


def search_page(page, total_pages, results):
    return {
        "page": page,
        "total_pages": total_pages,
        "total_results": total_pages * 20,
        "results": results,
    }


def client_for(handler):
    """A TmdbClient whose HTTP requests go to `handler` instead of the network."""
    requests = []

    def record(request):
        requests.append(request)
        return handler(request)

    return TmdbClient(TOKEN, transport=httpx2.MockTransport(record)), requests


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(tmdb.time, "sleep", sleeps.append)
    return sleeps


def test_fetches_every_page_in_order():
    pages = {
        1: [movie(1, "A Christmas Story"), movie(2, "Christmas Vacation")],
        2: [movie(3, "Christmas with the Kranks")],
    }

    def handler(request):
        page = int(request.url.params["page"])
        return httpx2.Response(200, json=search_page(page, 2, pages[page]))

    client, requests = client_for(handler)
    results = list(client.search_movies("christmas"))

    assert [m.tmdb_id for m in results] == [1, 2, 3]
    assert [int(r.url.params["page"]) for r in requests] == [1, 2]
    first = results[0]
    assert first.title == "A Christmas Story"
    assert first.year == 2001
    assert first.source == "tmdb" and first.kind == "movie"


def test_sends_auth_header_and_query_params():
    def handler(request):
        return httpx2.Response(200, json=search_page(1, 1, []))

    client, requests = client_for(handler)
    list(client.search_movies("star trek"))

    (request,) = requests
    assert str(request.url).startswith("https://api.themoviedb.org/3/search/movie?")
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.url.params["query"] == "star trek"
    assert request.url.params["include_adult"] == "false"
    assert TOKEN not in str(request.url)


MIXED = [
    movie(1, "Regular", adult=False),
    movie(2, "Adult", adult=True),
    movie(3, "Unmarked"),  # no adult field at all
]


@pytest.mark.parametrize(
    ("mode", "sent", "expected_ids"),
    [
        # In "exclude" mode TMDB does the filtering, so whatever it returns is kept.
        ("exclude", "false", [1, 2, 3]),
        ("include", "true", [1, 2, 3]),
        ("only", "true", [2]),
    ],
)
def test_adult_modes(mode, sent, expected_ids):
    def handler(request):
        return httpx2.Response(200, json=search_page(1, 1, MIXED))

    client, requests = client_for(handler)
    results = list(client.search_movies("x", adult=mode))

    assert requests[0].url.params["include_adult"] == sent
    assert [m.tmdb_id for m in results] == expected_ids


def test_adult_flag_is_carried_onto_candidates():
    def handler(request):
        return httpx2.Response(200, json=search_page(1, 1, MIXED))

    client, _ = client_for(handler)
    assert [m.adult for m in client.search_movies("x", adult="include")] == [False, True, None]


@pytest.mark.parametrize("release_date", ["", None])
def test_unknown_release_date_gives_no_year(release_date):
    def handler(request):
        return httpx2.Response(200, json=search_page(1, 1, [movie(1, "X", release_date)]))

    client, _ = client_for(handler)
    (result,) = client.search_movies("x")
    assert result.year is None


def test_sparse_result_gives_none_for_missing_fields():
    # Real TMDB data: unrated entries come back with no vote_average/vote_count keys at all.
    sparse = {"id": 7, "title": "Bach: Christmas Oratorio", "adult": False, "poster_path": None}

    def handler(request):
        return httpx2.Response(200, json=search_page(1, 1, [sparse]))

    client, _ = client_for(handler)
    (result,) = client.search_movies("christmas")
    assert result.tmdb_id == 7
    assert result.title == "Bach: Christmas Oratorio"
    assert result.vote_average is None
    assert result.vote_count is None
    assert result.original_title is None
    assert result.original_language is None
    assert result.year is None


def test_result_without_id_or_title_is_skipped(caplog):
    results = [movie(1, "Elf"), {"id": 2}, {"title": "No ID"}, movie(3, "Krampus")]

    def handler(request):
        return httpx2.Response(200, json=search_page(1, 1, results))

    client, _ = client_for(handler)
    with caplog.at_level(logging.WARNING):
        found = list(client.search_movies("christmas"))

    assert [m.tmdb_id for m in found] == [1, 3]
    assert caplog.text.count("skipping TMDB result") == 2


def test_stops_at_500_pages_and_warns(caplog):
    def handler(request):
        page = int(request.url.params["page"])
        return httpx2.Response(200, json=search_page(page, 750, [movie(page, f"Movie {page}")]))

    client, requests = client_for(handler)
    with caplog.at_level(logging.WARNING):
        results = list(client.search_movies("the"))

    assert len(requests) == 500
    assert len(results) == 500
    assert "750 pages" in caplog.text


def test_retries_after_rate_limit(no_sleep):
    responses = [
        httpx2.Response(429, headers={"Retry-After": "2"}),
        httpx2.Response(200, json=search_page(1, 1, [movie(1, "Dracula")])),
    ]
    client, requests = client_for(lambda request: responses.pop(0))

    results = list(client.search_movies("dracula"))

    assert [m.title for m in results] == ["Dracula"]
    assert len(requests) == 2
    assert no_sleep == [2.0]


def test_gives_up_after_repeated_rate_limits(no_sleep):
    client, requests = client_for(lambda request: httpx2.Response(429))

    with pytest.raises(TmdbError, match="429"):
        list(client.search_movies("dracula"))

    assert len(requests) == tmdb.MAX_RETRIES + 1
    assert no_sleep == [tmdb.DEFAULT_RETRY_AFTER_SECONDS] * tmdb.MAX_RETRIES


def test_error_status_raises_without_leaking_token():
    body = {"status_code": 7, "status_message": "Invalid API key: You must be granted a valid key."}
    client, _ = client_for(lambda request: httpx2.Response(401, json=body))

    with pytest.raises(TmdbError) as excinfo:
        list(client.search_movies("dracula"))

    message = str(excinfo.value)
    assert "401" in message
    assert "Invalid API key" in message
    assert TOKEN not in message


def test_unexpected_response_shape_raises():
    client, _ = client_for(lambda request: httpx2.Response(200, json={"oops": True}))

    with pytest.raises(TmdbError, match="unexpected response"):
        list(client.search_movies("dracula"))


# --- Series (/search/tv) ---


def series(id, name, first_air_date="2019-11-29", **overrides):
    return {
        "id": id,
        "name": name,
        "original_name": name,
        "first_air_date": first_air_date,
        "original_language": "en",
        "vote_average": 6.4,
        "vote_count": 85,
        "origin_country": ["US"],  # not stored yet; ignored
        **overrides,
    }


def test_series_search_uses_tv_endpoint_and_maps_fields():
    def handler(request):
        return httpx2.Response(
            200, json=search_page(1, 1, [series(65550, "Christmas Wars", "2022-12-13")])
        )

    client, requests = client_for(handler)
    (result,) = client.search_series("christmas")

    assert requests[0].url.path == "/3/search/tv"
    assert requests[0].url.params["include_adult"] == "false"
    assert result.kind == "series"
    assert result.source == "tmdb"
    assert result.tmdb_id == 65550
    assert result.title == "Christmas Wars"
    assert result.original_title == "Christmas Wars"
    assert result.year == 2022
    assert result.vote_count == 85


def test_series_search_fetches_every_page():
    def handler(request):
        page = int(request.url.params["page"])
        return httpx2.Response(200, json=search_page(page, 3, [series(page, f"Show {page}")]))

    client, requests = client_for(handler)
    results = list(client.search_series("show"))

    assert [s.tmdb_id for s in results] == [1, 2, 3]
    assert len(requests) == 3


def test_sparse_series_gives_none_for_missing_fields():
    def handler(request):
        return httpx2.Response(200, json=search_page(1, 1, [{"id": 9, "name": "Unaired"}]))

    client, _ = client_for(handler)
    (result,) = client.search_series("x")
    assert result.title == "Unaired"
    assert result.year is None
    assert result.original_title is None
    assert result.vote_count is None
    assert result.adult is None


def test_series_without_id_or_name_is_skipped(caplog):
    # A series has "name", not "title": a "title" key alone doesn't count.
    results = [series(1, "Good"), {"id": 2}, {"id": 3, "title": "Wrong key"}, {"name": "No ID"}]

    def handler(request):
        return httpx2.Response(200, json=search_page(1, 1, results))

    client, _ = client_for(handler)
    with caplog.at_level(logging.WARNING):
        found = list(client.search_series("x"))

    assert [s.tmdb_id for s in found] == [1]
    assert caplog.text.count("skipping TMDB result") == 3


SERIES_MIXED = [
    series(1, "Regular", adult=False),
    series(2, "Adult", adult=True),
    series(3, "Unmarked"),
]


@pytest.mark.parametrize(
    ("mode", "sent", "expected_ids"),
    [
        ("exclude", "false", [1, 2, 3]),
        ("include", "true", [1, 2, 3]),
        ("only", "true", [2]),
    ],
)
def test_series_adult_modes(mode, sent, expected_ids):
    def handler(request):
        return httpx2.Response(200, json=search_page(1, 1, SERIES_MIXED))

    client, requests = client_for(handler)
    results = list(client.search_series("x", adult=mode))

    assert requests[0].url.params["include_adult"] == sent
    assert [s.tmdb_id for s in results] == expected_ids
