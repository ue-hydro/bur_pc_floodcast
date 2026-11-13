"""Explicit TOML configuration, resolved exactly once at the application boundary."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from floodcast.core.errors import FloodcastError


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    checkout_root: Path | None = None
    data_root: Path
    runtime: Literal["docker", "local"] = "docker"
    scheduler: Literal["local"] = "local"
    registry_lock_timeout_s: StrictInt = Field(default=60, ge=0)
    build_jobs: StrictInt = Field(default=1, gt=0)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_schema(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be integer 1")
        return value

    @model_validator(mode="after")
    def validate_roots(self) -> RuntimeConfig:
        paths = [self.data_root] + ([self.checkout_root] if self.checkout_root else [])
        for path in paths:
            if not path.is_absolute() or path == Path(path.anchor):
                raise ValueError("roots must be absolute directories below a filesystem root")
            if path != path.resolve():
                raise ValueError("roots must be normalized and resolved before use")
            if path.exists() and not path.is_dir():
                raise ValueError(f"root is not a directory: {path}")
        checkout = self.checkout_root
        if checkout:
            if self.data_root == checkout or checkout.is_relative_to(self.data_root):
                raise ValueError("data_root must not contain or equal the checkout")
            if self.data_root.is_relative_to(checkout):
                # The shipped ignore rules reserve data/ for data inside a checkout.
                if not self.data_root.is_relative_to(checkout / "data"):
                    raise ValueError(
                        "data within the checkout must be under its ignored data/ directory"
                    )
        return self

    @property
    def registry_root(self) -> Path:
        return self.data_root / "installations"

    @property
    def runs_root(self) -> Path:
        return self.data_root / "runs"

    @property
    def validation_root(self) -> Path:
        return self.data_root / "validation"

    def require_checkout(self) -> Path:
        if self.checkout_root is None:
            raise FloodcastError("INVALID_CONFIG", "config", "checkout_root is required to build")
        expected = (
            "pyproject.toml",
            "containers/docker-compose.yaml",
            "containers/model-sources.toml",
        )
        missing = [
            str(self.checkout_root / rel)
            for rel in expected
            if not (self.checkout_root / rel).is_file()
        ]
        if missing:
            raise FloodcastError(
                "INVALID_CONFIG", "config", f"Incomplete checkout: {', '.join(missing)}"
            )
        return self.checkout_root


def read_document(path: Path) -> dict:
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FloodcastError(
            "INVALID_CONFIG", "config", f"Cannot read TOML: {path}", cause=str(exc)
        ) from exc
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise FloodcastError("INVALID_CONFIG", "config", "schema_version must be integer 1")
    return document


def load_config(
    config_path: Path | None = None,
    *,
    runtime: str | None = None,
    checkout_root: Path | None = None,
    data_root: Path | None = None,
    environment: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    allow_run: bool = False,
    project_defaults: bool = False,
) -> RuntimeConfig:
    """CLI overrides environment, which overrides one explicitly selected TOML file."""
    base = (cwd or Path.cwd()).resolve()
    env = os.environ if environment is None else environment
    selected = None if config_path is None else (base / config_path).resolve()
    values = {} if selected is None else read_document(selected)
    if allow_run:
        values.pop("run", None)
    if runtime is not None:
        values["runtime"] = runtime
    elif "FLOODCAST_RUNTIME" in env:
        values["runtime"] = env["FLOODCAST_RUNTIME"]
    defaults: dict[str, Path] = {}
    if project_defaults and selected is None:
        from floodcast.core.config.discovery import project_defaults as discover

        default_checkout, _ = discover(base)
        if default_checkout is not None:
            defaults = {"checkout_root": default_checkout}
    for key, override in (("checkout_root", checkout_root), ("data_root", data_root)):
        env_value = env.get(f"FLOODCAST_{key.upper()}")
        if override is not None:
            values[key] = (base / override).resolve()
        elif env_value is not None:
            if not env_value.strip():
                raise FloodcastError(
                    "INVALID_CONFIG", "config", f"FLOODCAST_{key.upper()} is empty"
                )
            values[key] = (base / env_value).resolve()
        elif key in values:
            if not isinstance(values[key], str) or not values[key].strip():
                raise FloodcastError("INVALID_CONFIG", "config", f"{key} must be a nonempty path")
            values[key] = ((selected.parent if selected else base) / values[key]).resolve()
        elif key in defaults:
            values[key] = defaults[key]
    if project_defaults and selected is None and "data_root" not in values:
        # Derive from the effective checkout after explicit/env overrides. Never
        # associate an explicitly selected checkout with a different registry.
        if values.get("checkout_root") is not None:
            values["data_root"] = values["checkout_root"] / "data"
    try:
        return RuntimeConfig.model_validate(values)
    except ValidationError as exc:
        raise FloodcastError("INVALID_CONFIG", "config", str(exc)) from exc
