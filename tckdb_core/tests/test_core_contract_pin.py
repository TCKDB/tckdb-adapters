"""Pin the tckdb-schemas contract the core's own suite validates payloads against.

The twin of ``tckdb_arc/tests/test_contract_pin.py``: both read the one
``TARGET_SCHEMAS_LINE`` in :mod:`tckdb_core.testing.contract`, so a bump that moves it
(``tools/tckdb_drift.py --bump``) is checked against every package's ``pyproject.toml``.
The assertions are :mod:`tckdb_core.testing.contract_pin`.
"""

from pathlib import Path

import pytest

from tckdb_core import constants
from tckdb_core.testing import contract_pin
from tckdb_core.testing.contract import ROUTE_SCHEMAS

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_installed_tckdb_schemas_is_the_targeted_line():
    contract_pin.assert_installed_schemas_is_the_targeted_line()


def test_pyproject_bounds_tckdb_schemas_to_the_targeted_line():
    contract_pin.assert_pyproject_bounds_schemas_to_the_targeted_line(PYPROJECT)


def test_contract_document_is_the_installed_version():
    contract_pin.assert_contract_document_is_the_installed_version()


def test_every_core_route_constant_has_a_shipped_schema():
    contract_pin.assert_routes_have_shipped_schemas({
        constants.CONFORMER_UPLOAD_ENDPOINT,
        constants.COMPUTED_SPECIES_ENDPOINT,
        constants.COMPUTED_REACTION_ENDPOINT,
        constants.TRANSITION_STATE_ENDPOINT,
        constants.ARTIFACTS_ENDPOINT_TEMPLATE,
        constants.THERMO_ENDPOINT,
        constants.KINETICS_ENDPOINT,
        constants.STATMECH_ENDPOINT,
        constants.TRANSPORT_ENDPOINT,
        constants.REACTION_ENDPOINT,
        constants.NETWORK_ENDPOINT,
        constants.NETWORK_PDEP_ENDPOINT,
    }, expected=ROUTE_SCHEMAS)  # every route in the kit's table has an endpoint constant


def test_the_pin_assertions_fail_on_a_wrong_bound_or_a_missing_route(tmp_path):
    loose = tmp_path / "pyproject.toml"
    loose.write_text('[project]\ndependencies = ["tckdb-schemas>=0.1"]\n')
    with pytest.raises(AssertionError):
        contract_pin.assert_pyproject_bounds_schemas_to_the_targeted_line(loose)
    absent = tmp_path / "absent.toml"
    absent.write_text('[project]\ndependencies = ["PyYAML>=6"]\n')
    with pytest.raises(AssertionError):
        contract_pin.assert_pyproject_bounds_schemas_to_the_targeted_line(absent)
    # An adapter that posts to a subset of the kit's routes passes ...
    contract_pin.assert_routes_have_shipped_schemas({constants.CONFORMER_UPLOAD_ENDPOINT})
    # ... unless it asked for exact equality, names a route the kit does not know, or names none.
    with pytest.raises(AssertionError):
        contract_pin.assert_routes_have_shipped_schemas({constants.CONFORMER_UPLOAD_ENDPOINT}, expected=ROUTE_SCHEMAS)
    with pytest.raises(AssertionError):
        contract_pin.assert_routes_have_shipped_schemas({"/uploads/thermoml"})
    with pytest.raises(AssertionError):
        contract_pin.assert_routes_have_shipped_schemas(set())
    assert contract_pin.release_line("0.175.2") == (0, 175)
