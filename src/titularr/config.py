"""The YAML config file: the TMDB token, Radarr/Sonarr connections, global exclusions
and saved queries.

String values may reference environment variables as ${NAME}; that's how secrets such
as API keys stay out of the file. See config.example.yaml.
"""

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
)

from titularr.arr import RadarrAdd, SonarrAdd
from titularr.exclusions import Exclusions
from titularr.pipeline import Query

CONFIG_ENV_VAR = "TITULARR_CONFIG"
DEFAULT_CONFIG_PATH = Path("config.yaml")

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_QUERY_NAME = re.compile(r"[A-Za-z0-9_-]+")


class ConfigError(Exception):
    """The config file is missing, unreadable or invalid. The message says what to fix."""


class _Strict(BaseModel):
    # Unknown keys are errors, so typos are caught instead of silently ignored.
    model_config = ConfigDict(frozen=True, extra="forbid")


class TmdbSettings(_Strict):
    token: str = Field(min_length=1)


class Sources(_Strict):
    tmdb: TmdbSettings


class ArrSettings(_Strict):
    """How to reach Radarr or Sonarr."""

    url: str
    api_key: str = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"url must start with http:// or https:// (got {value!r})")
        return value.rstrip("/")


class RadarrSettings(ArrSettings, RadarrAdd):
    """How to reach Radarr, and how movies are added to it by default."""


class SonarrSettings(ArrSettings, SonarrAdd):
    """How to reach Sonarr, and how series are added to it by default."""


class SavedQuery(Query):
    """A query saved in the config. Its `radarr:` and `sonarr:` settings override the
    radarr and sonarr sections'."""

    radarr: RadarrAdd | None = None
    sonarr: SonarrAdd | None = None


# Which kind of title each app's library covers.
_ARR_KINDS = {"radarr": "movies", "sonarr": "series"}
# The keys each app's section may have.
_ARR_KEYS = {"radarr": set(RadarrSettings.model_fields), "sonarr": set(SonarrSettings.model_fields)}


class Config(_Strict):
    sources: Sources
    radarr: RadarrSettings | None = None
    sonarr: SonarrSettings | None = None
    # Applies to every query, as a separate "global" layer next to the query's own.
    exclusions: Exclusions = Exclusions()
    queries: dict[str, SavedQuery] = {}

    # Problems that don't stop the config from loading (e.g. a radarr section that
    # can't be used). Set by load_config, not from the file.
    _warnings: tuple[str, ...] = PrivateAttr(default=())

    @property
    def warnings(self) -> tuple[str, ...]:
        return self._warnings

    @field_validator("queries")
    @classmethod
    def _check_queries(cls, value: dict[str, SavedQuery]) -> dict[str, SavedQuery]:
        for name, query in value.items():
            if not _QUERY_NAME.fullmatch(name):
                raise ValueError(f"query name {name!r} may only use letters, digits, '-' and '_'")
            if query.global_exclusions.active:
                raise ValueError(
                    f"query {name!r}: global exclusions belong in the top-level "
                    "'exclusions:' section, not inside a query"
                )
        return value

    def query(self, name: str) -> Query:
        """The saved query `name`, with the config's exclusions as its global layer."""
        try:
            saved = self.queries[name]
        except KeyError:
            available = ", ".join(sorted(self.queries)) or "none saved"
            raise ConfigError(f"no saved query named {name!r} (available: {available})") from None
        return saved.model_copy(update={"global_exclusions": self.exclusions})

    def radarr_add(self, query_name: str | None = None) -> RadarrAdd | None:
        """How to add movies: the radarr section's settings, with the saved query's
        overrides on top. None if there's no usable radarr section."""
        override = self.queries[query_name].radarr if query_name is not None else None
        return _merged(RadarrAdd, self.radarr, override)

    def sonarr_add(self, query_name: str | None = None) -> SonarrAdd | None:
        """How to add series: the sonarr section's settings, with the saved query's
        overrides on top. None if there's no usable sonarr section."""
        override = self.queries[query_name].sonarr if query_name is not None else None
        return _merged(SonarrAdd, self.sonarr, override)


def _merged[A: BaseModel](
    model: type[A], section: BaseModel | None, override: A | None
) -> A | None:
    """The add settings in an app's `section`, with `override` on top."""
    if section is None:
        return None
    add = model.model_validate(section.model_dump(include=set(model.model_fields)))
    if override is not None:
        # Only the settings the query actually sets replace the section's.
        add = add.model_copy(update=override.model_dump(exclude_unset=True))
    return add


def resolve_config_path(explicit: Path | None = None) -> Path:
    """The config file to use: `explicit`, then $TITULARR_CONFIG, then ./config.yaml."""
    if explicit is not None:
        return explicit
    if from_env := os.environ.get(CONFIG_ENV_VAR):
        return Path(from_env)
    return DEFAULT_CONFIG_PATH


def load_config(path: Path) -> Config:
    """Read, expand ${NAME} references in, and validate the config file at `path`."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except OSError as e:
        raise ConfigError(f"can't read config file {path}: {e}") from e

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {_yaml_problem(e)}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path} should contain settings (a YAML mapping) at the top level")

    # Radarr/Sonarr sections that are present but can't be used (missing settings or
    # unset variables) become warnings, so the rest of Titularr still works.
    warnings: list[str] = []
    for app, kinds in _ARR_KINDS.items():
        if app in data and (problem := _unusable(data[app], _ARR_KEYS[app])):
            warnings.append(
                f"{app} is in the config but can't be used ({problem}); "
                f"{kinds} won't be checked against your library"
            )
            del data[app]

    # Expanded after parsing, so a value containing YAML-special characters (as a
    # secret might) can't change the file's structure.
    data = _expand_env(data, path)
    try:
        config = Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"{path} has invalid settings:\n{_describe(e)}") from e
    config._warnings = tuple(warnings)
    return config


def _unusable(section: Any, keys: set[str]) -> str | None:
    """Why a radarr/sonarr section can't be used, or None if it can (or is malformed:
    unknown keys and bad values are left for validation to report as errors)."""
    if section is None:
        return "it has no settings"
    if not isinstance(section, dict) or set(section) - keys:
        return None
    problems = []
    for key in ("url", "api_key"):
        value = section.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            problems.append(f"{key} is missing")
        elif isinstance(value, str):
            unset = [n for n in _ENV_REFERENCE.findall(value) if n not in os.environ]
            if unset:
                problems.extend(f"{name} is not set" for name in unset)
            elif not _ENV_REFERENCE.sub(lambda m: os.environ[m.group(1)], value).strip():
                problems.append(f"{key} is empty")  # e.g. RADARR_API_KEY set to ""
    return "; ".join(problems) or None


def _expand_env(value: Any, path: Path) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigError(f"{path} uses ${{{name}}}, but {name} is not set")
            return os.environ[name]

        return _ENV_REFERENCE.sub(replace, value)
    if isinstance(value, dict):
        return {key: _expand_env(item, path) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item, path) for item in value]
    return value


def _yaml_problem(error: yaml.YAMLError) -> str:
    mark = getattr(error, "problem_mark", None)
    problem = getattr(error, "problem", None) or str(error)
    return f"line {mark.line + 1}: {problem}" if mark is not None else problem


def _describe(error: ValidationError) -> str:
    lines = []
    for err in error.errors():
        field = ".".join(str(part) for part in err["loc"])
        message = err["msg"].removeprefix("Value error, ")
        lines.append(f"  {field}: {message}" if field else f"  {message}")
    return "\n".join(lines)
