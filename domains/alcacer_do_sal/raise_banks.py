#!/usr/bin/env python3
"""Give the FLUXOS terrain river banks that hold the tide.

The 30 m surface model has no quay walls or dikes along the Sado: with the tide at its real height
(calibrated on the APA gauge 23F/01H), the riverfront of Alcácer and the floodplain flooded at
every spring tide (from ~1.5 m above mean sea level). The APA gauge in town measured up to 2.2 m in
Aug-Nov 2026 without the town flooding, so the real banks hold at least that. This script raises a
band of BANK_CELLS cells along both sides of the river (outside the permanent-water mask: carved
channel + margin, impacts.permanent_water) to at least CREST_M, wherever the terrain is lower.
Water above the crest still overtops the bank, so river floods are unaffected above that level.

Input fluxos/dem_channel.asc (carved channel, +10 m datum); output fluxos/dem_banks.asc, the
terrain FLUXOS uses ([fluxos] dem in domain.toml). Re-run after burn_channel.py.

    python domains/alcacer_do_sal/raise_banks.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from scipy import ndimage as nd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))
from floodcast.operational import impacts  # noqa: E402

CREST_M = 2.3          # m above mean sea level: just above the highest level the gauge measured (2.2 m)
BANK_CELLS = 2         # band width (cells of 20 m) on each side of the river
DATUM_OFFSET_M = 10.0  # as burn_channel.py


def main() -> None:
    src, dst = HERE / "fluxos" / "dem_channel.asc", HERE / "fluxos" / "dem_banks.asc"
    lines = src.read_text().splitlines()
    header, body = lines[:6], lines[6:]
    hdr = {ln.split()[0].lower(): float(ln.split()[1]) for ln in header}
    z = np.loadtxt(body)                                  # rows north-first, as in the file
    nodata = hdr.get("nodata_value", -9999.0)
    river = np.flipud(impacts.permanent_water(HERE))      # mask is south-first
    band = nd.binary_dilation(river, iterations=BANK_CELLS) & ~river
    crest = CREST_M + DATUM_OFFSET_M
    raise_ = band & (z != nodata) & (z < crest)
    z2 = np.where(raise_, crest, z)
    dz = (z2 - z)[raise_]
    with dst.open("w") as fh:
        fh.write("\n".join(header) + "\n")
        np.savetxt(fh, z2, fmt="%.3f")
    print(f"{dst.name}: {int(raise_.sum())} bank cells raised to {CREST_M} m MSL "
          f"(mean +{dz.mean():.2f} m, max +{dz.max():.2f} m); band {BANK_CELLS} cells")


if __name__ == "__main__":
    main()
