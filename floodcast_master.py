#!/usr/bin/env python3
"""FLOODCAST MASTER: the whole operational loop in one readable file.

    while True:
        if a NEW weather forecast exists:                 # IF 1
            screen its precipitation
            if rain exceeds the threshold:                # IF 2  -> run hydrology (SUMMA-mizuRoute)
                run the ensemble, get discharge at the outlet
                if discharge exceeds the threshold:       # IF 3  -> run hydraulics (FLUXOS)
                    run FLUXOS, write the flood map
        publish files; sleep; repeat

Everything else (building the domain, model inputs, calibration) is done separately.
This file only orchestrates; it calls the small modules in ``floodcast.operational``.

    python floodcast_master.py examples/operational/alcacer_do_sal.toml            # forever
    python floodcast_master.py examples/operational/alcacer_do_sal.toml --once     # one pass
    python floodcast_master.py examples/operational/alcacer_do_sal.toml --cycle 2026111812
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from floodcast.operational import OPERATIONAL_VERSION, ifs, policies, screening
from floodcast.operational.config import OperationalConfig
from floodcast.operational.cycle import MESSAGES, CycleProcessor
from floodcast.operational.daemon import setup_logging
from floodcast.operational.store import iso, max_level, now_utc, write_json
from floodcast.operational.system import build_processor, parse_cycle

log = logging.getLogger("floodcast.master")
STOP = False
PROG = None  # Progress (live progress.json for the site)


def new_cycles(cfg: OperationalConfig, store) -> list[datetime]:
    """Forecast cycles ECMWF has published and we have not processed yet (oldest first)."""
    fc = cfg.forecast
    out = []
    for cycle in sorted(ifs.cycle_candidates(now_utc(), fc.cycles, max_age_h=fc.max_cycle_age_h)):
        if store.is_complete(cycle):
            continue
        if ifs.cycle_is_available(cycle, source=fc.source, resolution=fc.resolution,
                                  max_lead_h=fc.screening_max_lead_h, ensemble=False):
            out.append(cycle)
    return out


def export_map_geometry(domain, dest: Path) -> None:
    """Light (simplified) basin geometry for the website maps: sub-basins and river reaches."""
    import json

    from shapely.geometry import mapping, shape

    dest.mkdir(parents=True, exist_ok=True)
    geo = domain.root / "geometry"
    for src, out, idf, tol in ((domain.hrus_geojson, "hrus.geojson", domain.hru_id_field, 0.002),
                               (geo / "river_network.geojson", "rivers.geojson", "segId", 0.0005)):
        if not src.is_file():
            continue
        feats = []
        for f in json.loads(src.read_text())["features"]:
            g = shape(f["geometry"]).simplify(tol, preserve_topology=True)
            props = {k: f["properties"][k] for k in (idf, "ups_area_km2") if k in f["properties"]}
            feats.append({"type": "Feature", "properties": props, "geometry": mapping(g)})
        text = json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":"))
        import re
        text = re.sub(r"(-?\d+\.\d{5})\d+", r"\1", text)  # ~1 m precision is plenty
        (dest / out).write_text(text)


def precip_grid(tp, cycle: datetime, max_h: float = 72.0) -> dict:
    """Interval rain (mm) per native IFS grid cell, cropped to the domain bbox."""
    import numpy as np

    acc = np.asarray(tp.data, dtype=float) * 1000.0  # m -> mm, accumulated from step 0
    steps = np.asarray(tp.steps_h, dtype=float)
    if steps[0] > 0:
        acc = np.concatenate([np.zeros((1, *acc.shape[1:])), acc])
        steps = np.concatenate([[0.0], steps])
    inc = np.clip(np.diff(acc, axis=0), 0.0, None)
    keep = steps[1:] <= max_h
    dlat = float(abs(tp.lats[1] - tp.lats[0])) if len(tp.lats) > 1 else 0.25
    dlon = float(abs(tp.lons[1] - tp.lons[0])) if len(tp.lons) > 1 else 0.25
    return {
        "cycle": iso(cycle), "lats": [float(v) for v in tp.lats], "lons": [float(v) for v in tp.lons],
        "dlat": dlat, "dlon": dlon, "end_hours": [float(h) for h in steps[1:][keep]],
        "mm": np.round(inc[keep], 2).tolist(),  # (T, Y, X), north first
    }


def write_precip_scenarios(path: Path, cycle: datetime, data: dict | None, step_h: int = 3,
                           max_h: int = 72) -> None:
    """Rain of the lower / central / upper scenarios on 0.25° cells, 3-hourly, for the rain map."""
    import numpy as np

    if not data:
        return
    cells = data["cells"]
    lats = sorted({c[0] for c in cells}, reverse=True)
    lons = sorted({c[1] for c in cells})
    out = {"cycle": iso(cycle), "dlat": 0.25, "dlon": 0.25, "lats": lats, "lons": lons, "scenarios": {}}
    for name, mm_h in data["mm_h"].items():
        mm_h = np.asarray(mm_h)[:max_h]
        n = mm_h.shape[0] // step_h
        sums = mm_h[: n * step_h].reshape(n, step_h, -1).sum(axis=1)       # (T3, HRU)
        grid = [[[None] * len(lons) for _ in lats] for _ in range(n)]
        for c_idx, cell in enumerate(sorted(set(map(tuple, cells)))):
            cols = [h for h, c in enumerate(cells) if tuple(c) == cell]
            y, x = lats.index(cell[0]), lons.index(cell[1])
            vals = sums[:, cols].mean(axis=1)
            for t in range(n):
                grid[t][y][x] = round(float(vals[t]), 2)
        out["scenarios"][name] = {"mm": grid}
        out["end_hours"] = [float((t + 1) * step_h) for t in range(n)]
    write_json(path, out)


def scenario_rain(p, scenario: str):
    """Hourly rain (mm/h) of a rainfall scenario on the sub-basin holding the FLUXOS window."""
    import numpy as np

    d = getattr(p, "last_precip_scenarios", None)
    name = {"p10": "lower", "p50": "central", "p90": "upper"}.get(scenario)
    if not d or name not in d.get("mm_h", {}):
        return None
    ids = d.get("hru_ids") or []
    hru = int(p.domain.raw.get("outlet", {}).get("outlet_hru_id", ids[-1] if ids else 0))
    col = ids.index(hru) if hru in ids else -1
    return np.asarray(d["mm_h"][name])[:, col]


def add_rain_to_forcing(scn_dir: Path, tp, cycle: datetime, domain, scenario_rain=None) -> None:
    """Forecast rain (mm/h) on the animation's clock (t_s): the scenario's rain on the sub-basin of the
    FLUXOS window when available, else the ECMWF control at the IFS cell over the town."""
    import json

    import numpy as np

    try:
        if scenario_rain is not None:
            for f in (scn_dir / "forcing.json", scn_dir / "webgl" / "forcing.json"):
                if not f.is_file():
                    continue
                doc = json.loads(f.read_text())
                start = datetime.fromisoformat(doc["sim_start_utc"].replace("Z", "+00:00"))
                offset = (start - cycle).total_seconds()
                doc["rain"] = [{"t_s": round(h * 3600 - offset), "mm_h": round(float(r), 2)}
                               for h, r in enumerate(scenario_rain)]
                doc["rain_source"] = "rainfall scenario, sub-basin of the FLUXOS window"
                write_json(f, doc)
            return
        if tp is None:
            return
        town = (domain.raw.get("outlet", {}) if hasattr(domain, "raw") else {})
        lat = float(town.get("town_lat", 38.3735)); lon = float(town.get("town_lon", -8.5127))
        iy = int(np.argmin(np.abs(np.asarray(tp.lats) - lat))); ix = int(np.argmin(np.abs(np.asarray(tp.lons) - lon)))
        acc = np.asarray(tp.data, dtype=float)[:, iy, ix] * 1000.0
        steps = np.asarray(tp.steps_h, dtype=float)
        if steps[0] > 0:
            acc = np.concatenate([[0.0], acc]); steps = np.concatenate([[0.0], steps])
        mm_h = np.clip(np.diff(acc), 0, None) / np.diff(steps)
        for f in (scn_dir / "forcing.json", scn_dir / "webgl" / "forcing.json"):
            if not f.is_file():
                continue
            doc = json.loads(f.read_text())
            start = datetime.fromisoformat(doc["sim_start_utc"].replace("Z", "+00:00"))
            offset = (start - cycle).total_seconds()
            doc["rain"] = [{"t_s": round(float(h0) * 3600 - offset), "mm_h": round(float(r), 2)}
                           for h0, r in zip(steps[:-1], mm_h)]
            doc["rain_source"] = f"ECMWF IFS control, 0.25° cell at {lat:.3f}N {lon:.3f}E"
            write_json(f, doc)
    except Exception as exc:
        log.warning("rain not added to FLUXOS forcing: %s", exc)


def failed_stage(status: dict, stage: str) -> bool:
    key = {"precip": "screening", "hydrology": "hydrology", "hydraulics": "hydraulics"}[stage]
    return (status["stages"].get(key) or {}).get("state") == "failed"


def handle_cycle(cfg: OperationalConfig, p: CycleProcessor, cycle: datetime, *, newest: bool) -> dict:
    """The three decisions. Failure at any stage => level 'unknown', never a silent 'no risk'."""
    fc, store = cfg.forecast, p.store
    out = store.cycle_dir(cycle)
    out.mkdir(parents=True, exist_ok=True)
    cache = p.cache_root / f"{cycle:%Y%m%d%H}"
    bbox = p.domain.bbox_tuple()
    status = {
        "domain_id": p.domain.domain_id, "cycle": iso(cycle), "issued_at": iso(now_utc()),
        "finished_at": None, "level": "unknown", "message": "", "errors": [],
        "stages": {"screening": {"state": "skipped", "triggered_hydrology": False, "reasons": []},
                   "hydrology": {"state": "skipped", "triggered_hydraulics": False, "reasons": []},
                   "hydraulics": {"state": "skipped", "scenarios": []}},
    }
    level = "none"
    control: dict = {}

    def fail(stage: str, exc: Exception) -> None:
        log.error("%s failed: %s\n%s", stage, exc, traceback.format_exc())
        status["errors"].append({"stage": stage, "type": type(exc).__name__, "message": str(exc)[:500]})
        status["stages"].setdefault(stage, {})["state"] = "failed"

    prog = PROG
    if prog:
        prog.begin(cycle)
        prog.start("precip")
        p.on_progress = lambda stage, frac, detail="": prog.update(stage, frac, detail)
    # ---- IF 1: weather. Fetch the new forecast and screen the rain ------------------------
    try:
        paths = ifs.fetch(cycle, members=[0], fields=[f for f in ifs.ALL_FIELDS if f != "tp"],
                          max_lead_h=6, workdir=cache, source=fc.source, resolution=fc.resolution,
                          retries=fc.retries, fallback_sources=ifs.mirrors_for(fc.source))
        paths += ifs.fetch(cycle, members=[0], fields=["tp"], max_lead_h=fc.screening_max_lead_h,
                           workdir=cache, source=fc.source, resolution=fc.resolution,
                           retries=fc.retries, fallback_sources=ifs.mirrors_for(fc.source))
        control = {k[0]: v for k, v in ifs.read_grib(paths, bbox).items() if k[1] == 0}
        weights = p._weights_for(control["tp"])
        screen = screening.screen_precipitation(
            control["tp"], weights, p._areas_m2(), cfg.screening, cycle=cycle,
            product="ECMWF IFS HRES (control) 0.25°", min_lead_h=min(72, fc.screening_max_lead_h))
        write_json(out / "screening.json", screen)
        if prog:
            prog.update("precip", 0.2, "ECMWF IFS")
        try:  # rain on the native IFS grid for the animated map (first 72 h)
            write_json(out / "precip_grid.json", precip_grid(control["tp"], cycle))
        except Exception as exc:
            log.warning("precip_grid.json not written: %s", exc)
        rain_trigger = screen["decision"]["outcome"] == "activate"
        unsure = screen["decision"]["outcome"] == "indeterminate"
        status["stages"]["screening"] = {"state": "done", "triggered_hydrology": rain_trigger,
                                         "reasons": screen["decision"]["reasons"]}
        try:  # bookkeeping: keep the hydrological state chain advancing every cycle
            p.hydrology.advance_state(cycle, control, weights)
        except Exception as exc:
            fail("state", exc)
    except Exception as exc:
        fail("screening", exc)
        rain_trigger = unsure = False

    # ---- IF 2: rain is high enough -> hydrology (only for the newest forecast) ------------
    q = hours = None
    if newest and (rain_trigger or unsure):
        try:
            members = p._ensemble_fields(cycle, cache, bbox)      # lower / central / upper rain
            if prog:
                prog.finish("precip")
                prog.start("hydrology")
                done_members = {"n": 0}

                def _member_done():
                    done_members["n"] += 1
                    prog.update("hydrology", done_members["n"] / 3.0, f"{done_members['n']}/3")
                p.hydrology.on_member_done = _member_done
            weights = p._weights_for(control["tp"]) if control else p._weights
            hours, q, state_info = p.hydrology.run_ensemble(cycle, members, weights, cache / "run")
            outlet = {"seg_id": p.domain.outlet_seg_id, "name": p.domain.name,
                      "area_km2": p.domain.area_km2}
            summary = policies.scenario_summary(cycle, hours, q, cfg.hydrology, outlet=outlet,
                                                initial_state=state_info, precip=p.last_ensemble_source)
            write_precip_scenarios(out / "precip_scenarios.json", cycle, getattr(p, "last_precip_scenarios", None))
            write_json(out / "hydrology" / "precip_ensemble.json",
                       {"cycle": iso(cycle), **p.last_ensemble_source, **p.last_precip_stats})
            write_json(out / "hydrology" / "summary.json", summary)
            if prog:
                prog.finish("hydrology")
            reach = getattr(p.hydrology, "last_reach_flow", None)
            if reach:
                write_json(out / "hydrology" / "reach_flow.json", {"cycle": iso(cycle), **reach})
            p._write_discharge_csv(out / "hydrology" / "discharge.csv", cycle, hours, q)
            level = summary["decision"]["level"]
            flow_trigger = policies.hydraulics_triggered(level, cfg.hydrology)
            status["stages"]["hydrology"] = {"state": "done", "triggered_hydraulics": flow_trigger,
                                             "reasons": summary["decision"]["reasons"]}

            # ---- IF 3: flow is high enough -> hydraulics (FLUXOS flood map) ----------------
            if flow_trigger:
                done = []
                watcher = None
                if prog:
                    from floodcast.operational.progress import FluxosWatcher

                    prog.start("hydraulics")
                    watcher = FluxosWatcher(prog, [cache / "fluxos" / s_ for s_ in cfg.hydraulics.scenarios],
                                            cfg.hydraulics.simulated_hours)
                    watcher.start()
                try:
                    from concurrent.futures import ThreadPoolExecutor

                    def run_one(scenario):
                        res = p.hydraulics.run(cycle, scenario, hours, q, cache / "fluxos" / scenario,
                                               out / "hydraulics" / scenario,
                                               member={"p10": 0, "p50": 1, "p90": 2}.get(scenario))
                        write_json(out / "hydraulics" / scenario / "summary.json", res)
                        add_rain_to_forcing(out / "hydraulics" / scenario, control.get("tp"), cycle, p.domain,
                                            scenario_rain=scenario_rain(p, scenario))
                        if watcher:
                            watcher.mark_done(list(cfg.hydraulics.scenarios).index(scenario))
                        return scenario

                    errors_h = []
                    with ThreadPoolExecutor(max_workers=cfg.hydraulics.parallel_scenarios) as pool:
                        futs = {pool.submit(run_one, sc): sc for sc in cfg.hydraulics.scenarios}
                        for fut in futs:
                            try:
                                done.append(fut.result())
                            except Exception as exc:  # one scenario failing does not drop the others
                                errors_h.append(exc)
                    if errors_h and not done:
                        raise errors_h[0]
                    for exc in errors_h:
                        fail("hydraulics", exc)
                    status["stages"]["hydraulics"] = {"state": "done", "scenarios": done}
                    try:
                        from floodcast.operational.hydraulics import likelihood_map

                        likelihood_map(out / "hydraulics", float(p.hydraulics.info["cellsize"]))
                    except Exception as exc:
                        log.warning("likelihood map not produced: %s", exc)
                except Exception as exc:
                    status["stages"]["hydraulics"] = {"state": "failed", "scenarios": done}
                    fail("hydraulics", exc)
                finally:
                    if watcher:
                        watcher.stop()
                        prog.finish("hydraulics", "done" if status["stages"]["hydraulics"].get("state") == "done" else "failed")
        except Exception as exc:
            fail("hydrology", exc)

    # ---- publish -------------------------------------------------------------------------
    if "failed" in (status["stages"]["screening"]["state"], status["stages"]["hydrology"]["state"]):
        final = "unknown"
    elif status["stages"]["hydrology"]["state"] == "done":
        final = max_level(level)
    elif status["stages"]["screening"]["state"] == "done" and not (rain_trigger or unsure):
        final = "none"
    else:
        final = "unknown"  # triggered but hydrology not run (older catch-up cycle)
    status["level"] = final
    status["message_pt"], status["message"] = MESSAGES[final]
    unavailable = any(e.get("type") == "IfsUnavailable" for e in status["errors"])
    age_h = (now_utc() - cycle).total_seconds() / 3600
    if unavailable and age_h < cfg.forecast.max_cycle_age_h:
        status["retry_pending"] = True   # not "finished": the next poll tries this cycle again
    else:
        status["finished_at"] = iso(now_utc())
    store.write_cycle_status(cycle, status)
    if newest:
        store.publish_latest(cycle, status, version=OPERATIONAL_VERSION,
                             uncalibrated=cfg.hydrology.thresholds_m3s.placeholder,
                             keep_cycles=cfg.publish.keep_cycles)
    if prog:
        for st in ("precip", "hydrology", "hydraulics"):
            cur = prog.doc.get("stages", {}).get(st, {}).get("state")
            if cur == "running":
                prog.finish(st, "failed" if failed_stage(status, st) else "done")
            elif cur == "pending":
                prog.skip(st)
        # next forecast cycle (+6 h), published by ECMWF ~7 h after its nominal time, plus run time
        prog.end(next_expected=cycle + timedelta(hours=13))
    log.info("cycle %s -> %s", iso(cycle), final)
    return status


def publish_site(cfg: OperationalConfig, store) -> None:
    """Rebuild the static website; optionally push it (``[publish] post_command``)."""
    if cfg.publish.output_dir is None:
        return
    from floodcast.operational.site import build_site

    build_site(store.root, cfg.publish.output_dir, site_title=cfg.publish.site_title,
               base_url=cfg.publish.base_url, chat_endpoint=cfg.publish.chat_endpoint)
    if cfg.publish.post_command:
        import subprocess

        try:
            subprocess.run(cfg.publish.post_command, shell=True, cwd=cfg.publish.output_dir,
                           check=True, timeout=cfg.publish.post_command_timeout_s,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except (subprocess.SubprocessError, OSError) as exc:
            log.error("publish.post_command failed: %s", type(exc).__name__)


def main() -> int:
    global STOP
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("config", type=Path)
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--cycle", help="process this cycle (e.g. 2026111812) instead of polling")
    args = ap.parse_args()

    cfg = OperationalConfig.from_file(args.config)
    domain, store, p = build_processor(cfg)
    global PROG
    from floodcast.operational.progress import Progress

    PROG = Progress(store.root, cfg.publish.output_dir)
    setup_logging(cfg, domain.domain_id)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: globals().update(STOP=True))

    from filelock import FileLock, Timeout

    lock = FileLock(str(store.root / "master.lock"))
    store.root.mkdir(parents=True, exist_ok=True)
    try:
        lock.acquire(timeout=0)
    except Timeout:
        sys.exit("another floodcast_master is already running")

    try:
        export_map_geometry(domain, store.root / "geometry")
    except Exception:
        log.exception("map geometry export failed")
    try:
        try:  # show a page ("no reliable data yet") immediately, not an empty folder
            publish_site(cfg, store)
        except Exception:
            log.exception("initial site build failed")
        while not STOP:
            started = now_utc()
            try:
                cycles = [parse_cycle(args.cycle)] if args.cycle else new_cycles(cfg, store)
                for i, cycle in enumerate(cycles):
                    if STOP:
                        break
                    write_json(store.root / "health.json",
                               {"state": "processing", "cycle": iso(cycle), "time": iso(now_utc())})
                    try:  # show "computation in progress" on the site straight away
                        publish_site(cfg, store)
                    except Exception:
                        log.exception("site refresh failed")
                    try:
                        handle_cycle(cfg, p, cycle, newest=(i == len(cycles) - 1))
                    except Exception:  # one bad cycle must never kill the service
                        log.exception("cycle %s crashed", iso(cycle))
                publish_site(cfg, store)
                write_json(store.root / "health.json",
                           {"state": "idle", "time": iso(now_utc()), "processed": len(cycles)})
            except Exception:
                log.exception("polling pass failed")
                write_json(store.root / "health.json", {"state": "error", "time": iso(now_utc())})
            if args.once or args.cycle:
                break
            wait = max(30.0, cfg.poll_interval_minutes * 60 - (now_utc() - started).total_seconds())
            end = time.monotonic() + wait
            while not STOP and time.monotonic() < end:
                time.sleep(1)
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
