"""Extra ensemble members from other forecast centres via the Open-Meteo ensemble API.

Open-Meteo serves GEFS (NOAA), ICON-EU-EPS (DWD), GEM (ECCC), ECMWF ENS, ... as point time series.
We request the sub-basin (HRU) centroids and turn each member into hourly SUMMA forcing directly
(``HruForcing``), so the hydrology can run them alongside the native ECMWF members.

Licence: Open-Meteo data are CC-BY 4.0; the free API is for non-commercial use (attribute it).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from .forcing import specific_humidity

log = logging.getLogger("floodcast.operational.openmeteo")

API = "https://ensemble-api.open-meteo.com/v1/ensemble"
# open-meteo model id -> (JSON suffix, display name)
MODELS = {
    "gfs025": ("ncep_gefs025", "NOAA GEFS 0.25°"),
    "icon_eu": ("icon_eu_eps", "DWD ICON-EU-EPS"),
    "icon_global": ("icon_global_eps", "DWD ICON-EPS"),
    "gem_global": ("gem_global_ensemble", "ECCC GEM"),
    "ecmwf_ifs025": ("ecmwf_ifs025_ensemble", "ECMWF IFS ENS"),
}
VARS = ("temperature_2m", "dew_point_2m", "precipitation", "surface_pressure", "wind_speed_10m",
        "shortwave_radiation", "cloud_cover")
SIGMA = 5.670374419e-8
# Some models end a few hours before the requested horizon (e.g. ICON-EU-EPS: 5 days from its own
# run time). Up to this many missing hours at the END are filled by persistence; longer gaps drop
# the member.
MAX_TAIL_FILL_H = 12


def _ffill_tail(a: np.ndarray, max_h: int) -> np.ndarray:
    """Forward-fill trailing NaN rows (time axis 0) when the gap is at most ``max_h`` rows."""
    if not np.isnan(a).any():
        return a
    ok = np.isfinite(a).all(axis=1)
    if not ok.any():
        return a
    last = int(np.where(ok)[0][-1])
    if (len(a) - 1 - last) <= max_h and ok[: last + 1].all():
        a = a.copy()
        a[last + 1:] = a[last]
    return a


@dataclass
class HruForcing:
    """Hourly SUMMA forcing for one member: hours = interval starts from the cycle, arrays (T, H)."""

    source: str
    member: int
    hours: np.ndarray
    arrays: dict[str, np.ndarray]
    hru_ids: tuple = ()

    def for_hrus(self, hru_ids) -> HruForcing:
        """Columns reordered to ``hru_ids`` (the order of the SUMMA forcing file)."""
        idx = [list(self.hru_ids).index(int(h)) for h in hru_ids]
        return HruForcing(self.source, self.member, self.hours, {k: v[:, idx] for k, v in self.arrays.items()},
                          tuple(int(h) for h in hru_ids))

    def truncate(self, lead_end_h: int) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        keep = self.hours < lead_end_h
        return self.hours[keep], {k: v[keep] for k, v in self.arrays.items()}


def longwave_down(t_k: np.ndarray, td_k: np.ndarray, cloud_frac: np.ndarray) -> np.ndarray:
    """Brutsaert (1975) clear-sky emissivity with a cloud correction (Crawford & Duchon-type)."""
    e_hpa = 6.112 * np.exp(17.62 * (td_k - 273.15) / (243.12 + td_k - 273.15))
    eps_clear = 1.24 * np.power(np.clip(e_hpa, 0.01, None) / t_k, 1.0 / 7.0)
    eps = np.clip(eps_clear * (1.0 + 0.22 * np.clip(cloud_frac, 0, 1) ** 2), 0.0, 1.0)
    return eps * SIGMA * t_k ** 4


def _get(params: dict, retries: int = 3) -> list:
    url = API + "?" + urllib.parse.urlencode(params, safe=",:")
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "floodcast-operational"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(data.get("reason", "open-meteo error"))
            return data if isinstance(data, list) else [data]
        except Exception as exc:  # network/limits: back off and retry
            last = exc
            limited = "limit" in str(exc).lower() or "429" in str(exc)
            time.sleep(65 if limited else 5 * (attempt + 1))
    raise RuntimeError(f"Open-Meteo request failed: {type(last).__name__}: {last}")


def fetch_members(cycle: datetime, hru_ids, lats, lons, models: dict[str, int], lead_end_h: int,
                  first_id: int = 100) -> tuple[dict[int, HruForcing], dict]:
    """Members keyed by synthetic ids (100.. GEFS, 200.. ICON, ...); and a summary of what was used."""
    hru_ids = list(hru_ids)
    # One request point per 0.25° cell (the coarsest model grid): fewer points = smaller API cost.
    cells = [(round(la * 4) / 4, round(lo * 4) / 4) for la, lo in zip(lats, lons)]
    uniq = sorted(set(cells))
    col_of = [uniq.index(c) for c in cells]          # HRU -> request point
    lats = [c[0] for c in uniq]
    lons = [c[1] for c in uniq]
    start = cycle.replace(tzinfo=None)
    end = start + timedelta(hours=lead_end_h)
    out: dict[int, HruForcing] = {}
    used: dict = {}
    for k, (model, max_members) in enumerate(models.items()):
        if model not in MODELS or max_members <= 0:
            log.warning("unknown/disabled Open-Meteo model %s", model)
            continue
        suffix, name = MODELS[model]
        try:
            points = _get({
                "latitude": ",".join(f"{v:.4f}" for v in lats),
                "longitude": ",".join(f"{v:.4f}" for v in lons),
                "models": model, "hourly": ",".join(VARS), "wind_speed_unit": "ms",
                "start_hour": start.strftime("%Y-%m-%dT%H:%M"), "end_hour": end.strftime("%Y-%m-%dT%H:%M"),
                "cell_selection": "land", "timeformat": "unixtime",
            })
        except Exception as exc:
            log.warning("%s unavailable: %s", name, exc)
            continue
        if len(points) != len(uniq):
            log.warning("%s: expected %d points, got %d", name, len(uniq), len(points))
            continue
        hourly0 = points[0]["hourly"]
        n_t = len(hourly0["time"])
        if n_t < lead_end_h + 1:
            log.warning("%s: only %d hourly values (need %d)", name, n_t, lead_end_h + 1)
            continue
        # member labels: control (no tag) + member01..NN
        # single-model requests have no model suffix in the keys (e.g. temperature_2m_member01)
        sfx = f"_{suffix}" if any(key.endswith(suffix) for key in hourly0) else ""
        tags = [""] + sorted({key.split("_member")[1].split("_")[0] for key in hourly0
                              if "_member" in key and key.startswith("temperature_2m")})
        kept = 0
        for m, tag in enumerate(tags[:max_members]):
            def series(var: str) -> np.ndarray:  # (T+1, H)
                key = f"{var}_member{tag}{sfx}" if tag else f"{var}{sfx}"
                cols = [np.array([np.nan if v is None else v for v in p["hourly"].get(key, [None] * n_t)], float)
                        for p in points]
                a = np.stack(cols, axis=1)[: lead_end_h + 1][:, col_of]  # back to HRU columns
                return _ffill_tail(a, MAX_TAIL_FILL_H)

            t = series("temperature_2m") + 273.15
            td = series("dew_point_2m") + 273.15
            ps = series("surface_pressure") * 100.0
            ws = series("wind_speed_10m")
            pr = series("precipitation")          # mm in the hour ending at each stamp
            sw = series("shortwave_radiation")    # W m-2 mean over the hour ending at each stamp
            cc = series("cloud_cover") / 100.0
            inst = lambda a: 0.5 * (a[:-1] + a[1:])  # noqa: E731  interval [h, h+1) centre
            arrays = {
                "airtemp": inst(t), "airpres": inst(ps), "spechum": specific_humidity(inst(td), inst(ps)),
                "windspd": inst(ws), "pptrate": np.clip(pr[1:], 0, None) / 3600.0,
                "SWRadAtm": np.clip(sw[1:], 0, None),
                "LWRadAtm": longwave_down(inst(t), inst(td), inst(cc)),
            }
            if any(not np.isfinite(v).all() for v in arrays.values()):
                continue  # incomplete member (model horizon shorter than requested)
            mid = first_id + 100 * k + m
            out[mid] = HruForcing(source=name, member=m, hours=np.arange(lead_end_h, dtype=float), arrays=arrays,
                                  hru_ids=tuple(int(h) for h in hru_ids))
            kept += 1
        used[name] = kept
        log.info("%s: %d members (%d request points)", name, kept, len(uniq))
        time.sleep(2)
    return out, used
