import logging
from datetime import UTC, datetime

import numpy as np
import pytest

from floodcast.data.catalog import IFS, S2S, SEASONAL, variables
from floodcast.data.config import DataConfig, ForecastConfig
from floodcast.data.utils.grib import FieldMessage


@pytest.fixture
def logger():
    return logging.getLogger("test.forecast")


@pytest.fixture
def config_factory(tmp_path):

    def build(product=IFS, **changes):
        values = dict(
            product=product,
            cycle=datetime(2026, 11, 17, tzinfo=UTC),
            members=(0, 1),
            fields={"tp": (6, 12)},
        )
        if product == IFS:
            values["resolution"] = "0p25"
        elif product == SEASONAL:
            values.update(
                cycle=datetime(2026, 11, 1, tzinfo=UTC), system="51", fields={"tp": (24, 48)}
            )
        values.update(changes)
        return DataConfig(
            domain_id="test_basin", data_root=tmp_path, forecast=ForecastConfig(**values)
        )

    return build


@pytest.fixture
def message_factory():

    def build(config, field="tp", member=0, lead=6, value=None, **changes):
        spec = variables(config.product)[field]
        units = {
            "tp": "kg m**-2" if config.product == S2S else "m",
            "ssrd": "J m**-2",
            "strd": "J m**-2",
            "2t": "K",
            "2d": "K",
            "10u": "m s**-1",
            "10v": "m s**-1",
            "sp": "Pa",
            "msl": "Pa",
        }[field]
        if value is None:
            value = lead * 0.001 * (member + 1) if field == "tp" else 280.0
        start = 0 if spec.kind == "accum" else lead - 24 if spec.kind == "avg" else lead
        payload = dict(
            param_id=spec.param_ids[0],
            units=units,
            step_type=spec.kind,
            start_hour=start,
            end_hour=lead,
            cycle=config.cycle,
            member=member,
            role="ensemble"
            if config.product == SEASONAL
            else "control"
            if member == 0
            else "perturbed",
            latitude=np.array([40.0, 40.0, 39.0, 39.0]),
            longitude=np.array([351.0, 352.0, 351.0, 352.0]),
            values=np.full(4, value),
            source_path="synthetic.grib",
            message_index=1,
        )
        payload.update(changes)
        return FieldMessage(**payload)

    return build


@pytest.fixture
def framed_grib():
    return b"GRIB" + b"\x00\x00\x00\x02" + (20).to_bytes(8, "big") + b"7777"


@pytest.fixture
def fake_services(framed_grib):

    class FakeServices:
        calls = 0
        fail_on = None

        def retrieve(self, part, config, target):
            self.calls += 1
            if self.fail_on == self.calls:
                raise RuntimeError("injected download failure")
            target.write_bytes(framed_grib)

    return FakeServices()


@pytest.fixture
def fake_reader(message_factory):

    class Reader:
        calls = 0

        def read(self, path, part, config):
            self.calls += 1
            catalogue = variables(config.product)
            if part.transport == "open_data":
                field = part.payload["param"][0]
                leads = part.payload["step"]
            else:
                field = next(
                    (
                        key
                        for key, spec in catalogue.items()
                        if spec.api_name == part.payload["variable"][0]
                    )
                )
                leads = [int(h) for h in part.payload["leadtime_hour"]]
            members = [0] if part.role == "control" else [m for m in config.members if m > 0]
            if part.role == "ensemble":
                members = config.members
            for member in members:
                for lead in leads:
                    yield message_factory(config, field, member, lead)

    return Reader()
