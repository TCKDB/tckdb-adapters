"""The source of the ARC adapter: ``adapter.py`` plus the sibling modules its code lives in.

Source scans (the warning-registry checks, the Hartree-constant guard) read the adapter's code
through these helpers, so a helper that moves between those files stays under the same checks.
"""

import ast
from pathlib import Path

import tckdb_arc.adapter as _adapter

PACKAGE_DIR = Path(_adapter.__file__).resolve().parent

#: Modules of ``tckdb_arc`` that hold no code moved out of ``adapter.py``: the entry points, the ones
#: ``adapter.py`` imports for their own sake, and the rest of the package.
NOT_ADAPTER_HELPERS = (
    "__init__", "_arc_optional", "_compat", "_logging", "_vendor", "adaptive", "adapter", "arc13",
    "cli", "config", "constraints", "evidence", "idempotency", "level_rules", "orca_neb",
    "payload_writer", "sweep", "warning_codes",
)


def _tckdb_arc_modules_imported_by_the_adapter() -> tuple[str, ...]:
    """The ``tckdb_arc`` modules ``adapter.py`` imports from, in the order it imports them."""
    found = []
    for node in ast.parse((PACKAGE_DIR / "adapter.py").read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
            continue
        parts = node.module.split(".")
        if parts[0] != "tckdb_arc":
            continue
        for name in ([parts[1]] if len(parts) > 1 else [a.name for a in node.names]):
            if name not in found:
                found.append(name)
    return tuple(found)


#: The modules the adapter's helpers live in, lowest layer first: every module ``adapter.py`` imports
#: from except the ones in ``NOT_ADAPTER_HELPERS``. A new helper module needs no entry here.
HELPER_MODULES = tuple(
    name for name in _tckdb_arc_modules_imported_by_the_adapter() if name not in NOT_ADAPTER_HELPERS)

ADAPTER_SOURCE_FILES = [PACKAGE_DIR / "adapter.py", *(PACKAGE_DIR / f"{name}.py" for name in HELPER_MODULES)]


def _without_imports(path: Path) -> str:
    """The file's text with its top-level import statements blanked, so a re-export is not a use."""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    for node in ast.parse(text).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for number in range(node.lineno - 1, node.end_lineno):
                lines[number] = ""
    return "\n".join(lines)


def adapter_source_text() -> str:
    """The text of every adapter source file, top-level imports blanked."""
    return "\n".join(_without_imports(path) for path in ADAPTER_SOURCE_FILES)


def adapter_source_tree() -> ast.Module:
    """One module node holding the top-level statements of every adapter source file.

    A node's ``lineno`` is its line in the file it came from.
    """
    body = []
    for path in ADAPTER_SOURCE_FILES:
        body.extend(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body)
    return ast.Module(body=body, type_ignores=[])
