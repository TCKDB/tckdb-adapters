"""The runtime contract check before every POST: `tckdb_core.validation` and its hook in the uploader.

Valid requests are the examples tckdb-schemas ships with each route's JSON Schema. Everything runs
against a stub client; nothing touches a network.
"""

import copy
import json
import math

import pytest
from tckdb_schemas import contract

from tckdb_core import constants, standalone, validation
from tckdb_core.config import TCKDBArtifactConfig, TCKDBConfig
from tckdb_core.standalone import STANDALONE_ROUTES, StandalonePayloadError
from tckdb_core.testing import contract as test_kit
from tckdb_core.testing.stubs import StubClient, StubResponse, upload_calls
from tckdb_core.uploader import TCKDBUploaderBase
from tckdb_core.validation import (
    LOCAL_VALIDATION_CODE,
    MAX_FINDING_CHARS,
    ROUTE_SCHEMAS,
    PayloadContractError,
    request_findings,
)

CODE = "payload_failed_local_validation"
ARTIFACTS = constants.ARTIFACTS_ENDPOINT_TEMPLATE
#: Every route that takes one request body through ``_upload``.
BODY_ENDPOINTS = sorted(ep for ep in ROUTE_SCHEMAS if ep != ARTIFACTS)


class DemoAdapter(TCKDBUploaderBase):
    PRODUCER_TAG = "demo"
    PRODUCER_NAME = "Demo"
    IDEMPOTENCY_NAMESPACE = "demo"

    def _project_label_for(self, output_doc):
        return output_doc.get("project")


def _config(tmp_path, **overrides):
    values = dict(
        enabled=True, base_url="https://example.invalid", payload_dir=str(tmp_path / "out"),
        api_key_env="DEMO_TCKDB_KEY", project_label="proj",
        artifacts=TCKDBArtifactConfig(upload=True, kinds=("output_log", "input")),
    )
    values.update(overrides)
    return TCKDBConfig(**values)


def _adapter(tmp_path, client=None, **overrides):
    client = client or StubClient(response=StubResponse({"warnings": []}))
    adapter = DemoAdapter(
        _config(tmp_path, **overrides), project_directory=tmp_path, client_factory=lambda *a, **k: client)
    return adapter, client


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("DEMO_TCKDB_KEY", "k")


def _example(endpoint):
    return copy.deepcopy(contract.json_schema(ROUTE_SCHEMAS[endpoint])["examples"][0])


def _written(adapter, endpoint, payload):
    return adapter._writer.write(
        label="demo", payload=payload, endpoint=endpoint, idempotency_key="demo-key-0123456789")


def _many_bad_items(count):
    """An artifact body with ``count`` items, each carrying a field the contract does not know (one finding each)."""
    item = _example(ARTIFACTS)["artifacts"][0]
    return {"artifacts": [{**item, "definitely_not_a_field": "z" * 1000} for _ in range(count)]}


def _broken(endpoint):
    """A request the contract refuses: the example plus a field no route accepts."""
    payload = _example(endpoint)
    payload["definitely_not_a_field"] = 1
    return payload


# ---------------------------------------------------------------- one table


def test_the_test_kit_and_the_runtime_read_one_table():
    assert test_kit.ROUTE_SCHEMAS is validation.ROUTE_SCHEMAS
    assert test_kit.REQUEST_MODELS is validation.REQUEST_MODELS is validation.request_models()
    assert test_kit._validator is validation.json_schema_validator
    for route in STANDALONE_ROUTES.values():
        assert validation.schema_name_for(route.endpoint) == route.schema_name
    assert standalone._validator is validation.json_schema_validator  # the earlier private name still resolves
    assert standalone.MAX_FINDING_CHARS == validation.MAX_FINDING_CHARS


def test_every_route_of_the_table_has_a_shipped_schema_and_a_valid_example():
    assert len(ROUTE_SCHEMAS) == 12
    for endpoint, schema_name in ROUTE_SCHEMAS.items():
        assert schema_name in contract.schema_names()
        assert request_findings(endpoint, _example(endpoint)) == [], endpoint
        test_kit.assert_request_is_accepted(_example(endpoint), schema_name)  # the kit agrees


def test_the_validator_is_built_once_per_schema():
    first = validation.json_schema_validator("ThermoUploadRequest")
    assert validation.json_schema_validator("ThermoUploadRequest") is first


def test_a_route_with_no_row_has_nothing_to_check_against():
    assert validation.schema_name_for("/uploads/unheard-of") is None
    assert request_findings("/uploads/unheard-of", {"anything": 1}) == []


# ---------------------------------------------------------------- findings


@pytest.mark.parametrize("endpoint", sorted(ROUTE_SCHEMAS))
def test_an_unknown_field_is_a_finding_on_every_route(endpoint):
    body = _example(endpoint)
    if endpoint == ARTIFACTS:
        body["artifacts"][0]["definitely_not_a_field"] = 1
        expected = "artifacts/0/definitely_not_a_field"  # a model reports the field it refuses
    elif validation.request_model(ROUTE_SCHEMAS[endpoint]) is not None:
        body["definitely_not_a_field"] = 1
        expected = "definitely_not_a_field"
    else:
        body["definitely_not_a_field"] = 1
        expected = "<root>"  # a JSON Schema reports the object that has the extra field
    findings = request_findings(endpoint, body)
    assert findings and findings[0].startswith(f"{expected}:") and "definitely_not_a_field" in (findings[0] + expected)


def test_findings_are_one_bounded_line_each():
    body = _example(constants.THERMO_ENDPOINT)
    body["x" * 5000] = "y" * 5000
    findings = request_findings(constants.THERMO_ENDPOINT, body)
    assert findings
    assert all("\n" not in line for line in findings)
    assert all(len(line) <= len("<root>: ") + MAX_FINDING_CHARS + len("... [truncated]") for line in findings)
    assert any(line.endswith("... [truncated]") for line in findings)


def test_the_number_of_findings_is_capped():
    findings = request_findings(ARTIFACTS, _many_bad_items(1000))
    assert len(findings) <= validation.MAX_FINDINGS + 1
    assert findings[-1].startswith("<more>:")


def test_a_value_that_is_not_json_is_a_finding():
    body = _example(constants.THERMO_ENDPOINT)
    body["note"] = math.nan
    (finding,) = request_findings(constants.THERMO_ENDPOINT, body)
    assert finding.startswith("<root>: not serialisable as the JSON the client sends")
    assert request_findings(constants.THERMO_ENDPOINT, ["not", "an", "object"])[0].startswith("<root>: a ThermoUploadRequest")


def test_the_published_model_decides_where_there_is_one(monkeypatch):
    """For a route with a published model the model is the verdict (the server runs it); the schema is not consulted."""
    class Refusing:
        @staticmethod
        def model_validate(wire):
            from pydantic import BaseModel, field_validator

            class Probe(BaseModel):
                geometry: dict

                @field_validator("geometry")
                @classmethod
                def never(cls, value):
                    raise ValueError("the model's own rule")

            Probe.model_validate(wire)

    monkeypatch.setitem(validation.request_models(), "ConformerUploadRequest", Refusing)
    findings = request_findings(constants.CONFORMER_UPLOAD_ENDPOINT, _example(constants.CONFORMER_UPLOAD_ENDPOINT))
    assert findings == ["geometry: the model's own rule"]  # pydantic's "Value error, " prefix is stripped


# ---------------------------------------------------------------- the hook in _upload


@pytest.mark.parametrize("endpoint", BODY_ENDPOINTS)
def test_a_valid_request_is_posted(tmp_path, endpoint):
    adapter, client = _adapter(tmp_path)
    payload = _example(endpoint)
    outcome = adapter._upload(_written(adapter, endpoint, payload), payload, endpoint=endpoint)
    assert outcome.status == "uploaded"
    (call,) = upload_calls(client)
    assert call["path"] == endpoint and call["json"] == payload
    sidecar = json.loads(outcome.sidecar_path.read_text())
    assert sidecar["status"] == "uploaded" and sidecar["warnings"] == []


@pytest.mark.parametrize("endpoint", BODY_ENDPOINTS)
def test_an_invalid_request_is_not_posted_and_the_sidecar_says_why(tmp_path, endpoint):
    adapter, client = _adapter(tmp_path, preflight=True)
    payload = _broken(endpoint)
    written = _written(adapter, endpoint, payload)
    outcome = adapter._upload(written, payload, endpoint=endpoint)

    assert client.calls == []  # neither the readiness probe nor the POST
    assert outcome.status == "failed"
    assert outcome.error.startswith(f"{CODE}: ") and "definitely_not_a_field" in outcome.error.splitlines()[0]
    assert "(not posted;" in outcome.error.splitlines()[0]
    sidecar = json.loads(outcome.sidecar_path.read_text())
    assert sidecar["status"] == "failed" and sidecar["uploaded_at"] is None
    assert sidecar["last_error"] == outcome.error
    # Not a server refusal: no HTTP status, no response body, no request id.
    assert sidecar["response_status_code"] is None and sidecar["response_body"] is None
    assert sidecar["request_ids"] == [] and sidecar["preflight"] is None
    (warning,) = sidecar["warnings"]
    assert warning["code"] == CODE == LOCAL_VALIDATION_CODE
    assert warning["context"]["source"] == "tckdb_core_self_check"  # the core's check, whichever producer runs it
    assert warning["context"]["action"] == "not_posted"
    assert warning["context"]["endpoint"] == endpoint
    assert warning["context"]["contract"] == ROUTE_SCHEMAS[endpoint]
    assert warning["context"]["finding_count"] == len(warning["context"]["findings"]) >= 1
    assert outcome.warnings == sidecar["warnings"]
    # The payload is on disk, exactly as built.
    assert json.loads(outcome.payload_path.read_text()) == payload


def test_the_sidecar_quotes_at_most_a_bounded_number_of_findings(tmp_path):
    adapter, _ = _adapter(tmp_path)
    payload = _many_bad_items(100)
    outcome = adapter._upload(_written(adapter, ARTIFACTS, payload), payload, endpoint=ARTIFACTS)
    sidecar = json.loads(outcome.sidecar_path.read_text())
    context = sidecar["warnings"][0]["context"]
    assert context["finding_count"] == 100 and len(context["findings"]) == validation.MAX_FINDINGS_IN_SIDECAR
    assert len(sidecar["last_error"]) < 4000  # ten findings of at most ~230 characters, not the payload


def test_strict_mode_raises_after_the_sidecar_is_written(tmp_path):
    adapter, client = _adapter(tmp_path, strict=True)
    payload = _broken(constants.THERMO_ENDPOINT)
    written = _written(adapter, constants.THERMO_ENDPOINT, payload)
    with pytest.raises(PayloadContractError, match=CODE) as caught:
        adapter._upload(written, payload, endpoint=constants.THERMO_ENDPOINT)
    assert any("definitely_not_a_field" in line for line in caught.value.errors)
    assert client.calls == []
    assert json.loads(written.sidecar_path.read_text())["status"] == "failed"
    assert written.payload_path.is_file()


def test_the_switch_turns_the_check_off(tmp_path):
    adapter, client = _adapter(tmp_path, validate_before_upload=False)
    payload = _broken(constants.THERMO_ENDPOINT)
    outcome = adapter._upload(_written(adapter, constants.THERMO_ENDPOINT, payload), payload,
                              endpoint=constants.THERMO_ENDPOINT)
    assert outcome.status == "uploaded"
    (call,) = upload_calls(client)
    assert call["json"] == payload


def test_the_check_is_on_by_default():
    assert TCKDBConfig().validate_before_upload is True


def test_a_missing_key_does_not_hide_the_contract_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_TCKDB_KEY")
    adapter, client = _adapter(tmp_path)
    payload = _broken(constants.THERMO_ENDPOINT)
    outcome = adapter._upload(_written(adapter, constants.THERMO_ENDPOINT, payload), payload,
                              endpoint=constants.THERMO_ENDPOINT)
    assert outcome.status == "failed" and outcome.error.startswith(f"{CODE}: ")


def test_a_server_refusal_is_still_a_server_refusal(tmp_path):
    """A request that passes the check and is refused by the server keeps its HTTP status and body."""
    from tckdb_client.client import TCKDBClient

    body = {"code": "some_rule", "detail": "the server's rule"}
    refusal = TCKDBClient._build_http_error(status_code=422, parsed=body, text=json.dumps(body), headers={})
    adapter, _ = _adapter(tmp_path, client=StubClient(raise_exc=refusal))
    payload = _example(constants.THERMO_ENDPOINT)
    outcome = adapter._upload(_written(adapter, constants.THERMO_ENDPOINT, payload), payload,
                              endpoint=constants.THERMO_ENDPOINT)
    sidecar = json.loads(outcome.sidecar_path.read_text())
    assert sidecar["status"] == "failed" and sidecar["response_status_code"] == 422
    assert sidecar["last_error"].startswith("HTTP 422") and CODE not in sidecar["last_error"]
    assert sidecar["warnings"] == []


# ---------------------------------------------------------------- artifact bodies


@pytest.fixture
def files(tmp_path):
    log = tmp_path / "run.log"
    log.write_bytes(b"output bytes")
    deck = tmp_path / "run.in"
    deck.write_bytes(b"input bytes")
    return log, deck


def test_an_artifact_batch_that_passes_is_posted(tmp_path, files):
    adapter, client = _adapter(tmp_path)
    outcomes = adapter.submit_artifact_batch_for_calculation(
        species_label="CH4", calculation_ref="calc_1", artifacts=[("output_log", files[0]), ("input", files[1])])
    assert [o.status for o in outcomes] == ["uploaded", "uploaded"]
    (call,) = upload_calls(client)
    assert request_findings(ARTIFACTS, call["json"]) == []


def _refused_filename(files):
    """Rename the log to one TCKDB's per-kind extension list refuses (an ``output_log`` is ``.log``, ``.out`` or ``.orca``)."""
    return files[0].rename(files[0].with_name("run.exe"))


def test_an_artifact_batch_the_contract_refuses_is_not_posted(tmp_path, files):
    files = (_refused_filename(files), files[1])
    adapter, client = _adapter(tmp_path, preflight=True)
    outcomes = adapter.submit_artifact_batch_for_calculation(
        species_label="CH4", calculation_ref="calc_1", artifacts=[("output_log", files[0]), ("input", files[1])])
    assert client.calls == []
    assert [o.status for o in outcomes] == ["failed", "failed"]
    for outcome in outcomes:
        assert outcome.error.startswith(f"{CODE}: ") and "filename extension '.exe'" in outcome.error
        sidecar = json.loads(outcome.sidecar_path.read_text())
        assert sidecar["status"] == "failed" and sidecar["response_status_code"] is None
        (warning,) = sidecar["warnings"]
        assert warning["code"] == CODE and warning["context"]["endpoint"] == ARTIFACTS
        assert warning["context"]["contract"] == "ArtifactsUploadRequest"
        assert any("artifacts/0" in line for line in warning["context"]["findings"])
    assert files[0].read_bytes() == b"output bytes"


def test_strict_mode_raises_for_an_artifact_batch_the_contract_refuses(tmp_path, files):
    files = (_refused_filename(files), files[1])
    adapter, client = _adapter(tmp_path, strict=True)
    with pytest.raises(PayloadContractError, match=CODE):
        adapter.submit_artifacts_for_calculation(
            species_label="CH4", calculation_ref="calc_1", file_path=files[0])
    assert client.calls == []
    (sidecar_path,) = (tmp_path / "out").rglob("*.meta.json")
    assert json.loads(sidecar_path.read_text())["status"] == "failed"


def test_the_switch_lets_an_artifact_batch_through(tmp_path, files):
    files = (_refused_filename(files), files[1])
    adapter, client = _adapter(tmp_path, validate_before_upload=False)
    (outcome,) = adapter.submit_artifacts_for_calculation(
        species_label="CH4", calculation_ref="calc_1", file_path=files[0]),
    assert outcome.status == "uploaded"
    assert len(upload_calls(client)) == 1


# ---------------------------------------------------------------- standalone kinds


@pytest.mark.parametrize("kind", sorted(STANDALONE_ROUTES))
def test_each_standalone_kind_is_checked_at_submission_and_again_before_the_post(tmp_path, kind):
    route = STANDALONE_ROUTES[kind]
    adapter, client = _adapter(tmp_path)
    outcome = adapter.submit_standalone(kind, _example(route.endpoint), label="rec")
    assert outcome.status == "uploaded"
    (call,) = upload_calls(client)
    assert call["path"] == route.endpoint

    # A request the contract refuses never reaches a file, a sidecar or the client.
    adapter, client = _adapter(tmp_path / "second")
    with pytest.raises(StandalonePayloadError) as caught:
        adapter.submit_standalone(kind, _broken(route.endpoint), label="rec")
    assert isinstance(caught.value, PayloadContractError)  # one except covers both refusals
    assert client.calls == [] and not (tmp_path / "second").exists()


@pytest.mark.parametrize("kind", sorted(STANDALONE_ROUTES))
def test_a_standalone_request_the_submission_check_let_through_is_still_checked_before_the_post(
        tmp_path, kind, monkeypatch):
    """The two checks are separate: with the submission-time one stubbed out, the pre-POST one holds."""
    route = STANDALONE_ROUTES[kind]
    monkeypatch.setattr(standalone, "validate_standalone_payload", lambda *a, **k: None)
    adapter, client = _adapter(tmp_path)
    outcome = adapter.submit_standalone(kind, _broken(route.endpoint), label="rec")
    assert outcome.status == "failed" and outcome.error.startswith(f"{CODE}: ")
    assert client.calls == []
    assert outcome.payload_path.is_file()


# ---------------------------------------------------------------- the model decides where there is one


def _conformer_with_charge(value):
    body = _example(constants.CONFORMER_UPLOAD_ENDPOINT)
    body["species_entry"]["charge"] = value
    return body


def test_a_numeric_string_the_server_accepts_is_not_refused_on_a_model_route(tmp_path):
    """The server runs the model in lax mode: ``"0"`` is an accepted charge, though the JSON Schema says integer."""
    endpoint = constants.CONFORMER_UPLOAD_ENDPOINT
    body = _conformer_with_charge("0")
    assert request_findings(endpoint, body) == []
    with pytest.raises(AssertionError, match="not of type 'integer'"):  # the test kit is the stricter of the two
        test_kit.assert_matches_contract(body, "ConformerUploadRequest")
    adapter, client = _adapter(tmp_path)
    outcome = adapter._upload(_written(adapter, endpoint, body), body, endpoint=endpoint)
    assert outcome.status == "uploaded" and upload_calls(client)[0]["json"]["species_entry"]["charge"] == "0"


def test_a_value_the_model_cannot_read_is_still_refused_on_a_model_route():
    (finding,) = request_findings(constants.CONFORMER_UPLOAD_ENDPOINT, _conformer_with_charge("abc"))
    assert finding.startswith("species_entry/charge:") and "valid integer" in finding


@pytest.mark.parametrize("endpoint", [ep for ep in BODY_ENDPOINTS if validation.request_model(ROUTE_SCHEMAS[ep]) is not None])
def test_the_model_is_the_verdict_on_each_model_route(endpoint, monkeypatch):
    """A schema-only finding does not refuse a model route; the model's does."""
    seen = []
    monkeypatch.setattr(validation, "_schema_lines", lambda *a, **k: seen.append(a) or ["schema: refuses"])
    assert request_findings(endpoint, _example(endpoint)) == [] and seen == []


@pytest.mark.parametrize("endpoint", [ep for ep in BODY_ENDPOINTS if validation.request_model(ROUTE_SCHEMAS[ep]) is None])
def test_a_route_without_a_model_is_checked_against_the_json_schema(endpoint):
    assert len(BODY_ENDPOINTS) - len([e for e in BODY_ENDPOINTS if validation.request_model(ROUTE_SCHEMAS[e])]) == 7
    assert request_findings(endpoint, _broken(endpoint))[0].startswith("<root>:")


def test_schema_alternatives_read_as_a_reason_not_as_none_of_the_above():
    body = _example(constants.THERMO_ENDPOINT)
    body["rights"] = {"license": "CC-BY-4.0"}
    (finding,) = request_findings(constants.THERMO_ENDPOINT, body)
    assert "closest alternative: 'depositor_attests_right_to_license' is a required property" in finding
    assert "null" not in finding.split("closest alternative:")[1]


# ---------------------------------------------------------------- the model's own message


class _Model:
    """A stand-in route model that refuses with one chosen message."""

    def __init__(self, message):
        self.message = message

    def model_validate(self, wire):
        from pydantic import BaseModel, field_validator

        message = self.message

        class Probe(BaseModel):
            geometry: dict

            @field_validator("geometry")
            @classmethod
            def never(cls, value):
                raise ValueError(message)

        Probe.model_validate(wire)


def _with_model(monkeypatch, message):
    monkeypatch.setitem(validation.request_models(), "ConformerUploadRequest", _Model(message))


def test_a_models_prose_is_not_cut_at_the_json_schema_bound(monkeypatch):
    prose = "some_rule_code: " + "the recorded value disagrees with the deck; " * 20 + "correct the recorded value."
    assert 300 < len(prose) < validation.MAX_MODEL_FINDING_CHARS
    _with_model(monkeypatch, prose)
    (finding,) = request_findings(constants.CONFORMER_UPLOAD_ENDPOINT, _example(constants.CONFORMER_UPLOAD_ENDPOINT))
    assert finding == f"geometry: {prose}" and not finding.endswith("[truncated]")


def test_a_pathological_models_echo_is_still_bounded(monkeypatch):
    _with_model(monkeypatch, "x" * 50_000)
    (finding,) = request_findings(constants.CONFORMER_UPLOAD_ENDPOINT, _example(constants.CONFORMER_UPLOAD_ENDPOINT))
    assert len(finding) < validation.MAX_MODEL_FINDING_CHARS + 100 and finding.endswith("... [truncated]")


def test_the_first_finding_is_on_the_first_line_of_last_error_and_the_codes_are_surfaced(tmp_path, monkeypatch):
    prose = "some_rule_code: the recorded freq.hessian_method says A but the deck says B; correct one."
    _with_model(monkeypatch, prose)
    endpoint = constants.CONFORMER_UPLOAD_ENDPOINT
    adapter, client = _adapter(tmp_path)
    body = _example(endpoint)
    outcome = adapter._upload(_written(adapter, endpoint, body), body, endpoint=endpoint)
    first = outcome.error.splitlines()[0]
    assert first.startswith(f"{CODE}: geometry: some_rule_code: the recorded freq.hessian_method says A but the deck says B")
    assert "Value error" not in outcome.error and "correct one." in first
    (warning,) = outcome.warnings
    assert warning["context"]["refusal_codes"] == ["some_rule_code"]
    assert warning["context"]["findings"] == [f"geometry: {prose}"]  # the full finding, in the sidecar
    assert client.calls == []


def test_several_findings_follow_the_first_line(tmp_path):
    adapter, _ = _adapter(tmp_path)
    payload = _many_bad_items(3)
    outcome = adapter._upload(_written(adapter, ARTIFACTS, payload), payload, endpoint=ARTIFACTS)
    lines = outcome.error.splitlines()
    assert lines[0].startswith(f"{CODE}: artifacts/0/definitely_not_a_field:") and "3 finding(s)" in lines[0]
    assert [ln.strip().startswith("also at artifacts/") for ln in lines[1:]] == [True, True]


def test_refusal_codes_are_read_only_from_a_models_code_prefix():
    assert validation.refusal_codes([
        "a/b: hessian_method_contradicts_input_deck: prose", "<root>: 'x' is a required property",
        "c: Extra inputs are not permitted", "d: hessian_method_contradicts_input_deck: again",
    ]) == ["hessian_method_contradicts_input_deck"]


# ---------------------------------------------------------------- a checker bug never blocks an upload


class _Broken:
    @staticmethod
    def model_validate(wire):
        raise KeyError("an internal bug")


@pytest.mark.parametrize("strict", [False, True])
def test_a_checker_that_raises_posts_unchecked_and_says_so(tmp_path, monkeypatch, caplog, strict):
    monkeypatch.setitem(validation.request_models(), "ConformerUploadRequest", _Broken)
    endpoint = constants.CONFORMER_UPLOAD_ENDPOINT
    adapter, client = _adapter(tmp_path, strict=strict)
    body = _broken(endpoint)  # would be refused if the check ran
    with caplog.at_level("WARNING"):
        outcome = adapter._upload(_written(adapter, endpoint, body), body, endpoint=endpoint)
    assert outcome.status == "uploaded" and len(upload_calls(client)) == 1
    sidecar = json.loads(outcome.sidecar_path.read_text())
    assert sidecar["status"] == "uploaded"  # not left pending
    (warning,) = sidecar["warnings"]
    assert warning["code"] == "payload_local_validation_unavailable"
    assert warning["context"]["source"] == "tckdb_core_self_check" and warning["context"]["action"] == "posted_unchecked"
    assert "KeyError" in warning["context"]["error"]
    assert "payload_local_validation_unavailable" in caplog.text


def test_an_artifact_checker_that_raises_posts_unchecked_and_says_so(tmp_path, files, monkeypatch):
    def broken():
        raise KeyError("an internal bug")

    monkeypatch.setattr(validation, "artifact_item_model", broken)
    adapter, client = _adapter(tmp_path)
    (outcome,) = adapter.submit_artifact_batch_for_calculation(
        species_label="CH4", calculation_ref="calc_1", artifacts=[("output_log", files[0])])
    assert outcome.status == "uploaded" and len(upload_calls(client)) == 1
    sidecar = json.loads(outcome.sidecar_path.read_text())
    assert [w["code"] for w in sidecar["warnings"]] == ["payload_local_validation_unavailable"]


# ---------------------------------------------------------------- cost


def test_importing_the_uploader_does_not_import_the_checkers_dependencies():
    import subprocess
    import sys

    code = (
        "import sys, tckdb_core.uploader\n"
        "heavy = [m for m in sys.modules if m == 'jsonschema' or m == 'tckdb_schemas.contract' "
        "or m.startswith('tckdb_schemas.workflows')]\n"
        "assert not heavy, heavy\n"
        "import tckdb_core.validation as v\n"
        "v.request_findings('/uploads/thermo', {})\n"
        "assert 'jsonschema' in sys.modules\n"
    )
    import os
    from pathlib import Path

    import tckdb_core

    env = {**os.environ, "PYTHONPATH": str(Path(tckdb_core.__file__).resolve().parents[1])}
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=os.sep)
    assert done.returncode == 0, done.stderr


def test_an_artifact_plan_is_not_read_and_encoded_a_second_time_to_be_checked(tmp_path, files, monkeypatch):
    import tckdb_core.uploader as uploader

    calls = []
    real = uploader._artifact_batch_bodies
    monkeypatch.setattr(uploader, "_artifact_batch_bodies", lambda prepared: calls.append(1) or real(prepared))
    adapter, client = _adapter(tmp_path)
    outcomes = adapter.submit_artifact_batch_for_calculation(
        species_label="CH4", calculation_ref="calc_1", artifacts=[("output_log", files[0]), ("input", files[1])])
    assert [o.status for o in outcomes] == ["uploaded", "uploaded"]
    assert calls == [1]  # the one that builds the body to post

    # A refused plan builds none: the verdict comes from the prepared metadata.
    calls.clear()
    bad = files[0].rename(files[0].with_name("run.exe"))
    adapter, client = _adapter(tmp_path / "second")
    outcomes = adapter.submit_artifact_batch_for_calculation(
        species_label="CH4", calculation_ref="calc_1", artifacts=[("output_log", bad)])
    assert [o.status for o in outcomes] == ["failed"] and calls == [] and client.calls == []


def test_an_empty_artifact_file_is_refused_from_its_metadata(tmp_path):
    empty = tmp_path / "empty.log"
    empty.write_bytes(b"")
    adapter, client = _adapter(tmp_path)
    outcomes = adapter.submit_artifact_batch_for_calculation(
        species_label="CH4", calculation_ref="calc_1", artifacts=[("output_log", empty)])
    assert client.calls == []
    assert [o.status for o in outcomes] == ["failed"] and "artifacts/0/bytes" in outcomes[0].error


@pytest.mark.parametrize("kind", sorted(STANDALONE_ROUTES))
def test_the_switch_does_not_disable_the_standalone_refusal_before_the_write(tmp_path, kind):
    """`submit_standalone` refuses a bad request before anything exists on disk, switch or not."""
    adapter, client = _adapter(tmp_path, validate_before_upload=False)
    with pytest.raises(StandalonePayloadError, match="does not match the tckdb-schemas contract"):
        adapter.submit_standalone(kind, _broken(STANDALONE_ROUTES[kind].endpoint), label="rec")
    assert client.calls == [] and not (tmp_path / "out").exists()


def test_standalone_validation_is_the_runtime_check():
    endpoint = constants.THERMO_ENDPOINT
    body = _broken(endpoint)
    assert standalone.standalone_payload_errors("thermo", body) == request_findings(endpoint, body)
    with pytest.raises(StandalonePayloadError) as caught:
        standalone.validate_standalone_payload("thermo", body)
    assert caught.value.errors == request_findings(endpoint, body)
    assert str(caught.value).startswith("thermo payload does not match the tckdb-schemas contract for ThermoUploadRequest")
