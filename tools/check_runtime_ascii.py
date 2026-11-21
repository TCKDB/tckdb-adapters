#!/usr/bin/env python3
"""Refuse non-ASCII in the adapters' strings that leave the process.

A string an adapter raises, logs or records as a warning reaches a terminal, a
sidecar file, a log or, through an upload, a TCKDB database column. A database
whose encoding is ``SQL_ASCII`` rolls back an upload over one em dash, so these
strings stay ASCII. Typographic characters carry no meaning there: ``--`` says
what an em dash says.

What is checked
    Every ``*.py`` under the packages in ``DEFAULT_TARGETS``, and only string
    literals at an emission site:

    * the argument of a ``raise``;
    * the argument of a logging call (``logger.warning(...)`` and the like);
    * an accumulator call: ``warnings.append(...)``, ``sink.add(...)`` and the
      other receivers in ``_ACCUMULATOR_RECEIVERS``, which is how an adapter
      collects the warnings it writes to a sidecar and sends to TCKDB;
    * a ``message=``, ``detail=``, ``reason=`` or ``msg=`` keyword argument.

    Docstrings, comments and other prose are not checked: they are read in a
    document, never stored. A literal assigned to a name and emitted later is
    not seen either; following it needs dataflow.

Escape hatch
    ``# tckdb: allow-non-ascii`` on the offending line, or on the line the
    emitting statement starts on, for a character that has to be there.

What is not checked
    Every ``*.py`` and ``*.sh`` in the repository is either under a target or
    named in ``UNSCANNED_BY_DESIGN`` with a reason; test trees and
    ``conftest.py`` files are out of scope by one shared reason. ``--audit``
    fails on a source file that is neither, and on a declaration that matches
    no file.

Usage
    python tools/check_runtime_ascii.py [PATH ...]
    python tools/check_runtime_ascii.py --audit
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The shipped packages, relative to the repository root.
DEFAULT_TARGETS = (
    "tckdb_core/tckdb_core",
    "tckdb_arc/tckdb_arc",
    "tckdb_chemkin/tckdb_chemkin",
    "tckdb_qcschema/tckdb_qcschema",
)

SOURCE_SUFFIXES = (".py", ".sh")

#: Never walked by the audit: caches, build output, and any dot-directory.
_AUDIT_SKIP_DIRS = frozenset({"__pycache__", "build", "dist", "site"})

TEST_TREE_REASON = "test code and data, which run on a developer's machine or in CI and upload nothing"

#: Source files no target covers, and why. A file is declared if its
#: repository-relative path equals a key or lies under one.
UNSCANNED_BY_DESIGN: tuple[tuple[str, str], ...] = (
    (
        "tckdb_qcschema/scripts",
        "fixture generators run by hand; they write test data, not emitted strings",
    ),
    (
        "tools",
        "repository tooling run by a maintainer or by CI; its output is a terminal line, "
        "a generated document or a pull request, never an upload",
    ),
)

ALLOW_MARKER = "# tckdb: allow-non-ascii"

_LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log"})
_LOG_RECEIVERS = frozenset({"logger", "log", "logging", "_logger", "LOGGER"})
_LOG_FACTORIES = frozenset({"getLogger", "get_logger"})
#: Receivers whose ``append``/``add`` collects a message that is written or sent.
_ACCUMULATOR_RECEIVERS = frozenset({"warnings", "errors", "messages", "notes", "sink", "_sink"})
_ACCUMULATOR_METHODS = frozenset({"append", "add"})
_TEXT_KEYWORDS = frozenset({"message", "detail", "reason", "msg"})


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    kind: str
    text: str
    characters: str

    def render(self) -> str:
        snippet = self.text if len(self.text) <= 80 else self.text[:77] + "..."
        return f"{_relative(self.path)}:{self.line}: non-ASCII {self.characters!r} in a {self.kind}: {snippet!r}"


def _relative(path: Path) -> Path:
    try:
        return path.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return path


def _receiver_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_log_call(call: ast.Call) -> bool:
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in _LOG_METHODS:
        return False
    receiver = func.value
    if isinstance(receiver, ast.Call):
        return _receiver_name(receiver.func) in _LOG_FACTORIES
    return _receiver_name(receiver) in _LOG_RECEIVERS


def _is_accumulator_call(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _ACCUMULATOR_METHODS
        and _receiver_name(func.value) in _ACCUMULATOR_RECEIVERS
    )


class _Visitor(ast.NodeVisitor):
    """String constants at an emission site, each recorded once."""

    def __init__(self) -> None:
        self.hits: dict[int, tuple[ast.Constant, str, int]] = {}

    def _record(self, node: ast.AST, kind: str, anchor: int, *, specific: bool = False) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                if id(child) not in self.hits or specific:
                    self.hits[id(child)] = (child, kind, anchor)

    def visit_Raise(self, node: ast.Raise) -> None:
        if node.exc is not None:
            self._record(node.exc, "raised message", node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _is_log_call(node):
            self._record(node, "log message", node.lineno)
        if _is_accumulator_call(node):
            self._record(node, "accumulated warning", node.lineno)
        for keyword in node.keywords:
            if keyword.arg in _TEXT_KEYWORDS:
                self._record(keyword.value, f"{keyword.arg}= argument", node.lineno, specific=True)
        self.generic_visit(node)


def check_source(source: str, path: Path) -> list[Finding]:
    """Findings for one module's *source*."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 0, "unparseable module", str(exc.msg), "")]
    visitor = _Visitor()
    visitor.visit(tree)
    allowed = {n for n, line in enumerate(source.splitlines(), start=1) if ALLOW_MARKER in line}
    findings: list[Finding] = []
    for constant, kind, anchor in visitor.hits.values():
        offenders = sorted({c for c in constant.value if ord(c) > 127})
        if not offenders:
            continue
        span = set(range(constant.lineno, (constant.end_lineno or constant.lineno) + 1)) | {anchor}
        if allowed & span:
            continue
        findings.append(Finding(path, constant.lineno, kind, constant.value, "".join(offenders)))
    return sorted(findings, key=lambda f: (str(f.path), f.line))


def walk(path: Path) -> list[Path]:
    """The ``*.py`` files *path* expands to."""
    return sorted(path.rglob("*.py")) if path.is_dir() else [path]


def check_path(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    for file in walk(path):
        findings.extend(check_source(file.read_text(encoding="utf-8"), file))
    return findings


def default_target_paths() -> list[Path]:
    return [REPO_ROOT / target for target in DEFAULT_TARGETS]


def _is_test_path(relative: str) -> bool:
    parts = relative.split("/")
    return "tests" in parts or parts[-1] == "conftest.py"


def repository_sources() -> list[str]:
    """Every ``*.py`` and ``*.sh`` in the repository, repository-relative."""
    out: list[str] = []
    for path in REPO_ROOT.rglob("*"):
        if path.suffix not in SOURCE_SUFFIXES or path.is_dir():
            continue
        parts = path.relative_to(REPO_ROOT).parts
        if any(p.startswith(".") or p in _AUDIT_SKIP_DIRS or p.endswith(".egg-info") for p in parts[:-1]):
            continue
        out.append("/".join(parts))
    return sorted(out)


def audit() -> tuple[list[str], dict[str, int]]:
    """Source files neither scanned nor declared, and the count per declaration."""
    scanned = default_target_paths()
    counts: dict[str, int] = {"tests/ trees": 0, **{prefix: 0 for prefix, _ in UNSCANNED_BY_DESIGN}}
    undeclared: list[str] = []
    for relative in repository_sources():
        absolute = REPO_ROOT / relative
        if any(target in absolute.parents for target in scanned):
            continue
        if _is_test_path(relative):
            counts["tests/ trees"] += 1
            continue
        for prefix, _ in UNSCANNED_BY_DESIGN:
            if relative == prefix or relative.startswith(prefix + "/"):
                counts[prefix] += 1
                break
        else:
            undeclared.append(relative)
    return undeclared, counts


def run_audit() -> int:
    undeclared, counts = audit()
    for target in default_target_paths():
        print(f"scanned    {len(walk(target)):>4}  {_relative(target)}")
    for label, count in counts.items():
        print(f"declared   {count:>4}  {label}")
    status = 0
    empty = [label for label, count in counts.items() if count == 0]
    if empty:
        print(f"error: declared out of scope but matching no file: {', '.join(empty)}", file=sys.stderr)
        status = 1
    for relative in undeclared:
        print(f"undeclared       {relative}")
    if undeclared:
        print(
            f"\n{len(undeclared)} source file(s) are neither under DEFAULT_TARGETS nor named in "
            "UNSCANNED_BY_DESIGN. Add the tree to one of them.",
            file=sys.stderr,
        )
        status = 1
    if status == 0:
        print("audit: every source file is scanned or declared out of scope")
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("targets", nargs="*", help=f"files or directories (default: {', '.join(DEFAULT_TARGETS)})")
    parser.add_argument("--audit", action="store_true", help="fail on a source file neither scanned nor declared")
    args = parser.parse_args(argv)
    if args.audit:
        if args.targets:
            parser.error("--audit takes no paths")
        return run_audit()

    findings: list[Finding] = []
    for target in [Path(t) for t in args.targets] or default_target_paths():
        if not target.exists():
            print(f"error: no such path: {target}", file=sys.stderr)
            return 2
        files = walk(target)
        if target.is_dir() and not files:
            print(f"error: {target} contains no *.py to check", file=sys.stderr)
            return 2
        for file in files:
            findings.extend(check_source(file.read_text(encoding="utf-8"), file))
        print(f"checked {len(files)} file(s) under {_relative(target)}")
    for finding in findings:
        print(finding.render())
    if findings:
        print(
            f"\n{len(findings)} non-ASCII character(s) in strings that reach a terminal, a sidecar, "
            f"a log or TCKDB. Use ASCII (-- for an em dash, ... for an ellipsis, -> for an arrow), or "
            f"mark the line with `{ALLOW_MARKER}` when the character has to be there.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
