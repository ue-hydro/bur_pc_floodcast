# Operational output contract

Everything the operational system publishes is plain files under
`<data_root>/operational/<domain_id>/`. The public website is generated **only**
from these files (`floodcast operate site`), so the engine and the site are
decoupled.

```
<data_root>/operational/<domain_id>/
  latest.json                 pointer to the newest completed cycle + current alert level
  history.json                list of past cycles (newest first), compact
  cycles/<YYYYmmddHH>/
    status.json               cycle summary (schema below)
    screening.json            precipitation screening (always present)
    hydrology/summary.json    only if hydrology was triggered
    hydrology/discharge.csv   time_utc,member_0,member_1,...  (m3/s at the outlet)
    hydraulics/<scenario>/    only if hydraulics was triggered
      summary.json
      max_depth.png           transparent PNG, depth colour ramp, georeferenced by bounds in summary.json
      max_depth.geojson       (optional) polygons by depth class
      depth_timeseries.csv    time_utc, wet_area_km2, max_depth_m, volume_m3
```

All timestamps are UTC ISO-8601 with `Z`. Discharge in m3/s, depth in m,
precipitation in mm (kg m-2).

## latest.json

```json
{
  "domain_id": "alcacer_do_sal",
  "name": "Alcácer do Sal (Sado)",
  "cycle": "2026-11-18T12:00:00Z",
  "updated_at": "2026-11-18T18:41:07Z",
  "level": "none",                      
  "level_label": "No action",           
  "message": "…short human sentence…",
  "path": "cycles/2026111812/status.json",
  "system": {"version": "…", "uncalibrated": true}
}
```

`level` is one of `none`, `watch`, `warning`, `alert`, `unknown` (data/model failure).

## status.json

```json
{
  "domain_id": "alcacer_do_sal",
  "cycle": "2026-11-18T12:00:00Z",
  "issued_at": "…Z", "finished_at": "…Z",
  "level": "watch",
  "message": "…",
  "stages": {
    "screening":  {"state": "done|skipped|failed", "triggered_hydrology": true,  "reasons": ["PRECIP_48H_ABOVE_WATCH"]},
    "hydrology":  {"state": "done|skipped|failed", "triggered_hydraulics": false, "reasons": []},
    "hydraulics": {"state": "done|skipped|failed", "scenarios": ["p90"]}
  },
  "errors": []
}
```

## screening.json

```json
{
  "forecast_cycle": "…Z", "member": 0, "product": "ifs control 0.25°",
  "areal_mean_precip_mm": {"times_utc": ["…Z"], "interval_mm": [0.0, …], "cumulative_mm": [0.0, …]},
  "windows": [{"hours": 24, "max_areal_mean_mm": 31.2, "max_hru_mm": 55.0, "start_utc": "…Z"}],
  "thresholds": {"watch": {"24": 30, "48": 50}, "warning": {"24": 60, "48": 90}},
  "decision": {"outcome": "activate|do_not_activate|indeterminate", "level": "none|watch|warning", "reasons": ["…"]}
}
```

## hydrology/summary.json

```json
{
  "outlet": {"seg_id": 123, "name": "Sado at Alcácer do Sal", "area_km2": 6200},
  "members": 11,
  "times_utc": ["…Z"],
  "quantiles_m3s": {"p10": [..], "p50": [..], "p90": [..], "min": [..], "max": [..]},
  "peak": {"p50_m3s": 0, "p90_m3s": 0, "max_m3s": 0, "time_of_p50_peak_utc": "…Z"},
  "thresholds_m3s": {"watch": 300, "warning": 600, "alert": 1000},
  "exceedance_probability": {"watch": 0.4, "warning": 0.1, "alert": 0.0},
  "decision": {"outcome": "activate|do_not_activate|indeterminate", "level": "none|watch|warning|alert", "reasons": ["…"]},
  "initial_state": {"valid_time_utc": "…Z", "age_hours": 6.0, "spinup": "…"}
}
```

## hydraulics/<scenario>/summary.json

```json
{
  "scenario": "p90",
  "bounds_wgs84": {"west": .., "south": .., "east": .., "north": ..},
  "crs": "EPSG:32629",
  "max_depth_m": 0.0, "flooded_area_km2": 0.0,
  "inflow_peak_m3s": 0.0, "simulated_hours": 72,
  "image": "max_depth.png",
  "caveats": ["DSM-based terrain, river bathymetry not resolved", "…"]
}
```
