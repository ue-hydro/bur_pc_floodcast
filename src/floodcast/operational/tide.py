"""Sea level (astronomical tide + surge) forecast at the Sado mouth, from the free Open-Meteo marine
API. Alcácer do Sal is tidal: high water in the estuary holds the river back, so the timing of high
tide against the flow peak matters. (FLUXOS does not yet take it as a downstream boundary: shown on
the site and used in the headline; see docs/operational for the planned boundary condition.)"""

from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

API = "https://marine-api.open-meteo.com/v1/marine"
SADO_MOUTH = (38.47, -8.92)


def fetch(cycle: datetime, hours: int = 72, cache: Path | None = None) -> dict | None:
    start = cycle.replace(tzinfo=None)
    if cache is not None and cache.is_file():
        return json.loads(cache.read_text())
    params = {"latitude": SADO_MOUTH[0], "longitude": SADO_MOUTH[1], "hourly": "sea_level_height_msl",
              "cell_selection": "sea", "timeformat": "unixtime",
              "start_hour": start.strftime("%Y-%m-%dT%H:%M"),
              "end_hour": (start + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")}
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode(params, safe=":"),
                                 headers={"User-Agent": "floodcast-operational"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        d = json.loads(resp.read().decode("utf-8"))
    h = d.get("hourly") or {}
    vals = h.get("sea_level_height_msl") or []
    if not vals or all(v is None for v in vals):
        return None
    doc = {
        "times_utc": [datetime.utcfromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%SZ") for t in h["time"]],
        "sea_level_m": vals,
        "point": [d.get("latitude"), d.get("longitude")],
        "source": "Open-Meteo marine API (sea level incl. tide and surge), Sado estuary mouth",
    }
    highs = [i for i in range(1, len(vals) - 1) if vals[i] is not None and vals[i - 1] is not None
             and vals[i + 1] is not None and vals[i] >= vals[i - 1] and vals[i] > vals[i + 1]]
    doc["high_tides"] = [{"time_utc": doc["times_utc"][i], "level_m": vals[i]} for i in highs]
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(doc))
    return doc


def _extremes(times: list[float], vals: list[float]) -> list[tuple[float, float, str]]:
    """High ('H') and low ('L') waters of an hourly series, refined with a parabola through the
    three samples around each turning point."""
    out = []
    for i in range(1, len(vals) - 1):
        a, b, c = vals[i - 1], vals[i], vals[i + 1]
        kind = "H" if b >= a and b > c else "L" if b <= a and b < c else None
        if kind is None:
            continue
        curv = a - 2 * b + c
        dx = max(-0.5, min(0.5, 0.5 * (a - c) / curv)) if curv else 0.0
        out.append((times[i] + dx * (times[i + 1] - times[i]), b - 0.25 * (a - c) * dx, kind))
    keep = []   # strictly alternating H/L (keep the higher high / lower low of a repeated pair)
    for e in out:
        if keep and keep[-1][2] == e[2]:
            if (e[2] == "H") == (e[1] > keep[-1][1]):
                keep[-1] = e
            continue
        keep.append(e)
    return keep


def transfer(doc: dict, p: dict, step_s: int = 900) -> dict:
    """Sea level at the mouth -> water level at the Alcácer reach, from the tide's high and low
    waters: each high water becomes ``hw_offset_m + hw_scale * level`` ``hw_lag_h`` later, each low
    water ``lw_offset_m + lw_scale * level`` ``lw_lag_h`` later, joined by half-cosines (the
    estuary amplifies and delays high water, damps and delays low water more). Calibrated against
    the APA gauge in town (domain.toml [fluxos.tide_transfer])."""
    from datetime import timezone

    t = [datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() for s in doc.get("times_utc", [])]
    v = doc.get("sea_level_m", [])
    pairs = [(a, float(b)) for a, b in zip(t, v) if b is not None]
    ext = _extremes([a for a, _ in pairs], [b for _, b in pairs])
    if len(ext) < 3:
        return doc
    pts = []
    for te, ve, k in ext:
        k = k.lower()
        pts.append((te + float(p[f"{k}w_lag_h"]) * 3600.0, float(p[f"{k}w_offset_m"]) + float(p[f"{k}w_scale"]) * ve))
    pts.sort()
    t0 = int(pts[0][0] // step_s + 1) * step_s
    out_t, out_v = [], []
    j = 0
    for ts in range(t0, int(pts[-1][0]), step_s):
        while pts[j + 1][0] < ts:
            j += 1
        (ta, va), (tb, vb) = pts[j], pts[j + 1]
        f = (ts - ta) / (tb - ta)
        if vb < va:   # ebb: the town reach drops fast right after high water (fall_shape < 1)
            f = f ** float(p.get("fall_shape", 1.0))
        out_t.append(datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        out_v.append(round(va + (vb - va) * (1 - math.cos(math.pi * f)) / 2, 3))
    return {**doc, "times_utc": out_t, "sea_level_m": out_v, "transfer": dict(p),
            "source": doc.get("source", "") + " -> Alcácer reach (high/low-water transfer, calibrated on APA gauge 23F/01H)"}
