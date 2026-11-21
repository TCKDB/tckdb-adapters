"""Every package states the TCKDB bounds the core does.

``TARGET_SCHEMAS_LINE`` (``tckdb_core/tckdb_core/testing/contract.py``) is the one line the suites
target. The core's and the ARC adapter's contract-pin tests compare their ``tckdb-schemas``
bound with it, and the file-format adapters (``tckdb-chemkin``, ``tckdb-qcschema``), which do not
import the core, compare the bounds they state with the installed packages in their own
``tests/test_contract_pin.py``. Nothing in those suites looks across packages, and no suite checks a
``tckdb-client`` bound against the core's: this does. Every package in the drift tool's table
(``PACKAGES`` in ``tools/tckdb_drift.py``) that states a bound states the core's line, so a
package moved on its own, or left behind by a bump, fails here.

Standard library and pytest only: CI runs this directory with nothing else installed.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("tckdb_drift_under_test", REPO_ROOT / "tools" / "tckdb_drift.py")
drift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drift)


def disagreements(texts: dict[str, str]) -> list[str]:
    """One message per package whose TCKDB bound is not the core's line (``texts``: pyproject.toml by path)."""
    core = drift.core_package()
    found = []
    for name in drift.TCKDB_PACKAGES:
        wanted = drift.pinned_line(texts[core.pyproject], name)
        for pkg in drift.PACKAGES:
            if drift.states_dependency(texts[pkg.pyproject], name):
                line = drift.pinned_line(texts[pkg.pyproject], name)
                if line != wanted:
                    found.append(
                        f"{pkg.directory} states {name} {drift.fmt_line(line)}.x, the core states {drift.fmt_line(wanted)}.x"
                    )
    return found


def test_every_package_states_the_cores_tckdb_lines():
    problems = disagreements(drift.read_pyprojects(REPO_ROOT))
    assert not problems, (
        "; ".join(problems) + ". Move them together with `python tools/tckdb_drift.py --bump` "
        "(it leaves a package that is ahead where it is), after reading the contract change (docs/tckdb_pin.md)."
    )


def test_the_core_states_both_bounds_and_the_contract_line_is_its_schemas_line():
    core_text = (REPO_ROOT / drift.core_package().pyproject).read_text()
    assert drift.states_dependency(core_text, "tckdb-schemas") and drift.states_dependency(core_text, "tckdb-client")
    target = drift.TARGET_LINE_RE.search((REPO_ROOT / drift.CONTRACT_PY).read_text())
    assert target is not None
    assert (int(target[1]), int(target[2])) == drift.pinned_line(core_text, "tckdb-schemas")


def test_the_check_finds_a_package_on_another_line_and_ignores_one_that_states_no_bound():
    def text(schemas=None, client="0.5"):
        deps = [f'"tckdb-client{drift.bound_for(drift.line_of(client + ".0"))}"']
        if schemas:
            deps.append(f'"tckdb-schemas{drift.bound_for(drift.line_of(schemas + ".0"))}"')
        return "[project]\ndependencies = [" + ", ".join(deps) + "]\n"

    texts = {pkg.pyproject: text(schemas="0.7") for pkg in drift.PACKAGES}
    assert disagreements(texts) == []
    chemkin_like = drift.PACKAGES[-1].pyproject
    texts[chemkin_like] = text(schemas=None)  # no tckdb-schemas bound: nothing to compare
    assert disagreements(texts) == []
    texts[chemkin_like] = text(schemas="0.8", client="0.6")
    found = disagreements(texts)
    assert len(found) == 2
    assert any("tckdb-schemas 0.8.x, the core states 0.7.x" in message for message in found)
    assert any("tckdb-client 0.6.x, the core states 0.5.x" in message for message in found)
    assert all(re.match(rf"{re.escape(drift.PACKAGES[-1].directory)} states ", message) for message in found)
