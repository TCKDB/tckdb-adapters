"""The source of the ARC adapter: ``adapter.py`` plus the sibling modules its helpers live in.

Source scans (the warning-registry checks, the Hartree-constant guard) read the adapter's code
through these helpers, so a helper that moves between those files stays under the same checks.
"""

import ast
from pathlib import Path

import tckdb_arc.adapter as _adapter

PACKAGE_DIR = Path(_adapter.__file__).resolve().parent

#: The modules the adapter's helpers live in, lowest layer first; ``adapter.py`` imports from each.
HELPER_MODULES = (
    "self_check", "geometry", "levels", "calculations", "path_search",
    "thermo", "provenance", "statmech", "kinetics", "reactions",
)

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
