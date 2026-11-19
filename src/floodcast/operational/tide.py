"""Sea level (astronomical tide + surge) forecast at the Sado mouth, from the free Open-Meteo marine
API. Alcácer do Sal is tidal: high water in the estuary holds the river back, so the timing of high
tide against the flow peak matters. (FLUXOS does not yet take it as a downstream boundary: shown on
the site and used in the headline; see docs/operational for the planned boundary condition.)"""

from __future__ import annotations

import json
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
