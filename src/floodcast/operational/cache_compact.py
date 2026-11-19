"""Shrink a finished forecast's ECMWF download cache to the basin.

The open-data files cover a large area (the ensemble ones are 1-3 GB per forecast), while the model
only reads a few 0.25 deg cells around the basin. ``compact_cycle`` re-encodes every GRIB message of a
cycle's cache to the domain bounding box plus a margin, keeping the file names and GRIB format, so
``ifs.read_grib`` and the cache reuse in ``ifs.download`` work unchanged (a re-run of that forecast
still finds its files). Each file is verified against the original before it is replaced.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

log = logging.getLogger("floodcast.operational.cache")
DONE_MARK = ".compacted"


def _crop_file(src: Path, west: float, south: float, east: float, north: float) -> tuple[int, int]:
    import eccodes as ec

    tmp = src.with_suffix(".crop.tmp")
    n = 0
    with src.open("rb") as fin, tmp.open("wb") as fout:
        while (msg := ec.codes_grib_new_from_file(fin)) is not None:
            try:
                if ec.codes_get(msg, "gridType") != "regular_ll":
                    ec.codes_write(msg, fout)      # leave anything unusual untouched
                    continue
                ni, nj = ec.codes_get(msg, "Ni"), ec.codes_get(msg, "Nj")
                lats = ec.codes_get_array(msg, "latitudes").reshape(nj, ni)[:, 0]
                lons_raw = ec.codes_get_array(msg, "longitudes").reshape(nj, ni)[0, :]
                lons = np.where(lons_raw > 180.0, lons_raw - 360.0, lons_raw)
                ys = np.where((lats >= south) & (lats <= north))[0]
                xs = np.where((lons >= west) & (lons <= east))[0]
                if ys.size == 0 or xs.size == 0 or np.any(np.diff(xs) != 1) or np.any(np.diff(ys) != 1):
                    ec.codes_write(msg, fout)
                    continue
                vals = ec.codes_get_values(msg).reshape(nj, ni)[ys[0]:ys[-1] + 1, xs[0]:xs[-1] + 1]
                out = ec.codes_clone(msg)
                try:
                    ec.codes_set(out, "Ni", int(vals.shape[1]))
                    ec.codes_set(out, "Nj", int(vals.shape[0]))
                    ec.codes_set(out, "latitudeOfFirstGridPointInDegrees", float(lats[ys[0]]))
                    ec.codes_set(out, "latitudeOfLastGridPointInDegrees", float(lats[ys[-1]]))
                    ec.codes_set(out, "longitudeOfFirstGridPointInDegrees", float(lons_raw[xs[0]]))
                    ec.codes_set(out, "longitudeOfLastGridPointInDegrees", float(lons_raw[xs[-1]]))
                    ec.codes_set_values(out, vals.ravel())
                    ec.codes_write(out, fout)
                finally:
                    ec.codes_release(out)
                n += 1
            finally:
                ec.codes_release(msg)
    return n, tmp.stat().st_size


def compact_cycle(cache_dir: Path, bbox, margin_deg: float = 0.5) -> dict:
    """Crop every *.grib2 under ``cache_dir`` (incl. ens/) to ``bbox`` + margin, verified."""
    from .ifs import Bbox, read_grib

    cache_dir = Path(cache_dir)
    if (cache_dir / DONE_MARK).exists():
        return {"skipped": "already compacted"}
    w, s, e, n = bbox.west - margin_deg, bbox.south - margin_deg, bbox.east + margin_deg, bbox.north + margin_deg
    before = after = 0
    for src in sorted(cache_dir.rglob("*.grib2")):
        size0 = src.stat().st_size
        try:
            _, size1 = _crop_file(src, w, s, e, n)
            tmp = src.with_suffix(".crop.tmp")
            # verify: what the model reads (the padded basin bbox) is identical within packing error
            a = read_grib([src], bbox)
            b = read_grib([tmp], bbox)
            ok = a.keys() == b.keys() and all(
                np.allclose(a[k].data, b[k].data, rtol=1e-4, atol=1e-6, equal_nan=True)
                and np.array_equal(a[k].steps_h, b[k].steps_h) for k in a)
            if not ok:
                tmp.unlink(missing_ok=True)
                log.warning("cache compaction: %s kept as is (verification failed)", src.name)
                before += size0; after += size0
                continue
            os.replace(tmp, src)
            before += size0; after += size1
        except Exception as exc:  # never lose data: leave the original
            src.with_suffix(".crop.tmp").unlink(missing_ok=True)
            log.warning("cache compaction: %s kept as is (%s)", src.name, exc)
            before += size0; after += size0
    (cache_dir / DONE_MARK).write_text(f"{before} -> {after} bytes\n")
    return {"before_mb": round(before / 1e6, 1), "after_mb": round(after / 1e6, 1)}
