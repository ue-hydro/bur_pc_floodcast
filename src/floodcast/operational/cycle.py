"""Process one IFS cycle through screening -> hydrology -> hydraulics -> outputs."""

from __future__ import annotations

import logging
import traceback
from datetime import datetime
from pathlib import Path
from typing import Protocol

import numpy as np

from . import OPERATIONAL_VERSION, ifs, policies, screening
from .config import OperationalConfig
from .domain import Domain
from .forcing import HruWeights, compute_weights
from .ifs import FieldStack
from .store import OperationalStore, iso, max_level, now_utc, write_json

log = logging.getLogger("floodcast.operational.cycle")

Fields = dict[str, FieldStack]


class HydrologyEngine(Protocol):
    def advance_state(self, cycle: datetime, control: Fields, weights: HruWeights) -> dict: ...

    def run_ensemble(
        self, cycle: datetime, members: dict[int, Fields], weights: HruWeights, workdir: Path
    ) -> tuple[np.ndarray, np.ndarray, dict]:
        """Returns (hours_from_cycle (T,), discharge (members, T) m3/s, initial_state info)."""


class HydraulicsEngine(Protocol):
    def run(
        self, cycle: datetime, scenario: str, hours: np.ndarray, q: np.ndarray,
        workdir: Path, outdir: Path,
    ) -> dict: ...


MESSAGES = {
    "none": ("Sem previsão de cheia significativa.", "No significant flood signal forecast."),
    "watch": (
        "Vigilância: possível subida do caudal do Sado nos próximos dias.",
        "Watch: possible rise of the Sado flow in the coming days.",
    ),
    "warning": (
        "Aviso: previsão de caudais elevados do Sado; acompanhe os canais oficiais.",
        "Warning: high Sado flows forecast; follow official channels.",
    ),
    "alert": (
        "Alerta: previsão de caudais muito elevados do Sado; siga as indicações da Proteção Civil.",
        "Alert: very high Sado flows forecast; follow Civil Protection instructions.",
    ),
    "unknown": (
        "Estado desconhecido: a previsão não pôde ser avaliada. Não assuma ausência de risco.",
        "Status unknown: the forecast could not be evaluated. Do not assume there is no risk.",
    ),
}


class CycleProcessor:
    def __init__(
        self, config: OperationalConfig, domain: Domain, store: OperationalStore,
        hydrology: HydrologyEngine | None = None, hydraulics: HydraulicsEngine | None = None,
    ) -> None:
        self.config, self.domain, self.store = config, domain, store
        self.hydrology, self.hydraulics = hydrology, hydraulics
        self._weights: HruWeights | None = None
        self._areas: dict[int, float] | None = None
        self.cache_root = config.data_root / "operational" / domain.domain_id / "cache"

    # ------------------------------------------------------------------------------
    def _weights_for(self, grid: FieldStack) -> HruWeights:
        w = self._weights
        if w is None or w.lats.shape != grid.lats.shape or w.lons.shape != grid.lons.shape:
            w = compute_weights(self.domain.hrus_geojson, self.domain.hru_id_field,
                                grid.lats, grid.lons)
            self._weights = w
        return w

    def _areas_m2(self) -> dict[int, float]:
        if self._areas is None:
            self._areas = screening.hru_areas_m2(self.domain.hrus_geojson, self.domain.hru_id_field)
        return self._areas

    # ------------------------------------------------------------------------------
    def process(self, cycle: datetime, *, full: bool = True) -> dict:
        fc = self.config.forecast
        outdir = self.store.cycle_dir(cycle)
        outdir.mkdir(parents=True, exist_ok=True)
        status: dict = {
            "domain_id": self.domain.domain_id,
            "cycle": iso(cycle),
            "issued_at": iso(now_utc()),
            "finished_at": None,
            "level": "unknown",
            "message": "",
            "stages": {
                "screening": {"state": "skipped", "triggered_hydrology": False, "reasons": []},
                "hydrology": {"state": "skipped", "triggered_hydraulics": False, "reasons": []},
                "hydraulics": {"state": "skipped", "scenarios": []},
            },
            "errors": [],
        }
        self.store.write_cycle_status(cycle, status)
        cache = self.cache_root / f"{cycle:%Y%m%d%H}"
        bbox = self.domain.bbox_tuple()
        control: Fields = {}
        screen_doc: dict | None = None
        # ---- A. screening (+ short control fields for the state chain) -----------------
        try:
            paths = ifs.fetch(
                cycle, members=[0], fields=[f for f in ifs.ALL_FIELDS if f != "tp"],
                max_lead_h=6, workdir=cache, source=fc.source, resolution=fc.resolution,
                retries=fc.retries,
            ) + ifs.fetch(
                cycle, members=[0], fields=["tp"], max_lead_h=fc.screening_max_lead_h,
                workdir=cache, source=fc.source, resolution=fc.resolution, retries=fc.retries,
            )
            control = {k[0]: v for k, v in ifs.read_grib(paths, bbox).items() if k[1] == 0}
            weights = self._weights_for(control["tp"])
            screen_doc = screening.screen_precipitation(
                control["tp"], weights, self._areas_m2(), self.config.screening,
                cycle=cycle, product="ECMWF IFS HRES (control) 0.25°",
                min_lead_h=min(72, fc.screening_max_lead_h),
            )
            write_json(outdir / "screening.json", screen_doc)
            decision = screen_doc["decision"]
            status["stages"]["screening"] = {
                "state": "done",
                "triggered_hydrology": decision["outcome"] == "activate",
                "reasons": decision["reasons"],
            }
        except Exception as exc:
            self._fail(status, "screening", exc)
        self.store.write_cycle_status(cycle, status)

        # ---- B0. keep the hydrological state chain warm every cycle ---------------------
        if self.hydrology is not None and control:
            try:
                self.hydrology.advance_state(cycle, control, self._weights_for(control["tp"]))
            except Exception as exc:
                self._fail(status, "state", exc, stage=None)

        # ---- B/C/D. only for the newest cycle and only when triggered -------------------
        q = hours = None
        hyd_level = "none"
        triggered = status["stages"]["screening"]["triggered_hydrology"]
        indeterminate = bool(screen_doc) and screen_doc["decision"]["outcome"] == "indeterminate"
        if full and self.hydrology is not None and (triggered or indeterminate):
            try:
                hydro_dir = outdir / "hydrology"
                members = self._ensemble_fields(cycle, cache, bbox)  # lower / central / upper rain
                weights = self._weights
                hours, q, state_info = self.hydrology.run_ensemble(
                    cycle, members, weights, cache / "run"
                )
                outlet = {"seg_id": self.domain.outlet_seg_id, "name": self.domain.name,
                          "area_km2": self.domain.area_km2}
                summary = policies.scenario_summary(
                    cycle, hours, q, self.config.hydrology, outlet=outlet, initial_state=state_info,
                    precip=getattr(self, "last_ensemble_source", None),
                )
                write_json(hydro_dir / "summary.json", summary)
                self._write_discharge_csv(hydro_dir / "discharge.csv", cycle, hours, q)
                hyd_level = summary["decision"]["level"]
                status["stages"]["hydrology"] = {
                    "state": "done",
                    "triggered_hydraulics": policies.hydraulics_triggered(
                        hyd_level, self.config.hydrology
                    ),
                    "reasons": summary["decision"]["reasons"],
                }
            except Exception as exc:
                self._fail(status, "hydrology", exc)
            self.store.write_cycle_status(cycle, status)

        if (
            status["stages"]["hydrology"]["triggered_hydraulics"]
            and self.hydraulics is not None and q is not None
        ):
            done: list[str] = []
            try:
                for scenario in self.config.hydraulics.scenarios:
                    summary = self.hydraulics.run(
                        cycle, scenario, hours, q, cache / "fluxos" / scenario,
                        outdir / "hydraulics" / scenario,
                        member={"p10": 0, "p50": 1, "p90": 2}.get(scenario),
                    )
                    write_json(outdir / "hydraulics" / scenario / "summary.json", summary)
                    done.append(scenario)
                status["stages"]["hydraulics"] = {"state": "done", "scenarios": done}
            except Exception as exc:
                status["stages"]["hydraulics"] = {"state": "failed", "scenarios": done}
                self._fail(status, "hydraulics", exc, stage=None)

        # ---- level + publication ----------------------------------------------------------
        status["level"] = self._final_level(status, hyd_level, screen_doc)
        pt, en = MESSAGES[status["level"]]
        status["message"], status["message_pt"] = en, pt
        status["finished_at"] = iso(now_utc())
        self.store.write_cycle_status(cycle, status)
        if full:
            self.store.publish_latest(
                cycle, status, version=OPERATIONAL_VERSION,
                uncalibrated=self.config.hydrology.thresholds_m3s.placeholder,
                keep_cycles=self.config.publish.keep_cycles,
            )
        return status

    # ------------------------------------------------------------------------------
    def _ensemble_fields(self, cycle, cache, bbox) -> dict:
        """Three rainfall scenarios (lower / median / upper) as hourly SUMMA forcing.

        1. ECMWF ENS: control (all variables) + every member's precipitation. IFS Open Data has the
           ensemble only at 00/12 UTC, so a 06/18 UTC cycle uses the run 6 h earlier, shifted onto
           this cycle's clock (lagged ensemble); the hydrology still starts from this cycle's state.
        2. Every other free precipitation forecast (Open-Meteo ensembles + single-model runs).
        3. Rank all members by cumulative basin rain over ``precip_window_h``; pick the real members
           at P10 / P50 / P90. Each scenario = ECMWF-control forcing with that member's rainfall.
        """
        from dataclasses import replace
        from datetime import timedelta

        from . import forcing as fz
        from . import precip_ensemble as pe
        from .openmeteo import HruForcing

        fc = self.config.forecast
        lag_h = cycle.hour % 12
        ens_cycle = cycle - timedelta(hours=lag_h)
        lead = fc.ensemble_max_lead_h
        kw = dict(workdir=cache / "ens", source=fc.source, resolution=fc.resolution, retries=fc.retries,
                  fallback_sources=ifs.mirrors_for(fc.source))
        paths = ifs.fetch(ens_cycle, members=[0], fields=ifs.ALL_FIELDS, max_lead_h=lead + lag_h, **kw)
        status: dict[str, str] = {}
        try:
            paths += ifs.fetch(ens_cycle, members=range(1, fc.ensemble_members), fields=["tp"],
                               max_lead_h=lead + lag_h, **kw)
        except Exception as exc:  # perturbed members are optional; the control is not
            status["ECMWF IFS ENS perturbed"] = f"failed ({type(exc).__name__})"
        stacks = ifs.read_grib(paths, bbox)

        def shift(st):
            if not lag_h:
                return st
            keep = st.steps_h >= lag_h  # accumulations: increments are differences, offset drops out
            return replace(st, steps_h=st.steps_h[keep] - lag_h, data=st.data[keep])

        control = {name: shift(st) for (name, m), st in stacks.items() if m == 0}
        if set(control) != set(ifs.ALL_FIELDS):
            raise RuntimeError(f"incomplete ECMWF control fields: {sorted(control)}")
        weights = self._weights_for(control["tp"])
        lead = int(min(lead, control["tp"].steps_h.max()))
        hours, base = fz.summa_forcing_arrays(control, weights, lead_end_h=lead)
        mid = hours + 0.5
        ecmwf = {f"m{m}": fz._accum_to_hourly_rate(shift(st), weights, mid, 1000.0) * 3600.0
                 for (name, m), st in stacks.items() if name == "tp"}

        # other centres, at the HRU representative points in the forcing column order
        lats_all, lons_all, ids_all = self._hru_centroids()
        order = [ids_all.index(int(h)) for h in weights.hru_ids]
        lats = [lats_all[i] for i in order]
        lons = [lons_all[i] for i in order]
        members, src_status = pe.collect(
            cycle, lats, lons, lead, ecmwf, ecmwf["m0"], ensembles=fc.precip_ensembles,
            deterministic=fc.precip_deterministic, max_members=fc.precip_max_members_per_source,
            cache_dir=cache / "precip",
            progress=(lambda d, t, name: self.on_progress("precip", 0.2 + 0.8 * d / t, f"{d}/{t} · {name}"))
            if getattr(self, "on_progress", None) else None)
        src_status.update(status)
        if lag_h:
            src_status = {(k + f" (lagged {lag_h} h)" if k.startswith("ECMWF IFS ENS") else k): v
                          for k, v in src_status.items()}
        areas = np.array([self._areas_m2()[int(h)] for h in weights.hru_ids])
        sel = pe.select_bounds(members, areas, min(fc.precip_window_h, lead))
        names = {"p10": "lower", "p50": "central", "p90": "upper"}
        scenarios: dict[int, HruForcing] = {}
        chosen = {}
        for k, (pkey, idx) in enumerate(sel["picks"].items()):
            m = members[idx]
            arrays = dict(base)
            arrays["pptrate"] = m.mm_h / 3600.0
            scenarios[k] = HruForcing(source=m.source, member=k, hours=hours, arrays=arrays,
                                      hru_ids=tuple(int(h) for h in weights.hru_ids))
            chosen[names[pkey]] = {"source": m.source, "member": m.label,
                                   "total_mm": round(float(sel["totals_mm"][idx]), 1),
                                   "filled_hours_from_ecmwf": m.filled_h}
        self.last_ensemble_source = {
            "ens_cycle": ens_cycle.isoformat(), "lag_hours": lag_h, "sources": src_status,
            "members_total": len(members), "window_h": min(fc.precip_window_h, lead), "scenarios": chosen,
        }
        # per-scenario rain on the 0.25° cells that hold the sub-basin points (for the rain map)
        cells = [(round(la * 4) / 4, round(lo * 4) / 4) for la, lo in zip(lats, lons)]
        self.last_precip_scenarios = {
            "cells": cells, "hru_ids": [int(h) for h in weights.hru_ids],
            "mm_h": {names[p]: members[i].mm_h for p, i in sel["picks"].items()},
        }
        self.last_precip_stats = {
            "hours": hours.tolist(),
            "cum_p10": np.round(sel["cum_quantiles"]["p10"], 2).tolist(),
            "cum_p50": np.round(sel["cum_quantiles"]["p50"], 2).tolist(),
            "cum_p90": np.round(sel["cum_quantiles"]["p90"], 2).tolist(),
            "cum_min": np.round(sel["cum_min"], 2).tolist(), "cum_max": np.round(sel["cum_max"], 2).tolist(),
            "scenario_cum": {names[p]: np.round(np.cumsum(sel["basin_mm_h"][i]), 2).tolist()
                             for p, i in sel["picks"].items()},
        }
        return scenarios

    def _hru_centroids(self):
        import json as _json

        from shapely.geometry import shape

        feats = _json.loads(Path(self.domain.hrus_geojson).read_text())["features"]
        lats, lons, ids = [], [], []
        for f in feats:
            c = shape(f["geometry"]).representative_point()
            lats.append(c.y); lons.append(c.x); ids.append(int(f["properties"][self.domain.hru_id_field]))
        return lats, lons, ids

    @staticmethod
    def _write_discharge_csv(path: Path, cycle: datetime, hours: np.ndarray, q: np.ndarray) -> None:
        from datetime import timedelta

        path.parent.mkdir(parents=True, exist_ok=True)
        header = "time_utc," + ",".join(f"member_{i}" for i in range(q.shape[0]))
        rows = [
            iso(cycle + timedelta(hours=float(h))) + "," + ",".join(f"{v:.3f}" for v in q[:, t])
            for t, h in enumerate(hours)
        ]
        path.write_text(header + "\n" + "\n".join(rows) + "\n")

    @staticmethod
    def _fail(status: dict, stage_name: str, exc: Exception, *, stage: str | None = "x") -> None:
        log.error("%s failed: %s", stage_name, exc)
        log.debug("%s", traceback.format_exc())
        status["errors"].append({"stage": stage_name, "type": type(exc).__name__,
                                 "message": str(exc)[:500]})
        if stage_name in status["stages"]:
            status["stages"][stage_name]["state"] = "failed"

    @staticmethod
    def _final_level(status: dict, hyd_level: str, screen_doc: dict | None) -> str:
        stages = status["stages"]
        if stages["screening"]["state"] == "failed":
            return "unknown"
        if stages["hydrology"]["state"] == "failed":
            return "unknown"
        if stages["hydrology"]["state"] == "done":
            return max_level(hyd_level)
        if screen_doc and screen_doc["decision"]["outcome"] == "do_not_activate":
            return "none"
        # Screening triggered (or could not decide) but hydrology did not run, e.g. an older
        # catch-up cycle: the level cannot be asserted.
        return "unknown"
