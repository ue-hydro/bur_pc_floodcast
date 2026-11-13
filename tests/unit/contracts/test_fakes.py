"""Shared doubles keep incomplete arrangements visibly distinct from success."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from floodcast.core.contracts import RunHandle, RunStatus
from floodcast.core.errors import ErrorCode, FloodcastError
from helpers.fakes import (
    FakeBuildBackend,
    FakeExecutor,
    FakeRegistry,
    FakeValidationRunner,
)


def test_builder_records_requested_spec_and_never_creates_success_implicitly():
    backend = FakeBuildBackend()
    spec = object()
    attempt_id = uuid4()
    with pytest.raises(AssertionError, match="explicitly configured candidate"):
        backend.build(spec, attempt_id)
    assert backend.calls == [(spec, attempt_id)]


def test_validation_requires_an_explicit_report_and_preserves_exception():
    runner = FakeValidationRunner()
    request = object()
    with pytest.raises(AssertionError, match="explicitly configured report"):
        runner.validate(request)
    failure = FloodcastError(ErrorCode.DOCKER_ENGINE_UNAVAILABLE, "validate", "engine unavailable")
    runner.error = failure
    with pytest.raises(FloodcastError) as caught:
        runner.validate(request)
    assert caught.value is failure
    assert runner.calls == [request, request]


def test_executor_records_calls_and_configured_status(tmp_path):
    handle = RunHandle(run_id=uuid4(), manifest_path=tmp_path / "run.json")
    executor = FakeExecutor(handle=handle, status=RunStatus.RUNNING)
    execution = object()
    assert executor.submit(execution) is handle
    assert executor.poll(handle) == RunStatus.RUNNING
    with pytest.raises(AssertionError, match="explicitly configured result"):
        executor.wait(handle)
    with pytest.raises(AssertionError, match="explicitly configured cancellation"):
        executor.cancel(handle)
    assert executor.submissions == [execution]
    assert executor.polls == executor.waits == executor.cancellations == [handle]


def test_executor_does_not_translate_configured_denial_to_another_error(tmp_path):
    handle = RunHandle(run_id=uuid4(), manifest_path=tmp_path / "run.json")
    denial = FloodcastError(ErrorCode.DOCKER_ACCESS_DENIED, "poll", "access denied")
    executor = FakeExecutor(errors={"poll": denial})
    with pytest.raises(FloodcastError) as caught:
        executor.poll(handle)
    assert caught.value is denial
    assert executor.polls == [handle]


def test_registry_captures_requested_identity_and_generation():
    registry = FakeRegistry()
    installation_id = uuid4()
    with pytest.raises(AssertionError, match="explicitly configured installation"):
        registry.resolve("fluxos", "docker", installation_id)
    assert registry.resolve_calls == [("fluxos", "docker", installation_id)]
    candidate, report = object(), object()
    with pytest.raises(AssertionError, match="explicitly configured record"):
        registry.promote(candidate, report, 7)
    assert registry.promotions == [(candidate, report, 7)]
    assert registry.list_installations() == []
    assert registry.list_calls == 1


def test_fake_instances_do_not_share_recorded_calls():
    first, second = FakeRegistry(), FakeRegistry()
    event = object()
    first.record_attempt(event)
    assert first.events == [event]
    assert second.events == []


def test_registry_double_filters_explicit_ids_without_silently_omitting_missing():
    first = SimpleNamespace(record=SimpleNamespace(installation_id=uuid4()))
    second = SimpleNamespace(record=SimpleNamespace(installation_id=uuid4()))
    registry = FakeRegistry(listings=[first, second])
    assert registry.list_installations() == [first, second]
    assert registry.list_installations(installation_ids=(second.record.installation_id,)) == [
        second
    ]
    assert registry.list_installations(installation_ids=()) == []
    with pytest.raises(FloodcastError) as caught:
        registry.list_installations(installation_ids=(uuid4(),))
    assert caught.value.code == ErrorCode.INSTALLATION_NOT_FOUND
