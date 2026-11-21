"""Pin the TCKDB release lines this package's ``pyproject.toml`` states to the installed ones.

``tckdb-chemkin`` builds on ``tckdb-client`` directly and does not depend on ``tckdb-adapters-core``,
so it cannot use that package's contract-pin kit (``tckdb_core.testing.contract_pin``), which is
written against the core's ``TARGET_SCHEMAS_LINE``. These tests do the same job for the bounds
this package states: each is a single minor line (``>=X.Y,<X.(Y+1)``), and the installed
distribution is on that line, so a silent upgrade or downgrade of the TCKDB packages fails here,
naming the version, until the bound and the tests move together
(``python tools/tckdb_drift.py --bump``, after reading the contract change).
``tools/tests/test_pin_lines.py`` checks that every package states the same lines as the core.
"""

from __future__ import annotations

import re
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
TCKDB_PACKAGES = ("tckdb-client", "tckdb-schemas")
BOUND = re.compile(r">=(\d+)\.(\d+)(?:\.\d+)?,<(\d+)\.(\d+)(?:\.\d+)?")


def stated_lines(pyproject_text: str) -> dict[str, tuple[int, int]]:
    """The minor line each TCKDB dependency is bounded to, for the ones the package states.

    A bound that is not exactly one minor line is an assertion failure.
    """
    dependencies = tomllib.loads(pyproject_text)["project"]["dependencies"]
    lines: dict[str, tuple[int, int]] = {}
    for name in TCKDB_PACKAGES:
        specs = [d for d in dependencies if re.match(rf"{re.escape(name)}(?![\w.-])", d)]
        assert len(specs) <= 1, f"{name} is named more than once: {specs}"
        if not specs:
            continue
        spec = re.sub(r"^\s*\[[^\]]*\]", "", specs[0][len(name):]).split(";")[0].replace(" ", "")
        match = BOUND.fullmatch(spec)
        assert match is not None, f"{name}{spec} is not a single minor line (>=X.Y,<X.(Y+1))"
        low, high = (int(match[1]), int(match[2])), (int(match[3]), int(match[4]))
        assert high == (low[0], low[1] + 1), f"{name}{spec} is not a single minor line (>=X.Y,<X.(Y+1))"
        lines[name] = low
    return lines


def installed_line(name: str) -> tuple[int, int]:
    major, minor = metadata.version(name).split(".")[:2]
    return int(major), int(minor)


def what_to_read(name: str, line: tuple[int, int]) -> str:
    """Where the changes since a release line are written down, for the distribution ``name``.

    ``tckdb-schemas`` ships its contract changelog (``python -m tckdb_schemas.contract --since``);
    ``tckdb-client`` ships none, but its metadata links its changelog, which is what this names.
    """
    if name == "tckdb-schemas":
        return f"`python -m tckdb_schemas.contract --since {line[0]}.{line[1]}.0`"
    links = [value.split(",", 1)[1].strip() for value in metadata.metadata(name).get_all("Project-URL") or []
             if value.lower().startswith("changelog,")]
    return f"the {name} changelog at {links[0]}" if links else f"the {name} changelog in the TCKDB repository (clients/python/CHANGELOG.md)"


def test_the_package_states_a_tckdb_bound_on_one_minor_line():
    assert stated_lines(PYPROJECT.read_text(encoding="utf-8")), "no tckdb-client or tckdb-schemas bound"


def test_every_stated_bound_is_the_installed_line():
    for name, line in stated_lines(PYPROJECT.read_text(encoding="utf-8")).items():
        assert installed_line(name) == line, (
            f"tests ran against {name} {metadata.version(name)}, but pyproject.toml bounds it to "
            f"{line[0]}.{line[1]}.x. Read {what_to_read(name, line)} "
            "before moving the pin, then move it with `python tools/tckdb_drift.py --bump`."
        )


def test_each_distribution_is_pointed_at_its_own_changelog():
    assert what_to_read("tckdb-schemas", (0, 177)) == "`python -m tckdb_schemas.contract --since 0.177.0`"
    client = what_to_read("tckdb-client", (0, 210))
    assert "tckdb_schemas.contract" not in client and "tckdb-client changelog" in client  # never the schemas command
    assert "CHANGELOG.md" in client


def test_the_checks_fail_on_a_loose_bound_or_a_wrong_line():
    def text(*deps):
        return "[project]\ndependencies = [" + ", ".join(f'"{d}"' for d in deps) + "]\n"

    assert stated_lines(text("tckdb-client >=0.5,<0.6 ; python_version >= '3.11'", "rdkit")) == {"tckdb-client": (0, 5)}
    assert stated_lines(text("rdkit")) == {}  # none stated: the first test above is what catches that
    for loose in ("tckdb-client>=0.5", "tckdb-client>=0.5,<0.7", "tckdb-client==0.5.1"):
        with pytest.raises(AssertionError):
            stated_lines(text(loose))
    with pytest.raises(AssertionError):
        stated_lines(text("tckdb-client>=0.5,<0.6", "tckdb-client>=0.5,<0.6"))
