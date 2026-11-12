"""Mutation regressions against actual model outputs, never against fabricated runs.

Set FLOODCAST_HYDROLOGY_OUTPUTS to a completed check directory containing
summa-reference, summa-reference-ida and mizuroute-reference. Missing evidence is
reported as skipped, never passed. All mutations are confined to temporary copies.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import netCDF4
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_outputs import validate_mizuroute, validate_summa  # noqa: E402

EVIDENCE = os.environ.get("FLOODCAST_HYDROLOGY_OUTPUTS")


@unittest.skipUnless(EVIDENCE, "actual model outputs not supplied")
class ModelOutputRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evidence = Path(EVIDENCE)
        cls.original_hashes = cls.hashes()

    @classmethod
    def hashes(cls):
        return {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in cls.evidence.rglob("*.nc")
        }

    @classmethod
    def tearDownClass(cls):
        if cls.hashes() != cls.original_hashes:
            raise AssertionError("original model evidence changed during mutation tests")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="floodcast-validator-")
        self.output = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def stage(self, check):
        paths = list((self.evidence / check).glob("*.nc"))
        self.assertTrue(paths, f"no actual outputs for {check}")
        for path in paths:
            shutil.copyfile(path, self.output / path.name)

    def summa(self, ida=False):
        case = "celia1990-ida" if ida else "celia1990"
        return validate_summa(ROOT / f"tests/fixtures/summa/{case}/data", self.output)

    def mizu(self):
        return validate_mizuroute(ROOT / "tests/fixtures/mizuroute/cameo/data", self.output)

    def head(self):
        return netCDF4.Dataset(next(self.output.glob("*timestep.nc")), "a")

    def routing(self):
        return netCDF4.Dataset(next(self.output.glob("*.h.*.nc")), "a")

    def test_unmodified_iterative_output_passes(self):
        self.stage("summa-reference")
        self.assertEqual(self.summa()["status"], "passed")

    def test_unmodified_ida_output_passes(self):
        self.stage("summa-reference-ida")
        self.assertEqual(self.summa(ida=True)["status"], "passed")

    def test_unmodified_routing_output_passes(self):
        self.stage("mizuroute-reference")
        report = self.mizu()
        self.assertEqual(report["analytic_headwater_count"], 2507)
        self.assertEqual(report["volume_balance_reach_count"], 6895)

    def test_iterative_millionfold_head_rejected(self):
        self.stage("summa-reference")
        with self.head() as d:
            d["mLayerMatricHead"][:] *= 1e6
        with self.assertRaisesRegex(ValueError, "numerical reference mismatch"):
            self.summa()

    def test_ida_millionfold_head_rejected(self):
        self.stage("summa-reference-ida")
        with self.head() as d:
            d["mLayerMatricHead"][:] *= 1e6
        with self.assertRaisesRegex(ValueError, "numerical reference mismatch"):
            self.summa(ida=True)

    def test_iterative_small_local_error_rejected(self):
        self.stage("summa-reference")
        with self.head() as d:
            d["mLayerMatricHead"][45, 30, 0] += 0.001
        with self.assertRaisesRegex(ValueError, "numerical reference mismatch"):
            self.summa()

    def test_ida_small_local_error_rejected(self):
        self.stage("summa-reference-ida")
        with self.head() as d:
            d["mLayerMatricHead"][45, 30, 0] += 0.001
        with self.assertRaisesRegex(ValueError, "numerical reference mismatch"):
            self.summa(ida=True)

    def test_iterative_millimetres_rejected(self):
        self.stage("summa-reference")
        with self.head() as d:
            d["mLayerMatricHead"].units = "mm"
        with self.assertRaisesRegex(ValueError, "units"):
            self.summa()

    def test_ida_millimetres_rejected(self):
        self.stage("summa-reference-ida")
        with self.head() as d:
            d["mLayerMatricHead"].units = "mm"
        with self.assertRaisesRegex(ValueError, "units"):
            self.summa(ida=True)

    def test_head_dimension_relabel_rejected(self):
        self.stage("summa-reference")
        with self.head() as d:
            d.renameDimension("midSoil", "unidentified_depth")
        with self.assertRaisesRegex(ValueError, "dimensions"):
            self.summa()

    def test_soil_layer_reversal_rejected(self):
        self.stage("summa-reference-ida")
        with self.head() as d:
            d["mLayerMatricHead"][:] = d["mLayerMatricHead"][:, ::-1, :]
        with self.assertRaisesRegex(ValueError, "numerical reference mismatch"):
            self.summa(ida=True)

    def test_thousandfold_routing_discharge_rejected(self):
        self.stage("mizuroute-reference")
        with self.routing() as d:
            d["DWroutedRunoff"][:] *= 1000
        with self.assertRaisesRegex(ValueError, "headwater hydrographs"):
            self.mizu()

    def test_reversed_discharge_with_unchanged_ids_rejected(self):
        self.stage("mizuroute-reference")
        with self.routing() as d:
            d["DWroutedRunoff"][:] = d["DWroutedRunoff"][:, ::-1]
        with self.assertRaisesRegex(ValueError, "headwater hydrographs"):
            self.mizu()

    def test_reversed_ids_with_unchanged_discharge_rejected(self):
        self.stage("mizuroute-reference")
        with self.routing() as d:
            d["reachID"][:] = d["reachID"][::-1]
        with self.assertRaisesRegex(ValueError, "headwater hydrographs"):
            self.mizu()

    def test_consistent_reach_permutation_passes(self):
        self.stage("mizuroute-reference")
        with self.routing() as d:
            d["reachID"][:] = d["reachID"][::-1]
            d["DWroutedRunoff"][:] = d["DWroutedRunoff"][:, ::-1]
        self.assertEqual(self.mizu()["status"], "passed")

    def test_interior_reach_volume_error_rejected(self):
        self.stage("mizuroute-reference")
        # The largest first-day flow is interior; this leaves headwaters unchanged.
        with self.routing() as d:
            index = int(np.argmax(d["DWroutedRunoff"][0]))
            d["DWroutedRunoff"][0, index] *= 2
        with self.assertRaisesRegex(ValueError, "volume balance by reach ID"):
            self.mizu()

    def test_final_storage_error_rejected(self):
        self.stage("mizuroute-reference")
        with netCDF4.Dataset(next(self.output.glob("*.r.*.nc")), "a") as d:
            d["volume_dw"][:] *= 2
        with self.assertRaisesRegex(ValueError, "volume balance by reach ID"):
            self.mizu()

    def test_zero_time_bounds_rejected(self):
        self.stage("mizuroute-reference")
        with self.routing() as d:
            d["time_bounds"][:] = 0
        with self.assertRaisesRegex(ValueError, "time_bounds"):
            self.mizu()

    def test_bounds_gap_rejected(self):
        self.stage("mizuroute-reference")
        with self.routing() as d:
            d["time_bounds"][1, 0] += 0.5
        with self.assertRaisesRegex(ValueError, "discontinuity"):
            self.mizu()

    def test_last_bound_truncated_rejected(self):
        self.stage("mizuroute-reference")
        with self.routing() as d:
            d["time_bounds"][-1, 1] -= 0.5
        with self.assertRaisesRegex(ValueError, "daily intervals"):
            self.mizu()

    def test_interval_end_timestamp_rejected(self):
        self.stage("mizuroute-reference")
        with self.routing() as d:
            d["time"][:] += 1
        with self.assertRaisesRegex(ValueError, "time axis"):
            self.mizu()

    def test_bounds_units_rejected(self):
        self.stage("mizuroute-reference")
        with self.routing() as d:
            d["time_bounds"].units = "hours since 1949-01-01 00:00:00"
        with self.assertRaisesRegex(ValueError, "units"):
            self.mizu()

    def test_missing_bounds_rejected(self):
        self.stage("mizuroute-reference")
        with self.routing() as d:
            d.renameVariable("time_bounds", "removed_bounds")
        with self.assertRaises(IndexError):
            self.mizu()

    def test_discharge_dimensions_rejected(self):
        self.stage("mizuroute-reference")
        with self.routing() as d:
            d.renameDimension("seg", "unidentified_reach")
        with self.assertRaisesRegex(ValueError, "dimensions"):
            self.mizu()

    def test_restart_wrong_time_rejected(self):
        self.stage("mizuroute-reference")
        with netCDF4.Dataset(next(self.output.glob("*.r.*.nc")), "a") as d:
            d["restart_time"][...] -= 1
        with self.assertRaisesRegex(ValueError, "restart time"):
            self.mizu()


if __name__ == "__main__":
    unittest.main()
