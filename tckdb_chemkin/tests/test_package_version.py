"""The adapter's version is written down twice, so it can disagree.

``tckdb-chemkin`` ships and versions separately from ``tckdb-client``:
it has its own ``pyproject.toml`` and its own ``__version__``. Nothing
derives one from the other -- unlike ``tckdb_client.__version__``, which
reads installed distribution metadata and therefore cannot drift -- so a
release that bumps the packaging metadata and forgets the module (or the
other way round) ships a package that misreports itself to anyone who
prints ``tckdb_chemkin.__version__``.

One assertion is cheaper than finding out from a bug report which of the
two numbers was the real one.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import tckdb_chemkin

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _declared_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    assert match is not None, f"no version in {PYPROJECT}"
    return match.group(1)


def test_module_version_matches_the_packaging_metadata():
    assert tckdb_chemkin.__version__ == _declared_version()


def test_the_adapter_versions_separately_from_the_client():
    """Its own distribution, so its own number.

    The package has its own ``pyproject.toml`` and reaches ``tckdb-client``
    only as a declared dependency, so the two version numbers are independent.
    """
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    assert project["name"] == "tckdb-chemkin"
    assert any(dep.startswith("tckdb-client") for dep in project["dependencies"])
