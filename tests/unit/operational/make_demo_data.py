"""Synthetic operational output directory following docs/operational/outputs.md."""

from __future__ import annotations

import json
import math
import struct
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

CYCLE = datetime(2026, 11, 18, 12, tzinfo=UTC)
BOUNDS = {"west": -8.545, "south": 38.345, "east": -8.475, "north": 38.395}
_RAMP = ((0.1, (158, 202, 225)), (0.5, (66, 146, 198)), (1.0, (33, 113, 181)), (2.0, (8, 48, 107)))


def _iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_png(path: Path, rgba_rows: list[bytes], width: int, height: int) -> None:
    raw = b"".join(b"\x00" + row for row in rgba_rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def write_depth_png(path: Path, width: int = 120, height: int = 90) -> None:
    rows = []
    for j in range(height):
        row = bytearray()
        for i in range(width):
            # a sinuous channel with decaying depth away from it
            centre = height * (0.5 + 0.18 * math.sin(i / width * 2 * math.pi))
            depth = 2.6 * math.exp(-abs(j - centre) / 7.0) - 0.15 * (i / width)
            colour = None
            for limit, rgb in _RAMP:
                if depth >= limit:
                    colour = rgb
            row += bytes((*colour, 200)) if colour else b"\x00\x00\x00\x00"
        rows.append(bytes(row))
    write_png(path, rows, width, height)


def _hydrograph(times: int, peak: float, member_scale: float, shift: int) -> list[float]:
    values = []
    for i in range(times):
        x = (i - 10 - shift) / 6.0
        values.append(15.0 + peak * member_scale * math.exp(-0.5 * x * x) if x > -2 else 15.0)
    return values


def write_demo_operational_dir(
    path: Path,
    *,
    level: str = "warning",
    hydrology: bool = True,
    hydraulics: bool = True,
    failed: bool = False,
    uncalibrated: bool = True,
    updated_at: datetime | None = None,
    cycle: datetime = CYCLE,
    previous_cycles: int = 2,
) -> Path:
    """Write latest.json, history.json and ``previous_cycles + 1`` cycle directories."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    updated = updated_at or cycle + timedelta(hours=6, minutes=41)
    if failed:
        level = "unknown"
    cycle_id = cycle.strftime("%Y%m%d%H")
    history = []

    for back in range(previous_cycles, 0, -1):
        old = cycle - timedelta(hours=12 * back)
        _write_cycle(path, old, level="none", hydrology=False, hydraulics=False, failed=False)
        history.append({"cycle": _iso(old), "level": "none", "message": "No action needed."})
    _write_cycle(path, cycle, level=level, hydrology=hydrology and not failed, hydraulics=hydraulics and not failed, failed=failed)
    history.append({"cycle": _iso(cycle), "level": level, "message": _message(level, failed)})
    history.reverse()

    labels = {"none": "No action", "watch": "Watch", "warning": "Warning", "alert": "Alert", "unknown": "Unknown"}
    (path / "latest.json").write_text(
        json.dumps(
            {
                "domain_id": "alcacer_do_sal",
                "name": "Alcácer do Sal (Sado)",
                "cycle": _iso(cycle),
                "updated_at": _iso(updated),
                "level": level,
                "level_label": labels[level],
                "message": _message(level, failed),
                "path": f"cycles/{cycle_id}/status.json",
                "system": {"version": "0.0.1.dev1", "uncalibrated": uncalibrated},
            },
            indent=2,
        )
    )
    (path / "history.json").write_text(json.dumps(history, indent=2))
    return path


def _message(level: str, failed: bool) -> str:
    if failed:
        return "Forecast cycle failed: IFS data incomplete."
    return {
        "none": "No significant rainfall or flow expected.",
        "watch": "Rainfall above the watch threshold expected on Thursday.",
        "warning": "Ensemble flow at Alcácer do Sal likely to exceed the warning threshold.",
        "alert": "High flood risk at Alcácer do Sal.",
    }[level]


def _write_cycle(root: Path, cycle: datetime, *, level, hydrology, hydraulics, failed) -> None:
    cdir = root / "cycles" / cycle.strftime("%Y%m%d%H")
    cdir.mkdir(parents=True, exist_ok=True)
    times = [cycle + timedelta(hours=3 * (i + 1)) for i in range(32)]
    rng = np.random.default_rng(int(cycle.timestamp()) % 1000)
    base = 4.0 if level in ("watch", "warning", "alert") else 0.3
    interval = [max(0.0, float(base * math.exp(-0.5 * ((i - 12) / 5.0) ** 2) * (0.6 + rng.random()))) for i in range(32)]
    cumulative = list(np.cumsum(interval))
    windows = [
        {"hours": 24, "max_areal_mean_mm": round(sum(interval[8:16]), 1), "max_hru_mm": round(sum(interval[8:16]) * 1.7, 1), "start_utc": _iso(times[8])},
        {"hours": 48, "max_areal_mean_mm": round(sum(interval[4:20]), 1), "max_hru_mm": round(sum(interval[4:20]) * 1.6, 1), "start_utc": _iso(times[4])},
    ]
    triggered = hydrology
    (cdir / "screening.json").write_text(
        json.dumps(
            {
                "forecast_cycle": _iso(cycle),
                "member": 0,
                "product": "ifs control 0.25°",
                "areal_mean_precip_mm": {
                    "times_utc": [_iso(t) for t in times],
                    "interval_mm": [round(v, 2) for v in interval],
                    "cumulative_mm": [round(float(v), 2) for v in cumulative],
                },
                "windows": windows,
                "thresholds": {"watch": {"24": 30, "48": 50}, "warning": {"24": 60, "48": 90}},
                "decision": {
                    "outcome": "activate" if triggered else "do_not_activate",
                    "level": "watch" if triggered else "none",
                    "reasons": ["PRECIP_48H_ABOVE_WATCH"] if triggered else ["PRECIP_BELOW_THRESHOLDS"],
                },
            },
            indent=1,
        )
    )
    stages = {
        "screening": {"state": "failed" if failed else "done", "triggered_hydrology": bool(triggered), "reasons": []},
        "hydrology": {"state": "done" if hydrology else ("failed" if failed else "skipped"), "triggered_hydraulics": bool(hydraulics), "reasons": []},
        "hydraulics": {"state": "done" if hydraulics else ("failed" if failed else "skipped"), "scenarios": ["p90"] if hydraulics else []},
    }
    if hydrology:
        _write_hydrology(cdir, cycle, times, level)
    if hydraulics:
        _write_hydraulics(cdir)
    (cdir / "status.json").write_text(
        json.dumps(
            {
                "domain_id": "alcacer_do_sal",
                "cycle": _iso(cycle),
                "issued_at": _iso(cycle + timedelta(hours=6)),
                "finished_at": _iso(cycle + timedelta(hours=6, minutes=41)),
                "level": level,
                "message": _message(level, failed),
                "stages": stages,
                "errors": ["IFS acquisition: 3 members incomplete"] if failed else [],
            },
            indent=1,
        )
    )


def _write_hydrology(cdir: Path, cycle: datetime, times, level: str) -> None:
    (cdir / "hydrology").mkdir(exist_ok=True)
    n = len(times)
    members = 11
    rng = np.random.default_rng(7)
    peak = {"watch": 350.0, "warning": 700.0, "alert": 1200.0}.get(level, 150.0)
    matrix = np.array([_hydrograph(n, peak, 0.5 + rng.random(), int(rng.integers(-2, 3))) for _ in range(members)])
    q = {
        "p10": np.percentile(matrix, 10, axis=0),
        "p50": np.percentile(matrix, 50, axis=0),
        "p90": np.percentile(matrix, 90, axis=0),
        "min": matrix.min(axis=0),
        "max": matrix.max(axis=0),
    }
    thresholds = {"watch": 300.0, "warning": 600.0, "alert": 1000.0}
    peaks = matrix.max(axis=1)
    summary = {
        "outlet": {"seg_id": 123, "name": "Sado at Alcácer do Sal", "area_km2": 6200},
        "members": members,
        "times_utc": [_iso(t) for t in times],
        "quantiles_m3s": {k: [round(float(v), 1) for v in arr] for k, arr in q.items()},
        "peak": {
            "p50_m3s": round(float(q["p50"].max()), 1),
            "p90_m3s": round(float(q["p90"].max()), 1),
            "max_m3s": round(float(q["max"].max()), 1),
            "time_of_p50_peak_utc": _iso(times[int(q["p50"].argmax())]),
        },
        "thresholds_m3s": thresholds,
        "exceedance_probability": {k: round(float((peaks > v).mean()), 2) for k, v in thresholds.items()},
        "decision": {"outcome": "activate" if level in ("warning", "alert") else "do_not_activate", "level": level, "reasons": ["FLOW_P90_ABOVE_WARNING"]},
        "initial_state": {"valid_time_utc": _iso(cycle), "age_hours": 6.0, "spinup": "demo"},
    }
    (cdir / "hydrology" / "summary.json").write_text(json.dumps(summary, indent=1))
    header = "time_utc," + ",".join(f"member_{i}" for i in range(members))
    lines = [header] + [f"{_iso(t)}," + ",".join(f"{matrix[m, i]:.2f}" for m in range(members)) for i, t in enumerate(times)]
    (cdir / "hydrology" / "discharge.csv").write_text("\n".join(lines) + "\n")


def _write_hydraulics(cdir: Path) -> None:
    sdir = cdir / "hydraulics" / "p90"
    sdir.mkdir(parents=True, exist_ok=True)
    write_depth_png(sdir / "max_depth.png")
    (sdir / "summary.json").write_text(
        json.dumps(
            {
                "scenario": "p90",
                "bounds_wgs84": BOUNDS,
                "crs": "EPSG:32629",
                "max_depth_m": 2.6,
                "flooded_area_km2": 3.4,
                "inflow_peak_m3s": 820.0,
                "simulated_hours": 72,
                "image": "max_depth.png",
                "caveats": ["DSM-based terrain, river bathymetry not resolved", "Synthetic demo data"],
            },
            indent=1,
        )
    )
    (sdir / "depth_timeseries.csv").write_text("time_utc,wet_area_km2,max_depth_m,volume_m3\n2026-11-18T18:00:00Z,1.0,0.8,100000\n")
