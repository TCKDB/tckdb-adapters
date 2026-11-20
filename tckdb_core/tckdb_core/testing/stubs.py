"""Stand-ins for ``tckdb_client.TCKDBClient`` in an adapter's unit tests.

Hand one to the adapter through ``client_factory`` so an upload runs the whole
pipeline (readiness probe, POST, sidecar bookkeeping) without a network::

    client = StubClient(response=StubResponse({"id": 1}))
    adapter = MyAdapter(config, client_factory=lambda cfg, key: client)
    ...
    assert [call["path"] for call in upload_calls(client)] == ["/uploads/conformers"]
"""

from __future__ import annotations

from typing import Any


class StubResponse:
    """What ``TCKDBClient.request_json`` returns: body, status, replay flag, headers."""

    def __init__(self, data: Any, status_code: int = 201, replayed: bool = False, headers=None):
        self.data = data
        self.status_code = status_code
        self.idempotency_replayed = replayed
        self.headers = headers or {}


class StubClient:
    """A client that records every call and answers with a fixed response.

    ``/readyz`` answers ``readyz_response`` (ready by default); every other path
    raises ``raise_exc`` when given, else returns ``response``. An exception
    instance passed as ``readyz_response`` is raised by the probe.
    """

    def __init__(self, *, response=None, raise_exc=None, readyz_response=None):
        self._response = response
        self._raise_exc = raise_exc
        self._readyz_response = readyz_response or StubResponse(
            {"status": "ready", "alembic_revision": "test-rev"},
            status_code=200,
            headers={"X-Request-ID": "req-readyz"},
        )
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def request_json(
        self,
        method,
        path,
        *,
        json=None,
        authenticated=True,
        idempotency_key=None,
    ):
        self.calls.append(
            dict(
                method=method,
                path=path,
                json=json,
                authenticated=authenticated,
                idempotency_key=idempotency_key,
            )
        )
        if path == "/readyz":
            if isinstance(self._readyz_response, BaseException):
                raise self._readyz_response
            return self._readyz_response
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response

    def close(self):
        self.closed = True


class SequencedReadyzClient(StubClient):
    """A stub whose ``/readyz`` answers are taken from a sequence.

    Each probe takes the next item of ``readyz_sequence``; the last item repeats
    once the sequence is used up. An exception instance is raised, anything else
    is returned. Other paths behave as in :class:`StubClient`. This drives
    "K failed probes, then ready" without real sleeps.
    """

    def __init__(self, *, readyz_sequence, response=None, raise_exc=None):
        super().__init__(response=response, raise_exc=raise_exc)
        self._readyz_sequence = list(readyz_sequence)
        self._readyz_idx = 0

    def request_json(
        self,
        method,
        path,
        *,
        json=None,
        authenticated=True,
        idempotency_key=None,
    ):
        if path == "/readyz":
            self.calls.append(dict(method=method, path=path, json=json,
                                   authenticated=authenticated,
                                   idempotency_key=idempotency_key))
            idx = min(self._readyz_idx, len(self._readyz_sequence) - 1)
            self._readyz_idx += 1
            item = self._readyz_sequence[idx]
            if isinstance(item, BaseException):
                raise item
            return item
        return super().request_json(
            method, path, json=json, authenticated=authenticated,
            idempotency_key=idempotency_key,
        )


def upload_calls(client) -> list[dict[str, Any]]:
    """The recorded calls of a stub client, without the readiness probes."""
    return [call for call in client.calls if call.get("path") != "/readyz"]
