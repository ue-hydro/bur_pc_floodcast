"""Tests for failure detection in the reference evaluator; no Docker required."""

from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "integration" / "hydraulics"
spec = importlib.util.spec_from_file_location("verify_fluxos", ROOT / "verify_fluxos.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class OutputEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.expected = json.loads(
            (ROOT / "cases" / "subthreshold-storage" / "expected.json").read_text()
        )
        for name, item in self.expected["snapshots"].items():
            self.write(name, item["volume_m3"])

    def write(
        self,
        name,
        depth,
        *,
        cell=(2, 2),
        coordinates=None,
        duplicate=False,
        short=False,
    ):
        headers = self.expected["output_columns"]
        if short:
            headers = headers[:-1]
        row = [
            cell[0],
            cell[1],
            (100 + cell[1]) if coordinates is None else coordinates[0],
            (200 + cell[0]) if coordinates is None else coordinates[1],
            10 + float(depth) if str(depth) != "nan" else 10,
            depth,
            0,
            0,
            0,
            0,
            0,
            -1,
            0,
            0,
            0,
        ]
        with (self.directory / name).open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(headers)
            writer.writerow(row[: len(headers)])
            if duplicate:
                writer.writerow(row[: len(headers)])

    def test_analytical_storage(self):
        result = verify.evaluate_outputs(self.directory, self.expected)
        self.assertAlmostEqual(result["2.txt"]["volume_m3"], 0.0002)

    def test_missing_snapshot_rejected(self):
        (self.directory / "2.txt").unlink()
        with self.assertRaisesRegex(ValueError, "coverage"):
            verify.evaluate_outputs(self.directory, self.expected)

    def test_extra_snapshot_rejected(self):
        self.write("3.txt", 0.0003)
        with self.assertRaisesRegex(ValueError, "coverage"):
            verify.evaluate_outputs(self.directory, self.expected)

    def test_zero_volume_rejected(self):
        self.write("2.txt", 0)
        with self.assertRaisesRegex(ValueError, "Volume"):
            verify.evaluate_outputs(self.directory, self.expected)

    def test_nonfinite_rejected(self):
        self.write("2.txt", "nan")
        with self.assertRaisesRegex(ValueError, "Non-finite"):
            verify.evaluate_outputs(self.directory, self.expected)

    def test_negative_depth_rejected(self):
        self.write("2.txt", -0.0002)
        with self.assertRaisesRegex(ValueError, "Negative"):
            verify.evaluate_outputs(self.directory, self.expected)

    def test_wrong_cell_rejected(self):
        self.write("2.txt", 0.0002, cell=(3, 2))
        with self.assertRaisesRegex(ValueError, "locations"):
            verify.evaluate_outputs(self.directory, self.expected)

    def test_duplicate_cell_rejected(self):
        self.expected["snapshots"]["2.txt"]["wet_cells"] = 2
        self.write("2.txt", 0.0001, duplicate=True)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            verify.evaluate_outputs(self.directory, self.expected)

    def test_missing_column_rejected(self):
        self.write("2.txt", 0.0002, short=True)
        with self.assertRaisesRegex(ValueError, "columns"):
            verify.evaluate_outputs(self.directory, self.expected)

    def test_path_escape_rejected(self):
        self.expected["snapshots"]["../external.txt"] = self.expected["snapshots"].pop("2.txt")
        with self.assertRaisesRegex(ValueError, "plain"):
            verify.evaluate_outputs(self.directory, self.expected)

    def test_wet_spreading_required(self):
        item = self.expected["snapshots"]["2.txt"]
        item.pop("wet_cells")
        item["minimum_wet_cells"] = 2
        with self.assertRaisesRegex(ValueError, "spreading"):
            verify.evaluate_outputs(self.directory, self.expected)

    def test_empty_dry_domain_accepted(self):
        for name, item in self.expected["snapshots"].items():
            item.update(wet_cells=0, cell_ids=[], volume_m3=0)
            with (self.directory / name).open("w", newline="") as stream:
                csv.writer(stream).writerow(self.expected["output_columns"])
        metrics = verify.evaluate_outputs(self.directory, self.expected)
        self.assertEqual(metrics["2.txt"]["wet_cells"], 0)

    def test_indices_outside_domain_rejected_without_exact_cell_list(self):
        self.expected["snapshots"]["2.txt"].pop("cell_ids")
        for cell in ((1000, 2), (2, 1000)):
            with self.subTest(cell=cell):
                self.write("2.txt", 0.0002, cell=cell)
                with self.assertRaisesRegex(ValueError, "cell index"):
                    verify.evaluate_outputs(self.directory, self.expected)

    def test_coordinates_must_agree_with_indices(self):
        self.write("2.txt", 0.0002, coordinates=(102.5, 202))
        with self.assertRaisesRegex(ValueError, "Coordinates disagree"):
            verify.evaluate_outputs(self.directory, self.expected)

    def test_unexpected_boundary_wetting_rejected(self):
        self.expected["snapshots"]["2.txt"]["forbid_boundary_wet"] = True
        self.write("2.txt", 0.0002, cell=(1, 2))
        with self.assertRaisesRegex(ValueError, "boundary wetting"):
            verify.evaluate_outputs(self.directory, self.expected)

    def test_cell_area_must_agree_with_grid(self):
        self.expected["grid"]["cellsize"] = 2
        with self.assertRaisesRegex(ValueError, "Cell area"):
            verify.evaluate_outputs(self.directory, self.expected)

    def test_expected_grid_must_match_dem(self):
        config = self.directory / "modset.json"
        config.write_text(
            json.dumps({"DEM_FILE": str(ROOT / "cases/subthreshold-storage/dem.asc")})
        )
        verify.verify_grid_definition(config, self.expected)
        self.expected["grid"]["nrows"] = 6
        with self.assertRaisesRegex(ValueError, "differs from fixture DEM"):
            verify.verify_grid_definition(config, self.expected)


if __name__ == "__main__":
    unittest.main()
