"""A broken image profile must not invalidate another independent image group."""

import json
from hashlib import sha256

import pytest

from floodcast.core.errors import ErrorCode, FloodcastError
from floodcast.services.catalog import (
    build_fingerprint,
    load_build_catalog,
    load_catalog,
    recipe_files,
)


@pytest.fixture
def catalog_checkout(tmp_path):
    context = tmp_path / "containers"
    context.mkdir()
    (context / "docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (context / ".dockerignore").write_text("**/__pycache__/\n", encoding="utf-8")
    for group, model_ids in (
        ("hydrology", ("summa", "mizuroute")),
        ("hydraulics", ("fluxos",)),
    ):
        directory = context / group
        directory.mkdir()
        (directory / "Dockerfile").write_text(
            "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n", encoding="utf-8"
        )
        (directory / "rule.json").write_text('{"expected_exit_code":0}', encoding="utf-8")
        models, checks = [], []
        for model in model_ids:
            check_id = f"{model}.load"
            models.append(
                {
                    "model_id": model,
                    "group": group,
                    "executable": f"/opt/{model}",
                    "check_ids": [check_id],
                }
            )
            checks.append(
                {
                    "check_id": check_id,
                    "model_id": model,
                    "level": "load",
                    "argv": [f"/opt/check-{model}"],
                    "timeout_s": 10,
                    "expected_result_ref": {
                        "path": "rule.json",
                        "sha256": sha256((directory / "rule.json").read_bytes()).hexdigest(),
                    },
                }
            )
        (directory / "checks.json").write_text(
            json.dumps({"group": group, "models": models, "checks": checks}),
            encoding="utf-8",
        )
    manifest = "schema_version = 1\n"
    for model in ("summa", "mizuroute", "fluxos"):
        manifest += (
            f'\n[sources.{model}]\nurl = "https://example.org/{model}.git"\n'
            f'kind = "git"\nrevision = "{"a" * 40}"\n'
        )
    for group, sources in (
        ("hydrology", '["summa", "mizuroute"]'),
        ("hydraulics", '["fluxos"]'),
    ):
        manifest += (
            f'\n[groups.{group}]\nplatform = "linux/amd64"\n'
            f'base_ref = "ubuntu@sha256:{"b" * 64}"\nsources = {sources}\n'
            f'validation_profile = "{group}/checks.json"\n'
            f'toolchain_path = "/opt/floodcast/share/{group}/toolchain.json"\n'
            'build_options = { APT_SNAPSHOT = "20261101T000000Z" }\n'
        )
    (context / "model-sources.toml").write_text(manifest, encoding="utf-8")
    return tmp_path


def test_resolves_relative_fixture_paths_and_pinned_build_arguments(catalog_checkout):
    specs, profiles, toolchains = load_catalog(catalog_checkout, ("hydrology",), 2)
    assert set(specs) == {"hydrology"}
    assert specs["hydrology"].build_options["BUILD_JOBS"] == "2"
    assert specs["hydrology"].build_options["SUMMA_REVISION"] == "a" * 40
    assert (
        specs["hydrology"].build_options["MIZUROUTE_REPOSITORY"]
        == "https://example.org/mizuroute.git"
    )
    assert (
        profiles["hydrology"].checks[0].expected_result_ref.path
        == catalog_checkout / "containers" / "hydrology" / "rule.json"
    )
    assert toolchains["hydrology"] == "/opt/floodcast/share/hydrology/toolchain.json"


def test_invalid_unselected_source_does_not_block_other_group(catalog_checkout):
    manifest = catalog_checkout / "containers" / "model-sources.toml"
    text = manifest.read_text("utf-8")
    text = text.replace(
        '[sources.fluxos]\nurl = "https://example.org/fluxos.git"\nkind = "git"\nrevision = "'
        + "a" * 40
        + '"',
        '[sources.fluxos]\nurl = "https://example.org/fluxos.git"\n'
        'kind = "git"\nrevision = "unresolved"',
    )
    manifest.write_text(text, encoding="utf-8")
    specs, _, _ = load_catalog(catalog_checkout, ("hydrology",), 1)
    assert set(specs) == {"hydrology"}
    with pytest.raises(FloodcastError) as exc:
        load_catalog(catalog_checkout, ("hydraulics",), 1)
    assert exc.value.code == ErrorCode.INVALID_CONFIG


def test_profile_path_cannot_escape_its_group(catalog_checkout):
    manifest = catalog_checkout / "containers" / "model-sources.toml"
    manifest.write_text(
        manifest.read_text("utf-8").replace('"hydraulics/checks.json"', '"hydrology/checks.json"'),
        encoding="utf-8",
    )
    with pytest.raises(FloodcastError) as exc:
        load_catalog(catalog_checkout, ("hydraulics",), 1)
    assert exc.value.code == ErrorCode.INVALID_CONFIG


@pytest.mark.parametrize("value", ["true", "2"])
def test_manifest_schema_rejects_boolean_and_unknown_version(catalog_checkout, value):
    manifest = catalog_checkout / "containers" / "model-sources.toml"
    manifest.write_text(
        manifest.read_text("utf-8").replace("schema_version = 1", f"schema_version = {value}"),
        encoding="utf-8",
    )
    with pytest.raises(FloodcastError) as exc:
        load_catalog(catalog_checkout, ("hydrology",), 1)
    assert exc.value.code == ErrorCode.INVALID_CONFIG


def test_build_catalog_never_reads_validation_profiles(catalog_checkout):
    manifest = catalog_checkout / "containers/model-sources.toml"
    text = manifest.read_text("utf-8")
    text = text.replace(
        'sources = ["summa", "mizuroute"]',
        'sources = ["summa", "mizuroute"]\nmodels = ['
        '{model_id="summa", executable="/opt/summa"},'
        '{model_id="mizuroute", executable="/opt/mizuroute"}]',
    )
    manifest.write_text(text, encoding="utf-8")
    before = load_build_catalog(catalog_checkout, ("hydrology",), 1)[0]["hydrology"]
    (catalog_checkout / "containers/hydrology/checks.json").unlink()
    (catalog_checkout / "containers/hydrology/rule.json").unlink()
    after = load_build_catalog(catalog_checkout, ("hydrology",), 1)[0]["hydrology"]
    assert before == after
    assert after.required_check_ids == ()


def test_only_copied_scripts_affect_build_identity(catalog_checkout):
    context = catalog_checkout / "containers"
    scripts = context / "hydrology/scripts"
    scripts.mkdir()
    actual = scripts / "build.py"
    actual.write_text("build-v1", encoding="utf-8")
    generator = scripts / "generate_fixture.py"
    generator.write_text("generator-v1", encoding="utf-8")
    dockerfile = context / "hydrology/Dockerfile"
    dockerfile.write_text(
        "FROM scratch\nCOPY hydrology/scripts/build.py /build.py\n", encoding="utf-8"
    )
    before = load_catalog(catalog_checkout, ("hydrology",), 1)[0]["hydrology"]
    assert generator not in recipe_files(context, "hydrology")
    generator.write_text("generator-v2", encoding="utf-8")
    after_generator = load_catalog(catalog_checkout, ("hydrology",), 1)[0]["hydrology"]
    assert build_fingerprint(before) == build_fingerprint(after_generator)
    actual.write_text("build-v2", encoding="utf-8")
    after_build = load_catalog(catalog_checkout, ("hydrology",), 1)[0]["hydrology"]
    assert build_fingerprint(before) != build_fingerprint(after_build)


def test_test_policy_does_not_change_build_fingerprint(catalog_checkout):
    spec = load_catalog(catalog_checkout, ("hydrology",), 1)[0]["hydrology"]
    changed = spec.model_copy(
        update={
            "required_check_ids": ("new-test",),
            "dependency_lock": spec.dependency_lock.model_copy(update={"sha256": "f" * 64}),
        }
    )
    assert build_fingerprint(spec) == build_fingerprint(changed)


def test_run_mount_never_silently_omits_build_inputs(catalog_checkout):
    context = catalog_checkout / "containers"
    (context / "hydrology/Dockerfile").write_text(
        "FROM scratch\nRUN --mount=type=bind,source=hidden,target=/data cat /data\n",
        encoding="utf-8",
    )
    with pytest.raises(FloodcastError, match="RUN mounts"):
        recipe_files(context, "hydrology")


def test_json_add_and_directory_copy_are_hashed(catalog_checkout):
    context = catalog_checkout / "containers"
    scripts = context / "hydrology/scripts"
    scripts.mkdir()
    (scripts / "build.py").write_text("build", encoding="utf-8")
    asset = context / "hydrology/local asset.tar"
    asset.write_bytes(b"synthetic")
    (context / "hydrology/Dockerfile").write_text(
        "FROM scratch\nCOPY hydrology/scripts/ /scripts/\n"
        'ADD ["hydrology/local asset.tar", "/data/"]\n',
        encoding="utf-8",
    )
    inputs = recipe_files(context, "hydrology")
    assert scripts / "build.py" in inputs
    assert asset in inputs


def test_tab_separated_copy_is_not_omitted(catalog_checkout):
    context = catalog_checkout / "containers"
    source = context / "hydrology/input.txt"
    source.write_text("input", encoding="utf-8")
    (context / "hydrology/Dockerfile").write_text(
        "FROM scratch\nCOPY\thydrology/input.txt /input.txt\n", encoding="utf-8"
    )
    assert source in recipe_files(context, "hydrology")
