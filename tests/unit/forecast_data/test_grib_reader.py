import numpy as np
import pytest

from floodcast.data.data_manager import DataManager
from floodcast.data.errors import DataError
from floodcast.data.utils.grib import EcCodesReader


class FakeEcCodes:
    def __init__(self, metadata=None, bitmap=None):
        self.new_calls = 0
        self.released = 0
        self.metadata = dict(
            gridType="regular_ll",
            centre="ecmf",
            numberOfPoints=4,
            iDirectionIncrementInDegrees=0.25,
            jDirectionIncrementInDegrees=0.25,
            bitmapPresent=0,
            stepUnits=1,
            startStep=0,
            endStep=6,
            dataDate=20261117,
            dataTime=0,
            paramId=228,
            units="m",
            stepType="accum",
        )
        self.metadata.update(metadata or {})
        self.bitmap = bitmap

    def codes_grib_new_from_file(self, stream):
        self.new_calls += 1
        return 1 if self.new_calls == 1 else None

    def codes_get(self, handle, key):
        return self.metadata[key]

    def codes_get_values(self, handle):
        return np.array([0.001, 0.002, 0.003, 0.004])

    def codes_get_array(self, handle, key):
        return {
            "latitudes": [40, 40, 39, 39],
            "longitudes": [351, 352, 351, 352],
            "bitmap": self.bitmap,
        }[key]

    def codes_is_defined(self, handle, key):
        return key in self.metadata

    def codes_release(self, handle):
        self.released += 1


def test_decoder_reads_native_units_bounds_and_releases(config_factory, tmp_path):
    f = config_factory().forecast
    part = DataManager(config_factory()).plan().parts[0]
    p = tmp_path / "fake"
    p.write_bytes(b"not-a-live-grib")
    fake = FakeEcCodes()
    messages = list(EcCodesReader(fake).read(p, part, f))
    assert len(messages) == 1 and messages[0].end_hour == 6 and (messages[0].member == 0)
    assert messages[0].units == "m" and fake.released == 1


@pytest.mark.parametrize(
    "metadata",
    [
        {"gridType": "reduced_gg"},
        {"centre": "kwbc"},
        {"numberOfPoints": 100000000},
        {"stepUnits": 99},
        {"number": 1},
    ],
)
def test_decoder_rejects_invalid_metadata_and_releases(config_factory, tmp_path, metadata):
    f = config_factory().forecast
    part = DataManager(config_factory()).plan().parts[0]
    p = tmp_path / "fake"
    p.write_bytes(b"x")
    fake = FakeEcCodes(metadata)
    with pytest.raises(DataError):
        list(EcCodesReader(fake).read(p, part, f))
    assert fake.released == 1


def test_decoder_perturbed_requires_member(config_factory, tmp_path):
    f = config_factory().forecast
    part = DataManager(config_factory()).plan().parts[1]
    p = tmp_path / "fake"
    p.write_bytes(b"x")
    with pytest.raises(DataError, match="member"):
        list(EcCodesReader(FakeEcCodes()).read(p, part, f))


def test_decoder_bitmap_propagates_missing(config_factory, tmp_path):
    f = config_factory().forecast
    part = DataManager(config_factory()).plan().parts[0]
    p = tmp_path / "fake"
    p.write_bytes(b"x")
    message = list(EcCodesReader(FakeEcCodes({"bitmapPresent": 1}, [1, 0, 1, 1])).read(p, part, f))[
        0
    ]
    assert np.isnan(message.values[1])
