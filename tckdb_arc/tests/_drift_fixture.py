"""A synthetic mini-repo for the drift tool's tests.

The tests must not depend on the checkout's real pins: the drift workflow bumps the
real files to the newest TCKDB before it runs the suite. Everything here uses fixed
values (schemas 0.54 / client 0.95, adapter 1.2.3). The four packages mirror the real
ones: the core and the two file-format adapters hold ``__version__`` and a sectioned
changelog, the ARC adapter has a marker changelog and a README sentence, and the CHEMKIN
adapter has no tckdb-schemas dependency.
"""

from pathlib import Path

PIN_REPO = "https://example.org/tckdb.git"
OLD_SHA = "a" * 40
PINNED_SCHEMAS = (0, 54)
PINNED_CLIENT = (0, 95)
ADAPTER_VERSION = "1.2.3"

CORE_CHANGELOG = """# Changelog: tckdb-adapters-core

Releases are listed newest first.

## 0.1.0

First release.
"""

CHEMKIN_CHANGELOG = """# Changelog: tckdb-chemkin

Releases are listed newest first.

## 0.7.7

Requires `tckdb-client>=0.94,<0.95` (was `>=0.93,<0.94`).
"""

QCSCHEMA_CHANGELOG = """# Changelog: tckdb-qcschema

Releases are listed newest first.

## 0.9.9

Requires `tckdb-client>=0.94,<0.95` and `tckdb-schemas>=0.53,<0.54`.
"""

FILES = {
    "tckdb_arc/pyproject.toml": f'''[project]
name = "tckdb-arc"
version = "{ADAPTER_VERSION}"
dependencies = [
    "tckdb-adapters-core>=0.1.0,<0.2",  # pipeline
    "tckdb-client>=0.95,<0.96",   # transport
    "tckdb-schemas>=0.54,<0.55", # contract
    "PyYAML>=6",
]
''',
    "tckdb_core/pyproject.toml": '''[project]
name = "tckdb-adapters-core"
version = "0.1.0"
dependencies = [
    "tckdb-client>=0.95,<0.96",   # transport
    "tckdb-schemas>=0.54,<0.55", # contract
]
''',
    "tckdb_core/tckdb_core/__init__.py": '"""core"""\n\n__version__ = "0.1.0"\n',
    "tckdb_core/CHANGELOG.md": CORE_CHANGELOG,
    "tckdb_chemkin/pyproject.toml": '''[project]
name = "tckdb-chemkin"
version = "0.7.7"
dependencies = [
    # tckdb-client is 0.x and a minor may break, so the window holds the
    # client line this release was tested with.
    "tckdb-client>=0.95,<0.96",
    # Every import resolves species identity.
    "rdkit>=2024.3",
]
''',
    "tckdb_chemkin/tckdb_chemkin/__init__.py": '"""chemkin"""\n\n__version__ = "0.7.7"\n',
    "tckdb_chemkin/CHANGELOG.md": CHEMKIN_CHANGELOG,
    "tckdb_qcschema/pyproject.toml": '''[project]
name = "tckdb-qcschema"
version = "0.9.9"
dependencies = [
    # tckdb-client and tckdb-schemas are 0.x and a minor may break, so each
    # window holds the line this release was tested with.
    "tckdb-client>=0.95,<0.96",
    "tckdb-schemas>=0.54,<0.55",
    "qcelemental==0.51.2",
]
''',
    "tckdb_qcschema/tckdb_qcschema/__init__.py": '"""qcschema"""\n\n__version__ = "0.9.9"\n',
    "tckdb_qcschema/CHANGELOG.md": QCSCHEMA_CHANGELOG,
    "tckdb_core/tckdb_core/testing/contract.py": '"""doc"""\n\nTARGET_SCHEMAS_LINE = (0, 54)\nOTHER = 1\n',
    "tckdb-pin.toml": f'''# pin
[tckdb]
repo = "{PIN_REPO}"
sha = "{OLD_SHA}"
schemas_subdir = "schemas/python/tckdb-schemas"
client_subdir = "clients/python"
''',
    "README.md": f'''# adapters

The tested contract is `tckdb-client` 0.95.x with `tckdb-schemas` 0.54.x
(notes).

```bash
pip install \\
  "tckdb-client @ git+{PIN_REPO}@{OLD_SHA}#subdirectory=clients/python" \\
  "tckdb-schemas @ git+{PIN_REPO}@{OLD_SHA}#subdirectory=schemas/python/tckdb-schemas"
```
''',
    "tckdb_arc/README.md": """# tckdb-arc

Requires Python 3.11+, `tckdb-client` 0.95.x and `tckdb-schemas` 0.54.x.

Other text.
""",
    "tckdb_arc/CHANGELOG.md": """# Changelog: tckdb-arc

## Tracked TCKDB releases

<!-- tckdb-drift:changelog -->

## 1.2.3

Other text.
""",
    # Files a bump must leave alone.
    "tckdb_chemkin/README.md": "# tckdb-chemkin\n\nIt installs `tckdb-client` and RDKit with it.\n",
    "tckdb_qcschema/README.md": "# tckdb-qcschema\n\nIt installs `tckdb-client` and `tckdb-schemas`.\n",
    ".github/workflows/ci.yml": "name: ci\n",
    ".github/workflows/tckdb-drift.yml": "name: drift\n",
    "AGENTS.md": "# rules\n",
    "CLAUDE.md": "@AGENTS.md\n",
}

#: The files a bump rewrites; the rest of FILES must stay byte-identical. The tool's own
#: list (``tckdb_drift.bumped_files()``) must equal this one.
BUMPED = [
    "tckdb_core/pyproject.toml",
    "tckdb_core/tckdb_core/__init__.py",
    "tckdb_core/CHANGELOG.md",
    "tckdb_arc/pyproject.toml",
    "tckdb_arc/README.md",
    "tckdb_arc/CHANGELOG.md",
    "tckdb_chemkin/pyproject.toml",
    "tckdb_chemkin/tckdb_chemkin/__init__.py",
    "tckdb_chemkin/CHANGELOG.md",
    "tckdb_qcschema/pyproject.toml",
    "tckdb_qcschema/tckdb_qcschema/__init__.py",
    "tckdb_qcschema/CHANGELOG.md",
    "tckdb_core/tckdb_core/testing/contract.py",
    "tckdb-pin.toml",
    "README.md",
]


def build(root: Path) -> Path:
    for rel, text in FILES.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
    return root
