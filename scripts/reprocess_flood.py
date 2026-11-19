#!/usr/bin/env python3
"""Redo the FLUXOS post-processing of a past forecast from its saved 15-min series.

Every FLUXOS run keeps a compressed copy of its snapshots (depth and speed every print step) as
``cycles/<id>/hydraulics/<scenario>/fluxos_series.npz``. After a change to the post-processing
(new layers, timing, impact rules), this rebuilds the maps, timing rasters, impacts and
summary.json of that forecast without running FLUXOS again, then rebuilds the local site.

    python scripts/reprocess_flood.py run/laptop.toml 2026111912            # all scenarios
    python scripts/reprocess_flood.py run/laptop.toml 2026111912 p50 p90

The online page is not touched (it updates with the next complete forecast, or deploy/publish.sh).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from floodcast.operational import impacts  # noqa: E402
from floodcast.operational.config import OperationalConfig  # noqa: E402
from floodcast.operational.store import iso  # noqa: E402
from floodcast.operational.system import build_processor  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    cfg = OperationalConfig.from_file(Path(sys.argv[1]))
    cycle_id, wanted = sys.argv[2], sys.argv[3:]
    domain, store, p = build_processor(cfg)
    hdir = store.root / "cycles" / cycle_id / "hydraulics"
    done = 0
    for scn_dir in sorted(d for d in hdir.iterdir() if d.is_dir()):
        if wanted and scn_dir.name not in wanted:
            continue
        series, summ_file = scn_dir / "fluxos_series.npz", scn_dir / "summary.json"
        if not series.is_file() or not summ_file.is_file():
            print(f"{scn_dir.name}: no saved series (run made before series were kept) - skipped")
            continue
        old = json.loads(summ_file.read_text())
        sim_start = datetime.fromisoformat(old["window_start_utc"].replace("Z", "+00:00"))
        new = p.hydraulics._postprocess(None, scn_dir, scn_dir.name)
        for fr in new.get("frames", []):
            fr["time_utc"] = iso(sim_start + timedelta(seconds=fr["t_s"]))
        new["impacts"] = impacts.compute(scn_dir, domain.root, p.hydraulics.info,
                                         domain.fluxos.get("crs", "EPSG:32629"), sim_start)
        merged = {**old, **new}   # keeps scenario, member, inflow, tide, webgl, window ... from the run
        summ_file.write_text(json.dumps(merged, indent=1))
        print(f"{scn_dir.name}: re-processed ({len(new.get('frames', []))} frames, "
              f"{(new['impacts'] or {}).get('buildings_flooded', 0)} buildings with water)")
        done += 1
    if done and cfg.publish.output_dir is not None:
        from floodcast.operational.hydraulics import likelihood_map
        from floodcast.operational.site import build_site

        try:
            likelihood_map(hdir, float(p.hydraulics.info["cellsize"]))
        except Exception as exc:  # optional product
            print(f"likelihood map not rebuilt: {exc}")
        build_site(store.root, cfg.publish.output_dir, site_title=cfg.publish.site_title,
                   base_url=cfg.publish.base_url, chat_endpoint=cfg.publish.chat_endpoint)
        print("local site rebuilt")
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
