"""A synthetic mini-repo for the drift tool's tests.

The tests must not depend on the checkout's real pins: the drift workflow bumps the
real files to the newest TCKDB before it runs the suite. Everything here uses fixed
values (schemas 0.54 / client 0.95; each package's version is in ``VERSIONS``).

The repository's *shape* is not written down here. It comes from the tool's own table,
``PACKAGES`` in ``tools/tckdb_drift.py``: one synthetic package per row, with the version
file, README sentence and changelog style its flags name, and the TCKDB dependencies its
real ``pyproject.toml`` states (the CHEMKIN adapter has no tckdb-schemas bound, the ARC
adapter requires the core). So adding a package means adding its row and nothing in the
tests or here.
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("tckdb_drift_for_fixture", REPO / "tools" / "tckdb_drift.py")
drift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drift)

PIN_REPO = "https://example.org/tckdb.git"
OLD_SHA = "a" * 40
PINNED_SCHEMAS = (0, 54)
PINNED_CLIENT = (0, 95)
#: The synthetic starting version of each package; a package without an entry starts at DEFAULT_VERSION.
VERSIONS = {
    "tckdb_core": "0.1.0",
    "adapters/tckdb_arc": "1.2.3",
    "adapters/tckdb_chemkin": "0.7.7",
    "adapters/tckdb_qcschema": "0.9.9",
}
DEFAULT_VERSION = "0.3.0"
#: The directories of the packages, in the tool's order.
DIRECTORIES = [pkg.directory for pkg in drift.PACKAGES]

_DEPENDENCY_LINES = {
    drift.CORE_DIST: '    "tckdb-adapters-core>=0.1.0,<0.2",  # pipeline',
    "tckdb-client": '    "tckdb-client>=0.95,<0.96",   # transport',
    "tckdb-schemas": '    "tckdb-schemas>=0.54,<0.55", # contract',
}


def version_of(pkg) -> str:
    return VERSIONS.get(pkg.directory, DEFAULT_VERSION)


def _stated(pkg) -> list[str]:
    """The TCKDB dependencies the real package states, in a fixed order."""
    real = (REPO / pkg.pyproject).read_text()
    return [name for name in (drift.CORE_DIST, "tckdb-client", "tckdb-schemas") if drift.states_dependency(real, name)]


def _pyproject(pkg) -> str:
    lines = [_DEPENDENCY_LINES[name] for name in _stated(pkg)]
    return (
        f'[project]\nname = "{pkg.dist}"\nversion = "{version_of(pkg)}"\ndependencies = [\n'
        + "\n".join(lines) + '\n    "PyYAML>=6",\n]\n'
    )


def _changelog(pkg) -> str:
    version = version_of(pkg)
    if pkg.changelog_style == "marker":
        return (
            f"# Changelog: {pkg.dist}\n\n## Tracked TCKDB releases\n\n{drift.CHANGELOG_MARKER}\n\n"
            f"## {version}\n\nOther text.\n"
        )
    return f"# Changelog: {pkg.dist}\n\nReleases are listed newest first.\n\n## {version}\n\nFirst release.\n"


def _files() -> dict[str, str]:
    files: dict[str, str] = {}
    for pkg in drift.PACKAGES:
        files[pkg.pyproject] = _pyproject(pkg)
        files[pkg.changelog] = _changelog(pkg)
        if pkg.version_in_init:
            files[pkg.init] = f'"""{pkg.module}"""\n\n__version__ = "{version_of(pkg)}"\n'
        if pkg.readme_sentence:
            files[pkg.readme] = (
                f"# {pkg.dist}\n\nRequires Python 3.11+, `tckdb-client` 0.95.x and `tckdb-schemas` 0.54.x.\n\nOther text.\n"
            )
        else:  # a README a bump must leave alone
            files[pkg.readme] = f"# {pkg.dist}\n\nIt installs `tckdb-client` and `tckdb-schemas`.\n"
    files[drift.CONTRACT_PY] = '"""doc"""\n\nTARGET_SCHEMAS_LINE = (0, 54)\nOTHER = 1\n'
    files[drift.PIN_FILE] = f'''# pin
[tckdb]
repo = "{PIN_REPO}"
sha = "{OLD_SHA}"
schemas_subdir = "schemas/python/tckdb-schemas"
client_subdir = "clients/python"
'''
    files[drift.README] = f'''# adapters

The tested contract is `tckdb-client` 0.95.x with `tckdb-schemas` 0.54.x
(notes).

```bash
pip install \\
  "tckdb-client @ git+{PIN_REPO}@{OLD_SHA}#subdirectory=clients/python" \\
  "tckdb-schemas @ git+{PIN_REPO}@{OLD_SHA}#subdirectory=schemas/python/tckdb-schemas"
```
'''
    # Files a bump must leave alone.
    files.update({
        ".github/workflows/ci.yml": "name: ci\n",
        ".github/workflows/tckdb-drift.yml": "name: drift\n",
        "AGENTS.md": "# rules\n",
        "CLAUDE.md": "@AGENTS.md\n",
    })
    return files


FILES = _files()


def _bumped() -> list[str]:
    """The files a bump rewrites, from the table's flags (the tool's own list is ``bumped_files()``).

    The rest of ``FILES`` must stay byte-identical. A test checks the tool's list equals this one.
    """
    files: list[str] = []
    for pkg in drift.PACKAGES:
        files.append(pkg.pyproject)
        if pkg.version_in_init:
            files.append(pkg.init)
        if pkg.readme_sentence:
            files.append(pkg.readme)
        files.append(pkg.changelog)
    return files + [drift.CONTRACT_PY, drift.PIN_FILE, drift.README]


BUMPED = _bumped()


def build(root: Path) -> Path:
    for rel, text in FILES.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
    return root
