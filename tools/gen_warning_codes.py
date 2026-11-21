#!/usr/bin/env python3
"""Regenerate the warning-code documents under ``docs/contract/`` from the registries.

Each adapter has one document, listed in ``DOCUMENTS``. The ARC adapter's is
``docs/contract/WARNING_CODES.md``, from ``tckdb_arc.warning_codes.ArcWarning`` and
``tckdb_core.warning_codes.CoreWarning``. ``adapters/tckdb_arc/tests/test_warning_registry.py`` fails
when the committed document differs from what this writes::

    python tools/gen_warning_codes.py          # rewrite the documents
    python tools/gen_warning_codes.py --check  # exit 1 when one is stale
"""

import argparse
import sys
from pathlib import Path

from tckdb_arc import warning_codes as arc_warning_codes

REPO = Path(__file__).resolve().parents[1]

#: One ``(path relative to the repository root, render function)`` pair per adapter.
DOCUMENTS = (
    (arc_warning_codes.DOC_RELATIVE_PATH, arc_warning_codes.render_markdown),
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 when a document is stale instead of writing it")
    args = parser.parse_args(argv)
    status = 0
    for relative_path, render in DOCUMENTS:
        path = REPO / relative_path
        text = render()
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                print(f"{path} is stale; run: python tools/gen_warning_codes.py", file=sys.stderr)
                status = 1
        else:
            path.write_text(text, encoding="utf-8")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
