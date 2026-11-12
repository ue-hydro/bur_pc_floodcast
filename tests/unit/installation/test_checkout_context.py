"""Checkout-root build contexts retain immutable inputs and old record semantics."""

import json
import shutil
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from unit.installation.test_build_backend import build_setup as prepared_backend
from unit.installation.test_catalog import catalog_checkout as prepared_catalog

from floodcast.core.config import RuntimeConfig
from floodcast.core.contracts import BuildSpec, canonical_json_bytes
from floodcast.core.errors import FloodcastError
from floodcast.services.catalog import (
    build_fingerprint,
    load_build_catalog,
    load_catalog,
    recipe_files,
)
from floodcast.services.check_suite import load_current_validation_profile
from floodcast.services.installation_registry import Registry
from floodcast.services.installer import ReuseResolver

METHODS = ("standalone", "pip", "conda", "source-manual")


def write(checkout, relative, data):
    path = checkout / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")
    return path


def framework(checkout):
    files = {
        "pyproject.toml": "[project]\nname='floodcast'\nversion='0.0.0'\n",
        "environment.yml": "name: floodcast\n",
        "src/floodcast/__init__.py": "# framework\n",
    }
    for name, text in files.items():
        write(checkout, name, text)
    return {name: sha256((checkout / name).read_bytes()).hexdigest() for name in files}


def prepare_layout(checkout, group):
    write(checkout, ".dockerignore", "**\n!containers/**\n!scripts/**\n**/__pycache__/**\n")
    write(checkout, "scripts/build_model.py", "# scientific build helper\n")
    write(checkout, "scripts/unrelated.py", "# not copied by Dockerfile\n")
    write(checkout, "tests/test_model.py", "# not a build input\n")
    write(checkout, "data/result.json", "{}\n")
    write(checkout, "scripts/__pycache__/build_model.pyc", "cache")
    write(
        checkout,
        f"containers/{group}/Dockerfile",
        "FROM scratch AS build\nCOPY scripts/build_model.py /recipe/build.py\n"
        "FROM build AS pip\nCOPY --from=framework /src/floodcast/ /package/\n"
        "FROM pip AS conda\nFROM pip AS source-manual\nFROM build AS standalone\n",
    )
    write(
        checkout,
        "containers/docker-compose.yaml",
        "services:\n"
        + "".join(
            f"  {name}:\n    build:\n      context: ..\n"
            f"      dockerfile: containers/{name}/Dockerfile\n"
            for name in ("hydrology", "hydraulics")
        ),
    )


@pytest.fixture
def backend_setup(tmp_path, monkeypatch):
    return prepared_backend.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize("group", ["hydrology", "hydraulics"])
@pytest.mark.parametrize("method", METHODS)
def test_snapshot_uses_checkout_relative_inputs_and_compose(backend_setup, group, method):
    backend, client, old_spec, config = backend_setup
    checkout = config.require_checkout()
    prepare_layout(checkout, group)
    hashes = framework(checkout)
    recipes = recipe_files(checkout, group, layout="checkout")
    payload = old_spec.model_dump()
    payload.update(
        group=group,
        context_layout="checkout",
        method=method,
        recipe_paths=recipes,
        recipe_sha256={
            path.relative_to(checkout).as_posix(): sha256(path.read_bytes()).hexdigest()
            for path in recipes
        },
        framework_sha256={} if method == "standalone" else hashes,
    )
    spec = BuildSpec.model_validate(payload)
    backend.toolchain_paths[group] = "/opt/floodcast/toolchain.json"
    client.toolchain["group"] = group
    client.toolchain["sources"] = {"fluxos": client.toolchain["source"]}
    # These characters must stay literal through Compose's generated configuration.
    data = config.data_root / "$VAR ${VAR} with spaces"
    data.mkdir()
    backend.config = RuntimeConfig(checkout_root=checkout, data_root=data)
    attempt = uuid4()
    candidate = backend.build(spec, attempt)
    directory = backend.config.registry_root / "attempts" / str(attempt) / group
    snapshot = directory / "context"
    selected = {
        path.relative_to(snapshot).as_posix() for path in snapshot.rglob("*") if path.is_file()
    }
    assert selected == {
        ".dockerignore",
        "containers/docker-compose.yaml",
        f"containers/{group}/Dockerfile",
        "scripts/build_model.py",
    }
    commands = [call[1] for call in client.calls if call[0] == "run"]
    assert str(snapshot / "containers/docker-compose.yaml") in commands[0]
    assert commands[0][-1] == group
    selection = json.loads((directory / "compose-target.json").read_text("utf-8"))
    build = selection["services"][group]["build"]
    assert build["target"] == method
    if method == "standalone":
        assert "additional_contexts" not in build
        assert not (directory / "framework").exists()
    else:
        assert build["additional_contexts"]["framework"] == str(directory / "framework").replace(
            "$", "$$"
        )
        assert {
            path.relative_to(directory / "framework").as_posix(): sha256(
                path.read_bytes()
            ).hexdigest()
            for path in (directory / "framework").rglob("*")
            if path.is_file()
        } == hashes
    assert candidate.build_spec_sha256 == build_fingerprint(spec)


@pytest.fixture
def catalog_checkout(tmp_path):
    checkout = prepared_catalog.__wrapped__(tmp_path)
    manifest = checkout / "containers/model-sources.toml"
    document = manifest.read_text("utf-8")
    for group, ids in (("hydrology", ("summa", "mizuroute")), ("hydraulics", ("fluxos",))):
        prepare_layout(checkout, group)
        models = ",".join(f'{{model_id="{model}",executable="/opt/{model}"}}' for model in ids)
        document = document.replace(
            f"[groups.{group}]",
            f'[groups.{group}]\nbuild_context="checkout"\nmodels=[{models}]',
        )
        for name in ("checks.json", "rule.json"):
            old = checkout / f"containers/{group}/{name}"
            new = checkout / f"tests/models/{group}/{name}"
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(old, new)
        document = document.replace(
            f'"{group}/checks.json"', f'"../tests/models/{group}/checks.json"'
        )
    manifest.write_text(document, encoding="utf-8")
    return checkout


def test_catalog_uses_root_ignore_and_external_tests_only_when_validating(catalog_checkout):
    checkout = catalog_checkout
    build = load_build_catalog(checkout, ("hydrology", "hydraulics"), 1)[0]
    for group, spec in build.items():
        assert spec.context_layout == "checkout"
        assert ".dockerignore" in spec.recipe_sha256
        assert "containers/.dockerignore" not in spec.recipe_sha256
        assert "scripts/build_model.py" in spec.recipe_sha256
        assert not any("tests" in path.parts for path in spec.recipe_paths)
        profile = load_current_validation_profile(checkout, group)
        assert profile.checks[0].expectation.path.is_relative_to(checkout / "tests")
        assert load_catalog(checkout, (group,), 1)[1][group] == profile
    moved = checkout / "tests moved"
    (checkout / "tests").rename(moved)
    after = load_build_catalog(checkout, ("hydrology", "hydraulics"), 1)[0]
    assert {key: build_fingerprint(value) for key, value in build.items()} == {
        key: build_fingerprint(value) for key, value in after.items()
    }
    with pytest.raises(FloodcastError):
        load_current_validation_profile(checkout, "hydrology")


@pytest.mark.parametrize("value", ['"other"', "42", "true"])
def test_catalog_rejects_unknown_layout(catalog_checkout, value):
    manifest = catalog_checkout / "containers/model-sources.toml"
    manifest.write_text(
        manifest.read_text("utf-8").replace('build_context="checkout"', f"build_context={value}"),
        encoding="utf-8",
    )
    with pytest.raises(FloodcastError, match="source manifest"):
        load_build_catalog(catalog_checkout, ("hydraulics",), 1)


@pytest.mark.parametrize("source", ["../secret.txt", "/secret.txt", "scripts/../../secret.txt"])
def test_checkout_context_cannot_copy_outside_checkout(catalog_checkout, source):
    recipe = catalog_checkout / "containers/hydraulics/Dockerfile"
    recipe.write_text(f"FROM scratch\nCOPY {source} /secret\n", encoding="utf-8")
    with pytest.raises(FloodcastError, match="escapes context"):
        load_build_catalog(catalog_checkout, ("hydraulics",), 1)


def test_external_validation_cannot_escape_allowed_checkout_directories(catalog_checkout):
    manifest = catalog_checkout / "containers/model-sources.toml"
    manifest.write_text(
        manifest.read_text("utf-8").replace(
            "../tests/models/hydraulics/checks.json", "../../outside/checks.json"
        ),
        encoding="utf-8",
    )
    assert load_build_catalog(catalog_checkout, ("hydraulics",), 1)[0]
    with pytest.raises(FloodcastError):
        load_current_validation_profile(catalog_checkout, "hydraulics")


@pytest.mark.parametrize("method", METHODS)
def test_legacy_default_fingerprint_and_archive_are_unchanged(backend_setup, method):
    _, _, spec, config = backend_setup
    if method != "standalone":
        payload = spec.model_dump()
        payload.update(method=method, framework_sha256=framework(config.require_checkout()))
        spec = BuildSpec.model_validate(payload)
    assert spec.context_layout == "containers"
    old = spec.model_dump(mode="json", exclude={"context_layout"})
    raw = canonical_json_bytes(old)
    parsed = BuildSpec.model_validate_json(raw)
    excluded = {"recipe_paths", "dependency_lock", "required_check_ids"}
    if method == "standalone":
        excluded.update({"method", "framework_sha256"})
    old_fingerprint = sha256(
        canonical_json_bytes({key: value for key, value in old.items() if key not in excluded})
    ).hexdigest()
    assert build_fingerprint(parsed) == old_fingerprint
    record = SimpleNamespace(build_spec_sha256=sha256(raw).hexdigest())
    archive = config.registry_root / "attempts" / str(uuid4()) / spec.group
    archive.mkdir(parents=True)
    (archive / "build-spec.json").write_bytes(raw)
    for source in spec.recipe_paths:
        target = archive / "context" / source.relative_to(config.require_checkout() / "containers")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    assert ReuseResolver(Registry(config.registry_root), object(), {})._matches_build(record, spec)
    assert (archive / "build-spec.json").read_bytes() == raw


def test_checkout_layout_does_not_accept_changed_script(backend_setup):
    backend, client, old, config = backend_setup
    checkout = config.require_checkout()
    prepare_layout(checkout, old.group)
    recipes = recipe_files(checkout, old.group, layout="checkout")
    payload = old.model_dump()
    payload.update(
        context_layout="checkout",
        recipe_paths=recipes,
        recipe_sha256={
            path.relative_to(checkout).as_posix(): sha256(path.read_bytes()).hexdigest()
            for path in recipes
        },
    )
    spec = BuildSpec.model_validate(payload)
    write(checkout, "scripts/build_model.py", "changed after resolution\n")
    with pytest.raises(FloodcastError, match="Recipe changed"):
        backend.build(spec, uuid4())
    assert client.calls == []
