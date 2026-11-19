"""Fill the per-run templates of the Alcácer do Sal domain (standard library only).

Every template uses ``@@TOKEN@@`` placeholders so the files stay readable by their
model parsers' neighbours and by humans. The functions below validate that no token
is left unfilled; they never touch the model installation or the workflow store.

    from render_templates import render_summa_file_manager, render_mizuroute_config, render_fluxos_modset

``render_fluxos_modset`` fills a *structured* JSON template, so numbers stay numbers.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

DOMAIN_DIR = Path(__file__).resolve().parent
_TOKEN = re.compile(r"@@([A-Z0-9_]+)@@")


def _timestamp(value: datetime | str, *, seconds: bool) -> str:
    if isinstance(value, str):
        return value
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")


def render_text(template: str, values: Mapping[str, object]) -> str:
    """Replace @@KEY@@ tokens; unknown or unfilled tokens are an error, never silently kept."""
    missing = sorted(set(_TOKEN.findall(template)) - set(values))
    if missing:
        raise KeyError(f"template tokens without a value: {', '.join(missing)}")
    return _TOKEN.sub(lambda match: str(values[match.group(1)]), template)


def render_summa_file_manager(
    *,
    start: datetime | str,
    end: datetime | str,
    settings_path: str,
    forcing_path: str,
    output_path: str,
    out_prefix: str,
    init_condition_file: str = "coldState.nc",
    template: Path = DOMAIN_DIR / "summa" / "fileManager.template.txt",
) -> str:
    """Return the text of a SUMMA fileManager.txt. Times are UTC, ``YYYY-MM-DD HH:MM``."""
    return render_text(
        template.read_text(),
        {
            "SIM_START": _timestamp(start, seconds=False),
            "SIM_END": _timestamp(end, seconds=False),
            "SETTINGS_PATH": settings_path.rstrip("/"),
            "FORCING_PATH": forcing_path.rstrip("/"),
            "OUTPUT_PATH": output_path.rstrip("/"),
            "OUT_PREFIX": out_prefix,
            "INIT_CONDITION_FILE": init_condition_file,
        },
    )


def render_mizuroute_config(
    *,
    mizuroute_path: str,
    template: Path = DOMAIN_DIR / "summa" / "mizuroute_config.template.toml",
) -> str:
    """Return the TOML passed to the coupled SUMMA with ``-c``. ``mizuroute_path`` is the directory (as
    seen by the model process) holding ``topology.nc`` and ``param.nml.default``."""
    return render_text(template.read_text(), {"MIZUROUTE_PATH": mizuroute_path.rstrip("/")})


def render_fluxos_modset(
    *,
    comment: str,
    dem_file: str,
    sim_start: datetime | str,
    inflow_file: str,
    output_folder: str,
    inflow_xy: tuple[float, float] | None = None,
    print_step_s: int | None = None,
    roughness_height_m: float | None = None,
    template: Path = DOMAIN_DIR / "fluxos" / "modset.template.json",
) -> dict:
    """Return the FLUXOS modset as a dict (``json.dumps`` it). ``inflow_xy`` defaults to the
    domain's main-stem inflow cell (UTM 29N metres, from fluxos/dem_info.json)."""
    info = json.loads((DOMAIN_DIR / "fluxos" / "dem_info.json").read_text())
    x, y = inflow_xy if inflow_xy is not None else (info["inflow_x"], info["inflow_y"])
    modset = json.loads(template.read_text())
    modset["COMMENT"] = comment
    modset["DEM_FILE"] = dem_file
    modset["SIM_DATETIME_START"] = _timestamp(sim_start, seconds=True)
    modset["INFLOW_FILE"]["FILENAME"] = inflow_file
    modset["INFLOW_FILE"]["DISCHARGE_LOCATION"]["X_COORDINATE"] = float(x)
    modset["INFLOW_FILE"]["DISCHARGE_LOCATION"]["Y_COORDINATE"] = float(y)
    modset["OUTPUT"]["OUTPUT_FOLDER"] = output_folder
    if print_step_s is not None:
        modset["OUTPUT"]["PRINT_STEP"] = int(print_step_s)
    if roughness_height_m is not None:
        modset["ROUGNESS_HEIGHT"] = float(roughness_height_m)
    leftover = [key for key, value in _walk(modset) if isinstance(value, str) and _TOKEN.search(value)]
    if leftover:
        raise KeyError(f"unfilled modset tokens at: {', '.join(leftover)}")
    return modset


def write_fluxos_inflow(
    path: Path, times_s: list[float], discharge_m3s: list[float], *, resample_s: float | None = 30.0
) -> Path:
    """Write a FLUXOS inflow CSV (time s from the modset start, discharge m3/s, tracer 0).

    FLUXOS skips the first row, so a header row is written there. The last time stamp defines the
    simulated duration, and the value of row k is applied over the interval (t[k-1], t[k]].

    ``resample_s`` (default 30 s) linearly interpolates the series onto a regular grid. This is NOT
    cosmetic: while the domain is still dry, FLUXOS lets one time step span the whole interval to the
    next row, so coarse rows (e.g. hourly mizuRoute output) dump Q*dt into a single cell at once and
    created +12 % volume in the Alcácer test (30 s rows conserved volume to <0.1 %). Pass ``None`` only
    for series that are already fine.
    """
    if len(times_s) != len(discharge_m3s) or len(times_s) < 2:
        raise ValueError("need at least two (time, discharge) pairs of equal length")
    if any(b <= a for a, b in zip(times_s, times_s[1:], strict=False)) or times_s[0] != 0:
        raise ValueError("times must start at 0 and increase strictly")
    if any(q < 0 for q in discharge_m3s):
        raise ValueError("discharge must be non-negative")
    if resample_s is not None:
        if resample_s <= 0:
            raise ValueError("resample_s must be positive")
        grid, t = [], 0.0
        while t < times_s[-1]:
            grid.append(t)
            t += resample_s
        grid.append(float(times_s[-1]))
        series, k = [], 0
        for t in grid:
            while k < len(times_s) - 2 and times_s[k + 1] < t:
                k += 1
            t0, t1, q0, q1 = times_s[k], times_s[k + 1], discharge_m3s[k], discharge_m3s[k + 1]
            series.append(q0 + (q1 - q0) * (t - t0) / (t1 - t0))
        times_s, discharge_m3s = grid, series
    lines = ["t (sec),inflow,concentration"] + [f"{t:g},{q:.6g},0" for t, q in zip(times_s, discharge_m3s, strict=True)]
    path.write_text("\n".join(lines) + "\n")
    return path


def _walk(node: object, prefix: str = ""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, f"{prefix}{key}.")
    else:
        yield prefix.rstrip("."), node
