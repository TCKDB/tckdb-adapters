"""Check a request against the contract tckdb-schemas ships, before it is posted.

``tckdb_schemas.contract`` carries one JSON Schema per upload route, generated from TCKDB's
own routes and models, and publishes a pydantic model for the four whole-request routes
(conformer, computed-species, computed-reaction, transition-state) and for the items of an
artifact batch. This module is the one place that knows which schema and model belong to
which route, and the one place that runs them:

- :data:`ROUTE_SCHEMAS` is the table, endpoint to contract surface, for every route the core
  posts to. The test kit (:mod:`tckdb_core.testing.contract`) and the standalone routes
  (:mod:`tckdb_core.standalone`) read it; there is no second copy.
- :func:`request_findings` checks a request the way the client will send it (a JSON round
  trip, NaN and infinity refused). **At run time the published model alone decides for a route
  that has one**, because the server runs that model in pydantic's lax mode (``"0"`` is an
  accepted ``charge``), while the JSON Schema is stricter (it says "not of type integer").
  Only a route with no published model (the seven standalone record routes) is checked against
  the shipped JSON Schema. The test kit's ``assert_request_is_accepted`` is deliberately
  stricter, model then schema, so a builder is held to the schema in tests. Every finding is one
  bounded line, ``"<path>: <message>"``.
- :class:`PreUploadValidationMixin` is the runtime hook. :class:`~tckdb_core.uploader.TCKDBUploaderBase`
  inherits it and calls it before the readiness probe and the POST of every route, artifact
  batches included. A request that fails is not posted; the sidecar records ``failed`` with
  the code ``payload_failed_local_validation`` (distinct from any server refusal, which
  carries an HTTP status), strict mode raises :class:`PayloadContractError`, and the payload
  file stays on disk. ``config.validate_before_upload = False`` turns the check off. A bug in the
  checker itself never blocks an upload: the request is posted unchecked and the sidecar says
  ``payload_local_validation_unavailable``.

jsonschema, the shipped schemas and the published models are imported on the first check, so
importing the uploader (or running offline with the check off) does not pay for them.

What this does not cover: the route handlers' own rules (ownership and role checks,
composition checks), request headers, and JSON Schema ``format`` keywords (no format checker
is enabled). A request that passes here can still be refused by the server. The check follows
the *installed* tckdb-schemas, which may be older or newer than the server; that is what the
switch is for.
"""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from itertools import islice
from typing import TYPE_CHECKING, Any

from tckdb_core.adapter_warnings import AdapterWarning
from tckdb_core.constants import (
    ARTIFACTS_ENDPOINT_TEMPLATE,
    COMPUTED_REACTION_ENDPOINT,
    COMPUTED_SPECIES_ENDPOINT,
    CONFORMER_UPLOAD_ENDPOINT,
    KINETICS_ENDPOINT,
    NETWORK_ENDPOINT,
    NETWORK_PDEP_ENDPOINT,
    REACTION_ENDPOINT,
    STATMECH_ENDPOINT,
    THERMO_ENDPOINT,
    TRANSITION_STATE_ENDPOINT,
    TRANSPORT_ENDPOINT,
)
from tckdb_core.warning_codes import CoreWarning

if TYPE_CHECKING:  # pragma: no cover
    from jsonschema import Draft202012Validator

    from tckdb_core.outcomes import ArtifactUploadOutcome, UploadOutcome
    from tckdb_core.payload_writer import WrittenPayload

#: Upload endpoints and the contract surface each one posts. The single table: the test kit,
#: the standalone routes and the runtime check all read it.
ROUTE_SCHEMAS: Mapping[str, str] = {
    CONFORMER_UPLOAD_ENDPOINT: "ConformerUploadRequest",
    COMPUTED_SPECIES_ENDPOINT: "ComputedSpeciesUploadRequest",
    COMPUTED_REACTION_ENDPOINT: "ComputedReactionUploadRequest",
    TRANSITION_STATE_ENDPOINT: "TransitionStateUploadRequest",
    ARTIFACTS_ENDPOINT_TEMPLATE: "ArtifactsUploadRequest",
    THERMO_ENDPOINT: "ThermoUploadRequest",
    KINETICS_ENDPOINT: "KineticsUploadRequest",
    STATMECH_ENDPOINT: "StatmechUploadRequest",
    TRANSPORT_ENDPOINT: "TransportUploadRequest",
    REACTION_ENDPOINT: "ReactionUploadRequest",
    NETWORK_ENDPOINT: "NetworkUploadRequest",
    NETWORK_PDEP_ENDPOINT: "NetworkPDepUploadRequest",
}

#: Where the published pydantic model of each route's request lives, by contract surface. The
#: artifact route's request model is backend-side; its items are ``ArtifactIn``
#: (:data:`ARTIFACT_ITEM_MODEL`). The standalone thermo, kinetics, statmech, transport, reaction
#: and network requests are backend-side too (tckdb-schemas publishes only nested payloads of
#: them), so for those routes the shipped JSON Schema is the check.
_MODEL_HOMES = {
    "ConformerUploadRequest": ("tckdb_schemas.workflows.conformer_upload", "ConformerUploadRequest"),
    "ComputedSpeciesUploadRequest": (
        "tckdb_schemas.workflows.computed_species_upload", "ComputedSpeciesUploadRequest"),
    "ComputedReactionUploadRequest": (
        "tckdb_schemas.workflows.computed_reaction_upload", "ComputedReactionUploadRequest"),
    "TransitionStateUploadRequest": (
        "tckdb_schemas.workflows.transition_state_upload", "TransitionStateUploadRequest"),
}
_ARTIFACT_ITEM_HOME = ("tckdb_schemas.fragments.artifact", "ArtifactIn")

#: The sidecar and log code of a request refused before the POST.
LOCAL_VALIDATION_CODE: str = CoreWarning.PAYLOAD_FAILED_LOCAL_VALIDATION.value
#: The sidecar code of a request posted unchecked because the checker itself failed.
LOCAL_VALIDATION_UNAVAILABLE_CODE: str = CoreWarning.PAYLOAD_LOCAL_VALIDATION_UNAVAILABLE.value
#: ``source`` of the warnings the check writes: the core's own, whichever producer runs it.
CHECK_SOURCE = "tckdb_core"

#: Longest JSON Schema finding message kept; jsonschema messages quote the offending value, which
#: can be large.
MAX_FINDING_CHARS = 200
#: Longest pydantic finding message kept. Models state a rule in prose, with the values that
#: disagree and the remedy, so the bound is generous; it only stops a pathological echo.
MAX_MODEL_FINDING_CHARS = 2000
#: Most findings collected for one request; a request with more ends with a marker line.
MAX_FINDINGS = 200
#: Findings quoted in an exception message (first one on its first line) and in the sidecar
#: warning's context.
MAX_FINDINGS_SHOWN = 10
MAX_FINDINGS_IN_SIDECAR = 20

#: Content stand-in when an artifact is checked from its prepared metadata: valid base64, non-empty.
_CONTENT_PLACEHOLDER = "AA=="

_REFUSAL_CODE = re.compile(r"[^:]*: ([a-z][a-z0-9]*(?:_[a-z0-9]+)+): ")


def clip(text: str, limit: int = MAX_FINDING_CHARS) -> str:
    """``text`` cut to ``limit`` characters (default :data:`MAX_FINDING_CHARS`), marked when it was cut."""
    return text if len(text) <= limit else text[:limit] + "... [truncated]"


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


@lru_cache(maxsize=None)
def request_models() -> dict[str, Any]:
    """The published request models by contract surface (one dict, shared with the test kit)."""
    return {
        name: getattr(importlib.import_module(module), attr) for name, (module, attr) in _MODEL_HOMES.items()
    }


def request_model(schema_name: str) -> Any | None:
    """The published pydantic model of the route whose contract surface is ``schema_name``, or ``None``."""
    return request_models().get(schema_name)


def __getattr__(name: str) -> Any:
    # ``REQUEST_MODELS`` and ``ARTIFACT_ITEM_MODEL`` are names the test kit imports; resolving them
    # on first use keeps the models out of ``import tckdb_core.uploader``.
    if name == "REQUEST_MODELS":
        return request_models()
    if name == "ARTIFACT_ITEM_MODEL":
        return artifact_item_model()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@lru_cache(maxsize=None)
def artifact_item_model() -> Any:
    """Model of each item of an artifact batch body (``{"artifacts": [...]}``): ``ArtifactIn``."""
    module, attr = _ARTIFACT_ITEM_HOME
    return getattr(importlib.import_module(module), attr)


class PayloadContractError(ValueError):
    """A request is not one TCKDB's contract accepts for its route.

    ``errors`` lists each finding as ``"<path>: <message>"``.
    """

    def __init__(self, message: str, errors: Iterable[str] = ()):
        self.errors = list(errors)
        super().__init__(message)


def schema_name_for(endpoint: str) -> str | None:
    """The contract surface ``endpoint`` posts, or ``None`` when the table has no row for it."""
    return ROUTE_SCHEMAS.get(endpoint)


@lru_cache(maxsize=None)
def json_schema_validator(schema_name: str) -> "Draft202012Validator":
    """The validator of a shipped schema; built once per process, as building one is the costly part."""
    from jsonschema import Draft202012Validator
    from tckdb_schemas import contract

    schema = contract.json_schema(schema_name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def wire_json(payload: Any) -> Any:
    """The payload as the client sends it: JSON, never NaN or Infinity."""
    return json.loads(json.dumps(payload, allow_nan=False))


def _location(path: Iterable[Any]) -> str:
    return "/".join(map(str, path)) or "<root>"


def _schema_message(error: Any) -> str:
    """The finding's message; for an ``anyOf`` / ``oneOf`` the closest alternative is named too."""
    message = clip(_one_line(error.message))
    if error.validator in ("anyOf", "oneOf") and error.context:
        from jsonschema.exceptions import best_match

        # The ``null`` branch of an optional field is never the useful one to quote.
        alternatives = [e for e in error.context if not (e.validator == "type" and e.validator_value == "null")]
        closest = best_match(alternatives or error.context)
        if closest is not None:
            inner = list(closest.absolute_path)[len(list(error.absolute_path)):]
            where = f"{_location(inner)}: " if inner else ""
            message = clip(_one_line(error.message), 120)
            message = clip(f"{message}; closest alternative: {where}{clip(_one_line(closest.message), 100)}", MAX_FINDING_CHARS + 60)
    return message


def _schema_lines(schema_name: str, wire: Any) -> list[str]:
    findings = list(islice(json_schema_validator(schema_name).iter_errors(wire), MAX_FINDINGS + 1))
    findings.sort(key=lambda e: list(map(str, e.path)))
    lines = [f"{_location(e.absolute_path)}: {_schema_message(e)}" for e in findings[:MAX_FINDINGS]]
    if len(findings) > MAX_FINDINGS:
        lines.append(f"<more>: further findings not shown (first {MAX_FINDINGS} listed)")
    return lines


def _capped(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_FINDINGS:
        return lines
    return [*lines[:MAX_FINDINGS], f"<more>: further findings not shown (first {MAX_FINDINGS} listed)"]


def _model_lines(model: Any, wire: Any, prefix: Iterable[Any] = ()) -> list[str]:
    """What ``model.model_validate(wire)`` refuses, one line each; empty when the model accepts it.

    The model's prose is kept nearly whole (:data:`MAX_MODEL_FINDING_CHARS`) and loses only
    pydantic's ``"Value error, "`` prefix, so a contract refusal reads ``<code>: <what disagrees>``.
    """
    from pydantic import ValidationError

    try:
        model.model_validate(wire)
    except ValidationError as exc:
        lines = []
        for item in exc.errors(include_url=False, include_context=False, include_input=False):
            message = _one_line(item["msg"])
            if message.startswith("Value error, "):
                message = message[len("Value error, "):]
            lines.append(f"{_location([*prefix, *item['loc']])}: {clip(message, MAX_MODEL_FINDING_CHARS)}")
        return lines
    return []


def schema_findings(schema_name: str, payload: Any, *, round_trip: bool = True) -> list[str]:
    """Every way ``payload`` fails the shipped JSON Schema ``schema_name``, as ``"<path>: <message>"``.

    The request is checked as the client sends it: through a JSON round trip (skipped with
    ``round_trip=False`` for a body made only of JSON types), so a value that is not JSON,
    or a NaN or infinity, is a finding. An empty list means the shape is accepted; it says
    nothing about the rules the route handlers apply. This is the JSON Schema alone, stricter
    than the model the server runs; :func:`request_findings` is the run-time check.
    """
    if not isinstance(payload, Mapping):
        return [f"<root>: a {schema_name} is a JSON object, got {type(payload).__name__}"]
    if round_trip:
        try:
            payload = wire_json(payload)
        except (TypeError, ValueError) as exc:
            return [f"<root>: not serialisable as the JSON the client sends ({clip(str(exc))})"]
    return _schema_lines(schema_name, payload)


def request_findings(endpoint: str, payload: Any, *, round_trip: bool = True) -> list[str]:
    """Every way ``payload`` fails the contract of ``endpoint``'s route; empty when it passes.

    A route with a published pydantic model (the four whole-request routes; the items of an
    artifact batch) is decided by that model alone: it is what the server runs, in lax mode,
    so a request it accepts is not refused here even where the shipped JSON Schema would be
    stricter. A route without one is checked against its shipped JSON Schema. An endpoint with
    no row in :data:`ROUTE_SCHEMAS` has no contract to check against and yields no findings.
    """
    schema_name = schema_name_for(endpoint)
    if schema_name is None:
        return []
    if not isinstance(payload, Mapping):
        return [f"<root>: a {schema_name} is a JSON object, got {type(payload).__name__}"]
    wire = payload
    if round_trip:
        try:
            wire = wire_json(payload)
        except (TypeError, ValueError) as exc:
            return [f"<root>: not serialisable as the JSON the client sends ({clip(str(exc))})"]
    if schema_name == "ArtifactsUploadRequest":
        items = wire.get("artifacts")
        if not isinstance(items, list) or not items:
            return ["artifacts: a non-empty list of artifacts is required"]
        lines: list[str] = []
        for index, item in enumerate(items):
            lines.extend(_model_lines(artifact_item_model(), item, ("artifacts", index)))
        return _capped(lines)
    model = request_model(schema_name)
    if model is not None:
        return _capped(_model_lines(model, wire))
    return _schema_lines(schema_name, wire)


def validate_request(endpoint: str, payload: Any, *, round_trip: bool = True) -> None:
    """Raise :class:`PayloadContractError` unless ``payload`` passes ``endpoint``'s contract."""
    errors = request_findings(endpoint, payload, round_trip=round_trip)
    if errors:
        raise contract_error(endpoint, errors)


def contract_error(endpoint: str, errors: list[str], *, what: str = "payload") -> PayloadContractError:
    """The exception for ``errors`` found in a request to ``endpoint``; its message quotes the first few."""
    shown = "\n".join(f"  at {line}" for line in errors[:MAX_FINDINGS_SHOWN])
    more = f"\n  ... and {len(errors) - MAX_FINDINGS_SHOWN} more" if len(errors) > MAX_FINDINGS_SHOWN else ""
    return PayloadContractError(
        f"{what} does not match the tckdb-schemas contract for {schema_name_for(endpoint) or endpoint} "
        f"({len(errors)} finding(s)):\n{shown}{more}",
        errors,
    )


def refusal_codes(errors: Iterable[str]) -> list[str]:
    """The stable refusal codes the findings carry (``hessian_method_contradicts_input_deck``), in order.

    The models state a contract rule as ``<code>: <prose>``; a JSON Schema finding never has that shape.
    """
    codes: list[str] = []
    for line in errors:
        match = _REFUSAL_CODE.match(line)
        if match and match.group(1) not in codes:
            codes.append(match.group(1))
    return codes


class PreUploadValidationMixin:
    """The runtime check before a POST, for a class that has the base uploader's pipeline.

    :class:`~tckdb_core.uploader.TCKDBUploaderBase` inherits it; it relies on that class's
    ``_config``, ``_record_failure``, ``_record_artifact_batch_failure``, ``_log`` and
    ``_writer``.

    Failure semantics. The payload file is already on disk when the check runs (the write
    always precedes the request), so it stays there for inspection. Nothing is sent: no
    readiness probe, no POST. The sidecar is ``failed``; its ``last_error`` begins with
    ``payload_failed_local_validation:`` followed by the first finding (so the one line a
    sweep prints names what disagreed), and its ``warnings`` gain an entry with that code and
    the first findings, so it cannot be mistaken for a server refusal (those carry
    ``response_status_code`` and the server's body). The outcome is ``failed`` with the same
    text as ``error``. In strict mode the failure raises :class:`PayloadContractError` after
    the sidecar is written, like any other failed upload.

    A checker that itself raises is not a verdict on the request: the error is logged, the
    sidecar gains a ``payload_local_validation_unavailable`` warning, and the request is posted
    unchecked, in strict mode too.
    """

    def _local_refusal(
        self, endpoint: str, errors: list[str], *, what: str,
    ) -> tuple[PayloadContractError, dict[str, Any]]:
        """The exception and the sidecar warning for a request that failed the check."""
        schema = schema_name_for(endpoint)
        lines = [
            f"{LOCAL_VALIDATION_CODE}: {errors[0]} "
            f"(not posted; {what} has {len(errors)} finding(s) against {schema})"
        ]
        lines += [f"  also at {line}" for line in errors[1:MAX_FINDINGS_SHOWN]]
        if len(errors) > MAX_FINDINGS_SHOWN:
            lines.append(f"  ... and {len(errors) - MAX_FINDINGS_SHOWN} more (see the sidecar)")
        refusal = PayloadContractError("\n".join(lines), errors)
        context: dict[str, Any] = {
            "action": "not_posted",
            "endpoint": endpoint,
            "contract": schema,
            "findings": errors[:MAX_FINDINGS_IN_SIDECAR],
            "finding_count": len(errors),
        }
        codes = refusal_codes(errors)
        if codes:
            context["refusal_codes"] = codes
        warning = AdapterWarning(
            LOCAL_VALIDATION_CODE,
            f"The request does not match the tckdb-schemas contract for {schema}, "
            f"so it was not posted ({len(errors)} finding(s)).",
            None,
            context,
        ).to_dict(CHECK_SOURCE)
        return refusal, warning

    def _unavailable_warning(self, endpoint: str, exc: BaseException) -> dict[str, Any]:
        """Log a checker failure and return the sidecar warning that says the request went unchecked."""
        self._log.warning(  # type: ignore[attr-defined]
            "TCKDB %s: the local contract check could not run for %s (%s: %s); posting unchecked.",
            LOCAL_VALIDATION_UNAVAILABLE_CODE, endpoint, type(exc).__name__, _one_line(str(exc))[:300],
            exc_info=True,
        )
        return AdapterWarning(
            LOCAL_VALIDATION_UNAVAILABLE_CODE,
            f"The local contract check could not run ({type(exc).__name__}), so the request was posted unchecked.",
            None,
            {"action": "posted_unchecked", "endpoint": endpoint, "error": clip(_one_line(f"{type(exc).__name__}: {exc}"))},
        ).to_dict(CHECK_SOURCE)

    def _refuse_invalid_payload(
        self, written: "WrittenPayload", endpoint: str, payload: Any,
    ) -> "UploadOutcome | None":
        """The failed outcome for a payload the contract refuses, or ``None`` when it passes, the check is off, or it could not run."""
        if not self._config.validate_before_upload:  # type: ignore[attr-defined]
            return None
        try:
            errors = request_findings(endpoint, payload)
        except Exception as exc:  # a checker bug must not block an upload
            written.sidecar.warnings = [*written.sidecar.warnings, self._unavailable_warning(endpoint, exc)]
            return None
        if not errors:
            return None
        refusal, warning = self._local_refusal(endpoint, errors, what=f"{written.sidecar.payload_kind} payload")
        written.sidecar.warnings = [*written.sidecar.warnings, warning]
        return self._record_failure(written, str(refusal), refusal)  # type: ignore[attr-defined]

    def _refuse_invalid_artifact_batch(self, prepared: list[Any]) -> "list[ArtifactUploadOutcome] | None":
        """The failed outcomes for an artifact plan whose items the contract refuses, else ``None``.

        Each item is checked with what ``ArtifactIn`` validates (kind, filename, sha256, byte
        count, non-empty content) taken from the prepared metadata, so no file is read or encoded
        a second time. Every item is checked before any batch is posted, so a plan spanning
        several calculations is sent whole or not at all.
        """
        if not self._config.validate_before_upload:  # type: ignore[attr-defined]
            return None
        groups: dict[Any, list[Any]] = {}
        for item in prepared:
            groups.setdefault(item.handle, []).append(item)
        try:
            errors: list[str] = []
            for handle, group in groups.items():
                body = {"artifacts": [
                    {"kind": item.kind, "filename": item.filename, "content_base64": _CONTENT_PLACEHOLDER,
                     "sha256": item.sha256, "bytes": item.bytes}
                    for item in group
                ]}
                errors.extend(
                    f"{handle}/{line}" if len(groups) > 1 else line
                    for line in request_findings(ARTIFACTS_ENDPOINT_TEMPLATE, body, round_trip=False)
                )
        except Exception as exc:  # a checker bug must not block an upload
            warning = self._unavailable_warning(ARTIFACTS_ENDPOINT_TEMPLATE, exc)
            for item in prepared:
                item.written.sidecar.warnings = [*item.written.sidecar.warnings, warning]
            return None
        if not errors:
            return None
        refusal, warning = self._local_refusal(ARTIFACTS_ENDPOINT_TEMPLATE, errors, what="artifact batch")
        for item in prepared:
            item.written.sidecar.warnings = [*item.written.sidecar.warnings, warning]
        return self._record_artifact_batch_failure(prepared, str(refusal), refusal)  # type: ignore[attr-defined]


__all__ = [
    "ARTIFACT_ITEM_MODEL",
    "CHECK_SOURCE",
    "LOCAL_VALIDATION_CODE",
    "LOCAL_VALIDATION_UNAVAILABLE_CODE",
    "MAX_FINDINGS",
    "MAX_FINDING_CHARS",
    "MAX_MODEL_FINDING_CHARS",
    "PayloadContractError",
    "PreUploadValidationMixin",
    "REQUEST_MODELS",
    "ROUTE_SCHEMAS",
    "artifact_item_model",
    "clip",
    "contract_error",
    "json_schema_validator",
    "refusal_codes",
    "request_findings",
    "request_model",
    "request_models",
    "schema_findings",
    "schema_name_for",
    "validate_request",
    "wire_json",
]
