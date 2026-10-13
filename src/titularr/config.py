"""The YAML config file: the TMDB token, global exclusions and saved queries.

String values may reference environment variables as ${NAME}; that's how secrets such
as the TMDB token stay out of the file. See config.example.yaml.
"""

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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


class Config(_Strict):
    sources: Sources
    # Applies to every query, as a separate "global" layer next to the query's own.
    exclusions: Exclusions = Exclusions()
    queries: dict[str, Query] = {}

    @field_validator("queries")
    @classmethod
    def _check_queries(cls, value: dict[str, Query]) -> dict[str, Query]:
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

    # Expanded after parsing, so a value containing YAML-special characters (as a
    # secret might) can't change the file's structure.
    data = _expand_env(data, path)
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"{path} has invalid settings:\n{_describe(e)}") from e


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
