"""The contract-pin assertions every adapter package's suite makes.

The contract hook proves a payload conforms to whichever tckdb-schemas is installed.
These assertions pin which one that is, so a silent upgrade or downgrade fails by
name until someone reads ``python -m tckdb_schemas.contract --since <old>`` and moves
:data:`~tckdb_core.testing.contract.TARGET_SCHEMAS_LINE` and the ``pyproject.toml``
bound together. A package's ``test_contract_pin.py`` is four one-line tests::

    from pathlib import Path
    from tckdb_core.testing import contract_pin

    PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

    def test_installed_tckdb_schemas_is_the_targeted_line():
        contract_pin.assert_installed_schemas_is_the_targeted_line()

    def test_pyproject_bounds_tckdb_schemas_to_the_targeted_line():
        contract_pin.assert_pyproject_bounds_schemas_to_the_targeted_line(PYPROJECT)

    def test_contract_document_is_the_installed_version():
        contract_pin.assert_contract_document_is_the_installed_version()

    def test_every_route_has_a_shipped_schema():
        contract_pin.assert_routes_have_shipped_schemas({...the endpoints the adapter posts to...})
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from pathlib import Path

from tckdb_schemas import contract

from tckdb_core.testing.contract import ROUTE_SCHEMAS, TARGET_SCHEMAS_LINE, installed_schemas_version


def release_line(version: str) -> tuple[int, int]:
    """``(major, minor)`` of an ``X.Y.Z`` version string."""
    major, minor = version.split(".")[:2]
    return int(major), int(minor)


def assert_installed_schemas_is_the_targeted_line() -> None:
    """The installed tckdb-schemas is on :data:`TARGET_SCHEMAS_LINE`."""
    installed = installed_schemas_version()
    assert release_line(installed) == TARGET_SCHEMAS_LINE, (
        f"tests ran against tckdb-schemas {installed}, but the suite targets "
        f"{TARGET_SCHEMAS_LINE[0]}.{TARGET_SCHEMAS_LINE[1]}.x. Read "
        f"`python -m tckdb_schemas.contract --since "
        f"{TARGET_SCHEMAS_LINE[0]}.{TARGET_SCHEMAS_LINE[1]}.0` before moving the pin."
    )


def assert_pyproject_bounds_schemas_to_the_targeted_line(pyproject: str | Path) -> None:
    """The package's ``tckdb-schemas`` dependency is ``>=X.Y,<X.(Y+1)`` for the targeted line."""
    dependencies = tomllib.loads(Path(pyproject).read_text())["project"]["dependencies"]
    specs = [d for d in dependencies if re.match(r"tckdb-schemas\b", d)]
    assert len(specs) == 1, f"{pyproject} must name tckdb-schemas exactly once, found {specs}"
    major, minor = TARGET_SCHEMAS_LINE
    assert specs[0].replace(" ", "") == f"tckdb-schemas>={major}.{minor},<{major}.{minor + 1}", specs[0]


def assert_contract_document_is_the_installed_version() -> None:
    """The contract document tckdb-schemas ships is stamped with the installed version."""
    stamp = re.search(r"tckdb-schemas version `([^`]+)`", contract.markdown())
    assert stamp is not None
    assert stamp.group(1) == installed_schemas_version()


def assert_routes_have_shipped_schemas(posted: Iterable[str]) -> None:
    """Every endpoint in ``posted`` is a key of ``ROUTE_SCHEMAS`` and each has a shipped JSON Schema.

    ``posted`` is the set of endpoints the adapter sends to. It must equal the kit's
    ``ROUTE_SCHEMAS`` keys, so a route added on either side fails here until the other follows.
    """
    assert set(posted) == set(ROUTE_SCHEMAS)
    assert set(ROUTE_SCHEMAS.values()) <= set(contract.schema_names())
