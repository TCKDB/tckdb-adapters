"""``TCKDBUploaderBase._write_and_upload``: the tail every submit method shares, on stubs only."""

import json
import logging

import pytest
from tckdb_client.errors import TCKDBConnectionError

from tckdb_core.config import TCKDBConfig
from tckdb_core.payload_writer import PayloadWriter
from tckdb_core.testing.stubs import StubClient, StubResponse, upload_calls
from tckdb_core.uploader import TCKDBUploaderBase

RIGHTS = {"license": "CC-BY-4.0", "depositor_attests_right_to_license": True}
ENDPOINT = "/uploads/widgets"


class DemoAdapter(TCKDBUploaderBase):
    PRODUCER_TAG = "demo"
    PRODUCER_NAME = "Demo"
    IDEMPOTENCY_NAMESPACE = "demo"
    LOGGER_NAME = "demo_logger"


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("DEMO_TCKDB_KEY", "k")


def _adapter(tmp_path, client=None, **overrides):
    values = dict(
        enabled=True, base_url="https://example.invalid", payload_dir=str(tmp_path),
        api_key_env="DEMO_TCKDB_KEY", preflight=False, project_label="proj",
    )
    values.update(overrides)
    return DemoAdapter(
        TCKDBConfig(**values), client_factory=(lambda cfg, key: client) if client else None)


def _submit(adapter, payload=None, **overrides):
    values = dict(
        payload={"v": 1} if payload is None else payload, kind="widget", endpoint=ENDPOINT,
        label="a.b", project_label="proj", species_label="a", conformer_label="b",
    )
    values.update(overrides)
    return adapter._write_and_upload(**values)


def _sidecar(outcome):
    return json.loads(outcome.sidecar_path.read_text())


def test_upload_off_writes_the_files_and_finishes_skipped_without_a_request(tmp_path):
    client = StubClient(response=StubResponse({}))
    outcome = _submit(_adapter(tmp_path, client, upload=False))
    assert outcome.status == "skipped" and client.calls == []
    assert outcome.payload_path == tmp_path / PayloadWriter.SUBDIR / "a.b.payload.json"
    assert json.loads(outcome.payload_path.read_text()) == {"v": 1}
    sidecar = _sidecar(outcome)
    assert sidecar["status"] == "skipped"
    assert sidecar["endpoint"] == ENDPOINT
    assert sidecar["payload_kind"] == "widget"
    assert sidecar["base_url"] == "https://example.invalid"
    assert sidecar["idempotency_key"] == outcome.idempotency_key


def test_upload_on_posts_the_written_payload_under_the_key_to_the_endpoint(tmp_path):
    client = StubClient(response=StubResponse({"id": 7}))
    outcome = _submit(_adapter(tmp_path, client, upload=True))
    (call,) = upload_calls(client)
    assert (call["method"], call["path"], call["json"]) == ("POST", ENDPOINT, {"v": 1})
    assert call["idempotency_key"] == outcome.idempotency_key
    assert outcome.status == "uploaded" and _sidecar(outcome)["status"] == "uploaded"


def test_the_key_is_in_the_producers_namespace_and_hashes_the_finished_payload(tmp_path):
    adapter = _adapter(tmp_path, upload=False, rights=dict(RIGHTS))
    with_rights = _submit(adapter, label="x", species_label="spc", conformer_label="c0")
    parts = with_rights.idempotency_key.split(":")
    assert parts[:5] == ["demo", "proj", "spc", "c0", "widget"] and len(parts[5]) == 16
    # The hash covers ``rights``: the same payload under no rights is another upload.
    without = _submit(_adapter(tmp_path, upload=False), label="y", species_label="spc", conformer_label="c0")
    assert without.idempotency_key != with_rights.idempotency_key
    assert with_rights.idempotency_key == adapter._idempotency_key(
        {"v": 1, "rights": RIGHTS}, kind="widget", project_label="proj", species_label="spc", conformer_label="c0")


def test_no_project_label_leaves_the_key_without_that_part(tmp_path):
    outcome = _submit(_adapter(tmp_path, upload=False), project_label=None)
    assert outcome.idempotency_key.startswith("demo:a:b:widget:")


def test_configured_rights_overwrite_what_the_payload_states(tmp_path):
    payload = {"v": 1, "rights": {"license": "CC0-1.0"}}
    outcome = _submit(_adapter(tmp_path, upload=False, rights=dict(RIGHTS)), payload)
    assert payload["rights"] == RIGHTS
    assert json.loads(outcome.payload_path.read_text())["rights"] == RIGHTS


def test_the_attached_rights_are_a_copy_of_the_configured_ones(tmp_path):
    adapter = _adapter(tmp_path, upload=False, rights=dict(RIGHTS))
    payload = {"v": 1}
    _submit(adapter, payload)
    assert payload["rights"] == RIGHTS
    assert type(payload["rights"]) is dict and payload["rights"] is not adapter._config.rights


def test_a_percent_sign_in_log_name_is_logged_literally(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="demo_logger"):
        outcome = _submit(_adapter(tmp_path, upload=False), log_name="50%s-widget")
        _submit(_adapter(tmp_path, upload=False), log_name="50%s-widget", label="p", is_partial=True)
    messages = [r.getMessage() for r in caplog.records]
    assert f"TCKDB 50%s-widget payload written: {outcome.payload_path} (key={outcome.idempotency_key})" in messages
    assert any(m.startswith("TCKDB partial 50%s-widget sidecar written;") for m in messages)


def test_without_configured_rights_the_payload_is_written_as_given(tmp_path):
    payload = {"v": 1, "rights": {"license": "CC0-1.0"}}
    outcome = _submit(_adapter(tmp_path, upload=False), payload)
    assert json.loads(outcome.payload_path.read_text()) == {"v": 1, "rights": {"license": "CC0-1.0"}}


def test_attach_rights_false_leaves_the_payload_to_the_caller(tmp_path):
    outcome = _submit(_adapter(tmp_path, upload=False, rights=dict(RIGHTS)), attach_rights=False)
    assert json.loads(outcome.payload_path.read_text()) == {"v": 1}


def test_subdir_and_label_name_the_files_and_warnings_seed_the_sidecar(tmp_path):
    warnings = [{"code": "w1", "message": "m"}]
    outcome = _submit(
        _adapter(tmp_path, upload=False), label="my label/1", subdir=PayloadWriter.THERMO_SUBDIR,
        warnings=warnings)
    assert outcome.payload_path.parent == tmp_path / "thermo"
    assert outcome.payload_path.name.startswith("my")
    assert _sidecar(outcome)["warnings"] == warnings
    assert outcome.warnings == warnings


def test_upload_adds_the_servers_warnings_after_the_producers(tmp_path):
    server = {"code": "server", "message": "s"}
    client = StubClient(response=StubResponse({"id": 1, "warnings": [server]}))
    producer = {"code": "mine", "message": "m"}
    outcome = _submit(_adapter(tmp_path, client, upload=True), warnings=[producer])
    assert [w["code"] for w in _sidecar(outcome)["warnings"]] == ["mine", "server"]


@pytest.mark.parametrize("log_name, expected", [
    ("", "TCKDB payload written: "),
    ("widget-kind", "TCKDB widget-kind payload written: "),
])
def test_the_write_is_logged_through_the_producers_logger(tmp_path, caplog, log_name, expected):
    with caplog.at_level(logging.INFO, logger="demo_logger"):
        outcome = _submit(_adapter(tmp_path, upload=False), log_name=log_name)
    (line,) = [r for r in caplog.records if "payload written" in r.getMessage()]
    assert line.name == "demo_logger"
    assert line.getMessage() == f"{expected}{outcome.payload_path} (key={outcome.idempotency_key})"


def test_a_partial_record_is_written_with_the_infix_and_never_posted(tmp_path, caplog):
    client = StubClient(response=StubResponse({}))
    with caplog.at_level(logging.INFO, logger="demo_logger"):
        outcome = _submit(_adapter(tmp_path, client, upload=True), is_partial=True, log_name="widget-kind")
    assert outcome.status == "skipped" and client.calls == []
    assert outcome.payload_path.name == "a.b.partial.payload.json"
    assert _sidecar(outcome)["is_partial"] is True
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("TCKDB partial widget-kind sidecar written; live upload skipped: ") for m in messages)
    assert not any("payload written" in m for m in messages)


def test_a_key_over_the_servers_limit_raises_before_anything_is_written(tmp_path):
    with pytest.raises(ValueError):
        _submit(_adapter(tmp_path, upload=False), species_label="x" * 250)
    assert list(tmp_path.rglob("*.json")) == []


def test_an_upload_failure_is_recorded_and_strict_mode_raises(tmp_path):
    boom = TCKDBConnectionError("down")
    lenient = _submit(_adapter(tmp_path, StubClient(raise_exc=boom), upload=True))
    assert lenient.status == "failed" and "down" in lenient.error
    assert _sidecar(lenient)["status"] == "failed"
    with pytest.raises(TCKDBConnectionError):
        _submit(_adapter(tmp_path, StubClient(raise_exc=boom), upload=True, strict=True), label="c.d")


def test_a_missing_api_key_is_a_recorded_failure_not_a_request(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_TCKDB_KEY")
    client = StubClient(response=StubResponse({}))
    outcome = _submit(_adapter(tmp_path, client, upload=True))
    assert outcome.status == "failed" and "API key not configured" in outcome.error
    assert client.calls == []
    assert outcome.payload_path.exists()
