"""Pin the tckdb-schemas contract this suite validates payloads against.

The conftest hook checks every built payload against the JSON Schemas the
installed tckdb-schemas ships. That proves conformance only to the contract
that happens to be installed, so these tests pin which one it is: a silent
upgrade (or downgrade) of tckdb-schemas fails here, naming the version, until
someone reads ``python -m tckdb_schemas.contract --since <old>`` and moves
``TARGET_SCHEMAS_LINE`` and the ``pyproject.toml`` bound together. The
assertions are :mod:`tckdb_core.testing.contract_pin`.
"""

from pathlib import Path

from tckdb_core.testing import contract_pin

from tckdb_arc import adapter as adapter_module

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_installed_tckdb_schemas_is_the_targeted_line():
    contract_pin.assert_installed_schemas_is_the_targeted_line()


def test_pyproject_bounds_tckdb_schemas_to_the_targeted_line():
    contract_pin.assert_pyproject_bounds_schemas_to_the_targeted_line(PYPROJECT)


def test_contract_document_is_the_installed_version():
    contract_pin.assert_contract_document_is_the_installed_version()


def test_every_adapter_route_has_a_shipped_schema():
    contract_pin.assert_routes_have_shipped_schemas({
        adapter_module.CONFORMER_UPLOAD_ENDPOINT,
        adapter_module.COMPUTED_SPECIES_ENDPOINT,
        adapter_module.COMPUTED_REACTION_ENDPOINT,
        adapter_module.TRANSITION_STATE_ENDPOINT,
        adapter_module.ARTIFACTS_ENDPOINT_TEMPLATE,
    })
