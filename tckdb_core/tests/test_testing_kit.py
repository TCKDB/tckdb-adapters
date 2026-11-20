"""The test kit other adapters' suites import: contract checks, the contract hook, the live-gate guards."""

import pytest

from tckdb_core.testing import contract, live
from tckdb_core.testing.backend_import import backend_modules  # noqa: F401  (importable by an adapter's tests)


def test_contract_validate_runs_the_model_then_the_schema():
    from tckdb_schemas.workflows.conformer_upload import ConformerUploadRequest

    with pytest.raises(ValueError):
        contract.contract_validate(ConformerUploadRequest, {})
    assert set(contract.REQUEST_MODELS) == {
        "ConformerUploadRequest", "ComputedSpeciesUploadRequest",
        "ComputedReactionUploadRequest", "TransitionStateUploadRequest"}


def test_artifact_bodies_group_by_calculation(tmp_path):
    from types import SimpleNamespace

    one, two = tmp_path / "a.log", tmp_path / "b.log"
    one.write_text("a")
    two.write_text("b")
    items = [SimpleNamespace(kind="output_log", path=str(one), calculation_id=1, sha256="x", bytes=1),
             SimpleNamespace(kind="input", path=str(two), calculation_id=1),
             SimpleNamespace(kind="output_log", path=str(two), calculation_id=2)]
    bodies = contract.artifacts_request_bodies(items)
    assert [len(b["artifacts"]) for b in bodies] == [2, 1]
    assert bodies[0]["artifacts"][0]["filename"] == "a.log" and bodies[0]["artifacts"][0]["sha256"] == "x"


def test_the_loopback_guard_and_commit_probe():
    assert live.assert_loopback_url("http://127.0.0.1:58010/api/v1")
    for bad in ("https://tckdb.example.org/api/v1", "http://127.0.0.1:8010/api/v1"):
        with pytest.raises(ValueError):
            live.assert_loopback_url(bad)
    assert live.assert_integration_backend(
        {"components": {"artifact_storage": {"bucket": "tckdb-integ-artifacts"}}}) == "tckdb-integ-artifacts"
    with pytest.raises(ValueError):
        live.assert_integration_backend({"components": {"artifact_storage": {"bucket": "tckdb-artifacts"}}})
    assert live.commit_probe({"primary_calculation": {"calculation_id": 7}}) == "/calculations/7"
    assert live.commit_probe({"calculation_keys": {"a": 9, "b": 4}}) == "/calculations/4"
    assert live.commit_probe("no") is None


def test_warning_code_registry_type():
    from tckdb_core.warning_codes import CodedEnum, CoreWarning, registry

    assert CoreWarning.CALCULATION_REF_NOT_RETURNED == "calculation_ref_not_returned"
    assert str(CoreWarning.CALCULATION_REF_NOT_RETURNED) == f"{CoreWarning.CALCULATION_REF_NOT_RETURNED}" == \
        "calculation_ref_not_returned"
    assert type(CoreWarning.CALCULATION_REF_NOT_RETURNED.value) is str
    assert issubclass(CoreWarning, CodedEnum)
    rows = list(registry(CoreWarning))
    assert rows[0][0] == "calculation_ref_not_returned" and "\n" not in rows[0][1]


def test_stub_client_records_calls_and_answers_the_probe():
    from tckdb_core.testing.stubs import StubClient, StubResponse, upload_calls

    client = StubClient(response=StubResponse({"id": 7}, headers={"X-Request-ID": "req-1"}))
    assert client.request_json("GET", "/readyz", authenticated=False).data["status"] == "ready"
    answer = client.request_json("POST", "/uploads/conformers", json={"a": 1}, idempotency_key="k" * 20)
    assert (answer.data, answer.status_code, answer.idempotency_replayed) == ({"id": 7}, 201, False)
    assert [c["path"] for c in client.calls] == ["/readyz", "/uploads/conformers"]
    assert [(c["path"], c["json"], c["idempotency_key"]) for c in upload_calls(client)] == [
        ("/uploads/conformers", {"a": 1}, "k" * 20)]
    client.close()
    assert client.closed

    failing = StubClient(raise_exc=RuntimeError("refused"), readyz_response=OSError("down"))
    with pytest.raises(OSError):
        failing.request_json("GET", "/readyz")
    with pytest.raises(RuntimeError):
        failing.request_json("POST", "/uploads/conformers", json={})


def test_sequenced_readyz_client_repeats_its_last_answer():
    from tckdb_core.testing.stubs import SequencedReadyzClient, StubResponse, upload_calls

    ready = StubResponse({"status": "ready"}, status_code=200)
    client = SequencedReadyzClient(readyz_sequence=[OSError("down"), ready], response=StubResponse({"ok": True}))
    with pytest.raises(OSError):
        client.request_json("GET", "/readyz")
    assert client.request_json("GET", "/readyz") is ready
    assert client.request_json("GET", "/readyz") is ready
    assert client.request_json("POST", "/uploads/conformers", json={}).data == {"ok": True}
    assert len(client.calls) == 4 and len(upload_calls(client)) == 1


def _readme_producer_example() -> str:
    """The first fenced ``python`` block under "Writing a producer" in the package README."""
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Writing a producer", 1)[1]
    return section.split("```python\n", 1)[1].split("\n```", 1)[0]


def test_the_readme_producer_example_runs_against_a_stub_client(tmp_path, monkeypatch):
    """The README's example is executed as written, so it cannot drift from the API."""
    import json
    from types import SimpleNamespace

    from tckdb_core.config import TCKDBConfig
    from tckdb_core.constants import COMPUTED_SPECIES_ENDPOINT
    from tckdb_core.testing.stubs import StubClient, StubResponse, upload_calls
    from tckdb_core.warning_codes import CodedEnum

    namespace: dict = {"__name__": "readme_producer_example"}
    exec(compile(_readme_producer_example(), "tckdb_core/README.md", "exec"), namespace)
    MyToolAdapter, MyToolWarning = namespace["MyToolAdapter"], namespace["MyToolWarning"]
    assert issubclass(MyToolWarning, CodedEnum)

    record = SimpleNamespace(label="h2", smiles="[H][H]")
    monkeypatch.setenv("TCKDB_STUB_KIT_KEY", "secret")

    def config(**overrides):
        return TCKDBConfig(**{"enabled": True, "base_url": "http://127.0.0.1:1/api/v1", "payload_dir": str(tmp_path),
                              "api_key_env": "TCKDB_STUB_KIT_KEY", "project_label": "proj", **overrides})

    assert MyToolAdapter(config(enabled=False)).submit_species(record) is None
    assert MyToolAdapter(config(upload=False)).submit_species(record).status == "skipped"

    client = StubClient(response=StubResponse({"id": 3}))
    adapter = MyToolAdapter(config(rights={"license": "CC-BY-4.0"}), client_factory=lambda cfg, key: client)
    assert adapter.submit_species(record).status == "uploaded"
    (call,) = upload_calls(client)
    assert call["path"] == COMPUTED_SPECIES_ENDPOINT
    assert call["json"] == {"species_entry": {"smiles": "[H][H]"}, "rights": {"license": "CC-BY-4.0"}}
    assert call["idempotency_key"].startswith("mytool:proj:h2:")
    sidecar = json.loads((tmp_path / "computed_species" / "h2.meta.json").read_text())
    assert sidecar["status"] == "uploaded" and sidecar["idempotency_key"] == call["idempotency_key"]
    (warning,) = sidecar["warnings"]
    assert warning["code"] == MyToolWarning.REFERENCE_PRESSURE_NOT_STATED.value
    assert warning["context"] == {"source": "tckdb_mytool_self_check", "action": "reference_pressure_omitted"}
