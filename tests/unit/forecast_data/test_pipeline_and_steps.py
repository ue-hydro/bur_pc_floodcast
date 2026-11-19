import json
from types import SimpleNamespace

import numpy as np
import pytest
import xarray as xr

from floodcast.data.catalog import IFS, S2S, SEASONAL
from floodcast.data.data_manager import DataManager
from floodcast.data.errors import DataError
from floodcast.data.utils.storage import file_sha256
from floodcast.data.workflow import register_forecast_steps


@pytest.mark.parametrize("product", [IFS, S2S, SEASONAL])
def test_acquisition_to_real_netcdf_with_fake_transport_and_reader(
    config_factory, fake_services, fake_reader, product
):
    manager = DataManager(config_factory(product), services=fake_services, reader=fake_reader)
    raw = manager.acquire_forcings()
    processed = manager.run_model_agnostic_preprocessing(raw.manifest_path)
    payload = json.loads(processed.manifest_path.read_text())
    assert payload["quality"] == "requested_fields_complete"
    assert payload["model_ready"] is False
    assert payload["raw_manifest_sha256"] == file_sha256(raw.manifest_path)
    with xr.open_dataset(processed.files[0], engine="scipy", decode_timedelta=False) as ds:
        assert ds.sizes["member"] == 2
        assert ds.precipitation_flux.attrs["units"] == "kg m-2 s-1"
        assert np.isfinite(ds.precipitation_flux).all()
        assert "valid_time_bounds" in ds
    calls = fake_reader.calls
    assert manager.run_model_agnostic_preprocessing(raw.manifest_path) == processed
    assert fake_reader.calls == calls


def test_canonical_output_tampering_rejected(config_factory, fake_services, fake_reader):
    manager = DataManager(config_factory(), services=fake_services, reader=fake_reader)
    raw = manager.acquire_forcings()
    result = manager.run_model_agnostic_preprocessing(raw.manifest_path)
    result.files[0].write_bytes(b"broken")
    with pytest.raises(DataError):
        manager.run_model_agnostic_preprocessing(raw.manifest_path)


def test_unknown_declared_schema_is_hard_error(config_factory, fake_services, fake_reader):
    manager = DataManager(config_factory(), services=fake_services, reader=fake_reader)
    raw = manager.acquire_forcings()
    payload = json.loads(raw.manifest_path.read_text())
    payload["declared_schema"] = "other-v3"
    raw.manifest_path.write_text(json.dumps(payload))
    with pytest.raises(DataError, match="schema"):
        manager.run_model_agnostic_preprocessing(raw.manifest_path)


def test_wrong_product_cannot_normalize_raw(config_factory, fake_services, fake_reader):
    raw = DataManager(config_factory(), services=fake_services).acquire_forcings()
    with pytest.raises(DataError):
        DataManager(config_factory(S2S), reader=fake_reader).run_model_agnostic_preprocessing(
            raw.manifest_path
        )


def test_no_partial_success_on_missing_members(config_factory, fake_services, fake_reader):
    manager = DataManager(config_factory(), services=fake_services, reader=fake_reader)
    raw = manager.acquire_forcings()

    class Empty:
        def read(self, *args):
            return iter(())

    manager.processor.reader = Empty()
    with pytest.raises(DataError, match="Missing"):
        manager.run_model_agnostic_preprocessing(raw.manifest_path)
    assert not (raw.manifest_path.parent.parent / "canonical" / "canonical_manifest.json").exists()


def test_stage2_step_adapters_share_R_and_preserve_evidence(
    config_factory, fake_services, fake_reader, logger
):
    config = config_factory()
    manager = DataManager(config, services=fake_services, reader=fake_reader)
    register_forecast_steps(manager.R, services=fake_services, reader=fake_reader)
    request = SimpleNamespace(domain_id=config.domain_id, cycle_time=config.forecast.cycle)
    acquire = manager.R.steps.resolve("forecast.acquire").factory()
    step = SimpleNamespace(configuration={"data": config.model_dump(mode="json")})
    context = SimpleNamespace(request=request, step=step, logger=logger, dependencies={})
    acquired = acquire.execute(context)
    assert acquired.status == "succeeded"
    assert len(acquired.outputs) == 3
    normalize = manager.R.steps.resolve("forecast.normalize").factory()
    step = SimpleNamespace(
        configuration={"data": config.model_dump(mode="json"), "acquisition_step": "get"}
    )
    normalized = normalize.execute(
        SimpleNamespace(request=request, step=step, logger=logger, dependencies={"get": acquired})
    )
    assert normalized.status == "succeeded"
    assert "NOT_MODEL_READY" in normalized.reason_codes[0]
    assert all(file_sha256(ref.path) == ref.sha256 for ref in normalized.outputs)
    request.domain_id = "different_basin"
    with pytest.raises(Exception, match="domain/cycle"):
        normalize.execute(
            SimpleNamespace(
                request=request, step=step, logger=logger, dependencies={"get": acquired}
            )
        )
