"""``submit_artifacts_for_calculation`` and its batch form, on a producer that knows no ARC."""

import json
import logging

import pytest

from tckdb_core.config import TCKDBArtifactConfig, TCKDBConfig, UPLOAD_MODE_COMPUTED_SPECIES
from tckdb_core.testing.stubs import StubClient, StubResponse, upload_calls
from tckdb_core.uploader import TCKDBUploaderBase, artifact_batch_digest


class DemoAdapter(TCKDBUploaderBase):
    PRODUCER_TAG = "demo"
    PRODUCER_NAME = "Demo"
    IDEMPOTENCY_NAMESPACE = "demo"
    LOGGER_NAME = "demo_logger"

    def _project_label_for(self, output_doc):
        return output_doc.get("project")


def _config(tmp_path, **kw):
    kw.setdefault("preflight", False)
    kw.setdefault("artifacts", TCKDBArtifactConfig(upload=True, kinds=("output_log", "input")))
    return TCKDBConfig(
        enabled=True, base_url="https://example.invalid", payload_dir=str(tmp_path / "out"),
        api_key_env="DEMO_TCKDB_KEY", **kw,
    )


@pytest.fixture
def files(tmp_path):
    log = tmp_path / "run.log"
    log.write_bytes(b"output bytes")
    deck = tmp_path / "run.in"
    deck.write_bytes(b"input bytes")
    return log, deck


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("DEMO_TCKDB_KEY", "k")


def _adapter(tmp_path, client=None, **kw):
    client = client or StubClient(response=StubResponse({"warnings": []}))
    adapter = DemoAdapter(
        _config(tmp_path, **kw), project_directory=tmp_path,
        client_factory=lambda *a, **k: client,
    )
    return adapter, client


def test_a_disabled_config_returns_none(tmp_path, files):
    adapter = DemoAdapter(TCKDBConfig(enabled=False))
    assert adapter.submit_artifacts_for_calculation(
        species_label="CH4", calculation_ref="calc_1", file_path=files[0]) is None
    assert adapter.submit_artifact_batch_for_calculation(
        species_label="CH4", calculation_ref="calc_1", artifacts=[("output_log", files[0])]) is None


def test_one_file_is_posted_to_the_calculations_artifact_route(tmp_path, files, key):
    adapter, client = _adapter(tmp_path)
    outcome = adapter.submit_artifacts_for_calculation(
        species_label="CH4", calculation_ref="calc_1", calculation_id=7, file_path=files[0],
        output_doc={"project": "proj A"})
    assert outcome.status == "uploaded"
    (call,) = upload_calls(client)
    assert call["method"] == "POST" and call["path"] == "/calculations/calc_1/artifacts"
    (art,) = call["json"]["artifacts"]
    assert art["kind"] == "output_log" and art["filename"] == "run.log" and art["bytes"] == 12
    assert call["idempotency_key"].startswith("demo:proj-A:CH4:artifact:calc_1-")
    assert call["idempotency_key"].endswith(":artifact-batch")
    sidecar = json.loads(outcome.sidecar_path.read_text())
    assert sidecar["status"] == "uploaded"
    assert sidecar["calculation_ref"] == "calc_1" and sidecar["calculation_id"] == 7
    assert outcome.idempotency_key and "calc_1" in outcome.idempotency_key


def test_a_batch_is_one_post_per_calculation_keyed_on_the_batch_digest(tmp_path, files, key):
    adapter, client = _adapter(tmp_path)
    outcomes = adapter.submit_artifact_batch_for_calculation(
        species_label="CH4", calculation_ref="calc_1",
        artifacts=[("output_log", files[0]), ("input", files[1])])
    assert [o.status for o in outcomes] == ["uploaded", "uploaded"]
    (call,) = upload_calls(client)
    assert [a["kind"] for a in call["json"]["artifacts"]] == ["output_log", "input"]
    # The key carries the digest of the batch's items (handle, kind, filename, sha256, size);
    # ``calculation_key`` is not one of its inputs, so items prepared here digest the same.
    prepared = [
        adapter._prepare_artifact_upload(
            output_doc={}, species_label="CH4", calculation_id=None, calculation_ref="calc_1",
            kind=kind, file_path=path, artifact_cfg=adapter._config.artifacts)
        for kind, path in (("output_log", files[0]), ("input", files[1]))
    ]
    digest = artifact_batch_digest(prepared)
    # The project is unknown (no config label, empty document), so the prefix says so.
    assert call["idempotency_key"] == f"demo:unknown:CH4:artifact:calc_1-{digest}:artifact-batch"


def test_the_config_project_label_wins_over_the_hook(tmp_path, files, key):
    adapter, client = _adapter(tmp_path, project_label="from config")
    adapter.submit_artifacts_for_calculation(
        species_label="CH4", calculation_ref="calc_1", file_path=files[0],
        output_doc={"project": "from doc"})
    assert upload_calls(client)[0]["idempotency_key"].startswith("demo:from-config:CH4:artifact:")


def test_skipped_items_come_first_and_the_rest_are_still_posted(tmp_path, files, key):
    adapter, client = _adapter(tmp_path)
    outcomes = adapter.submit_artifact_batch_for_calculation(
        species_label="CH4", calculation_ref="calc_1",
        artifacts=[("output_log", files[0]), ("input", tmp_path / "missing.in")])
    assert [o.status for o in outcomes] == ["skipped", "uploaded"]
    assert outcomes[0].kind == "input" and outcomes[0].skip_reason.startswith("file missing")
    assert len(upload_calls(client)[0]["json"]["artifacts"]) == 1


def test_all_skipped_posts_nothing(tmp_path, files, key):
    adapter, client = _adapter(tmp_path, artifacts=TCKDBArtifactConfig(upload=False))
    outcome = adapter.submit_artifacts_for_calculation(
        species_label="CH4", calculation_ref="calc_1", file_path=files[0])
    assert outcome.status == "skipped" and outcome.skip_reason == "artifacts.upload is False"
    assert client.calls == []


def test_a_bundle_mode_suppresses_the_standalone_upload_of_inline_kinds(tmp_path, files, key):
    adapter, client = _adapter(tmp_path, upload_mode=UPLOAD_MODE_COMPUTED_SPECIES)
    outcomes = adapter.submit_artifact_batch_for_calculation(
        species_label="CH4", calculation_ref="calc_1",
        artifacts=[("output_log", files[0]), ("input", files[1])])
    assert [o.status for o in outcomes] == ["skipped", "skipped"]
    assert all("carries kind=" in o.skip_reason and "inline in the bundle" in o.skip_reason
               for o in outcomes)
    assert client.calls == []


def test_without_a_ref_nothing_is_posted_and_the_skip_is_logged(tmp_path, files, key, caplog):
    adapter, client = _adapter(tmp_path)
    with caplog.at_level(logging.WARNING, logger="demo_logger"):
        outcomes = adapter.submit_artifact_batch_for_calculation(
            species_label="CH4", calculation_id=42,
            artifacts=[("output_log", files[0]), ("input", files[1])])
    assert client.calls == []
    assert [o.status for o in outcomes] == ["skipped", "skipped"]
    assert [o.kind for o in outcomes] == ["output_log", "input"]
    assert all(o.calculation_id == 42 and o.calculation_ref is None and o.sidecar_path is None
               for o in outcomes)
    assert all(o.skip_reason.startswith("calculation_ref_not_returned:") for o in outcomes)
    assert "calculation_ref_not_returned" in caplog.text
    (record,) = [r for r in caplog.records if "calculation_ref_not_returned" in r.getMessage()]
    assert record.name == "demo_logger" and record.levelno == logging.WARNING
    assert list((tmp_path / "out").rglob("*")) == []


def test_an_empty_ref_with_an_id_counts_as_no_ref_and_warns(tmp_path, files, key, caplog):
    adapter, client = _adapter(tmp_path)
    with caplog.at_level(logging.WARNING, logger="demo_logger"):
        (outcome,) = adapter.submit_artifact_batch_for_calculation(
            species_label="CH4", calculation_ref="", calculation_id=3,
            artifacts=[("output_log", files[0])])
    assert outcome.status == "skipped" and client.calls == []
    assert outcome.skip_reason.startswith("calculation_ref_not_returned:")
    assert "calculation_ref_not_returned" in caplog.text


def test_an_empty_ref_without_an_id_raises(tmp_path, files, key):
    adapter, _ = _adapter(tmp_path)
    with pytest.raises(ValueError, match="needs a calculation_ref"):
        adapter.submit_artifact_batch_for_calculation(
            species_label="CH4", calculation_ref="", artifacts=[("output_log", files[0])])


def test_an_empty_artifact_list_returns_an_empty_list(tmp_path, key):
    adapter, client = _adapter(tmp_path)
    assert adapter.submit_artifact_batch_for_calculation(
        species_label="CH4", calculation_ref="calc_1", artifacts=[]) == []
    assert client.calls == []


def test_the_single_file_method_survives_a_producer_overriding_the_batch_signature(
        tmp_path, files, key):
    class OtherSignature(DemoAdapter):
        def submit_artifact_batch_for_calculation(self, *, record, calc, artifacts):
            return super().submit_artifact_batch_for_calculation(
                species_label=record["label"], calculation_ref=calc, artifacts=artifacts)

    client = StubClient(response=StubResponse({"warnings": []}))
    adapter = OtherSignature(
        _config(tmp_path), project_directory=tmp_path, client_factory=lambda *a, **k: client)
    # The base single-file method does not go through the overridden public batch method.
    outcome = adapter.submit_artifacts_for_calculation(
        species_label="CH4", calculation_ref="calc_1", file_path=files[0])
    assert outcome.status == "uploaded"
    assert upload_calls(client)[0]["path"] == "/calculations/calc_1/artifacts"
    # And the override still reaches the base batch logic.
    (again,) = adapter.submit_artifact_batch_for_calculation(
        record={"label": "CH4"}, calc="calc_1", artifacts=[("input", files[1])])
    assert again.status == "uploaded"


def test_neither_a_ref_nor_an_id_raises(tmp_path, files, key):
    adapter, _ = _adapter(tmp_path)
    with pytest.raises(ValueError, match="needs a calculation_ref"):
        adapter.submit_artifact_batch_for_calculation(
            species_label="CH4", artifacts=[("output_log", files[0])])


def test_a_refused_batch_is_recorded_failed(tmp_path, files, key):
    adapter, client = _adapter(tmp_path, client=StubClient(raise_exc=RuntimeError("boom")))
    outcome = adapter.submit_artifacts_for_calculation(
        species_label="CH4", calculation_ref="calc_1", file_path=files[0])
    assert outcome.status == "failed"
    assert json.loads(outcome.sidecar_path.read_text())["status"] == "failed"
