"""Locate the Git checkout installed by pip without searching parent directories.

PEP 610 records the source directory for ``pip install .``. Only the exact cwd
or that recorded local directory can provide application defaults.
"""

from __future__ import annotations

import json
import tomllib
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname


def is_checkout(path: Path) -> bool:
    """Require project identity and the installation resources, not a folder name."""
    try:
        with (path / "pyproject.toml").open("rb") as stream:
            name = tomllib.load(stream).get("project", {}).get("name")
        return name == "floodcast" and all(
            (path / resource).is_file()
            for resource in ("containers/model-sources.toml", "containers/docker-compose.yaml")
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def installed_checkout() -> Path | None:
    """Read local-directory provenance; downloaded wheels/archives have no checkout."""
    try:
        document = distribution("floodcast").read_text("direct_url.json")
        if document is None:
            return None
        metadata = json.loads(document)
        if not isinstance(metadata, dict) or not isinstance(metadata.get("dir_info"), dict):
            return None
        if not isinstance(metadata.get("url"), str):
            return None
        parsed = urlsplit(metadata["url"])
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
            return None
        if parsed.query or parsed.fragment:
            return None
        path = Path(url2pathname(parsed.path)).resolve()
        return path if is_checkout(path) else None
    except (PackageNotFoundError, OSError, ValueError, TypeError, KeyError):
        return None


def project_defaults(cwd: Path | None = None) -> tuple[Path | None, Path | None]:
    current = (cwd or Path.cwd()).resolve()
    checkout = current if is_checkout(current) else installed_checkout()
    return (checkout, checkout / "data") if checkout else (None, None)
