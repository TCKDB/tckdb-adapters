"""The retry policy the base uploader hands the client, and the server error code a failure records.

The retry logic itself belongs to ``tckdb-client``; these tests cover what this package decides:
the policy built from the config, who may override it, and what is written down when an upload
fails with a coded response. Nothing here touches a network or sleeps.
"""

import functools
import json
import math
from unittest import mock

import httpx
import pytest

from tckdb_client.errors import TCKDBConnectionError, TCKDBHTTPError
from tckdb_client.retry import RetryPolicy

from tckdb_core import uploader
from tckdb_core.config import MAX_RETRY_ATTEMPTS, TCKDBArtifactConfig, TCKDBConfig
from tckdb_core.constants import PREFLIGHT_MAX_ATTEMPTS
from tckdb_core.outcomes import TCKDBReadinessError
from tckdb_core.testing.stubs import StubClient, StubResponse
from tckdb_core.uploader import TCKDBUploaderBase


class DemoAdapter(TCKDBUploaderBase):
    PRODUCER_TAG = "demo"
    PRODUCER_NAME = "Demo"
    IDEMPOTENCY_NAMESPACE = "demo"

    def _project_label_for(self, output_doc):
        return output_doc.get("project")


def _config(tmp_path, **kw):
    kw.setdefault("preflight", False)
    kw.setdefault("validate_before_upload", False)
    kw.setdefault("artifacts", TCKDBArtifactConfig(upload=True, kinds=("output_log",)))
    return TCKDBConfig(
        enabled=True, base_url="http://tckdb.test/api/v1", payload_dir=str(tmp_path / "out"),
        api_key_env="DEMO_TCKDB_KEY", **kw,
    )


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("DEMO_TCKDB_KEY", "k")


def _written(adapter, key="demo-key-0123456789"):
    return adapter._writer.write(
        label="demo", payload={"a": 1}, endpoint="/uploads/conformers", idempotency_key=key,
    )


def _http_error(status, code, **kw):
    body = {"code": code, "detail": "refused"}
    return TCKDBHTTPError(
        f"HTTP {status}", status_code=status, code=code, response_json=body, **kw,
    )


# --------------------------------------------------------------------------- the config


def test_the_defaults_are_three_attempts_and_a_one_second_backoff():
    config = TCKDBConfig()
    assert (config.retry_max_attempts, config.retry_backoff_seconds) == (3, 1.0)


@pytest.mark.parametrize("attempts", [0, -1, 11, 100, 2.5, "3", None, True])
def test_a_bad_attempt_count_is_rejected(attempts):
    with pytest.raises(ValueError, match="retry_max_attempts"):
        TCKDBConfig(retry_max_attempts=attempts)


@pytest.mark.parametrize("backoff", [-0.1, math.inf, math.nan, "1", None, True])
def test_a_bad_backoff_is_rejected(backoff):
    with pytest.raises(ValueError, match="retry_backoff_seconds"):
        TCKDBConfig(retry_backoff_seconds=backoff)


def test_the_attempt_count_range_is_one_to_ten():
    assert MAX_RETRY_ATTEMPTS == 10
    assert TCKDBConfig(retry_max_attempts=1).retry_max_attempts == 1
    assert TCKDBConfig(retry_max_attempts=MAX_RETRY_ATTEMPTS).retry_max_attempts == 10


def test_zero_backoff_and_an_integer_backoff_are_accepted():
    assert TCKDBConfig(retry_backoff_seconds=0).retry_backoff_seconds == 0
    assert TCKDBConfig(retry_backoff_seconds=2).retry_backoff_seconds == 2


# --------------------------------------------------------------------------- the policy


def test_the_policy_is_built_from_the_config_and_passed_to_the_client(tmp_path):
    adapter = DemoAdapter(_config(tmp_path, retry_max_attempts=5, retry_backoff_seconds=0.25))
    with mock.patch.object(uploader, "TCKDBClient") as client_class:
        adapter._make_client("secret")
    (call,) = client_class.call_args_list
    assert call.args == ("http://tckdb.test/api/v1",)
    assert call.kwargs["api_key"] == "secret"
    policy = call.kwargs["retry"]
    assert isinstance(policy, RetryPolicy)
    assert (policy.max_attempts, policy.backoff_base) == (5, 0.25)


def test_every_other_knob_stays_the_clients_default(tmp_path):
    adapter = DemoAdapter(_config(tmp_path))
    policy = adapter._retry_policy()
    default = RetryPolicy()
    for name in ("max_backoff", "jitter", "retry_status_codes", "non_retryable_codes",
                 "respect_retry_after", "max_retry_after"):
        assert getattr(policy, name) == getattr(default, name), name
    assert (policy.max_attempts, policy.backoff_base) == (3, 1.0)


def test_one_attempt_sends_no_policy(tmp_path):
    adapter = DemoAdapter(_config(tmp_path, retry_max_attempts=1))
    assert adapter._retry_policy() is None
    with mock.patch.object(uploader, "TCKDBClient") as client_class:
        adapter._make_client("secret")
    assert client_class.call_args.kwargs["retry"] is None


def test_the_real_client_carries_the_policy(tmp_path):
    client = DemoAdapter(_config(tmp_path))._make_client("secret")
    try:
        assert isinstance(client._retry, RetryPolicy) and client._retry.max_attempts == 3
    finally:
        client.close()


def test_a_client_factory_keeps_full_control(tmp_path):
    seen = []
    sentinel = object()

    def factory(config, api_key):
        seen.append((config, api_key))
        return sentinel

    config = _config(tmp_path, retry_max_attempts=7)
    adapter = DemoAdapter(config, client_factory=factory)
    with mock.patch.object(uploader, "TCKDBClient") as client_class:
        assert adapter._make_client("secret") is sentinel
    client_class.assert_not_called()
    assert seen == [(config, "secret")]


def test_the_policys_sleep_goes_through_a_method_a_test_can_rebind(tmp_path):
    adapter = DemoAdapter(_config(tmp_path))
    waited = []
    adapter._sleep_between_retries = waited.append
    adapter._retry_policy().sleep(1.5)
    assert waited == [1.5]


def test_the_default_retry_sleep_is_the_module_function(tmp_path, monkeypatch):
    waited = []
    monkeypatch.setattr(uploader, "retry_sleep", waited.append)
    DemoAdapter(_config(tmp_path))._sleep_between_retries(0.25)
    assert waited == [0.25]


# --------------------------------------------------------------------------- the failure code


def test_a_coded_failure_is_recorded_on_the_sidecar_and_the_outcome(tmp_path):
    client = StubClient(raise_exc=_http_error(422, "hessian_method_contradicts_input_deck"))
    adapter = DemoAdapter(_config(tmp_path), client_factory=lambda *a, **k: client)
    written = _written(adapter)
    outcome = adapter._upload(written, {"a": 1})
    sidecar = json.loads(written.sidecar_path.read_text())
    assert outcome.status == sidecar["status"] == "failed"
    assert outcome.server_error_code == "hessian_method_contradicts_input_deck"
    assert sidecar["server_error_code"] == "hessian_method_contradicts_input_deck"
    assert sidecar["response_status_code"] == 422
    assert sidecar["last_error"].startswith("HTTP 422: ")


@pytest.mark.parametrize("raised", [
    TCKDBConnectionError("Network error: boom"),
    TCKDBHTTPError("HTTP 500", status_code=500, response_text="oops"),
    TCKDBHTTPError("HTTP 500", status_code=500, code=""),
    ValueError("local"),
])
def test_a_failure_without_a_code_leaves_it_null(tmp_path, raised):
    adapter = DemoAdapter(
        _config(tmp_path), client_factory=lambda *a, **k: StubClient(raise_exc=raised))
    written = _written(adapter)
    outcome = adapter._upload(written, {"a": 1})
    assert outcome.status == "failed"
    assert outcome.server_error_code is None
    assert json.loads(written.sidecar_path.read_text())["server_error_code"] is None


def test_a_readiness_failure_has_no_server_code(tmp_path):
    adapter = DemoAdapter(
        _config(tmp_path, preflight=True),
        client_factory=lambda *a, **k: StubClient(readyz_response=TCKDBConnectionError("down")))
    adapter._sleep_between_probes = lambda _seconds: None
    written = _written(adapter)
    outcome = adapter._upload(written, {"a": 1})
    assert outcome.status == "failed"
    assert outcome.server_error_code is None


def test_a_coded_readyz_503_is_not_recorded_as_the_uploads_server_code(tmp_path):
    """The field is the code of the failed upload request; the probe never made one."""
    error = _http_error(503, "database_unavailable")
    adapter = DemoAdapter(
        _config(tmp_path, preflight=True),
        client_factory=lambda *a, **k: StubClient(readyz_response=error))
    adapter._sleep_between_probes = lambda _seconds: None
    written = _written(adapter)
    outcome = adapter._upload(written, {"a": 1})
    sidecar = json.loads(written.sidecar_path.read_text())
    assert outcome.status == sidecar["status"] == "failed"
    assert outcome.server_error_code is None and sidecar["server_error_code"] is None
    assert sidecar["preflight"]["ready"] is False and sidecar["response_status_code"] == 503


def test_a_success_leaves_the_code_null(tmp_path):
    client = StubClient(response=StubResponse({"ok": True}))
    adapter = DemoAdapter(_config(tmp_path), client_factory=lambda *a, **k: client)
    written = _written(adapter)
    outcome = adapter._upload(written, {"a": 1})
    assert outcome.status == "uploaded" and outcome.server_error_code is None
    assert json.loads(written.sidecar_path.read_text())["server_error_code"] is None


def test_a_coded_failure_in_strict_mode_is_still_recorded_before_it_raises(tmp_path):
    error = _http_error(503, "object_store_unavailable")
    adapter = DemoAdapter(
        _config(tmp_path, strict=True),
        client_factory=lambda *a, **k: StubClient(raise_exc=error))
    written = _written(adapter)
    with pytest.raises(TCKDBHTTPError):
        adapter._upload(written, {"a": 1})
    assert json.loads(written.sidecar_path.read_text())["server_error_code"] == "object_store_unavailable"


@pytest.fixture
def artifact_file(tmp_path):
    path = tmp_path / "run.log"
    path.write_bytes(b"output bytes")
    return path


def _submit_artifacts(adapter, path):
    return adapter.submit_artifact_batch_for_calculation(
        species_label="CH4", calculation_ref="calc_1", artifacts=[("output_log", path)])


def test_a_coded_artifact_failure_is_recorded_on_the_sidecar_and_the_outcome(tmp_path, artifact_file):
    client = StubClient(raise_exc=_http_error(409, "idempotency_conflict"))
    adapter = DemoAdapter(
        _config(tmp_path), project_directory=tmp_path, client_factory=lambda *a, **k: client)
    (outcome,) = _submit_artifacts(adapter, artifact_file)
    sidecar = json.loads(outcome.sidecar_path.read_text())
    assert outcome.status == sidecar["status"] == "failed"
    assert outcome.server_error_code == sidecar["server_error_code"] == "idempotency_conflict"


def test_an_artifact_failure_without_a_code_leaves_it_null(tmp_path, artifact_file):
    client = StubClient(raise_exc=TCKDBConnectionError("Network error: boom"))
    adapter = DemoAdapter(
        _config(tmp_path), project_directory=tmp_path, client_factory=lambda *a, **k: client)
    (outcome,) = _submit_artifacts(adapter, artifact_file)
    assert outcome.status == "failed" and outcome.server_error_code is None
    assert json.loads(outcome.sidecar_path.read_text())["server_error_code"] is None


def test_an_uploaded_artifact_leaves_the_code_null(tmp_path, artifact_file):
    client = StubClient(response=StubResponse({"warnings": []}))
    adapter = DemoAdapter(
        _config(tmp_path), project_directory=tmp_path, client_factory=lambda *a, **k: client)
    (outcome,) = _submit_artifacts(adapter, artifact_file)
    assert outcome.status == "uploaded" and outcome.server_error_code is None
    assert json.loads(outcome.sidecar_path.read_text())["server_error_code"] is None


# --------------------------------------------------------------------------- end to end


def _mock_client_class(handler):
    """``TCKDBClient`` bound to an in-memory transport, as ``_make_client`` constructs it."""
    from tckdb_client import TCKDBClient

    return functools.partial(TCKDBClient, transport=httpx.MockTransport(handler))


def _scripted(statuses, seen):
    """A handler answering ``statuses`` in turn (the last repeats); a 503 carries a coded body."""
    def handler(request):
        seen.append(request)
        status = statuses[min(len(seen), len(statuses)) - 1]
        if status == 503:
            return httpx.Response(
                503, json={"code": "object_store_unavailable", "detail": "store did not answer"})
        return httpx.Response(status, json={"warnings": []})
    return handler


def test_a_transient_503_is_replayed_with_the_same_idempotency_key(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(uploader, "TCKDBClient", _mock_client_class(_scripted([503, 201], seen)))
    adapter = DemoAdapter(_config(tmp_path))
    waited = []
    adapter._sleep_between_retries = waited.append
    written = _written(adapter)
    outcome = adapter._upload(written, {"a": 1})
    assert outcome.status == "uploaded" and outcome.server_error_code is None
    assert len(seen) == 2 and len(waited) == 1
    keys = [request.headers["Idempotency-Key"] for request in seen]
    assert keys == [written.sidecar.idempotency_key] * 2
    assert json.loads(written.sidecar_path.read_text())["server_error_code"] is None


def test_retries_that_run_out_record_the_last_servers_code(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(uploader, "TCKDBClient", _mock_client_class(_scripted([503], seen)))
    adapter = DemoAdapter(_config(tmp_path))
    waited = []
    adapter._sleep_between_retries = waited.append
    written = _written(adapter)
    outcome = adapter._upload(written, {"a": 1})
    assert len(seen) == 3 and len(waited) == 2
    assert outcome.status == "failed"
    assert outcome.server_error_code == "object_store_unavailable"
    assert json.loads(written.sidecar_path.read_text())["response_status_code"] == 503


def test_one_attempt_posts_once(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(uploader, "TCKDBClient", _mock_client_class(_scripted([503, 201], seen)))
    adapter = DemoAdapter(_config(tmp_path, retry_max_attempts=1))
    adapter._sleep_between_retries = lambda _seconds: pytest.fail("no wait expected")
    outcome = adapter._upload(_written(adapter), {"a": 1})
    assert len(seen) == 1
    assert outcome.status == "failed" and outcome.server_error_code == "object_store_unavailable"


def test_a_refusal_is_not_replayed_and_its_code_is_recorded(tmp_path, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(422, json={"code": "unit_mismatch", "detail": "bad"})

    monkeypatch.setattr(uploader, "TCKDBClient", _mock_client_class(handler))
    adapter = DemoAdapter(_config(tmp_path))
    adapter._sleep_between_retries = lambda _seconds: pytest.fail("no wait expected")
    outcome = adapter._upload(_written(adapter), {"a": 1})
    assert len(seen) == 1
    assert outcome.status == "failed" and outcome.server_error_code == "unit_mismatch"


def test_a_replayed_artifact_batch_keeps_its_idempotency_key(tmp_path, artifact_file, monkeypatch):
    seen = []
    monkeypatch.setattr(uploader, "TCKDBClient", _mock_client_class(_scripted([503, 201], seen)))
    adapter = DemoAdapter(_config(tmp_path), project_directory=tmp_path)
    adapter._sleep_between_retries = lambda _seconds: None
    (outcome,) = _submit_artifacts(adapter, artifact_file)
    assert outcome.status == "uploaded"
    assert len(seen) == 2
    assert seen[0].headers["Idempotency-Key"] == seen[1].headers["Idempotency-Key"]


def _probe_handler(seen, readyz_statuses):
    """``/readyz`` answers ``readyz_statuses`` in turn (the last repeats); uploads answer 503, then 201."""
    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/readyz"):
            n = len([r for r in seen if r.url.path.endswith("/readyz")])
            status = readyz_statuses[min(n, len(readyz_statuses)) - 1]
            if status == 200:
                return httpx.Response(200, json={"status": "ready"})
            return httpx.Response(status, json={"code": "database_unavailable"})
        n = len([r for r in seen if not r.url.path.endswith("/readyz")])
        if n == 1:
            return httpx.Response(503, json={"code": "object_store_unavailable"})
        return httpx.Response(201, json={"warnings": []})
    return handler


def test_the_probe_is_not_multiplied_by_the_policy_but_the_upload_is_still_replayed(
        tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        uploader, "TCKDBClient", _mock_client_class(_probe_handler(seen, [503, 200])))
    adapter = DemoAdapter(_config(tmp_path, preflight=True))
    probe_waits, retry_waits = [], []
    adapter._sleep_between_probes = probe_waits.append
    adapter._sleep_between_retries = retry_waits.append
    outcome = adapter._upload(_written(adapter), {"a": 1})
    assert outcome.status == "uploaded"
    readyz = [r for r in seen if r.url.path.endswith("/readyz")]
    posts = [r for r in seen if not r.url.path.endswith("/readyz")]
    # One request per probe attempt: the 503 is the probe loop's to retry, not the client's.
    assert len(readyz) == 2 and len(probe_waits) == 1
    # The upload's 503 is replayed by the policy client.
    assert len(posts) == 2 and len(retry_waits) == 1


def test_a_probe_that_never_gets_ready_makes_exactly_its_own_attempts(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        uploader, "TCKDBClient", _mock_client_class(_probe_handler(seen, [503])))
    adapter = DemoAdapter(_config(tmp_path, preflight=True))
    adapter._sleep_between_probes = lambda _seconds: None
    adapter._sleep_between_retries = lambda _seconds: pytest.fail("the client must not replay the probe")
    written = _written(adapter)
    outcome = adapter._upload(written, {"a": 1})
    assert outcome.status == "failed"
    assert len(seen) == PREFLIGHT_MAX_ATTEMPTS and all(r.url.path.endswith("/readyz") for r in seen)
    # The probe's coded 503 is not the upload's code.
    assert outcome.server_error_code is None
    assert json.loads(written.sidecar_path.read_text())["server_error_code"] is None


class _Recorder:
    """A ``TCKDBClient`` stand-in that records its construction and whether it was closed."""

    made = []

    def __init__(self, base_url, api_key=None, timeout=30.0, *, retry=None):
        self.retry, self.api_key, self.timeout, self.closed = retry, api_key, timeout, False
        self.calls = []
        _Recorder.made.append(self)

    def request_json(self, method, path, *, json=None, authenticated=True, idempotency_key=None):
        self.calls.append((method, path))
        if path == "/readyz":
            return StubResponse({"status": "ready"}, status_code=200)
        return StubResponse({"warnings": []})

    def close(self):
        self.closed = True


@pytest.fixture
def recorder(monkeypatch):
    _Recorder.made = []
    monkeypatch.setattr(uploader, "TCKDBClient", _Recorder)
    return _Recorder


def test_the_probe_client_has_no_policy_the_upload_client_has_and_both_are_closed(
        tmp_path, recorder):
    adapter = DemoAdapter(_config(tmp_path, preflight=True, retry_backoff_seconds=0.5))
    assert adapter._upload(_written(adapter), {"a": 1}).status == "uploaded"
    upload_client, probe_client = recorder.made
    assert isinstance(upload_client.retry, RetryPolicy) and probe_client.retry is None
    assert (probe_client.api_key, probe_client.timeout) == ("k", upload_client.timeout)
    assert probe_client.calls == [("GET", "/readyz")]
    assert [c[1] for c in upload_client.calls] == ["/uploads/conformers"]
    assert upload_client.closed and probe_client.closed


def test_the_probe_runs_once_per_instance_and_builds_its_client_only_then(tmp_path, recorder):
    adapter = DemoAdapter(_config(tmp_path, preflight=True))
    adapter._upload(_written(adapter, "demo-key-0000000001"), {"a": 1})
    adapter._upload(_written(adapter, "demo-key-0000000002"), {"a": 1})
    probes = [c for c in recorder.made if c.calls and c.calls[0][1] == "/readyz"]
    assert len(probes) == 1 and len(recorder.made) == 3  # two upload clients, one probe client


def test_the_api_key_is_resolved_once_per_upload_with_the_probe_on(tmp_path, recorder):
    adapter = DemoAdapter(_config(tmp_path, preflight=True))
    with mock.patch.object(
            TCKDBConfig, "resolve_api_key", autospec=True, return_value="resolved") as resolver:
        assert adapter._upload(_written(adapter), {"a": 1}).status == "uploaded"
    assert resolver.call_count == 1
    upload_client, probe_client = recorder.made
    assert probe_client.api_key == upload_client.api_key == "resolved"


def test_a_subclass_that_overrides_make_client_does_the_probe_with_its_own_client(
        tmp_path, recorder):
    own = StubClient(response=StubResponse({"warnings": []}))

    class Custom(DemoAdapter):
        def _make_client(self, api_key):
            return own

    adapter = Custom(_config(tmp_path, preflight=True))
    assert adapter._upload(_written(adapter), {"a": 1}).status == "uploaded"
    assert recorder.made == []
    assert [c["path"] for c in own.calls] == ["/readyz", "/uploads/conformers"]


def test_one_attempt_probes_through_the_one_client(tmp_path, recorder):
    adapter = DemoAdapter(_config(tmp_path, preflight=True, retry_max_attempts=1))
    assert adapter._upload(_written(adapter), {"a": 1}).status == "uploaded"
    (client,) = recorder.made
    assert client.retry is None and [c[1] for c in client.calls] == ["/readyz", "/uploads/conformers"]


def test_a_factory_client_does_the_probe_and_no_second_client_is_built(tmp_path, recorder):
    client = StubClient(response=StubResponse({"warnings": []}))
    adapter = DemoAdapter(_config(tmp_path, preflight=True), client_factory=lambda *a, **k: client)
    assert adapter._upload(_written(adapter), {"a": 1}).status == "uploaded"
    assert recorder.made == []
    assert [c["path"] for c in client.calls] == ["/readyz", "/uploads/conformers"]


@pytest.mark.parametrize("strict", [False, True])
def test_the_probe_client_is_closed_when_the_probe_fails(tmp_path, recorder, strict):
    def failing(self, method, path, **kw):
        self.calls.append((method, path))
        raise TCKDBConnectionError("down")

    with mock.patch.object(recorder, "request_json", failing):
        adapter = DemoAdapter(_config(tmp_path, preflight=True, strict=strict))
        adapter._sleep_between_probes = lambda _seconds: None
        written = _written(adapter)
        if strict:
            with pytest.raises(TCKDBReadinessError):
                adapter._upload(written, {"a": 1})
        else:
            assert adapter._upload(written, {"a": 1}).status == "failed"
    upload_client, probe_client = recorder.made
    assert len(probe_client.calls) == PREFLIGHT_MAX_ATTEMPTS and upload_client.calls == []
    assert upload_client.closed and probe_client.closed


def test_the_probe_client_is_closed_on_the_artifact_path(tmp_path, recorder, artifact_file):
    adapter = DemoAdapter(_config(tmp_path, preflight=True), project_directory=tmp_path)
    (outcome,) = _submit_artifacts(adapter, artifact_file)
    assert outcome.status == "uploaded"
    upload_client, probe_client = recorder.made
    assert probe_client.retry is None and probe_client.calls == [("GET", "/readyz")]
    assert upload_client.closed and probe_client.closed
