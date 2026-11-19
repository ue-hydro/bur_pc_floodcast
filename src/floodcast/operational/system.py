"""Assemble the operational system from a configuration file."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import ifs
from .config import OperationalConfig
from .cycle import CycleProcessor
from .daemon import Daemon, setup_logging
from .domain import Domain
from .engines import STATE_STEP_H, SummaMizuEngine
from .forcing import HruWeights, compute_weights
from .hydraulics import FluxosEngine
from .runners import ModelRunner
from .store import OperationalStore, read_json

log = logging.getLogger("floodcast.operational")


def build_processor(config: OperationalConfig) -> tuple[Domain, OperationalStore, CycleProcessor]:
    domain = Domain.load(config.domain)
    store = OperationalStore(config.data_root, domain.domain_id, name=domain.name)
    runner = ModelRunner(config.models, data_root=config.data_root)
    processor = CycleProcessor(
        config, domain, store,
        hydrology=SummaMizuEngine(config, domain, runner),
        hydraulics=FluxosEngine(config, domain, runner),
    )
    return domain, store, processor


def parse_cycle(text: str) -> datetime:
    """``2026-11-18T12`` / ``2026111812`` / ISO -> aware UTC datetime on a synoptic hour."""
    text = text.strip().replace("Z", "")
    for fmt in ("%Y%m%d%H", "%Y-%m-%dT%H", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError(f"cannot parse cycle {text!r}")


def spinup(config: OperationalConfig, *, end: datetime | None = None, days: int | None = None,
           source: str = "aws", workers: int = 3) -> Path:
    """Warm the SUMMA state from archived IFS Open Data (aws/azure mirrors keep history)."""
    domain, _, processor = build_processor(config)
    engine: SummaMizuEngine = processor.hydrology  # type: ignore[assignment]
    days = days or config.hydrology.spinup_days
    if end is None:
        end = ifs.latest_cycle(
            source=config.forecast.source, resolution=config.forecast.resolution,
            max_lead_h=config.forecast.screening_max_lead_h, ensemble=False,
        )
    cache = config.data_root / "operational" / domain.domain_id / "cache" / "spinup"
    bbox = domain.bbox_tuple()
    fc = config.forecast
    weights_holder: dict[str, HruWeights] = {}

    def fetch(cycle: datetime):
        paths = ifs.fetch(
            cycle, members=[0], fields=ifs.ALL_FIELDS, max_lead_h=STATE_STEP_H,
            workdir=cache / f"{cycle:%Y%m%d%H}", source=source, resolution=fc.resolution,
            retries=max(fc.retries, 6),
            fallback_sources=tuple(s for s in ("aws", "azure", "google") if s != source),
        )
        stacks = ifs.read_grib(paths, bbox)
        return {k[0]: v for k, v in stacks.items() if k[1] == 0}

    cycles = []
    t = end - timedelta(days=days)
    while t < end:
        cycles.append(t)
        t += timedelta(hours=STATE_STEP_H)
    log.info("spin-up: fetching %d archived cycles from %s", len(cycles), source)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        decoded = dict(zip(cycles, pool.map(fetch, cycles), strict=True))

    def fetch_cycle(cycle: datetime):
        fields = decoded.pop(cycle)
        if "w" not in weights_holder:
            tp = fields["tp"]
            weights_holder["w"] = compute_weights(
                domain.hrus_geojson, domain.hru_id_field, tp.lats, tp.lons
            )
        return fields, weights_holder["w"]

    path = engine.spinup(end, days, fetch_cycle)
    import shutil

    shutil.rmtree(cache, ignore_errors=True)
    return path


def status(config: OperationalConfig) -> dict:
    domain = Domain.load(config.domain)
    store = OperationalStore(config.data_root, domain.domain_id, name=domain.name)
    return {
        "latest": read_json(store.root / "latest.json"),
        "health": read_json(store.root / "health.json"),
        "history": (read_json(store.root / "history.json", []) or [])[:5],
    }


__all__ = ["Daemon", "build_processor", "parse_cycle", "setup_logging", "spinup", "status"]
