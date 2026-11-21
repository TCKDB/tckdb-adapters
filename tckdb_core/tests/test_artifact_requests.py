"""The exact requests, keys and sidecars a calculation's artifact upload produces.

These pin what goes on the wire for the standalone artifact path: the URL, the
body, the ``Idempotency-Key``, the order of the requests, and what each sidecar
records, including when a later request fails. A calculation whose files total
more than the client's inline budget goes out as several requests; the budget is
patched to a few bytes here so no test allocates 64 MiB.
"""

import base64
import functools
import hashlib
import json

import httpx

import pytest

from tckdb_client import TCKDBClient
from tckdb_client.builders import artifact as client_artifact
from tckdb_client.builders import PlannedArtifactUpload

from tckdb_core import uploader
from tckdb_core.config import TCKDBArtifactConfig, TCKDBConfig
from tckdb_core.testing.stubs import StubClient, StubResponse, upload_calls
from tckdb_core.uploader import TCKDBUploaderBase, artifact_batch_digest


class DemoAdapter(TCKDBUploaderBase):
    PRODUCER_TAG = "demo"
    PRODUCER_NAME = "Demo"
    IDEMPOTENCY_NAMESPACE = "demo"
    LOGGER_NAME = "demo_logger"

    def _project_label_for(self, output_doc):
        return output_doc.get("project")


class _FailsOnTheNthUpload(StubClient):
    """Answers the first ``n - 1`` uploads, then raises on the ``n``-th."""

    def __init__(self, n, exc):
        super().__init__(response=StubResponse({"warnings": []}, headers={"X-Request-ID": "req-ok"}))
        self._n = n
        self._exc = exc

    def request_json(self, method, path, **kwargs):
        if path != "/readyz" and len(upload_calls(self)) + 1 == self._n:
            self.calls.append(dict(method=method, path=path, **kwargs))
            raise self._exc
        return super().request_json(method, path, **kwargs)


def _config(tmp_path, **kw):
    kw.setdefault("preflight", False)
    kw.setdefault("artifacts", TCKDBArtifactConfig(upload=True, kinds=("output_log", "input")))
    return TCKDBConfig(
        enabled=True, base_url="https://example.invalid", payload_dir=str(tmp_path / "out"),
        api_key_env="DEMO_TCKDB_KEY", **kw,
    )


def _adapter(tmp_path, client=None, **kw):
    client = client or StubClient(
        response=StubResponse({"warnings": [{"code": "demo_warning"}]}, headers={"X-Request-ID": "req-1"}))
    adapter = DemoAdapter(
        _config(tmp_path, **kw), project_directory=tmp_path, client_factory=lambda *a, **k: client)
    return adapter, client


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("DEMO_TCKDB_KEY", "k")


@pytest.fixture
def files(tmp_path):
    log = tmp_path / "run.log"
    log.write_bytes(b"output bytes")
    deck = tmp_path / "run.in"
    deck.write_bytes(b"input bytes")
    return log, deck


def _entry(kind, path):
    data = path.read_bytes()
    return {
        "kind": kind,
        "filename": path.name,
        "content_base64": base64.b64encode(data).decode("ascii"),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def _digest(adapter, calculation_ref, items):
    prepared = [
        adapter._prepare_artifact_upload(
            output_doc={}, species_label="CH4", calculation_id=None, calculation_ref=calculation_ref,
            kind=kind, file_path=path, artifact_cfg=adapter._config.artifacts)
        for kind, path in items
    ]
    return artifact_batch_digest(prepared)


def test_one_small_artifact_sends_this_request_and_records_this_sidecar(tmp_path, files):
    adapter, client = _adapter(tmp_path)
    # Computed first: preparing an item writes its sidecar, so it would reset a finished one.
    digest = _digest(adapter, "calc_1", [("output_log", files[0])])
    (outcome,) = adapter.submit_artifact_batch_for_calculation(
        species_label="CH4", calculation_ref="calc_1", calculation_id=7, artifacts=[("output_log", files[0])],
        output_doc={"project": "proj A"})
    key = f"demo:proj-A:CH4:artifact:calc_1-{digest}:artifact-batch"
    (call,) = upload_calls(client)
    assert call == {
        "method": "POST", "path": "/calculations/calc_1/artifacts", "authenticated": True,
        "json": {"artifacts": [_entry("output_log", files[0])]}, "idempotency_key": key,
    }
    sidecar = json.loads(outcome.sidecar_path.read_text())
    assert outcome.status == sidecar["status"] == "uploaded"
    assert outcome.idempotency_key == sidecar["idempotency_key"]
    assert sidecar["calculation_ref"] == "calc_1" and sidecar["calculation_id"] == 7
    assert sidecar["kind"] == "output_log" and sidecar["filename"] == "run.log"
    assert sidecar["sha256"] == _entry("output_log", files[0])["sha256"] and sidecar["bytes"] == 12
    assert sidecar["response_status_code"] == 201
    assert sidecar["idempotency_replayed"] is False
    assert sidecar["warnings"] == [{"code": "demo_warning"}] and outcome.warnings == [{"code": "demo_warning"}]
    assert sidecar["request_ids"] == [{"operation": "artifact_upload", "request_id": "req-1", "status_code": 201}]
    assert sidecar["last_error"] is None and sidecar["server_error_code"] is None
    assert sidecar["response_body"] == outcome.response
    # The recorded body is the batch summary: one entry per request, JSON-shaped.
    assert outcome.response == [{
        "calculation_id": None, "calculation_ref": "calc_1", "calculation_keys": [f"calc_1-{digest}"],
        "artifact_count": 1, "response": {"warnings": [{"code": "demo_warning"}]},
    }]


def test_several_artifacts_of_one_calculation_share_one_request_and_one_key(tmp_path, files):
    adapter, client = _adapter(tmp_path)
    items = [("output_log", files[0]), ("input", files[1])]
    digest = _digest(adapter, "calc_1", items)
    outcomes = adapter.submit_artifact_batch_for_calculation(
        species_label="CH4", calculation_ref="calc_1", artifacts=items)
    (call,) = upload_calls(client)
    assert call["path"] == "/calculations/calc_1/artifacts"
    assert call["json"] == {"artifacts": [_entry(kind, path) for kind, path in items]}
    assert call["idempotency_key"] == f"demo:unknown:CH4:artifact:calc_1-{digest}:artifact-batch"
    assert [o.status for o in outcomes] == ["uploaded", "uploaded"]
    # Both sidecars carry the one response and each its own item's key.
    sidecars = [json.loads(o.sidecar_path.read_text()) for o in outcomes]
    assert [s["kind"] for s in sidecars] == ["output_log", "input"]
    assert all(s["response_status_code"] == 201 and s["status"] == "uploaded" for s in sidecars)
    assert sidecars[0]["response_body"] == sidecars[1]["response_body"]
    assert sidecars[0]["idempotency_key"] != sidecars[1]["idempotency_key"]


def test_two_calculations_are_two_requests_in_first_seen_order(tmp_path, files):
    adapter, client = _adapter(tmp_path)
    prepared = [
        adapter._prepare_artifact_upload(
            output_doc={}, species_label="CH4", calculation_id=None, calculation_ref=ref,
            kind=kind, file_path=path, artifact_cfg=adapter._config.artifacts)
        for ref, kind, path in (("calc_b", "output_log", files[0]), ("calc_a", "input", files[1]),
                                ("calc_b", "input", files[1]))
    ]
    outcomes = adapter._upload_artifact_batch(prepared=prepared, idempotency_key_prefix="demo:p:CH4:artifact")
    assert [o.status for o in outcomes] == ["uploaded"] * 3
    calls = upload_calls(client)
    assert [c["path"] for c in calls] == ["/calculations/calc_b/artifacts", "/calculations/calc_a/artifacts"]
    assert [len(c["json"]["artifacts"]) for c in calls] == [2, 1]
    # Each key is the prefix, the group's first item's calculation_key and ``artifact-batch``.
    assert calls[0]["idempotency_key"] == f"demo:p:CH4:artifact:{prepared[0].calculation_key}:artifact-batch"
    assert calls[1]["idempotency_key"] == f"demo:p:CH4:artifact:{prepared[1].calculation_key}:artifact-batch"


def test_artifact_upload_false_sends_nothing_and_writes_no_sidecar(tmp_path, files):
    adapter, client = _adapter(tmp_path, artifacts=TCKDBArtifactConfig(upload=False))
    outcomes = adapter.submit_artifact_batch_for_calculation(
        species_label="CH4", calculation_ref="calc_1", artifacts=[("output_log", files[0]), ("input", files[1])])
    assert [o.status for o in outcomes] == ["skipped", "skipped"]
    assert all(o.skip_reason == "artifacts.upload is False" and o.sidecar_path is None for o in outcomes)
    assert client.calls == [] and not (tmp_path / "out").exists()


# --------------------------------------------------------------------------- the size split


@pytest.fixture
def sized(tmp_path):
    """Four files of 4, 4, 4 and 10 bytes, named a, b, c and big."""
    paths = {}
    for name, size in (("a", 4), ("b", 4), ("c", 4), ("big", 10)):
        path = tmp_path / f"{name}.log"
        path.write_bytes(name[0].encode() * size)
        paths[name] = path
    return paths


def _budget(monkeypatch, size):
    """The client's inline budget, which the core reads when it cuts a plan into requests."""
    monkeypatch.setattr(client_artifact, "INLINE_TOTAL_BUDGET_BYTES", size)


PREFIX = "demo:p:CH4:artifact"


def _plan(adapter, names, sized, ref="calc_1"):
    """Prepared items for ``names``; one species label each, as a sidecar is named by species, calculation and kind."""
    return [
        adapter._prepare_artifact_upload(
            output_doc={}, species_label=f"CH4{name}", calculation_id=None, calculation_ref=ref,
            kind="output_log", file_path=sized[name], artifact_cfg=adapter._config.artifacts)
        for name in names
    ]


def _filenames(call):
    return [a["filename"] for a in call["json"]["artifacts"]]


def _requests(client):
    return [(_filenames(c), c["idempotency_key"]) for c in upload_calls(client)]


def _keys(prepared, *groups):
    """The keys a plan cut into ``groups`` (item indices) gets: first request plain, later ones numbered."""
    return [
        f"{PREFIX}:{prepared[group[0]].calculation_key}:" + ("artifact-batch" if n == 0 else f"artifact-batch-{n + 1}")
        for n, group in enumerate(groups)
    ]


@pytest.mark.parametrize("budget", [100, 12])  # under the budget, and exactly at it
def test_files_up_to_the_budget_are_one_request(tmp_path, sized, monkeypatch, budget):
    _budget(monkeypatch, budget)
    adapter, client = _adapter(tmp_path)
    prepared = _plan(adapter, "abc", sized)
    adapter._upload_artifact_batch(prepared=prepared, idempotency_key_prefix=PREFIX)
    assert _requests(client) == [(["a.log", "b.log", "c.log"], _keys(prepared, [0, 1, 2])[0])]


def test_one_byte_over_the_budget_is_two_requests_keyed_batch_and_batch_2(tmp_path, sized, monkeypatch):
    _budget(monkeypatch, 11)
    adapter, client = _adapter(tmp_path)
    prepared = _plan(adapter, "abc", sized)
    outcomes = adapter._upload_artifact_batch(prepared=prepared, idempotency_key_prefix=PREFIX)
    assert _requests(client) == [
        (["a.log", "b.log"], _keys(prepared, [0, 1], [2])[0]),
        (["c.log"], _keys(prepared, [0, 1], [2])[1]),
    ]
    assert upload_calls(client)[1]["idempotency_key"].endswith(":artifact-batch-2")
    assert {c["path"] for c in upload_calls(client)} == {"/calculations/calc_1/artifacts"}
    assert [o.status for o in outcomes] == ["uploaded"] * 3
    # Each sidecar records the response to its own request.
    sidecars = [json.loads(o.sidecar_path.read_text()) for o in outcomes]
    assert [s["response_body"][0]["artifact_count"] for s in sidecars] == [2, 2, 1]


def test_the_public_method_cuts_a_calculation_the_same_way(tmp_path, files, monkeypatch):
    _budget(monkeypatch, 11)  # run.log is 12 bytes, run.in 11
    adapter, client = _adapter(tmp_path)
    digest = _digest(adapter, "calc_1", [("output_log", files[0]), ("input", files[1])])
    outcomes = adapter.submit_artifact_batch_for_calculation(
        species_label="CH4", calculation_ref="calc_1", artifacts=[("output_log", files[0]), ("input", files[1])])
    first, second = upload_calls(client)
    assert _filenames(first) == ["run.log"] and _filenames(second) == ["run.in"]
    assert first["idempotency_key"] == f"demo:unknown:CH4:artifact:calc_1-{digest}:artifact-batch"
    assert second["idempotency_key"].startswith("demo:unknown:CH4:artifact:input-")
    assert second["idempotency_key"].endswith(":artifact-batch-2")
    assert [o.status for o in outcomes] == ["uploaded", "uploaded"]


def test_three_requests_are_numbered_in_order(tmp_path, sized, monkeypatch):
    _budget(monkeypatch, 4)
    adapter, client = _adapter(tmp_path)
    prepared = _plan(adapter, "abc", sized)
    adapter._upload_artifact_batch(prepared=prepared, idempotency_key_prefix=PREFIX)
    assert _requests(client) == list(zip(
        [["a.log"], ["b.log"], ["c.log"]], _keys(prepared, [0], [1], [2])))
    assert [k.rsplit(":", 1)[1] for _, k in _requests(client)] == [
        "artifact-batch", "artifact-batch-2", "artifact-batch-3"]


def test_a_file_over_the_budget_by_itself_goes_in_its_own_request(tmp_path, sized, monkeypatch):
    _budget(monkeypatch, 8)
    adapter, client = _adapter(tmp_path)
    adapter._upload_artifact_batch(prepared=_plan(adapter, ["a", "big", "c"], sized), idempotency_key_prefix=PREFIX)
    assert [names for names, _ in _requests(client)] == [["a.log"], ["big.log"], ["c.log"]]
    # First in the plan it still goes out, alone, and the next file starts a new request.
    adapter, client = _adapter(tmp_path / "second")
    adapter._upload_artifact_batch(prepared=_plan(adapter, ["big", "a", "b"], sized), idempotency_key_prefix=PREFIX)
    assert [names for names, _ in _requests(client)] == [["big.log"], ["a.log", "b.log"]]


def test_a_failure_on_request_two_of_three_records_uploaded_failed_and_not_sent(tmp_path, sized, monkeypatch):
    _budget(monkeypatch, 4)
    client = _FailsOnTheNthUpload(2, RuntimeError("second request refused"))
    adapter, _ = _adapter(tmp_path, client=client)
    outcomes = adapter._upload_artifact_batch(prepared=_plan(adapter, "abc", sized), idempotency_key_prefix=PREFIX)
    assert len(upload_calls(client)) == 2  # the third is never sent
    assert [o.status for o in outcomes] == ["uploaded", "failed", "failed"]
    first, second, third = (json.loads(o.sidecar_path.read_text()) for o in outcomes)
    assert first["status"] == "uploaded" and first["last_error"] is None
    assert first["response_status_code"] == 201 and first["response_body"][0]["artifact_count"] == 1
    assert second["status"] == "failed" and second["last_error"] == "second request refused"
    assert third["status"] == "failed"
    assert third["last_error"] == (
        "not sent: an earlier request for the same calculation failed: second request refused")
    assert third["response_status_code"] is None and third["server_error_code"] is None
    assert outcomes[2].error == third["last_error"]


def test_a_strict_failure_on_a_later_request_raises_after_every_sidecar_is_written(tmp_path, sized, monkeypatch):
    _budget(monkeypatch, 4)
    client = _FailsOnTheNthUpload(2, RuntimeError("second request refused"))
    adapter, _ = _adapter(tmp_path, client=client, strict=True)
    prepared = _plan(adapter, "abc", sized)
    with pytest.raises(RuntimeError, match="second request refused"):
        adapter._upload_artifact_batch(prepared=prepared, idempotency_key_prefix=PREFIX)
    assert [json.loads(p.written.sidecar_path.read_text())["status"] for p in prepared] == [
        "uploaded", "failed", "failed"]


def test_a_failure_in_one_calculation_leaves_the_calculations_before_it_uploaded(tmp_path, files):
    """Requests are one sequence: the ones the server stored stay recorded as stored."""
    client = _FailsOnTheNthUpload(2, RuntimeError("second request refused"))
    adapter, _ = _adapter(tmp_path, client=client)
    prepared = [
        adapter._prepare_artifact_upload(
            output_doc={}, species_label="CH4", calculation_id=None, calculation_ref=ref,
            kind=kind, file_path=path, artifact_cfg=adapter._config.artifacts)
        for ref, kind, path in (("calc_a", "output_log", files[0]), ("calc_b", "input", files[1]),
                                ("calc_c", "input", files[1]))
    ]
    outcomes = adapter._upload_artifact_batch(prepared=prepared, idempotency_key_prefix="demo:p:CH4:artifact")
    assert [c["path"] for c in upload_calls(client)] == [
        "/calculations/calc_a/artifacts", "/calculations/calc_b/artifacts"]
    assert [o.status for o in outcomes] == ["uploaded", "failed", "failed"]
    assert outcomes[2].error == (
        "not sent: an earlier artifact request, for calculation calc_b, failed: second request refused")


def test_a_replayed_later_request_keeps_its_own_key(tmp_path, sized, monkeypatch):
    _budget(monkeypatch, 4)
    seen = []

    def handler(request):
        seen.append(request)
        # The second POST (the second request's first attempt) is a transient 503.
        if len(seen) == 2:
            return httpx.Response(503, json={"code": "object_store_unavailable", "detail": "no"})
        return httpx.Response(201, json={"warnings": []})

    monkeypatch.setattr(uploader, "TCKDBClient", functools.partial(TCKDBClient, transport=httpx.MockTransport(handler)))
    adapter = DemoAdapter(_config(tmp_path, validate_before_upload=False), project_directory=tmp_path)
    adapter._sleep_between_retries = lambda _seconds: None
    outcomes = adapter._upload_artifact_batch(prepared=_plan(adapter, "ab", sized), idempotency_key_prefix=PREFIX)
    assert [o.status for o in outcomes] == ["uploaded", "uploaded"]
    keys = [r.headers["Idempotency-Key"] for r in seen]
    assert len(keys) == 3 and keys[1] == keys[2] and keys[0] != keys[1]
    assert keys[0].endswith(":artifact-batch") and keys[1].endswith(":artifact-batch-2")


def test_the_validation_hook_checks_every_item_before_the_first_request(tmp_path, sized, monkeypatch):
    _budget(monkeypatch, 4)
    bad = tmp_path / "bad.exe"
    bad.write_bytes(b"bbbb")
    adapter, client = _adapter(tmp_path)
    sized["bad"] = bad
    outcomes = adapter._upload_artifact_batch(prepared=_plan(adapter, ["a", "b", "bad"], sized), idempotency_key_prefix=PREFIX)
    assert client.calls == []
    assert [o.status for o in outcomes] == ["failed"] * 3


# --------------------------------------------------------------------------- parity with the client


def _recording_client(seen):
    def handler(request):
        if request.url.path.endswith("/readyz"):
            return httpx.Response(200, json={"status": "ready"})
        seen.append((request.url.path, request.headers.get("Idempotency-Key"), json.loads(request.content)))
        return httpx.Response(201, json={"warnings": []})
    return TCKDBClient("http://tckdb.test/api/v1", api_key="k", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("budget, names", [
    (100, "abc"),          # under
    (12, "abc"),           # exactly at the budget
    (11, "abc"),           # one byte over: two requests
    (4, "abc"),            # three requests
    (8, ["a", "big", "c"]),   # a file over the budget alone
    (8, ["big", "a", "b"]),   # ... first in the plan
])
def test_the_core_sends_what_the_client_sends_for_the_same_plan(tmp_path, sized, monkeypatch, budget, names):
    """Drift guard: ``TCKDBClient.upload_artifacts(batch_by_calculation=True)`` is the reference."""
    _budget(monkeypatch, budget)
    adapter, _ = _adapter(tmp_path)
    prepared = _plan(adapter, names, sized, ref="calc_7")
    plan = [
        PlannedArtifactUpload(
            calculation_key=p.calculation_key, calculation_id=7, path=p.path, kind=p.kind, label=p.label,
            sha256=p.sha256, bytes=p.bytes, calculation_ref="calc_7")
        for p in prepared
    ]
    prefix = "demo:p:CH4:artifact"
    from_client = []
    _recording_client(from_client).upload_artifacts(plan, idempotency_key_prefix=prefix, batch_by_calculation=True)
    from_core = []
    adapter._post_artifact_batches(_recording_client(from_core), prepared, idempotency_key_prefix=prefix)
    assert from_core == from_client
    assert len(from_core) == {100: 1, 12: 1, 11: 2, 4: 3}.get(budget, len(from_core))


# --------------------------------------------------------------------------- files that go wrong


def test_a_file_that_has_gone_missing_fails_the_request_it_would_join_and_the_rest_are_not_sent(
        tmp_path, sized, monkeypatch):
    """A file is sized when its turn comes; requests closed before it are out, the one it would join is not."""
    _budget(monkeypatch, 8)
    for name in "de":
        sized[name] = tmp_path / f"{name}.log"
        sized[name].write_bytes(name.encode() * 4)
    adapter, client = _adapter(tmp_path)
    prepared = _plan(adapter, "abcde", sized)
    sized["d"].unlink()
    outcomes = adapter._upload_artifact_batch(prepared=prepared, idempotency_key_prefix=PREFIX)
    # a and b closed as a request before d was sized; c waits for d, so c and d fail together.
    assert _requests(client) == [(["a.log", "b.log"], _keys(prepared, [0, 1])[0])]
    assert [o.status for o in outcomes] == ["uploaded", "uploaded", "failed", "failed", "failed"]
    sidecars = [json.loads(o.sidecar_path.read_text()) for o in outcomes]
    assert [s["status"] for s in sidecars[:2]] == ["uploaded", "uploaded"]
    assert all("d.log" in s["last_error"] and not s["last_error"].startswith("not sent") for s in sidecars[2:4])
    assert sidecars[4]["last_error"].startswith("not sent: an earlier request for the same calculation failed: ")


def test_a_missing_file_in_the_first_request_sends_nothing(tmp_path, sized, monkeypatch):
    adapter, client = _adapter(tmp_path)
    prepared = _plan(adapter, "abc", sized)
    sized["a"].unlink()
    outcomes = adapter._upload_artifact_batch(prepared=prepared, idempotency_key_prefix=PREFIX)
    assert client.calls == []
    assert [o.status for o in outcomes] == ["failed"] * 3
    assert "a.log" in outcomes[0].error and outcomes[1].error.startswith("not sent: ")


def test_a_file_that_cannot_be_read_fails_its_own_request(tmp_path, sized, monkeypatch):
    _budget(monkeypatch, 4)
    adapter, client = _adapter(tmp_path)
    prepared = _plan(adapter, "abc", sized)
    real = type(sized["b"]).read_bytes

    def read_bytes(self):
        if self.name == "b.log":
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(type(sized["b"]), "read_bytes", read_bytes)
    outcomes = adapter._upload_artifact_batch(prepared=prepared, idempotency_key_prefix=PREFIX)
    assert [names for names, _ in _requests(client)] == [["a.log"]]
    assert [o.status for o in outcomes] == ["uploaded", "failed", "failed"]
    assert "Permission denied" in outcomes[1].error and outcomes[2].error.startswith("not sent: ")


def test_a_missing_file_of_a_later_calculation_leaves_the_earlier_one_uploaded(tmp_path, files):
    adapter, client = _adapter(tmp_path)
    prepared = [
        adapter._prepare_artifact_upload(
            output_doc={}, species_label="CH4", calculation_id=None, calculation_ref=ref,
            kind=kind, file_path=path, artifact_cfg=adapter._config.artifacts)
        for ref, kind, path in (("calc_a", "output_log", files[0]), ("calc_b", "input", files[1]))
    ]
    files[1].unlink()
    outcomes = adapter._upload_artifact_batch(prepared=prepared, idempotency_key_prefix=PREFIX)
    assert [c["path"] for c in upload_calls(client)] == ["/calculations/calc_a/artifacts"]
    assert [o.status for o in outcomes] == ["uploaded", "failed"]


def test_the_server_error_code_lands_on_the_failed_request_only(tmp_path, sized, monkeypatch):
    from tckdb_client.errors import TCKDBHTTPError

    _budget(monkeypatch, 4)
    refusal = TCKDBHTTPError(
        "HTTP 409", status_code=409, code="idempotency_conflict",
        response_json={"code": "idempotency_conflict", "detail": "same key, different body"})
    adapter, client = _adapter(tmp_path, client=_FailsOnTheNthUpload(2, refusal))
    outcomes = adapter._upload_artifact_batch(prepared=_plan(adapter, "abc", sized), idempotency_key_prefix=PREFIX)
    assert [o.status for o in outcomes] == ["uploaded", "failed", "failed"]
    assert [o.server_error_code for o in outcomes] == [None, "idempotency_conflict", None]
    sidecars = [json.loads(o.sidecar_path.read_text()) for o in outcomes]
    assert [s["server_error_code"] for s in sidecars] == [None, "idempotency_conflict", None]
    assert sidecars[1]["response_status_code"] == 409 and sidecars[2]["response_status_code"] is None


def test_the_split_uses_the_size_on_disk_when_the_request_is_made_not_the_prepared_size(tmp_path, sized, monkeypatch):
    _budget(monkeypatch, 8)
    adapter, client = _adapter(tmp_path)
    prepared = _plan(adapter, "ab", sized)
    assert sum(item.bytes for item in prepared) == 8  # at the budget: one request, by the prepared sizes
    sized["b"].write_bytes(b"b" * 6)  # grown after it was prepared: 10 bytes on disk
    adapter._upload_artifact_batch(prepared=prepared, idempotency_key_prefix=PREFIX)
    assert [names for names, _ in _requests(client)] == [["a.log"], ["b.log"]]
