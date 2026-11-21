#!/usr/bin/env python3
"""Resolve and smoke-test one tckdb-adapters distribution for release.

Used by ``.github/workflows/publish-python.yml``. ``PACKAGES`` below is the one
table of what this repository publishes; the workflow's manual-run choices must
name the same distributions (``tools/tests/test_publish_python_workflow.py``).

``resolve`` takes a release tag ``<dist-name>-v<version>`` (or a bare
distribution name for a TestPyPI dry run) and prints ``key=value`` lines for
``$GITHUB_OUTPUT``:

- ``name``, ``version``, ``directory``, ``module``;
- ``deps``: the directories of the distributions in this repository it
  requires, dependencies first, space-separated. The workflow builds these
  from the same commit and installs them with the package, so a requirement
  window that excludes the in-repo version fails the install. Everything else,
  ``tckdb-client`` and ``tckdb-schemas`` included, comes from PyPI.

A tag whose version differs from the package's ``[project].version`` is
refused.

``smoke`` runs inside the clean environment the wheel was installed into. It
imports the module from site-packages, checks the installed version and
``__version__`` (where the module has one), and loads every console-script
entry point.
"""

from __future__ import annotations

import argparse
import importlib
import re
import sys
import sysconfig
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Package:
    name: str  # distribution name, as in [project].name and the release tag
    directory: str  # holds pyproject.toml, relative to the repository root
    module: str  # top-level import name


#: Every distribution this repository publishes, dependencies first.
PACKAGES: tuple[Package, ...] = (
    Package("tckdb-adapters-core", "tckdb_core", "tckdb_core"),
    Package("tckdb-arc", "tckdb_arc", "tckdb_arc"),
    Package("tckdb-chemkin", "tckdb_chemkin", "tckdb_chemkin"),
    Package("tckdb-qcschema", "tckdb_qcschema", "tckdb_qcschema"),
)

TAG_RE = re.compile(r"^(?P<name>tckdb-[a-z0-9]+(?:-[a-z0-9]+)*?)-v(?P<version>[0-9][^\s/]*)$")
_REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


class ReleaseError(ValueError):
    """A tag or package name that cannot be released as given."""


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def package_names() -> list[str]:
    return [package.name for package in PACKAGES]


def package(name: str) -> Package:
    for candidate in PACKAGES:
        if candidate.name == name:
            return candidate
    raise ReleaseError(f"{name!r} is not a released tckdb-adapters distribution; known: {', '.join(package_names())}")


def _project(name: str, root: Path) -> dict:
    return tomllib.loads((root / package(name).directory / "pyproject.toml").read_text())["project"]


def declared_version(name: str, root: Path = REPO_ROOT) -> str:
    return _project(name, root)["version"]


def parse_tag(tag: str) -> tuple[str, str]:
    match = TAG_RE.match(tag)
    if match is None:
        raise ReleaseError(f"tag {tag!r} is not of the form <dist-name>-v<version>")
    name = match.group("name")
    package(name)
    return name, match.group("version")


def in_repo_dependencies(name: str, root: Path = REPO_ROOT) -> list[str]:
    """Distributions of this repository ``name`` requires, transitively, dependencies first."""
    known = {_normalise(n): n for n in package_names()}
    ordered: list[str] = []

    def visit(current: str, trail: tuple[str, ...]) -> None:
        for requirement in _project(current, root).get("dependencies", []):
            match = _REQUIREMENT_NAME_RE.match(requirement)
            dependency = known.get(_normalise(match.group(1))) if match else None
            if dependency is None or dependency in ordered:
                continue
            if dependency in trail:
                raise ReleaseError(f"dependency cycle: {' -> '.join((*trail, dependency))}")
            visit(dependency, (*trail, dependency))
            ordered.append(dependency)

    visit(name, (name,))
    return ordered


def resolve(*, tag: str | None = None, package_name: str | None = None, root: Path = REPO_ROOT) -> dict[str, str]:
    if (tag is None) == (package_name is None):
        raise ReleaseError("give exactly one of a tag or a package name")
    if tag is not None:
        name, tag_version = parse_tag(tag)
    else:
        name, tag_version = package_name, None
    target = package(name)
    version = declared_version(name, root)
    if tag_version is not None and tag_version != version:
        raise ReleaseError(
            f"tag {tag!r} names version {tag_version}, but {target.directory}/pyproject.toml is version {version}; "
            "tag the commit that carries the version you mean to publish"
        )
    return {
        "name": name,
        "version": version,
        "directory": target.directory,
        "module": target.module,
        "deps": " ".join(package(dep).directory for dep in in_repo_dependencies(name, root)),
    }


def smoke(name: str, version: str) -> list[str]:
    """Check the installed distribution; return what was checked."""
    checked: list[str] = []
    installed = metadata.version(name)
    if installed != version:
        raise ReleaseError(f"installed {name} is {installed}, expected {version}")
    module_name = package(name).module
    module = importlib.import_module(module_name)
    site = Path(sysconfig.get_paths()["purelib"]).resolve()
    location = Path(module.__file__).resolve()
    if site not in location.parents:
        raise ReleaseError(f"{module_name} was imported from {location}, not from the installed wheel in {site}")
    checked.append(f"import {module_name} from {location}")
    module_version = getattr(module, "__version__", None)
    if module_version is not None and module_version != version:
        raise ReleaseError(f"{module_name}.__version__ is {module_version}, but the distribution is {version}")
    if module_version is not None:
        checked.append(f"{module_name}.__version__ == {version}")
    for entry_point in metadata.distribution(name).entry_points:
        if entry_point.group == "console_scripts":
            entry_point.load()
            checked.append(f"console script {entry_point.name} -> {entry_point.value}")
    return checked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    resolve_parser = sub.add_parser("resolve", help="print name, version, directory, module and deps")
    which = resolve_parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--tag", help="release tag <dist-name>-v<version>")
    which.add_argument("--package", help="distribution name, for a dry run without a tag")
    smoke_parser = sub.add_parser("smoke", help="check the installed distribution")
    smoke_parser.add_argument("--name", required=True)
    smoke_parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "resolve":
            for key, value in resolve(tag=args.tag, package_name=args.package).items():
                print(f"{key}={value}")
        else:
            for line in smoke(args.name, args.version):
                print(line)
    except (ReleaseError, ImportError, metadata.PackageNotFoundError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
