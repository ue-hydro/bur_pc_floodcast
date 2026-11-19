"""Real ecCodes encoding/decoding tests; fields are synthetic, not forecasts."""

import numpy as np
import pytest

from floodcast.data.catalog import IFS, S2S, SEASONAL, variables
from floodcast.data.data_manager import DataManager
from floodcast.data.errors import DataError
from floodcast.data.preprocessing.dataset_handlers.ifs_utils import IFSHandler
from floodcast.data.preprocessing.dataset_handlers.s2s_utils import S2SHandler
from floodcast.data.preprocessing.dataset_handlers.seasonal_utils import SeasonalHandler
from floodcast.data.utils.grib import count_grib_messages
from floodcast.data.utils.grib import EcCodesReader


@pytest.fixture
def native_grib(tmp_path):
    ec = pytest.importorskip("eccodes")

    def write(config, field, member, lead, *, overrides=None):
        edition = 1 if config.product == SEASONAL else 2
        handle = ec.codes_grib_new_from_samples(f"regular_ll_sfc_grib{edition}")
        spacing = {IFS: 0.25, S2S: 1.5, SEASONAL: 1.0}[config.product]
        spec = variables(config.product)[field]
        path = tmp_path / f"{config.product}-{field}-{member}-{lead}.grib"
        try:
            settings = {
                "centre": "ecmf",
                "Ni": 2,
                "Nj": 2,
                "latitudeOfFirstGridPointInDegrees": 40.0,
                "latitudeOfLastGridPointInDegrees": 40.0 - spacing,
                "longitudeOfFirstGridPointInDegrees": 351.0,
                "longitudeOfLastGridPointInDegrees": 351.0 + spacing,
                "iDirectionIncrementInDegrees": spacing,
                "jDirectionIncrementInDegrees": spacing,
                "jScansPositively": 0,
                "dataDate": int(config.cycle.strftime("%Y%m%d")),
                "dataTime": config.cycle.hour * 100,
                "generatingProcessIdentifier": 161,
            }
            for key, value in settings.items():
                ec.codes_set(handle, key, value)
            if edition == 1:
                ec.codes_set(handle, "setLocalDefinition", 1)
                ec.codes_set(handle, "localDefinitionNumber", 16)
                ec.codes_set(handle, "systemNumber", int(config.system))
                ec.codes_set(handle, "dataType", "fc")
                ec.codes_set(handle, "number", member)
            else:
                ec.codes_set(handle, "tablesVersion", 35)
                is_ensemble = config.product == S2S or member > 0
                ec.codes_set(
                    handle,
                    "typeOfProcessedData",
                    3 if is_ensemble and member == 0 else 4 if is_ensemble else 1,
                )
                if is_ensemble:
                    ec.codes_set(handle, "productDefinitionTemplateNumber", 1)
                    ec.codes_set(handle, "number", member)
                    ec.codes_set(
                        handle, "numberOfForecastsInEnsemble", 101 if config.product == S2S else 51
                    )
            ec.codes_set(handle, "stepType", spec.kind)
            ec.codes_set(handle, "paramId", spec.param_ids[0])
            ec.codes_set(handle, "stepUnits", 1)
            ec.codes_set(
                handle,
                "startStep",
                lead - 24 if spec.kind == "avg" else 0 if spec.kind == "accum" else lead,
            )
            ec.codes_set(handle, "endStep", lead)
            for key, value in (overrides or {}).items():
                ec.codes_set(handle, key, value)
            values = np.full(
                4,
                6.0
                if config.product == S2S and field == "tp"
                else 0.006
                if field == "tp"
                else 280.0,
            )
            ec.codes_set_values(handle, values)
            with path.open("wb") as stream:
                ec.codes_write(handle, stream)
        finally:
            ec.codes_release(handle)
        return path

    return write


@pytest.mark.parametrize(
    "product,handler", [(IFS, IFSHandler), (S2S, S2SHandler), (SEASONAL, SeasonalHandler)]
)
def test_native_grib_precipitation_round_trip(
    config_factory, logger, native_grib, product, handler
):
    lead = 24 if product == SEASONAL else 6
    config = config_factory(product, fields={"tp": [lead]}).forecast
    plan = DataManager(config_factory(product, fields={"tp": [lead]})).plan()
    messages = []
    for member in config.members:
        part = next(
            part
            for part in plan.parts
            if part.role == "ensemble" or part.role == ("control" if member == 0 else "perturbed")
        )
        path = native_grib(config, "tp", member, lead)
        assert count_grib_messages(path) == 1
        messages.extend(EcCodesReader().read(path, part, config))
    output, observed = handler(config, logger).process_dataset(messages)
    assert observed == (0, 1)
    np.testing.assert_allclose(output["tp"].precipitation_flux, 6.0 / (lead * 3600), rtol=1e-5)
    assert output["tp"].attrs["ifs_cycle"] == ("not_applicable" if product == SEASONAL else "50r1")
    assert '"edition": ' in output["tp"].attrs["observed_grib_encodings"]


@pytest.mark.parametrize("field,actual_id", [("2t", 228004), ("2d", 235168)])
def test_native_s2s_mean_parameter_aliases(config_factory, logger, native_grib, field, actual_id):
    config = config_factory(S2S, fields={field: [24]}, members=[0]).forecast
    plan = DataManager(config_factory(S2S, fields={field: [24]}, members=[0])).plan()
    path = native_grib(config, field, 0, 24)
    messages = list(EcCodesReader().read(path, plan.parts[0], config))
    assert messages[0].param_id == actual_id
    output, _ = S2SHandler(config, logger).process_dataset(messages)
    variable = variables(S2S)[field].canonical_name
    assert output[field][variable].attrs["temporal_semantics"] == "avg"
    np.testing.assert_allclose(output[field][variable], 280.0)


@pytest.mark.parametrize(
    "overrides,error",
    [
        ({"generatingProcessIdentifier": 158}, "MODEL_VERSION_MISMATCH"),
        ({"typeOfProcessedData": 3}, "PRODUCT_MISMATCH"),
    ],
)
def test_native_ifs_identity_must_match_request(config_factory, native_grib, overrides, error):
    config = config_factory(members=[0], fields={"tp": [6]})
    part = DataManager(config).plan().parts[0]
    path = native_grib(config.forecast, "tp", 0, 6, overrides=overrides)
    with pytest.raises(DataError) as exc:
        list(EcCodesReader().read(path, part, config.forecast))
    assert exc.value.code == error


def test_native_seasonal_system_must_match_request(config_factory, native_grib):
    config = config_factory(SEASONAL, members=[0], fields={"tp": [24]})
    part = DataManager(config).plan().parts[0]
    path = native_grib(config.forecast, "tp", 0, 24, overrides={"systemNumber": 5})
    with pytest.raises(DataError, match="seasonal system"):
        list(EcCodesReader().read(path, part, config.forecast))
