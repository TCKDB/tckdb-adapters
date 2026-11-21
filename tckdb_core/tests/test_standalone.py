"""The standalone record routes: transport and validation of a request a producer already built.

The requests are the examples tckdb-schemas ships with each route's JSON Schema, so they are
valid by the contract's own statement. The core builds none of them.
"""

import copy
import json
import re

import pytest
from tckdb_client.errors import TCKDBConnectionError
from tckdb_schemas import contract

from tckdb_core import constants
from tckdb_core.config import TCKDBConfig
from tckdb_core.payload_writer import PayloadWriter
from tckdb_core.standalone import (
    DEFAULT_QUALIFIER,
    STANDALONE_ROUTES,
    StandalonePayloadError,
    standalone_payload_errors,
    standalone_route,
    validate_standalone_payload,
)
from tckdb_core.testing.contract import ROUTE_SCHEMAS, assert_matches_contract
from tckdb_core.testing.stubs import SequencedReadyzClient, StubClient, StubResponse, upload_calls
from tckdb_core.uploader import TCKDBUploaderBase

KINDS = sorted(STANDALONE_ROUTES)
ENDPOINTS = {
    "thermo": "/uploads/thermo",
    "kinetics": "/uploads/kinetics",
    "statmech": "/uploads/statmech",
    "transport": "/uploads/transport",
    "reaction": "/uploads/reactions",
    "network": "/uploads/networks",
    "network_pdep": "/uploads/networks/pdep",
}
KEY_SHAPE = re.compile(r"^demo:proj:(?P<label>[^:]+):(?P<qualifier>[^:]+):(?P<kind>[^:]+):[0-9a-f]{16}$")
RIGHTS = {"license": "CC-BY-4.0", "depositor_attests_right_to_license": True}


class DemoAdapter(TCKDBUploaderBase):
    PRODUCER_TAG = "demo"
    PRODUCER_NAME = "Demo"
    IDEMPOTENCY_NAMESPACE = "demo"


def _example(kind):
    return copy.deepcopy(contract.json_schema(STANDALONE_ROUTES[kind].schema_name)["examples"][0])


def _config(tmp_path, **overrides):
    values = dict(
        enabled=True, base_url="https://example.invalid", payload_dir=str(tmp_path),
        api_key_env="DEMO_TCKDB_KEY", preflight=False, project_label="proj",
    )
    values.update(overrides)
    return TCKDBConfig(**values)


def _adapter(tmp_path, client=None, **overrides):
    return DemoAdapter(_config(tmp_path, **overrides), client_factory=(lambda cfg, key: client) if client else None)


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("DEMO_TCKDB_KEY", "k")


def _sidecar(outcome):
    return json.loads(outcome.sidecar_path.read_text())


# ---------------------------------------------------------------- the route table


def test_the_table_names_every_new_kind_once():
    assert set(STANDALONE_ROUTES) == set(ENDPOINTS)
    assert {route.endpoint for route in STANDALONE_ROUTES.values()} == set(ENDPOINTS.values())
    for kind, route in STANDALONE_ROUTES.items():
        assert route.kind == kind and route.endpoint == ENDPOINTS[kind]
    assert standalone_route("thermo").subdir == PayloadWriter.THERMO_SUBDIR == "thermo"
    with pytest.raises(ValueError, match="unknown standalone payload kind"):
        standalone_route("conformer_calculation")


def test_the_table_agrees_with_the_constants_and_the_test_kit():
    constant_pairs = {
        constants.THERMO_KIND: constants.THERMO_ENDPOINT,
        constants.KINETICS_KIND: constants.KINETICS_ENDPOINT,
        constants.STATMECH_KIND: constants.STATMECH_ENDPOINT,
        constants.TRANSPORT_KIND: constants.TRANSPORT_ENDPOINT,
        constants.REACTION_KIND: constants.REACTION_ENDPOINT,
        constants.NETWORK_KIND: constants.NETWORK_ENDPOINT,
        constants.NETWORK_PDEP_KIND: constants.NETWORK_PDEP_ENDPOINT,
    }
    assert {kind: route.endpoint for kind, route in STANDALONE_ROUTES.items()} == constant_pairs
    for route in STANDALONE_ROUTES.values():
        assert ROUTE_SCHEMAS[route.endpoint] == route.schema_name
        assert route.schema_name in contract.schema_names()


@pytest.mark.parametrize("kind", KINDS)
def test_each_endpoint_is_a_route_in_the_installed_contract(kind):
    route = STANDALONE_ROUTES[kind]
    document = contract.markdown()
    assert f"`POST /api/v1{route.endpoint}`" in document
    assert f"Surface `{route.schema_name}`" in document


def test_the_new_kinds_do_not_collide_with_the_existing_ones():
    existing = {
        constants.PAYLOAD_KIND, constants.COMPUTED_SPECIES_KIND,
        constants.COMPUTED_REACTION_KIND, constants.TRANSITION_STATE_KIND,
    }
    assert not existing & set(STANDALONE_ROUTES)
    subdirs = {route.subdir for route in STANDALONE_ROUTES.values()}
    assert len(subdirs) == len(STANDALONE_ROUTES)
    assert not subdirs & {
        PayloadWriter.SUBDIR, PayloadWriter.ARTIFACT_SUBDIR, PayloadWriter.COMPUTED_SPECIES_SUBDIR,
        PayloadWriter.COMPUTED_REACTION_SUBDIR, PayloadWriter.TRANSITION_STATE_SUBDIR,
    }


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize("kind", KINDS)
def test_the_contracts_own_example_is_accepted(kind):
    example = _example(kind)
    assert standalone_payload_errors(kind, example) == []
    validate_standalone_payload(kind, example)
    assert_matches_contract(example, STANDALONE_ROUTES[kind].schema_name)  # the test kit agrees


@pytest.mark.parametrize("kind", KINDS)
def test_an_unknown_top_level_field_is_refused(kind):
    payload = _example(kind)
    payload["not_a_field_of_any_route"] = 1
    with pytest.raises(StandalonePayloadError) as caught:
        validate_standalone_payload(kind, payload)
    assert any("not_a_field_of_any_route" in line for line in caught.value.errors)
    assert STANDALONE_ROUTES[kind].schema_name in str(caught.value)


@pytest.mark.parametrize("kind", [k for k in KINDS if contract.json_schema(STANDALONE_ROUTES[k].schema_name).get("required")])
def test_a_missing_required_field_is_refused(kind):
    payload = _example(kind)
    required = contract.json_schema(STANDALONE_ROUTES[kind].schema_name)["required"][0]
    del payload[required]
    errors = standalone_payload_errors(kind, payload)
    assert errors and any(required in line for line in errors)


def test_non_json_and_non_finite_values_are_refused():
    payload = _example("thermo")
    payload["h298_kj_mol"] = float("nan")
    assert any("not serialisable" in line for line in standalone_payload_errors("thermo", payload))
    payload["h298_kj_mol"] = object()
    assert any("not serialisable" in line for line in standalone_payload_errors("thermo", payload))
    assert standalone_payload_errors("thermo", ["not", "an", "object"])


def test_enum_values_are_checked_and_server_side_rules_are_not():
    payload = _example("thermo")
    payload["phase"] = "not_a_phase"
    assert standalone_payload_errors("thermo", payload)
    # The schema takes any string as enthalpy_reference_kind; TCKDB refuses an unknown one
    # (enthalpy_reference_kind_unrecognized), so only the live gate reaches that rule.
    payload = _example("thermo")
    payload["enthalpy_reference_kind"] = "not_a_reference_kind"
    assert standalone_payload_errors("thermo", payload) == []


# ---------------------------------------------------------------- offline


@pytest.mark.parametrize("kind", KINDS)
def test_offline_the_payload_and_a_skipped_sidecar_are_written(kind, tmp_path):
    client = StubClient(response=StubResponse({}))
    adapter = _adapter(tmp_path, client, upload=False)
    payload = _example(kind)
    outcome = adapter.submit_standalone(kind, payload, label="my label/1")

    assert outcome.status == "skipped"
    assert client.calls == []
    assert outcome.payload_path.parent == tmp_path / STANDALONE_ROUTES[kind].subdir
    assert json.loads(outcome.payload_path.read_text()) == payload
    sidecar = _sidecar(outcome)
    assert sidecar["status"] == "skipped"
    assert sidecar["endpoint"] == ENDPOINTS[kind]
    assert sidecar["payload_kind"] == kind
    assert sidecar["base_url"] == "https://example.invalid"
    assert sidecar["bundle_format_version"] == "0"
    assert sidecar["idempotency_key"] == outcome.idempotency_key
    match = KEY_SHAPE.match(outcome.idempotency_key)
    assert match and match["kind"] == kind and match["qualifier"] == DEFAULT_QUALIFIER
    assert match["label"].startswith("my")


def test_the_key_follows_the_payload_and_the_label(tmp_path):
    adapter = _adapter(tmp_path, upload=False)
    thermo = _example("thermo")
    first = adapter.submit_standalone("thermo", thermo, label="ethane").idempotency_key
    again = adapter.submit_standalone("thermo", thermo, label="ethane").idempotency_key
    assert first == again
    other_label = adapter.submit_standalone("thermo", thermo, label="methane").idempotency_key
    qualified = adapter.submit_standalone("thermo", thermo, label="ethane", qualifier="level-b").idempotency_key
    other_project = adapter.submit_standalone("thermo", thermo, label="ethane", project_label="elsewhere").idempotency_key
    changed = copy.deepcopy(thermo)
    changed["note"] = "edited"
    edited = adapter.submit_standalone("thermo", changed, label="ethane").idempotency_key
    assert len({first, other_label, qualified, other_project, edited}) == 5
    assert ":level-b:" in qualified and ":elsewhere:" in other_project
    # the payload kind keeps two kinds under one label apart
    assert first != adapter.submit_standalone("transport", _example("transport"), label="ethane").idempotency_key


def test_the_key_has_no_project_part_when_there_is_no_project(tmp_path):
    adapter = _adapter(tmp_path, upload=False, project_label=None)
    key = adapter.submit_standalone("statmech", _example("statmech"), label="x").idempotency_key
    assert key.startswith("demo:x:standalone:statmech:")


def test_a_disabled_config_submits_nothing(tmp_path):
    adapter = _adapter(tmp_path, enabled=False)
    assert adapter.submit_standalone("thermo", _example("thermo"), label="x") is None
    assert list(tmp_path.iterdir()) == []


def test_the_producers_warnings_are_seeded_in_the_sidecar(tmp_path):
    warning = {"code": "demo_note", "message": "m", "field": "note", "context": {"source": "demo_self_check"}}
    outcome = _adapter(tmp_path, upload=False).submit_standalone(
        "kinetics", _example("kinetics"), label="r", warnings=[warning])
    assert _sidecar(outcome)["warnings"] == [warning] == outcome.warnings


# ---------------------------------------------------------------- through the stub client


@pytest.mark.parametrize("kind", KINDS)
def test_the_request_is_posted_with_its_key_and_the_sidecar_records_the_answer(kind, tmp_path):
    answer = {"warnings": [{"code": "server_note", "message": "m", "field": None, "context": None}],
              "submission_ref": "sub_1"}
    client = StubClient(response=StubResponse(answer, replayed=True))
    outcome = _adapter(tmp_path, client).submit_standalone(kind, _example(kind), label="rec", qualifier="q1")

    (call,) = upload_calls(client)
    assert call["method"] == "POST" and call["path"] == ENDPOINTS[kind]
    assert call["json"] == _example(kind)
    assert call["idempotency_key"] == outcome.idempotency_key
    assert KEY_SHAPE.match(call["idempotency_key"])["qualifier"] == "q1"
    assert outcome.status == "uploaded" and outcome.submission_ref == "sub_1"
    sidecar = _sidecar(outcome)
    assert sidecar["status"] == "uploaded" and sidecar["idempotency_replayed"] is True
    assert sidecar["response_status_code"] == 201
    assert sidecar["warnings"][0]["code"] == "server_note"
    assert client.closed


def test_the_readiness_probe_runs_before_the_post(tmp_path):
    client = StubClient(response=StubResponse({}))
    _adapter(tmp_path, client, preflight=True).submit_standalone("transport", _example("transport"), label="x")
    assert [call["path"] for call in client.calls] == ["/readyz", "/uploads/transport"]


def test_a_server_that_never_gets_ready_leaves_the_payload_and_a_failed_sidecar(tmp_path):
    not_ready = StubResponse({"status": "starting"}, status_code=503)
    client = SequencedReadyzClient(readyz_sequence=[not_ready], response=StubResponse({}))
    adapter = _adapter(tmp_path, client, preflight=True)
    adapter._sleep_between_probes = lambda seconds: None
    outcome = adapter.submit_standalone("thermo", _example("thermo"), label="x")
    assert outcome.status == "failed"
    assert upload_calls(client) == []
    assert outcome.payload_path.exists() and _sidecar(outcome)["status"] == "failed"


def test_an_upload_failure_is_recorded_and_strict_raises(tmp_path):
    boom = TCKDBConnectionError("down")
    lenient = _adapter(tmp_path, StubClient(raise_exc=boom)).submit_standalone(
        "statmech", _example("statmech"), label="x")
    assert lenient.status == "failed" and "down" in lenient.error
    assert _sidecar(lenient)["status"] == "failed"
    with pytest.raises(TCKDBConnectionError):
        _adapter(tmp_path, StubClient(raise_exc=boom), strict=True).submit_standalone(
            "statmech", _example("statmech"), label="x")
    assert json.loads(lenient.sidecar_path.read_text())["last_error"]


def test_a_missing_api_key_is_a_recorded_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_TCKDB_KEY")
    client = StubClient(response=StubResponse({}))
    outcome = _adapter(tmp_path, client).submit_standalone("thermo", _example("thermo"), label="x")
    assert outcome.status == "failed" and "API key not configured" in outcome.error
    assert client.calls == []


# ---------------------------------------------------------------- refusals


@pytest.mark.parametrize("kind", KINDS)
def test_an_invalid_payload_is_refused_before_anything_is_written_or_sent(kind, tmp_path):
    client = StubClient(response=StubResponse({}))
    bad = _example(kind)
    bad["not_a_field_of_any_route"] = 1
    for strict in (False, True):
        with pytest.raises(StandalonePayloadError):
            _adapter(tmp_path, client, strict=strict).submit_standalone(kind, bad, label="x")
    assert client.calls == []
    assert list(tmp_path.iterdir()) == []


def test_other_bad_calls_are_refused(tmp_path):
    adapter = _adapter(tmp_path, StubClient(response=StubResponse({})))
    with pytest.raises(ValueError, match="unknown standalone payload kind"):
        adapter.submit_standalone("conformer_calculation", {}, label="x")
    with pytest.raises(StandalonePayloadError, match="label"):
        adapter.submit_standalone("thermo", _example("thermo"), label="  ")
    with pytest.raises(StandalonePayloadError, match="mapping"):
        adapter.submit_standalone("thermo", [1], label="x")
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------- rights


def test_configured_rights_are_added_to_a_copy(tmp_path):
    payload = _example("reaction")
    client = StubClient(response=StubResponse({}))
    outcome = _adapter(tmp_path, client, rights=RIGHTS).submit_standalone("reaction", payload, label="x")
    assert "rights" not in payload  # the caller's request is untouched
    (call,) = upload_calls(client)
    assert call["json"]["rights"] == RIGHTS
    assert json.loads(outcome.payload_path.read_text())["rights"] == RIGHTS
    # the key is built from the finished request, so rights change it
    bare = _adapter(tmp_path, upload=False).submit_standalone("reaction", payload, label="x")
    assert bare.idempotency_key != outcome.idempotency_key


def test_rights_the_request_states_are_kept_when_they_match_and_refused_when_they_differ(tmp_path):
    payload = _example("thermo")
    payload["rights"] = dict(RIGHTS)
    ok = _adapter(tmp_path, upload=False, rights=RIGHTS).submit_standalone("thermo", payload, label="x")
    assert ok.status == "skipped"
    payload["rights"] = {**RIGHTS, "license": "CC0-1.0"}
    with pytest.raises(StandalonePayloadError, match="rights"):
        _adapter(tmp_path, upload=False, rights=RIGHTS).submit_standalone("thermo", payload, label="y")
    # with no configured rights the request's own are sent as they are
    own = _adapter(tmp_path, upload=False).submit_standalone("thermo", payload, label="z")
    assert json.loads(own.payload_path.read_text())["rights"]["license"] == "CC0-1.0"


def test_rights_that_fail_the_schema_are_refused(tmp_path):
    with pytest.raises(StandalonePayloadError):
        _adapter(tmp_path, upload=False, rights={"license": "CC0-1.0", "nonsense": 1}).submit_standalone(
            "thermo", _example("thermo"), label="x")


# ---------------------------------------------------------------- qualifier, long values, long labels


def test_two_qualifiers_under_one_label_keep_two_files_and_archive_nothing(tmp_path):
    adapter = _adapter(tmp_path, upload=False)
    thermo = _example("thermo")
    first = adapter.submit_standalone("thermo", thermo, label="ethane", qualifier="level-a")
    second = adapter.submit_standalone("thermo", thermo, label="ethane", qualifier="level-b")
    assert first.payload_path != second.payload_path and first.sidecar_path != second.sidecar_path
    assert first.payload_path.name == "ethane.level-a.payload.json"
    for outcome in (first, second):
        assert outcome.payload_path.exists() and outcome.sidecar_path.exists()
    directory = tmp_path / "thermo"
    assert sorted(p.name for p in directory.iterdir() if p.is_file()) == [
        "ethane.level-a.meta.json", "ethane.level-a.payload.json",
        "ethane.level-b.meta.json", "ethane.level-b.payload.json"]
    assert not (directory / PayloadWriter.ARCHIVE_SUBDIR).exists()
    assert _sidecar(first)["idempotency_key"] != _sidecar(second)["idempotency_key"]


def test_a_large_offending_value_is_not_repeated_in_the_error(tmp_path):
    payload = _example("thermo")
    payload["phase"] = "x" * 50_000
    with pytest.raises(StandalonePayloadError) as caught:
        validate_standalone_payload("thermo", payload)
    assert len(str(caught.value)) < 2_000
    assert all(len(line) < 300 for line in caught.value.errors)
    assert "[truncated]" in str(caught.value)


def test_a_label_that_overflows_the_key_limit_is_refused_before_any_write(tmp_path):
    client = StubClient(response=StubResponse({}))
    with pytest.raises(StandalonePayloadError, match="idempotency key") as caught:
        _adapter(tmp_path, client).submit_standalone("thermo", _example("thermo"), label="x" * 300)
    assert caught.value.errors
    assert client.calls == [] and list(tmp_path.iterdir()) == []
