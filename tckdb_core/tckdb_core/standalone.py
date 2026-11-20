"""Submit a request a producer already built to one of TCKDB's standalone record routes.

The conformer, computed-species, computed-reaction and transition-state routes have
submit methods in each producer adapter, because each producer builds those requests
from its own output. The routes in :data:`STANDALONE_ROUTES` (thermo, kinetics,
statmech, transport, reaction, network, pressure-dependent network) take one record
each. This module gives them the same transport the other routes have, without any
builder: the producer hands over a finished request dictionary and
:meth:`StandaloneSubmitMixin.submit_standalone` does the rest.

For one request it

1. attaches ``config.rights`` when the request states none (a request that states
   different rights is refused);
2. validates the request against the route's shipped JSON Schema (:mod:`tckdb_core.validation`; this
   check is not governed by ``config.validate_before_upload``, which switches off the check
   :meth:`~tckdb_core.uploader.TCKDBUploaderBase._upload` makes just before the POST), and raises
   :class:`StandalonePayloadError` before anything is written when TCKDB's contract
   would refuse it;
3. hands the finished request to
   :meth:`~tckdb_core.uploader.TCKDBUploaderBase._write_and_upload`, which builds the
   idempotency key, writes the payload and a ``pending`` sidecar, and finishes as
   ``skipped`` when upload is off or uploads (readiness probe, POST, sidecar bookkeeping,
   strict mode).

What it does not do: build, complete or repair a request, or apply a rule the route
handlers apply on the server (ownership of cited calculations, composition checks).
The shipped JSON Schema checks shape only. tckdb-schemas publishes no pydantic model
for these standalone requests, so the schema is the only local check.

Nothing here reads a producer's output format.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tckdb_core.constants import (
    KINETICS_ENDPOINT,
    KINETICS_KIND,
    NETWORK_ENDPOINT,
    NETWORK_KIND,
    NETWORK_PDEP_ENDPOINT,
    NETWORK_PDEP_KIND,
    REACTION_ENDPOINT,
    REACTION_KIND,
    STATMECH_ENDPOINT,
    STATMECH_KIND,
    THERMO_ENDPOINT,
    THERMO_KIND,
    TRANSPORT_ENDPOINT,
    TRANSPORT_KIND,
)
from tckdb_core.payload_writer import PayloadWriter
from tckdb_core.validation import (
    MAX_FINDING_CHARS,  # noqa: F401  (public name, kept)
    ROUTE_SCHEMAS,
    PayloadContractError,
    clip as _clip,
    json_schema_validator as _validator,  # noqa: F401  (earlier private name, kept)
    schema_findings,
)

if TYPE_CHECKING:  # pragma: no cover
    from tckdb_core.outcomes import UploadOutcome


@dataclass(frozen=True)
class StandaloneRoute:
    """One standalone record route: its payload kind, endpoint, contract surface and sidecar bucket."""

    kind: str
    endpoint: str
    #: The ``tckdb_schemas.contract`` name of the route's request, e.g. ``ThermoUploadRequest``.
    schema_name: str
    #: The directory under the payload root that holds this kind's payloads and sidecars.
    subdir: str


def _routes(*routes: StandaloneRoute) -> Mapping[str, StandaloneRoute]:
    return {route.kind: route for route in routes}


#: The standalone record routes the core transports, by payload kind.
STANDALONE_ROUTES: Mapping[str, StandaloneRoute] = _routes(
    StandaloneRoute(THERMO_KIND, THERMO_ENDPOINT, ROUTE_SCHEMAS[THERMO_ENDPOINT], PayloadWriter.THERMO_SUBDIR),
    StandaloneRoute(KINETICS_KIND, KINETICS_ENDPOINT, ROUTE_SCHEMAS[KINETICS_ENDPOINT], PayloadWriter.KINETICS_SUBDIR),
    StandaloneRoute(STATMECH_KIND, STATMECH_ENDPOINT, ROUTE_SCHEMAS[STATMECH_ENDPOINT], PayloadWriter.STATMECH_SUBDIR),
    StandaloneRoute(TRANSPORT_KIND, TRANSPORT_ENDPOINT, ROUTE_SCHEMAS[TRANSPORT_ENDPOINT], PayloadWriter.TRANSPORT_SUBDIR),
    StandaloneRoute(REACTION_KIND, REACTION_ENDPOINT, ROUTE_SCHEMAS[REACTION_ENDPOINT], PayloadWriter.REACTION_SUBDIR),
    StandaloneRoute(NETWORK_KIND, NETWORK_ENDPOINT, ROUTE_SCHEMAS[NETWORK_ENDPOINT], PayloadWriter.NETWORK_SUBDIR),
    StandaloneRoute(
        NETWORK_PDEP_KIND, NETWORK_PDEP_ENDPOINT, ROUTE_SCHEMAS[NETWORK_PDEP_ENDPOINT], PayloadWriter.NETWORK_PDEP_SUBDIR
    ),
)

#: Idempotency-key slot used when the caller names no qualifier.
DEFAULT_QUALIFIER = "standalone"


class StandalonePayloadError(PayloadContractError):
    """The request is not one TCKDB's contract accepts for its route; nothing was written.

    ``errors`` lists each finding as ``"<path>: <message>"``. A
    :class:`~tckdb_core.validation.PayloadContractError`, so one ``except`` covers this and the
    refusal strict mode raises at the POST.
    """


def standalone_route(kind: str) -> StandaloneRoute:
    """The route for ``kind``; ``ValueError`` naming the known kinds when there is none."""
    try:
        return STANDALONE_ROUTES[kind]
    except KeyError:
        raise ValueError(
            f"unknown standalone payload kind {kind!r}; the core carries {sorted(STANDALONE_ROUTES)}"
        ) from None


def standalone_payload_errors(kind: str, payload: Any) -> list[str]:
    """Every way ``payload`` fails the route's shipped JSON Schema, as ``"<path>: <message>"``.

    The request is checked as the client sends it: through a JSON round trip, so a value
    that is not JSON, or a NaN or infinity, is a finding. An empty list means the shape is
    accepted; it says nothing about the rules the route handlers apply.
    """
    return schema_findings(standalone_route(kind).schema_name, payload)


def validate_standalone_payload(kind: str, payload: Any) -> None:
    """Raise :class:`StandalonePayloadError` unless ``payload`` matches the route's shipped schema."""
    errors = standalone_payload_errors(kind, payload)
    if errors:
        route = standalone_route(kind)
        shown = "\n".join(f"  at {line}" for line in errors[:10])
        raise StandalonePayloadError(
            f"{kind} payload does not match the tckdb-schemas contract for {route.schema_name} "
            f"({len(errors)} finding(s)):\n{shown}",
            errors,
        )


class StandaloneSubmitMixin:
    """:meth:`submit_standalone`, for a class that has the base uploader's pipeline.

    :class:`~tckdb_core.uploader.TCKDBUploaderBase` inherits it; it relies on that class's
    ``_config``, ``_idempotency_key``, ``_write_and_upload`` and ``_log``.
    """

    def submit_standalone(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        label: str,
        qualifier: str | None = None,
        project_label: str | None = None,
        warnings: Iterable[Mapping[str, Any]] | None = None,
    ) -> "UploadOutcome | None":
        """Validate, write and (when configured) upload one finished standalone request.

        ``kind`` is one of ``STANDALONE_ROUTES`` (``"thermo"``, ``"kinetics"``, ...).
        ``label`` names the record: it is the payload's file name and the species-or-reaction
        part of the idempotency key. ``qualifier`` fills the key's second slot (default
        ``"standalone"``) and, when given, the file name (``<label>.<qualifier>``); give it when
        one label has several records of a kind, for example two thermo records at different
        levels. ``project_label`` defaults to the config's.
        ``warnings`` are the producer's findings, written to the sidecar ahead of the server's.

        The caller's ``payload`` is never modified; ``config.rights`` is added to a copy when
        the request has no ``rights`` of its own.

        Returns ``None`` when the config is disabled. Raises :class:`StandalonePayloadError`,
        before any file is written, for a request the contract refuses, a request whose
        ``rights`` differ from the configured ones, an empty ``label``, or a label, qualifier or
        project label so long that the idempotency key exceeds the server's 200 characters; strict mode does not
        turn that into a recorded failure, because a payload that never passed validation
        has nothing to replay. Otherwise the outcome is that of ``_upload`` (``uploaded``,
        ``failed``, or ``skipped`` when ``config.upload`` is off).
        """
        route = standalone_route(kind)
        if not self._config.enabled:
            return None
        if not isinstance(label, str) or not label.strip():
            raise StandalonePayloadError("a standalone submission needs a non-empty label", ["label: empty"])
        if not isinstance(payload, Mapping):
            raise StandalonePayloadError(
                f"{kind} payload must be a mapping, got {type(payload).__name__}",
                [f"<root>: got {type(payload).__name__}"],
            )

        request = copy.deepcopy(dict(payload))
        configured = self._config.rights
        if configured is not None:
            stated = request.get("rights")
            if stated is None:
                request["rights"] = dict(configured)
            elif stated != dict(configured):
                raise StandalonePayloadError(
                    f"the {kind} payload states rights that differ from the configured rights; "
                    "state them in one place",
                    ["rights: differs from config.rights"],
                )
        validate_standalone_payload(kind, request)

        project = project_label if project_label is not None else self._config.project_label
        species_label, conformer_label = label, qualifier or DEFAULT_QUALIFIER
        try:  # fail before any write, with the contract error, when the key cannot be built
            self._idempotency_key(
                request,
                kind=route.kind,
                project_label=project,
                species_label=species_label,
                conformer_label=conformer_label,
            )
        except ValueError as exc:  # e.g. over the server's 200-character key limit
            raise StandalonePayloadError(
                f"cannot build an idempotency key for the {kind} payload: {exc}; "
                "shorten the label, qualifier or project label",
                [f"label: {_clip(str(exc))}"],
            ) from exc
        return self._write_and_upload(
            payload=request,
            kind=route.kind,
            endpoint=route.endpoint,
            # The qualifier is in the key, so it is in the file name too: two records under one
            # label must not overwrite each other on disk.
            label=f"{label}.{qualifier}" if qualifier else label,
            project_label=project,
            species_label=species_label,
            conformer_label=conformer_label,
            log_name=route.kind,
            subdir=route.subdir,
            warnings=[dict(item) for item in warnings or ()],
            attach_rights=False,  # reconciled with config.rights above
        )


__all__ = [
    "DEFAULT_QUALIFIER",
    "STANDALONE_ROUTES",
    "StandaloneRoute",
    "StandalonePayloadError",
    "StandaloneSubmitMixin",
    "standalone_payload_errors",
    "standalone_route",
    "validate_standalone_payload",
]
