"""ECMWF IFS Open Data: cycle discovery, retrieval and bbox-cropped GRIB decoding.

Deliberately small and self-contained: the operational loop needs (a) a cheap
control-run precipitation screen and (b) an ensemble pull when screening triggers.
Control (member 0) is stream ``oper``/``fc``; perturbed members are ``enfo``/``pf``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

log = logging.getLogger("floodcast.operational.ifs")

INSTANT_FIELDS = ("2t", "2d", "10u", "10v", "sp")
ACCUM_FIELDS = ("tp", "ssrd", "strd")
ALL_FIELDS = INSTANT_FIELDS + ACCUM_FIELDS


class IfsUnavailable(RuntimeError):
    """The requested cycle/fields are not (yet) available from the provider."""


@dataclass(frozen=True)
class Bbox:
    west: float
    south: float
    east: float
    north: float


@dataclass
class FieldStack:
    """All decoded fields of one variable for one member on the cropped grid."""

    short_name: str
    member: int
    steps_h: np.ndarray  # (T,) forecast hours
    lats: np.ndarray  # (Y,) descending (north first)
    lons: np.ndarray  # (X,) ascending, -180..180
    data: np.ndarray  # (T, Y, X) native units (tp m, ssrd/strd J m-2, 2t/2d K, wind m/s, sp Pa)


def lead_hours(max_lead_h: int, *, first: int = 0) -> list[int]:
    """IFS Open Data output steps: 3-hourly to 144 h, 6-hourly beyond."""
    steps = list(range(first, min(max_lead_h, 144) + 1, 3))
    steps += list(range(150, max_lead_h + 1, 6)) if max_lead_h > 144 else []
    return steps


def _client(source: str, resolution: str, *, maximum_retries: int = 3):
    from ecmwf.opendata import Client

    return Client(
        source=source,
        model="ifs",
        resol=resolution,
        infer_stream_keyword=False,
        maximum_retries=maximum_retries,
        retry_after=5,
    )


def latest_cycle(*, source: str, resolution: str, max_lead_h: int, ensemble: bool) -> datetime:
    """Newest cycle for which the farthest lead we need already exists."""
    client = _client(source, resolution)
    request = dict(
        type="pf" if ensemble else "fc",
        stream="enfo" if ensemble else "oper",
        param="tp",
        step=max_lead_h,
        levtype="sfc",
    )
    try:
        stamp = client.latest(**request)
    except Exception as exc:  # network or no-data: caller decides
        raise IfsUnavailable(f"cannot determine latest IFS cycle: {type(exc).__name__}") from exc
    return stamp.replace(tzinfo=UTC)


def cycle_is_available(cycle: datetime, *, source: str, resolution: str, max_lead_h: int,
                       ensemble: bool) -> bool:
    try:
        newest = latest_cycle(
            source=source, resolution=resolution, max_lead_h=max_lead_h, ensemble=ensemble
        )
    except IfsUnavailable:
        return False
    return newest >= cycle


def mirrors_for(source: str) -> tuple[str, ...]:
    """Other copies of the same IFS Open Data, tried when the primary source fails/limits."""
    return tuple(s for s in ("aws", "azure", "google", "ecmwf") if s != source)


STALL_TIMEOUT_S = (30, 120)   # (connect, seconds without any data) for every Open Data download


def _install_stall_timeout() -> None:
    """ecmwf-opendata downloads through multiurl without a timeout, so a connection that stops
    sending data (seen 23 Nov 2026: 19:43 -> 09:14) blocks the loop forever. Give every multiurl
    download a read timeout instead; the stall then raises and the retries below take over."""
    try:
        import multiurl.base as mb
    except ImportError:
        return
    if getattr(mb.DownloaderBase.__init__, "_fc_stall", False):
        return
    orig = mb.DownloaderBase.__init__

    def __init__(self, *args, timeout=None, **kwargs):
        orig(self, *args, timeout=STALL_TIMEOUT_S if timeout is None else timeout, **kwargs)

    __init__._fc_stall = True
    mb.DownloaderBase.__init__ = __init__


def _retrieve(clients: list, request: dict, target: Path, retries: int) -> None:
    """Try each mirror in turn, with exponential backoff between rounds (503 SlowDown).

    ``clients`` are zero-argument factories: some mirrors (azure) contact an authorisation
    service when the client is built, which can itself be rate limited.
    """
    _install_stall_timeout()
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        for factory in clients:
            try:
                factory().retrieve(request=request, target=str(target))
                return
            except Exception as exc:  # SDK errors may embed signed URLs: keep the type only
                last = exc
                log.warning("IFS retrieve attempt %d/%d failed: %s", attempt, retries,
                            type(exc).__name__)
        time.sleep(min(90, 5 * 2 ** (attempt - 1)))
    raise IfsUnavailable(f"IFS retrieval failed after {retries} attempts: {type(last).__name__}")


def fetch(
    cycle: datetime,
    *,
    members: Iterable[int],
    fields: Iterable[str],
    max_lead_h: int,
    workdir: Path,
    source: str = "ecmwf",
    resolution: str = "0p25",
    retries: int = 3,
    fallback_sources: tuple[str, ...] = (),
) -> list[Path]:
    """Download GRIB files (one per role/field) and return their paths. Idempotent."""
    members = sorted(set(members))
    fields = list(fields)
    workdir.mkdir(parents=True, exist_ok=True)
    first = 1 if fallback_sources else 3
    clients = [lambda: _client(source, resolution, maximum_retries=first)]
    clients += [lambda src=src: _client(src, resolution, maximum_retries=1)
                for src in fallback_sources]
    base = {"date": cycle.strftime("%Y%m%d"), "time": cycle.hour, "levtype": "sfc"}
    paths: list[Path] = []
    for field in fields:
        first = 3 if field in ACCUM_FIELDS else 0
        steps = lead_hours(max_lead_h, first=first)
        roles = []
        if 0 in members:
            roles.append(("control", {"stream": "oper", "type": "fc"}))
        perturbed = [m for m in members if m != 0]
        if perturbed:
            roles.append(("perturbed", {"stream": "enfo", "type": "pf", "number": perturbed}))
        for role, selection in roles:
            # the member list is part of the file name: a cache written for fewer members is not reused
            tag = f"_n{len(selection.get('number', [])) + 1}" if role == "perturbed" else ""
            target = workdir / f"{cycle:%Y%m%d%H}_{role}_{field}{tag}.grib2"
            if target.exists() and target.stat().st_size > 0:
                paths.append(target)
                continue
            partial = target.with_suffix(".part")
            _retrieve(clients, {**base, **selection, "param": [field], "step": steps}, partial,
                      retries)
            partial.replace(target)
            paths.append(target)
    return paths


def _wrap_lon(lon: np.ndarray) -> np.ndarray:
    return np.where(lon > 180.0, lon - 360.0, lon)


def read_grib(paths: Iterable[Path], bbox: Bbox) -> dict[tuple[str, int], FieldStack]:
    """Decode GRIB files, crop to ``bbox`` and stack by (shortName, member)."""
    import eccodes as ec

    pending: dict[tuple[str, int], dict[int, np.ndarray]] = {}
    grids: dict[tuple[str, int], tuple[np.ndarray, np.ndarray]] = {}
    for path in paths:
        with Path(path).open("rb") as handle:
            while (msg := ec.codes_grib_new_from_file(handle)) is not None:
                try:
                    short = ec.codes_get(msg, "shortName")
                    step = int(ec.codes_get(msg, "endStep"))
                    try:
                        member = int(ec.codes_get(msg, "number"))
                    except ec.KeyValueNotFoundError:
                        member = 0
                    if ec.codes_get(msg, "dataType") == "cf":
                        member = 0
                    ni, nj = ec.codes_get(msg, "Ni"), ec.codes_get(msg, "Nj")
                    lats = ec.codes_get_array(msg, "latitudes").reshape(nj, ni)[:, 0]
                    lons = _wrap_lon(ec.codes_get_array(msg, "longitudes").reshape(nj, ni)[0, :])
                    values = ec.codes_get_values(msg).reshape(nj, ni)
                finally:
                    ec.codes_release(msg)
                ymask = (lats >= bbox.south) & (lats <= bbox.north)
                xmask = (lons >= bbox.west) & (lons <= bbox.east)
                if not ymask.any() or not xmask.any():
                    raise IfsUnavailable("bbox does not intersect the IFS grid")
                key = (short, member)
                pending.setdefault(key, {})[step] = values[np.ix_(ymask, xmask)]
                grids[key] = (lats[ymask], lons[xmask])
    stacks: dict[tuple[str, int], FieldStack] = {}
    for key, per_step in pending.items():
        steps = sorted(per_step)
        stacks[key] = FieldStack(
            short_name=key[0],
            member=key[1],
            steps_h=np.array(steps, dtype=float),
            lats=grids[key][0],
            lons=grids[key][1],
            data=np.stack([per_step[s] for s in steps]),
        )
    return stacks


def cycle_candidates(now: datetime, cycles: Iterable[int], *, max_age_h: int) -> list[datetime]:
    """Synoptic cycles from newest to oldest within ``max_age_h`` of ``now`` (UTC)."""
    now = now.astimezone(UTC)
    out = []
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for back in range(0, max_age_h // 24 + 2):
        for hour in sorted(cycles, reverse=True):
            stamp = day - timedelta(days=back) + timedelta(hours=hour)
            if stamp <= now and (now - stamp) <= timedelta(hours=max_age_h):
                out.append(stamp)
    return sorted(out, reverse=True)
