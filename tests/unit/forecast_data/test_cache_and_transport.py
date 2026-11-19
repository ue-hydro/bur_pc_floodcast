import json

import pytest

from floodcast.data.acquisition.services.cds import CDSService
from floodcast.data.acquisition.services.ecds import ECDSService
from floodcast.data.acquisition.services.ecmwf_open_data import OpenDataService
from floodcast.data.cache.raw_forecast_cache import RawForecastCache
from floodcast.data.catalog import S2S, SEASONAL
from floodcast.data.data_manager import DataManager
from floodcast.data.errors import DataError
from floodcast.data.utils.grib import count_grib_messages
from floodcast.data.utils.storage import safe_file


@pytest.mark.parametrize("body", [b"", b"<html>403</html>", b"GRIB1234", b"PK1234567"])
def test_bad_raw_files_rejected(tmp_path, body):
    path = tmp_path / "bad"
    path.write_bytes(body)
    with pytest.raises(DataError):
        count_grib_messages(path)


def test_grib_framing_counts_not_scientific_validation(tmp_path, framed_grib):
    p = tmp_path / "x.grib"
    p.write_bytes(framed_grib * 3)
    assert count_grib_messages(p) == 3
    p.write_bytes(framed_grib[:-1])
    with pytest.raises(DataError):
        count_grib_messages(p)


def test_cache_reuses_verified_assets(config_factory, fake_services):
    manager = DataManager(config_factory(), services=fake_services)
    result = manager.acquire_forcings()
    assert fake_services.calls == 2
    assert manager.acquire_forcings() == result
    assert fake_services.calls == 2
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["status"] == "downloaded_not_scientifically_validated"
    assert manager.plan().request_sha256 in str(result.manifest_path)


def test_corruption_is_error_not_reuse(config_factory, fake_services):
    manager = DataManager(config_factory(), services=fake_services)
    result = manager.acquire_forcings()
    result.files[0].write_bytes(b"corrupt")
    with pytest.raises(DataError, match="integrity"):
        manager.acquire_forcings()
    assert fake_services.calls == 2


def test_partial_resume_uses_committed_parts(config_factory, fake_services):
    fake_services.fail_on = 2
    manager = DataManager(config_factory(), services=fake_services)
    with pytest.raises(RuntimeError):
        manager.acquire_forcings()
    assert not list(manager.config.data_root.rglob("acquisition_manifest.json"))
    fake_services.fail_on = None
    result = manager.acquire_forcings()
    assert fake_services.calls == 3
    assert len(result.files) == 2
    assert not list(result.manifest_path.parent.glob("*.part"))


def test_partial_cache_cannot_change_request(config_factory, fake_services, tmp_path):
    first = DataManager(config_factory()).plan()
    fake_services.fail_on = 2
    cache = RawForecastCache(tmp_path / "forced-same-root")
    with pytest.raises(RuntimeError):
        cache.acquire(first, fake_services)
    second = DataManager(config_factory(members=[0])).plan()
    with pytest.raises(DataError, match="another request"):
        cache.acquire(second, fake_services)


def test_damaged_manifest_not_silently_redownloaded(config_factory, fake_services):
    manager = DataManager(config_factory(), services=fake_services)
    result = manager.acquire_forcings()
    result.manifest_path.write_text("{")
    with pytest.raises(DataError):
        manager.acquire_forcings()
    assert fake_services.calls == 2


@pytest.mark.parametrize("relative", ["../secret", "/tmp/secret", "a/../../b", "C:\\secret"])
def test_unsafe_manifest_paths_rejected(tmp_path, relative):
    with pytest.raises(DataError):
        safe_file(tmp_path, relative)


def test_open_data_sdk_receives_explicit_selection(config_factory, tmp_path):
    plan = DataManager(config_factory()).plan()
    seen = {}

    class Client:
        def __init__(self, **kwargs):
            seen["init"] = kwargs

        def retrieve(self, **kwargs):
            seen["retrieve"] = kwargs

    OpenDataService(Client).retrieve(plan.parts[0], plan.config, tmp_path / "x")
    assert seen["init"] == dict(
        source="ecmwf",
        model="ifs",
        resol="0p25",
        infer_stream_keyword=False,
        maximum_retries=2,
        retry_after=2,
    )
    assert seen["retrieve"]["request"]["date"] == "20261117"


@pytest.mark.parametrize(
    "product,service,environment,endpoint",
    [
        (S2S, ECDSService, "FLOODCAST_ECDS_API_KEY", "https://ecds.ecmwf.int/api"),
        (SEASONAL, CDSService, "FLOODCAST_CDS_API_KEY", "https://cds.climate.copernicus.eu/api"),
    ],
)
def test_store_endpoint_and_secret_handling(
    config_factory, tmp_path, monkeypatch, product, service, environment, endpoint
):
    plan = DataManager(config_factory(product)).plan()
    monkeypatch.delenv(environment, raising=False)
    with pytest.raises(DataError, match=environment):
        service().retrieve(plan.parts[0], plan.config, tmp_path / "x")
    secret = "NEVER_SERIALIZE_ME"
    monkeypatch.setenv(environment, secret)
    seen = {}

    class Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def retrieve(self, *args):
            raise RuntimeError(f"url?key={secret}")

    with pytest.raises(DataError) as err:
        service(Client).retrieve(plan.parts[0], plan.config, tmp_path / "x")
    assert seen["url"] == endpoint and seen["key"] == secret
    assert secret not in str(err.value) and secret not in plan.model_dump_json()
    assert err.value.__suppress_context__ is True


def test_store_cannot_cross_products(config_factory, tmp_path):
    plan = DataManager(config_factory(SEASONAL)).plan()
    with pytest.raises(DataError, match="does not belong"):
        ECDSService().retrieve(plan.parts[0], plan.config, tmp_path / "x")
