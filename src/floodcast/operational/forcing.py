"""Area-weighted HRU averaging of IFS fields and SUMMA hourly forcing files.

Native IFS semantics are preserved: instantaneous fields are interpolated linearly
to hourly marks, accumulated fields (tp, ssrd, strd) become uniform fluxes inside each
native output interval, so the integral over any native interval is conserved exactly.
Timestamps identify interval starts (SUMMA convention used by Floodcast).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from .ifs import FieldStack

R_EARTH = 6371008.8
# netCDF4/HDF5 is not thread-safe: serialise every file write (ensemble members run in threads).
NC_LOCK = threading.RLock()
SUMMA_VARS = ("airtemp", "airpres", "pptrate", "spechum", "windspd", "SWRadAtm", "LWRadAtm")


@dataclass(frozen=True)
class HruWeights:
    """Sparse cell->HRU weights (rows sum to 1) for a fixed lat/lon crop."""

    hru_ids: np.ndarray  # (H,)
    lats: np.ndarray
    lons: np.ndarray
    matrix: np.ndarray  # (H, Y*X)

    def apply(self, field: np.ndarray) -> np.ndarray:
        """(..., Y, X) -> (..., H)."""
        flat = field.reshape(*field.shape[:-2], -1)
        return flat @ self.matrix.T


def compute_weights(
    hru_geojson: Path, hru_id_field: str, lats: np.ndarray, lons: np.ndarray,
    *, minimum_coverage: float = 0.999,
) -> HruWeights:
    """Intersect 0.25° cell boxes with HRU polygons in an equal-area projection."""
    import geopandas as gpd
    from shapely.geometry import box

    hrus = gpd.read_file(hru_geojson).to_crs(4326)
    dlat = float(np.abs(np.diff(lats)).mean()) if len(lats) > 1 else 0.25
    dlon = float(np.abs(np.diff(lons)).mean()) if len(lons) > 1 else 0.25
    cells = [
        box(lo - dlon / 2, la - dlat / 2, lo + dlon / 2, la + dlat / 2)
        for la in lats
        for lo in lons
    ]
    cell_df = gpd.GeoDataFrame({"cell": range(len(cells))}, geometry=cells, crs=4326)
    # Equal-area for fractions; EPSG:6933 is global cylindrical equal-area.
    hrus_ea, cells_ea = hrus.to_crs(6933), cell_df.to_crs(6933)
    matrix = np.zeros((len(hrus_ea), len(cells)))
    for i, geom in enumerate(hrus_ea.geometry):
        hit = cells_ea.sindex.query(geom, predicate="intersects")
        for j in hit:
            matrix[i, j] = geom.intersection(cells_ea.geometry.iloc[j]).area
        total = matrix[i].sum()
        if total / geom.area < minimum_coverage:
            raise ValueError(
                f"HRU {hrus_ea[hru_id_field].iloc[i]} only {total / geom.area:.4f} covered by "
                "the IFS crop; enlarge the bbox"
            )
        matrix[i] /= total
    return HruWeights(
        hru_ids=hrus_ea[hru_id_field].to_numpy().astype(np.int64),
        lats=lats, lons=lons, matrix=matrix,
    )


def _areal(stack: FieldStack, weights: HruWeights) -> np.ndarray:
    if stack.data.shape[1:] != (len(weights.lats), len(weights.lons)):
        raise ValueError("field grid differs from the grid used for the weights")
    return weights.apply(stack.data)  # (T, H)


def interval_precip_mm(stack_tp: FieldStack, weights: HruWeights) -> tuple[np.ndarray, np.ndarray]:
    """HRU precipitation per native interval: returns (end_steps_h (T-1,), mm (T-1, H)).

    ``tp`` is accumulated from step 0 in metres; the first step (3 h) accumulation is
    included as interval [0, 3].
    """
    acc = _areal(stack_tp, weights) * 1000.0  # m -> mm (kg m-2)
    steps = stack_tp.steps_h
    if steps[0] > 0:
        acc = np.vstack([np.zeros((1, acc.shape[1])), acc])
        steps = np.concatenate([[0.0], steps])
    inc = np.diff(acc, axis=0)
    # Independently packed GRIB accumulations can differ by packing noise (observed up to ~0.07 mm);
    # a larger decrease means corrupt/mixed data.
    if (inc < -0.5).any():
        raise ValueError("accumulated precipitation decreases with lead time")
    return steps[1:], np.clip(inc, 0.0, None)


def _hourly(steps_h: np.ndarray, hours: np.ndarray) -> np.ndarray:
    return hours


def _instant_to_hourly(stack: FieldStack, weights: HruWeights, hours: np.ndarray) -> np.ndarray:
    """Linear interpolation of an instantaneous field to hourly marks -> (len(hours), H)."""
    values = _areal(stack, weights)
    out = np.empty((len(hours), values.shape[1]))
    for h in range(values.shape[1]):
        out[:, h] = np.interp(hours, stack.steps_h, values[:, h])
    return out


def _accum_to_hourly_rate(stack: FieldStack, weights: HruWeights, hours: np.ndarray,
                          scale: float) -> np.ndarray:
    """Accumulated field -> uniform per-interval mean rate on each hourly step (per second)."""
    acc = _areal(stack, weights) * scale
    steps = stack.steps_h
    if steps[0] > 0:
        acc = np.vstack([np.zeros((1, acc.shape[1])), acc])
        steps = np.concatenate([[0.0], steps])
    rate = np.diff(acc, axis=0) / (np.diff(steps)[:, None] * 3600.0)  # per second
    idx = np.clip(np.searchsorted(steps, hours, side="right") - 1, 0, len(rate) - 1)
    return np.clip(rate[idx], 0.0, None)


def specific_humidity(dewpoint_k: np.ndarray, pressure_pa: np.ndarray) -> np.ndarray:
    """Magnus (water) saturation vapour pressure at the dew point -> specific humidity."""
    t_c = dewpoint_k - 273.15
    e_pa = 611.2 * np.exp(17.62 * t_c / (243.12 + t_c))
    e_pa = np.minimum(e_pa, 0.99 * pressure_pa)
    return 0.622 * e_pa / (pressure_pa - 0.378 * e_pa)


def summa_forcing_arrays(
    fields: dict[str, FieldStack], weights: HruWeights, *, lead_end_h: int, lead_start_h: int = 0
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Hourly SUMMA variables for one member: returns (hour offsets (T,), {name: (T, H)})."""
    hours = np.arange(lead_start_h, lead_end_h, 1.0)  # interval starts
    mid = hours + 0.5
    # Interval-centred instantaneous values: average of the two bounding hourly marks.
    def inst(name: str) -> np.ndarray:
        both = _instant_to_hourly(fields[name], weights, np.append(hours, lead_end_h))
        return 0.5 * (both[:-1] + both[1:])

    t2m, d2m, sp = inst("2t"), inst("2d"), inst("sp")
    u10, v10 = inst("10u"), inst("10v")
    out = {
        "airtemp": t2m,
        "airpres": sp,
        "spechum": specific_humidity(d2m, sp),
        "windspd": np.hypot(u10, v10),
        "pptrate": _accum_to_hourly_rate(fields["tp"], weights, mid, 1000.0),  # mm/s = kg m-2 s-1
        "SWRadAtm": _accum_to_hourly_rate(fields["ssrd"], weights, mid, 1.0),  # J m-2 -> W m-2
        "LWRadAtm": _accum_to_hourly_rate(fields["strd"], weights, mid, 1.0),
    }
    return hours, out


def write_summa_forcing(
    path: Path, hru_ids: np.ndarray, start: datetime, hours: np.ndarray,
    variables: dict[str, np.ndarray],
) -> Path:
    """SUMMA forcing NetCDF (dims time, hru); times in seconds since 1970 at interval starts."""
    import netCDF4 as nc

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.nc")
    with NC_LOCK, nc.Dataset(tmp, "w", format="NETCDF4") as ds:
        ds.createDimension("time", len(hours))
        ds.createDimension("hru", len(hru_ids))
        tv = ds.createVariable("time", "f8", ("time",))
        tv.units = "seconds since 1970-01-01 00:00:00"
        tv.calendar = "standard"
        epoch = datetime(1970, 1, 1, tzinfo=start.tzinfo)
        tv[:] = [(start + timedelta(hours=float(h)) - epoch).total_seconds() for h in hours]
        hv = ds.createVariable("hruId", "i8", ("hru",))
        hv[:] = hru_ids
        ds.createVariable("data_step", "f8", ())[...] = 3600.0
        meta = {
            "airtemp": ("K", "air temperature"),
            "airpres": ("Pa", "air pressure"),
            "pptrate": ("kg m-2 s-1", "precipitation rate"),
            "spechum": ("kg kg-1", "specific humidity"),
            "windspd": ("m s-1", "wind speed"),
            "SWRadAtm": ("W m-2", "downward shortwave radiation"),
            "LWRadAtm": ("W m-2", "downward longwave radiation"),
        }
        for name in SUMMA_VARS:
            var = ds.createVariable(name, "f8", ("time", "hru"), fill_value=-9999.0)
            var.units, var.long_name = meta[name]
            var[:, :] = variables[name]
    tmp.replace(path)
    return path
