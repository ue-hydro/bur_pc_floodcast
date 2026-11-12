"""Offline tests for rejecting plausible but invalid model evidence."""

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import netCDF4
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_outputs import time_axis, values, verify_inputs  # noqa: E402


class EvidenceValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_calendar_units_and_missing_time_step(self):
        with netCDF4.Dataset(self.root / "time.nc", "w") as dataset:
            dataset.createDimension("time", 3)
            time = dataset.createVariable("time", "f8", ("time",))
            time.units = "days since 2000-01-01 00:00:00"
            time[:] = [0, 1, 2]
            time_axis(time, datetime(2000, 1, 1), 3, 86400)
            time[:] = [0, 1, 3]
            with self.assertRaisesRegex(ValueError, "time axis"):
                time_axis(time, datetime(2000, 1, 1), 3, 86400)

    def test_missing_and_nonfinite_values_are_failures(self):
        with netCDF4.Dataset(self.root / "values.nc", "w") as dataset:
            dataset.createDimension("time", 2)
            variable = dataset.createVariable("runoff", "f8", ("time",))
            variable[0] = 1
            with self.assertRaisesRegex(ValueError, "missing"):
                values(variable)
            variable[:] = [1, np.nan]
            with self.assertRaisesRegex(ValueError, "non-finite"):
                values(variable)

    def test_fixture_tampering_is_detected(self):
        path = self.root / "input.txt"
        path.write_bytes(b"original")
        (self.root / "files.json").write_text(
            json.dumps([{"path": "input.txt", "sha256": hashlib.sha256(b"original").hexdigest()}])
        )
        verify_inputs(self.root)
        path.write_bytes(b"altered")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            verify_inputs(self.root)

    def test_fixture_manifest_cannot_escape_root(self):
        (self.root / "files.json").write_text('[{"path": "../other/input.nc", "sha256": "wrong"}]')
        with self.assertRaisesRegex(ValueError, "escapes"):
            verify_inputs(self.root)


if __name__ == "__main__":
    unittest.main()
