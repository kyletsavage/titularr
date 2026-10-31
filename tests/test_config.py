from pathlib import Path

import pytest

from titularr.arr import RadarrAdd, SonarrAdd
from titularr.config import ConfigError, load_config, resolve_config_path

EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"

MINIMAL = """\
sources:
  tmdb:
    token: abc
"""


@pytest.fixture
def write(tmp_path):
    """Write `text` to a config file (UTF-8, LF line endings) and return its path."""

    def _write(text, name="config.yaml"):
        path = tmp_path / name
        path.write_bytes(text.encode("utf-8"))
        return path

    return _write


# --- the example file ---


def test_example_config_loads(monkeypatch):
    monkeypatch.setenv("TMDB_API_TOKEN", "secret-token")
    config = load_config(EXAMPLE)

    assert config.sources.tmdb.token == "secret-token"
    assert config.exclusions.ids == {"movie:1399"}
    assert set(config.queries) == {"holiday-movies", "dracula"}
    holiday = config.query("holiday-movies")
    assert holiday.text == "christmas"
    assert holiday.kinds == ("movie",)
    assert holiday.match == "word"
    assert holiday.filters.min_votes == 500
    dracula = config.query("dracula")
    assert dracula.match == "substring"
    assert dracula.kinds == ("movie",)
    assert dracula.filters.languages is None  # "languages: any"
    assert dracula.exclusions.titles == ("dead and loving",)
    assert dracula.global_exclusions.ids == {"movie:1399"}


# --- environment variables ---


def test_env_references_are_expanded_everywhere(write, monkeypatch):
    monkeypatch.setenv("TOKEN", "t0k3n")
    monkeypatch.setenv("WORD", "carol")
    path = write(
        MINIMAL.replace("abc", "${TOKEN}")
        + "exclusions:\n  titles: ['${WORD}', 'x-${WORD}-y']\n"
        + "queries:\n  q:\n    text: christmas\n    filters:\n      min_votes: 5\n"
    )
    config = load_config(path)

    assert config.sources.tmdb.token == "t0k3n"
    assert config.exclusions.titles == ("carol", "x-carol-y")
    assert config.query("q").filters.min_votes == 5  # non-strings untouched


def test_unset_env_variable_is_named(write, monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    path = write(MINIMAL.replace("abc", "${NOT_SET_ANYWHERE}"))
    with pytest.raises(
        ConfigError, match=r"uses \$\{NOT_SET_ANYWHERE\}, but NOT_SET_ANYWHERE is not set"
    ):
        load_config(path)


def test_env_values_cant_change_the_yaml_structure(write, monkeypatch):
    monkeypatch.setenv("TOKEN", "abc\nqueries: {hacked: {text: x}}")
    config = load_config(write(MINIMAL.replace("abc", "${TOKEN}")))
    assert config.queries == {}
    assert config.sources.tmdb.token.startswith("abc\n")


# --- validation ---


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        (
            MINIMAL + "queries:\n  q:\n    text: x\n    filters:\n      min_vote: 5\n",
            "queries.q.filters.min_vote: Extra inputs are not permitted",
        ),
        (
            MINIMAL + "queries:\n  q:\n    text: x\n    filters:\n      min_votes: -1\n",
            "queries.q.filters.min_votes: Input should be greater than or equal to 0",
        ),
        (MINIMAL + "exclusion:\n  ids: [movie:1]\n", "exclusion: Extra inputs are not permitted"),
        ("sources:\n  tmdb:\n    token: ''\n", "sources.tmdb.token"),
        ("queries: {}\n", "sources: Field required"),
    ],
)
def test_invalid_settings_name_the_field(write, yaml_text, message):
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(yaml_text))
    assert message in str(excinfo.value)


@pytest.mark.parametrize("name", ["has space", "semi;colon", "dot.ted"])
def test_query_names_are_restricted(write, name):
    path = write(MINIMAL + f"queries:\n  '{name}':\n    text: x\n")
    with pytest.raises(ConfigError, match="may only use letters, digits"):
        load_config(path)


def test_global_exclusions_cant_be_set_inside_a_query(write):
    path = write(
        MINIMAL + "queries:\n  q:\n    text: x\n    global_exclusions:\n      ids: [movie:1]\n"
    )
    with pytest.raises(ConfigError, match="belong in the top-level 'exclusions:' section"):
        load_config(path)


def test_unknown_query_name_lists_available_ones(write):
    config = load_config(write(MINIMAL + "queries:\n  b:\n    text: x\n  a:\n    text: y\n"))
    with pytest.raises(ConfigError, match=r"no saved query named 'c' \(available: a, b\)"):
        config.query("c")


def test_query_gets_global_exclusions_and_keeps_its_own(write):
    path = write(
        MINIMAL
        + "exclusions:\n  ids: [movie:1]\n"
        + "queries:\n  q:\n    text: x\n    exclusions:\n      titles: [parody]\n"
    )
    query = load_config(path).query("q")
    assert query.global_exclusions.ids == {"movie:1"}
    assert query.exclusions.titles == ("parody",)


# --- reading the file ---


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml_reports_the_line(write):
    # A tab used for indentation on line 2 (YAML only allows spaces).
    with pytest.raises(ConfigError, match="not valid YAML: line 2"):
        load_config(write("sources:\n\ttmdb:\n    token: abc\n"))


@pytest.mark.parametrize("text", ["- a\n- b\n", "just text\n", ""])
def test_top_level_must_be_a_mapping(write, text):
    with pytest.raises(ConfigError, match=r"\(a YAML mapping\) at the top level"):
        load_config(write(text))


def test_utf8_file_with_accents_loads(write):
    config = load_config(write(MINIMAL + "exclusions:\n  titles: [Noël, Mañana]\n"))
    assert config.exclusions.titles == ("Noël", "Mañana")


def test_windows_line_endings_are_fine(write):
    config = load_config(write(MINIMAL.replace("\n", "\r\n")))
    assert config.sources.tmdb.token == "abc"


# --- where the config file is ---


def test_resolve_path_order(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit.yaml"
    monkeypatch.setenv("TITULARR_CONFIG", str(tmp_path / "from-env.yaml"))
    assert resolve_config_path(explicit) == explicit
    assert resolve_config_path(None) == tmp_path / "from-env.yaml"
    monkeypatch.delenv("TITULARR_CONFIG")
    assert resolve_config_path(None) == Path("config.yaml")


# --- radarr / sonarr sections ---


def test_arr_sections_are_optional(write):
    config = load_config(write(MINIMAL))
    assert config.radarr is None and config.sonarr is None
    assert config.warnings == ()


def test_arr_sections_load(write, monkeypatch):
    monkeypatch.setenv("RADARR_API_KEY", "r-key")
    text = (
        MINIMAL
        + "radarr:\n  url: http://radarr:7878/\n  api_key: ${RADARR_API_KEY}\n"
        + "sonarr:\n  url: https://host/sonarr\n  api_key: s-key\n"
    )
    config = load_config(write(text))

    assert config.radarr.url == "http://radarr:7878"  # trailing slash dropped
    assert config.radarr.api_key == "r-key"
    assert config.sonarr.url == "https://host/sonarr"
    assert config.warnings == ()


@pytest.mark.parametrize(
    ("section", "problem"),
    [
        ("radarr:\n  url: http://radarr:7878\n", "api_key is missing"),
        ("radarr:\n  api_key: k\n", "url is missing"),
        ("radarr:\n  url: ''\n  api_key: k\n", "url is missing"),
        ("radarr:\n  url: http://r\n  api_key: '  '\n", "api_key is missing"),
        ("radarr:\n", "it has no settings"),
        (
            "radarr:\n  url: http://r\n  api_key: ${UNSET_RADARR_KEY}\n",
            "UNSET_RADARR_KEY is not set",
        ),
    ],
)
def test_unusable_arr_section_is_a_warning(write, monkeypatch, section, problem):
    monkeypatch.delenv("UNSET_RADARR_KEY", raising=False)
    config = load_config(write(MINIMAL + section))

    assert config.radarr is None
    assert config.warnings == (
        (
            f"radarr is in the config but can't be used ({problem}); "
            "movies won't be checked against your library"
        ),
    )


def test_empty_env_variable_counts_as_empty(write, monkeypatch):
    monkeypatch.setenv("EMPTY_KEY", "")
    config = load_config(write(MINIMAL + "sonarr:\n  url: http://s\n  api_key: ${EMPTY_KEY}\n"))
    assert config.sonarr is None
    assert "api_key is empty" in config.warnings[0]
    assert "series won't be checked" in config.warnings[0]


def test_unset_variable_outside_arr_sections_is_still_an_error(write, monkeypatch):
    monkeypatch.delenv("UNSET_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="UNSET_TOKEN is not set"):
        load_config(write(MINIMAL.replace("abc", "${UNSET_TOKEN}")))


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ("radarr:\n  url: radarr:7878\n  api_key: k\n", "url must start with http:// or https://"),
        (
            "radarr:\n  url: http://r\n  apikey: k\n",
            "radarr.apikey: Extra inputs are not permitted",
        ),
        ("radarr: http://r\n", "radarr"),
    ],
)
def test_malformed_arr_section_is_still_an_error(write, section, message):
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(MINIMAL + section))
    assert message in str(excinfo.value)


def test_warnings_cant_be_set_from_the_file(write):
    with pytest.raises(ConfigError, match="warnings: Extra inputs are not permitted"):
        load_config(write(MINIMAL + "warnings: [hi]\n"))


# --- adding to Radarr ---

RADARR = "radarr:\n  url: http://radarr:7878\n  api_key: k\n"


def test_radarr_add_settings_default_to_safe_values(write):
    config = load_config(write(MINIMAL + RADARR))
    assert config.radarr_add() == RadarrAdd(
        quality_profile=None,
        root_folder=None,
        monitored=True,
        minimum_availability="released",
        search_on_add=False,
    )


def test_no_radarr_section_means_no_add_settings(write):
    assert load_config(write(MINIMAL)).radarr_add() is None


def test_saved_query_overrides_only_what_it_sets(write):
    text = (
        MINIMAL
        + RADARR
        + "  quality_profile: HD-1080p\n  root_folder: /movies\n  search_on_add: true\n"
        + "queries:\n"
        + "  kids:\n    text: christmas\n    radarr:\n      root_folder: /kids\n"
        + "  plain:\n    text: christmas\n"
    )
    config = load_config(write(text))

    kids = config.radarr_add("kids")
    assert (kids.quality_profile, kids.root_folder, kids.search_on_add) == (
        "HD-1080p",
        "/kids",
        True,
    )
    assert config.radarr_add("plain") == config.radarr_add()
    assert config.query("kids").text == "christmas"


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        (RADARR + "  quality_profiles: Any\n", "radarr.quality_profiles: Extra inputs"),
        (RADARR + "  minimum_availability: soon\n", "radarr.minimum_availability"),
        (
            "queries:\n  q:\n    text: a\n    radarr:\n      url: http://other\n",
            "queries.q.radarr.url: Extra inputs",
        ),
    ],
)
def test_invalid_add_settings_name_the_field(write, yaml_text, message):
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(MINIMAL + yaml_text))
    assert message in str(excinfo.value)


def test_unusable_radarr_section_with_add_settings_is_still_a_warning(write, monkeypatch):
    monkeypatch.delenv("UNSET_RADARR_KEY", raising=False)
    section = "radarr:\n  url: http://r\n  api_key: ${UNSET_RADARR_KEY}\n  root_folder: /movies\n"
    config = load_config(write(MINIMAL + section))

    assert config.radarr is None
    assert "UNSET_RADARR_KEY is not set" in config.warnings[0]


# --- adding to Sonarr ---

SONARR = "sonarr:\n  url: http://sonarr:8989\n  api_key: k\n"


def test_sonarr_add_settings_default_to_sonarrs_own(write):
    config = load_config(write(MINIMAL + SONARR))
    assert config.sonarr_add() == SonarrAdd(
        quality_profile=None,
        root_folder=None,
        monitor="all",
        season_folder=True,
        series_type="standard",
        monitor_new_items="all",
        search_on_add=False,
    )


def test_no_sonarr_section_means_no_add_settings(write):
    assert load_config(write(MINIMAL + RADARR)).sonarr_add() is None


def test_saved_query_overrides_sonarr_settings_too(write):
    text = (
        MINIMAL
        + SONARR
        + "  quality_profile: HD-1080p\n  root_folder: /tv\n"
        + "queries:\n"
        + "  anime:\n    text: gundam\n    sonarr:\n      series_type: anime\n"
        + "      root_folder: /anime\n"
    )
    config = load_config(write(text))

    anime = config.sonarr_add("anime")
    assert (anime.quality_profile, anime.root_folder, anime.series_type, anime.monitor) == (
        "HD-1080p",
        "/anime",
        "anime",
        "all",
    )
    assert config.radarr_add("anime") is None  # no radarr section


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        (SONARR + "  monitor: everything\n", "sonarr.monitor"),
        (SONARR + "  series_type: cartoon\n", "sonarr.series_type"),
        (SONARR + "  minimum_availability: released\n", "sonarr.minimum_availability: Extra"),
        (
            "queries:\n  q:\n    text: a\n    sonarr:\n      monitored: true\n",
            "queries.q.sonarr.monitored: Extra inputs",
        ),
    ],
)
def test_invalid_sonarr_settings_name_the_field(write, yaml_text, message):
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(MINIMAL + yaml_text))
    assert message in str(excinfo.value)


def test_unusable_sonarr_section_with_add_settings_is_still_a_warning(write, monkeypatch):
    monkeypatch.delenv("UNSET_SONARR_KEY", raising=False)
    section = "sonarr:\n  url: http://s\n  api_key: ${UNSET_SONARR_KEY}\n  monitor: future\n"
    config = load_config(write(MINIMAL + section))

    assert config.sonarr is None
    assert "UNSET_SONARR_KEY is not set" in config.warnings[0]


# --- tags ---


def test_query_tags_replace_the_sections(write):
    text = (
        MINIMAL
        + SONARR
        + "  tags: [english, hevc]\n"
        + "queries:\n"
        + "  anime:\n    text: gundam\n    sonarr:\n      tags: [Anime]\n"
        + "  untagged:\n    text: gundam\n    sonarr:\n      tags: []\n"
        + "  plain:\n    text: gundam\n"
    )
    config = load_config(write(text))

    assert config.sonarr_add("anime").tags == ("anime",)
    assert config.sonarr_add("untagged").tags == ()
    assert config.sonarr_add("plain").tags == ("english", "hevc")


def test_tags_must_be_a_list(write):
    with pytest.raises(ConfigError, match="radarr.tags"):
        load_config(write(MINIMAL + RADARR + "  tags: anime\n"))
