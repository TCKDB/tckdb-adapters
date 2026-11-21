"""Check payloads against the producer contract tckdb-schemas ships.

``tckdb_schemas.contract`` carries one JSON Schema per upload payload,
generated from TCKDB's own routes and models (tckdb-schemas 0.52+). Every
payload an adapter builds is validated against the schema for its route,
as the JSON the client sends (a ``json.dumps`` round trip, NaN refused), so
a payload the contract refuses fails the test that built it. The hook in
:mod:`tckdb_core.testing.contract_hook` applies this, after the route's
published pydantic model, to every builder an adapter's conftest registers;
tests call :func:`contract_validate` where they check a payload or fragment
themselves.

The route table (``ROUTE_SCHEMAS``) and the cached validators are the ones
:mod:`tckdb_core.validation` checks every request with before the POST, so the kit and the runtime
cover the same routes. The kit is deliberately the stricter of the two: it runs the published model
and then the JSON Schema (``assert_request_is_accepted``), so a builder is held to the schema in
tests, while at run time the model alone decides for a route that has one (it is what the server
runs, in lax mode; the schema would refuse ``"0"`` for an integer ``charge`` where the server
accepts it).

What this does not cover: the route handlers' own rules (ownership and
role checks such as ``thermo_energy_level_*``, composition checks such as
``calculation_geometry_composition_mismatch``), request headers, and JSON
Schema ``format`` keywords (no format checker is enabled). The live
integration gate (:mod:`tckdb_core.testing.live`) covers the handlers.

Every adapter's suite targets one tckdb-schemas line, :data:`TARGET_SCHEMAS_LINE`
(this line is the single copy; ``tools/tckdb_drift.py --bump`` rewrites it). A
different installed version fails ``test_contract_pin`` rather than silently
testing against a contract nobody read.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.metadata import version
from typing import Any

from jsonschema import Draft202012Validator
from tckdb_schemas import contract
from tckdb_schemas.fragments.artifact import ArtifactIn
from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest
from tckdb_schemas.workflows.computed_species_upload import ComputedSpeciesUploadRequest
from tckdb_schemas.workflows.conformer_upload import ConformerUploadRequest
from tckdb_schemas.workflows.transition_state_upload import TransitionStateUploadRequest

# The route table, the published models and the cached validators are the runtime check's
# (:mod:`tckdb_core.validation`); the kit reads the same objects, so a route added there is
# covered here and the two cannot drift. The names below stay importable from this module.
from tckdb_core.validation import (
    ROUTE_SCHEMAS,
    json_schema_validator as _validator,
    request_models,
    wire_json as _wire_json,
)

#: The published pydantic model of each route's request (the four whole-request routes); the
#: very dict the runtime check reads. The artifact route's request model lives in the backend; its
#: items are ``ArtifactIn``. The standalone requests are backend-side too, so for those routes the
#: shipped JSON Schema is the check.
REQUEST_MODELS = request_models()

#: The tckdb-schemas release line the suites were run against. Keep it equal
#: to the ``tckdb-schemas`` bound in each ``pyproject.toml``; moving it means
#: reading ``python -m tckdb_schemas.contract --since <old>`` first.
TARGET_SCHEMAS_LINE = (0, 176)


def installed_schemas_version() -> str:
    return version("tckdb-schemas")


@lru_cache(maxsize=None)
def _fragment_validator(model_name: str) -> Draft202012Validator:
    """Validator for a nested model, taken from the first route defining it."""
    for route in ROUTE_SCHEMAS.values():
        definitions = contract.json_schema(route).get("$defs", {})
        if model_name in definitions:
            return Draft202012Validator(
                {"$defs": definitions, "$ref": f"#/$defs/{model_name}"}
            )
    raise KeyError(
        f"{model_name!r} is neither a shipped payload schema nor a model any "
        f"adapter route's schema defines"
    )


def _check(validator: Draft202012Validator, payload: Any, name: str) -> None:
    errors = sorted(validator.iter_errors(_wire_json(payload)), key=lambda e: list(e.path))
    if errors:
        detail = "\n".join(
            f"  at {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
            for e in errors[:10]
        )
        raise AssertionError(
            f"payload does not match the tckdb-schemas {installed_schemas_version()} "
            f"contract for {name} ({len(errors)} error(s)):\n{detail}"
        )


def assert_matches_contract(payload: Any, schema_name: str) -> None:
    """Validate a whole request against its route's shipped JSON Schema."""
    _check(_validator(schema_name), payload, schema_name)


def assert_fragment_matches_contract(payload: Any, model_name: str) -> None:
    """Validate a nested block against its model's definition in the contract."""
    _check(_fragment_validator(model_name), payload, model_name)


def assert_request_is_accepted(payload: Any, schema_name: str) -> None:
    """Run the route's published pydantic model (when there is one), then its shipped JSON Schema.

    The model runs its validators (the JSON Schema cannot express them);
    the schema is the contract's own statement of the wire shape. Neither
    reaches the route handlers' database-side rules.
    """
    if schema_name == "ArtifactsUploadRequest":
        for artifact in payload["artifacts"]:
            ArtifactIn.model_validate(artifact)
    elif schema_name in REQUEST_MODELS:
        REQUEST_MODELS[schema_name].model_validate(_wire_json(payload))
    assert_matches_contract(payload, schema_name)


def contract_validate(model: Any, payload: Any) -> Any:
    """``model.model_validate(payload)``, then the contract's JSON Schema check.

    The pydantic model runs first, so a test expecting it to refuse still
    sees its error.
    """
    validated = model.model_validate(payload)
    name = model.__name__
    if name in contract.schema_names():
        assert_matches_contract(payload, name)
    else:
        assert_fragment_matches_contract(payload, name)
    return validated


def artifacts_request_bodies(prepared: Any) -> list[dict[str, Any]]:
    """The ``ArtifactsUploadRequest`` bodies an artifact plan becomes.

    Mirrors ``TCKDBClient.upload_artifacts(batch_by_calculation=True)``: one
    body per calculation (by its ``calc_`` ref, else its id), cut into several
    when its files exceed the client's inline budget, each item carrying the
    kind, filename, base64 content and the declared sha256/bytes the adapter
    prepared.
    """
    import base64
    from pathlib import Path

    from tckdb_client.builders import artifact as client_artifact

    budget = client_artifact.INLINE_TOTAL_BUDGET_BYTES
    groups: dict[Any, list[Any]] = {}
    for item in prepared:
        # The core addresses a calculation by its ref, so a plan of refs alone is cut by ref.
        groups.setdefault(getattr(item, "calculation_ref", None) or item.calculation_id, []).append(item)
    bodies: list[dict[str, Any]] = []
    for items in groups.values():
        current: list[dict[str, Any]] = []
        used = 0
        for item in items:
            size = Path(item.path).stat().st_size
            if current and used + size > budget:
                bodies.append({"artifacts": current})
                current, used = [], 0
            artifact = {
                "kind": item.kind,
                "filename": getattr(item, "filename", None) or Path(item.path).name,
                "content_base64": base64.b64encode(Path(item.path).read_bytes()).decode("ascii"),
            }
            if getattr(item, "sha256", None) is not None:
                artifact["sha256"] = item.sha256
            if getattr(item, "bytes", None) is not None:
                artifact["bytes"] = item.bytes
            current.append(artifact)
            used += size
        bodies.append({"artifacts": current})
    return bodies
