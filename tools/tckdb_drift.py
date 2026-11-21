#!/usr/bin/env python3
"""Track tckdb-schemas / tckdb-client releases in TCKDB and keep the pins current.

Stdlib only (Python >= 3.11). The TCKDB repo, pinned commit and package
subdirectories live in ``tckdb-pin.toml`` (read by ``ci.yml`` at runtime); this
tool never edits a workflow file. Modes:

``--check``
    Read the adapter's pinned minor lines (schemas, client) and the pinned TCKDB
    commit, resolve TCKDB's ``main`` (repo from ``tckdb-pin.toml``; ``--repo``
    and ``--ref`` override), read the two package versions at that commit, and print
    a JSON summary on stdout plus one human line on stderr. Exit 0 = no drift,
    10 = drift, anything else = error.

``--bump --sha SHA --schemas X.Y.Z --client A.B.C [--allow-downgrade]``
    Move the pins: the ``tckdb-schemas`` / ``tckdb-client`` dependency bounds in the
    ``pyproject.toml`` of every package in ``PACKAGES`` (a package that does not state
    one of the two keeps not stating it, and the comments beside the bounds stay), the
    ``TARGET_SCHEMAS_LINE`` in the core's ``testing/contract.py``, the ``sha`` in
    ``tckdb-pin.toml``, the install lines and "tested contract" sentence in
    ``README.md``, and the version sentence of each package README that has one.
    A package one of whose own files changed in that (its bounds, its README sentence,
    for the core also ``TARGET_SCHEMAS_LINE``) is released: its patch version goes up
    (``pyproject.toml`` and, where it has one, ``__version__``) and its ``CHANGELOG.md``
    gets an entry. When the core is released, every package that requires it gets its
    ``tckdb-adapters-core`` floor raised to the new core version, which releases that
    package too. A package nothing changed in is left alone, so a move of the commit
    alone (same release lines) releases nothing.
    Idempotent: running it again with the same values changes nothing. Refuses to move
    any package's line backwards unless ``--allow-downgrade``.

``--since-output [--from X.Y.Z]``
    Run ``python -m tckdb_schemas.contract --since <from>`` with the installed
    tckdb_schemas and print it. The default ``--from`` is the pinned line of the
    current ``pyproject.toml``, so after a ``--bump`` pass the pre-bump line.

``--install-specs [--sha SHA]``
    Print ``TCKDB_CLIENT_SPEC=`` / ``TCKDB_SCHEMAS_SPEC=`` pip specifiers built from
    ``tckdb-pin.toml`` (for ``$GITHUB_ENV``).

``--list-packages`` / ``--bumped-files``
    Print, one per line, the package directories in ``PACKAGES`` and every file
    ``--bump`` may rewrite (for the drift workflow and the publish script, so neither
    repeats the list).

Drift means an available package release line is *newer* than the pinned one; a
patch release inside the pinned line, or a new commit with the same versions, is
not drift (the pins already allow it).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import urllib.request
from pathlib import Path
from typing import NamedTuple

DEFAULT_REF = "main"
EXIT_DRIFT = 10


class Package(NamedTuple):
    """One package of this repository that a pin bump has to keep consistent.

    ``PACKAGES`` is the list of packages a bump moves. Every path below is derived from
    ``directory`` and ``module``, so moving a package means editing those fields. (The
    publish-to-PyPI list in ``tools/python_release.py`` is a separate table; a test checks
    that the two name the same directories.)
    """

    #: The package's directory, relative to the repository root.
    directory: str
    #: The importable package inside it: ``<directory>/<module>/__init__.py``.
    module: str
    #: The distribution name in ``pyproject.toml``.
    dist: str
    #: ``__init__.py`` holds ``__version__``, which must equal pyproject's.
    version_in_init: bool
    #: ``<directory>/README.md`` has the "`tckdb-client` A.B.x and `tckdb-schemas` X.Y.x" sentence.
    readme_sentence: bool
    #: ``"marker"``: one line under CHANGELOG_MARKER. ``"sections"``: a ``## X.Y.Z`` section, newest first.
    changelog_style: str
    #: What the release section says about the package itself (``sections`` style only).
    note: str = ""

    @property
    def pyproject(self) -> str:
        return f"{self.directory}/pyproject.toml"

    @property
    def init(self) -> str:
        return f"{self.directory}/{self.module}/__init__.py"

    @property
    def readme(self) -> str:
        return f"{self.directory}/README.md"

    @property
    def changelog(self) -> str:
        return f"{self.directory}/CHANGELOG.md"


CORE_DIST = "tckdb-adapters-core"
PACKAGES: tuple[Package, ...] = (
    Package(
        "tckdb_core", "tckdb_core", CORE_DIST, version_in_init=True, readme_sentence=False,
        changelog_style="sections",
        note="The contract kit's `TARGET_SCHEMAS_LINE` is {schemas_line}. Package code is unchanged.",
    ),
    Package(
        "adapters/tckdb_arc", "tckdb_arc", "tckdb-arc", version_in_init=False, readme_sentence=True,
        changelog_style="marker",
    ),
    Package(
        "adapters/tckdb_chemkin", "tckdb_chemkin", "tckdb-chemkin", version_in_init=True, readme_sentence=False,
        changelog_style="sections", note="Package code is unchanged.",
    ),
    Package(
        "adapters/tckdb_qcschema", "tckdb_qcschema", "tckdb-qcschema", version_in_init=True, readme_sentence=False,
        changelog_style="sections",
        note=(
            "Package code is unchanged. The payload records the adapter version, so its request "
            "body differs from {previous}'s; the idempotency keys do not."
        ),
    ),
)
#: The two TCKDB packages whose release lines the pin tracks.
TCKDB_PACKAGES = ("tckdb-schemas", "tckdb-client")
# The marker under which a ``marker``-style changelog gets its "tracked TCKDB ..." line.
CHANGELOG_MARKER = "<!-- tckdb-drift:changelog -->"
PIN_FILE = "tckdb-pin.toml"
README = "README.md"


def core_package() -> Package:
    return next(pkg for pkg in PACKAGES if pkg.dist == CORE_DIST)


#: The one ``TARGET_SCHEMAS_LINE`` line the test suites share (adapters/tckdb_arc/tests/_contract.py re-exports it).
CONTRACT_PY = f"{core_package().directory}/{core_package().module}/testing/contract.py"


def package(directory: str) -> Package:
    for pkg in PACKAGES:
        if pkg.directory == directory:
            return pkg
    raise DriftError(f"{directory} is not in PACKAGES")


def bumped_files() -> list[str]:
    """Every file ``--bump`` may rewrite, relative to the repository root."""
    files: list[str] = []
    for pkg in PACKAGES:
        files.append(pkg.pyproject)
        if pkg.version_in_init:
            files.append(pkg.init)
        if pkg.readme_sentence:
            files.append(pkg.readme)
        files.append(pkg.changelog)
    return files + [CONTRACT_PY, PIN_FILE, README]


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PIN_SHA = re.compile(r'^(sha\s*=\s*")[0-9a-f]{40}(")', re.M)

Line = tuple[int, int]


class DriftError(Exception):
    """A failure that is not drift (bad input, unreachable repository, unparsable file)."""


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", text.strip(), re.ASCII)
    if not m:
        raise DriftError(f"not an X.Y.Z version: {text!r}")
    return int(m[1]), int(m[2]), int(m[3])


def line_of(version: str) -> Line:
    major, minor, _ = parse_version(version)
    return major, minor


def fmt_line(line: Line) -> str:
    return f"{line[0]}.{line[1]}"


def bound_for(line: Line) -> str:
    """``>=X.Y,<X.(Y+1)`` for a minor line."""
    return f">={line[0]}.{line[1]},<{line[0]}.{line[1] + 1}"


# ---------------------------------------------------------------------------
# Reading the adapter's pins
# ---------------------------------------------------------------------------


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _dependency_spec(pyproject_text: str, package: str) -> str | None:
    """The version specifier of ``package`` (extras and markers removed, spaces dropped), or None."""
    deps = tomllib.loads(pyproject_text)["project"].get("dependencies", [])
    for dep in deps:
        m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^;]*)", dep)
        if m and _canonical(m[1]) == _canonical(package):
            return m[2].replace(" ", "")
    return None


def states_dependency(pyproject_text: str, package: str) -> bool:
    return _dependency_spec(pyproject_text, package) is not None


def pinned_line(pyproject_text: str, package: str) -> Line:
    """The minor line a ``pkg>=X.Y,<X.(Y+1)`` dependency pins."""
    spec = _dependency_spec(pyproject_text, package)
    if spec is None:
        raise DriftError(f"no {package} dependency in the pyproject.toml text")
    m = re.fullmatch(r">=([0-9]+)\.([0-9]+)(?:\.[0-9]+)?,<([0-9]+)\.([0-9]+)(?:\.[0-9]+)?", spec, re.ASCII)
    if not m or (int(m[3]), int(m[4])) != (int(m[1]), int(m[2]) + 1):
        raise DriftError(f"{package} is not pinned to a single minor line: {package}{spec!r}")
    return int(m[1]), int(m[2])


def read_pin_file(text: str) -> dict:
    """The ``[tckdb]`` table of tckdb-pin.toml, validated."""
    try:
        table = tomllib.loads(text)["tckdb"]
    except (tomllib.TOMLDecodeError, KeyError) as exc:
        raise DriftError(f"cannot read the [tckdb] table of {PIN_FILE}: {exc!r}") from exc
    for key in ("repo", "sha", "schemas_subdir", "client_subdir"):
        if not isinstance(table.get(key), str) or not table[key]:
            raise DriftError(f"{PIN_FILE} [tckdb] needs a non-empty string {key!r}")
    if not SHA_RE.match(table["sha"]):
        raise DriftError(f"{PIN_FILE} sha is not a 40-character lowercase hex commit")
    return table


def lowest_line(texts: dict[str, str], package: str) -> Line:
    """The oldest minor line any of the ``pyproject.toml`` texts pins ``package`` to.

    A package that does not state the dependency is skipped; one that states it must pin a
    single minor line. It is an error when none states it.
    """
    lines = [pinned_line(text, package) for text in texts.values() if states_dependency(text, package)]
    if not lines:
        raise DriftError(f"no package in this repository states a {package} dependency")
    return min(lines)


def read_pyprojects(root: Path) -> dict[str, str]:
    return {pkg.pyproject: (root / pkg.pyproject).read_text() for pkg in PACKAGES}


def read_pins(root: Path) -> dict:
    texts = read_pyprojects(root)
    # The oldest pin across the packages counts as "pinned", so one stale package reports drift.
    return {
        "schemas_line": lowest_line(texts, "tckdb-schemas"),
        "client_line": lowest_line(texts, "tckdb-client"),
        "sha": read_pin_file((root / PIN_FILE).read_text())["sha"],
    }


def package_version_of(pyproject_text: str) -> str:
    return tomllib.loads(pyproject_text)["project"]["version"]


# ---------------------------------------------------------------------------
# Reading the TCKDB repository
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if proc.returncode != 0:
        raise DriftError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def resolve_sha(repo: str, ref: str) -> str:
    """Resolve ``ref`` (branch, tag or full SHA) on ``repo`` to a commit SHA."""
    if SHA_RE.match(ref):
        return ref
    out = _git("ls-remote", repo, ref, f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}")
    found = {}
    for row in out.splitlines():
        sha, _, name = row.partition("\t")
        found[name] = sha
    for name in (f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", ref):
        if name in found:
            return found[name]
    raise DriftError(f"{repo} has no ref {ref!r}")


def _fetch_by_git(repo: str, sha: str, paths: list[str]) -> dict[str, str]:
    """Shallow-fetch one commit into a temp repo and read ``paths`` from it.

    GitHub does not support ``git archive --remote``, so this fetches the single
    commit (blobless when the server allows it) rather than cloning history.
    """
    tmp = Path(tempfile.mkdtemp(prefix="tckdb-drift-"))
    try:
        _git("init", "-q", cwd=tmp)
        try:
            _git("fetch", "-q", "--depth", "1", "--filter=blob:none", repo, sha, cwd=tmp)
        except DriftError:
            _git("fetch", "-q", "--depth", "1", repo, sha, cwd=tmp)
        return {p: _git("show", f"FETCH_HEAD:{p}", cwd=tmp) for p in paths}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fetch_by_raw_url(repo: str, sha: str, paths: list[str]) -> dict[str, str]:
    m = re.match(r"^(?:https://github\.com/|git@github\.com:)([^/]+)/(.+?)(?:\.git)?/?$", repo)
    if not m:
        raise DriftError(f"raw-URL fallback only supports github.com repos, got {repo!r}")
    owner, name = m[1], m[2]
    out = {}
    for p in paths:
        url = f"https://raw.githubusercontent.com/{owner}/{name}/{sha}/{p}"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (https, fixed host)
                out[p] = resp.read().decode()
        except OSError as exc:
            raise DriftError(f"could not fetch {url}: {exc}") from exc
    return out


def fetch_files(repo: str, sha: str, paths: list[str]) -> dict[str, str]:
    try:
        return _fetch_by_git(repo, sha, paths)
    except DriftError as git_err:
        try:
            return _fetch_by_raw_url(repo, sha, paths)
        except DriftError as raw_err:
            raise DriftError(f"{git_err}; fallback: {raw_err}") from raw_err


def package_version(pyproject_text: str, path: str) -> str:
    try:
        version = tomllib.loads(pyproject_text)["project"]["version"]
    except (tomllib.TOMLDecodeError, KeyError) as exc:
        raise DriftError(f"cannot read project.version from {path}: {exc!r}") from exc
    if not isinstance(version, str):
        raise DriftError(f"project.version in {path} is not a string")
    return "{}.{}.{}".format(*parse_version(version))


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------


def build_summary(pins: dict, repo: str, ref: str, sha: str, schemas_v: str, client_v: str) -> dict:
    def entry(pinned: Line, available: str) -> dict:
        avail_line = line_of(available)
        return {
            "pinned_line": fmt_line(pinned),
            "available_version": available,
            "available_line": fmt_line(avail_line),
            "newer": avail_line > pinned,
        }

    schemas = entry(pins["schemas_line"], schemas_v)
    client = entry(pins["client_line"], client_v)
    return {
        "repo": repo,
        "ref": ref,
        "pinned_sha": pins["sha"],
        "available_sha": sha,
        "available_sha7": sha[:7],
        "sha_changed": sha != pins["sha"],
        "schemas": schemas,
        "client": client,
        "drift": schemas["newer"] or client["newer"],
    }


def human_line(summary: dict) -> str:
    s, c = summary["schemas"], summary["client"]
    state = "DRIFT" if summary["drift"] else "no drift"
    return (
        f"{state}: tckdb-schemas {s['pinned_line']}.x -> {s['available_version']}, "
        f"tckdb-client {c['pinned_line']}.x -> {c['available_version']} "
        f"(TCKDB {summary['pinned_sha'][:7]} -> {summary['available_sha7']})"
    )


def run_check(root: Path, repo: str, ref: str) -> int:
    pins = read_pins(root)
    pin = read_pin_file((root / PIN_FILE).read_text())
    repo = repo or pin["repo"]
    sha = resolve_sha(repo, ref)
    schemas_path = f"{pin['schemas_subdir']}/pyproject.toml"
    client_path = f"{pin['client_subdir']}/pyproject.toml"
    files = fetch_files(repo, sha, [schemas_path, client_path])
    summary = build_summary(
        pins, repo, ref, sha,
        package_version(files[schemas_path], schemas_path),
        package_version(files[client_path], client_path),
    )
    print(json.dumps(summary, indent=2))
    print(human_line(summary), file=sys.stderr)
    return EXIT_DRIFT if summary["drift"] else 0


# ---------------------------------------------------------------------------
# --bump
# ---------------------------------------------------------------------------


def rewrite_pyproject(text: str, schemas_line: Line, client_line: Line, where: str) -> str:
    """Replace each TCKDB package's version specifier, keeping its extras, marker and comment.

    A package whose ``pyproject.toml`` does not state one of the two keeps not stating it.
    """
    stated = 0
    for package, line in (("tckdb-schemas", schemas_line), ("tckdb-client", client_line)):
        if not states_dependency(text, package):
            continue
        stated += 1
        text, n = re.subn(
            rf'"({re.escape(package)}(?:\s*\[[^\]"]*\])?)[^";]*?(\s*;[^"]*)?"',
            lambda m, line=line: f'"{m[1]}{bound_for(line)}{m[2] or ""}"',
            text, count=1,
        )
        if n != 1:
            raise DriftError(f'no "{package}..." dependency string in {where}')
    if not stated:
        raise DriftError(f"{where} states neither tckdb-schemas nor tckdb-client")
    return text


def rewrite_core_floor(text: str, core_version: str, where: str) -> str:
    """Raise the lower bound of a ``tckdb-adapters-core>=X.Y.Z,<...`` dependency to ``core_version``.

    Text that does not state the dependency is returned as it is; one that states it with a
    lower bound this cannot rewrite is an error.
    """
    if not states_dependency(text, CORE_DIST):
        return text
    new, n = re.subn(
        rf'("{re.escape(CORE_DIST)}(?:\s*\[[^\]"]*\])?\s*>=\s*)\d+(?:\.\d+)*(?=[\s,;"])',
        lambda m: f"{m[1]}{core_version}",
        text, count=1,
    )
    if n != 1:
        raise DriftError(f'cannot find a "{CORE_DIST}>=X.Y.Z" lower bound to raise in {where}')
    return new


def bump_patch(text: str, where: str) -> str:
    """Increment the patch of the first top-level-looking ``version = "X.Y.Z"``."""
    def repl(m: re.Match) -> str:
        return f'{m[1]}{m[2]}.{m[3]}.{int(m[4]) + 1}{m[5]}'

    new, n = re.subn(r'^(version\s*=\s*")(\d+)\.(\d+)\.(\d+)(")', repl, text, count=1, flags=re.M)
    if n != 1:
        raise DriftError(f"no version = \"X.Y.Z\" line in {where}")
    return new


def set_module_version(text: str, version: str, where: str) -> str:
    new, n = re.subn(r'^(__version__\s*=\s*")[^"]*(")', lambda m: f"{m[1]}{version}{m[2]}", text, count=1, flags=re.M)
    if n != 1:
        raise DriftError(f'no __version__ = "..." line in {where}')
    return new


def rewrite_contract_py(text: str, schemas_line: Line) -> str:
    new, n = re.subn(
        r"^(TARGET_SCHEMAS_LINE\s*=\s*)\(\d+,\s*\d+\)",
        lambda m: f"{m[1]}({schemas_line[0]}, {schemas_line[1]})",
        text, count=1, flags=re.M,
    )
    if n != 1:
        raise DriftError(f"no TARGET_SCHEMAS_LINE assignment in {CONTRACT_PY}")
    return new


def rewrite_pin_file(text: str, sha: str) -> str:
    """Set ``sha`` inside the ``[tckdb]`` table only (not a same-named key elsewhere)."""
    read_pin_file(text)
    header = re.search(r"^\[tckdb\][ \t]*(?:#.*)?$", text, re.M)
    if header is None:
        raise DriftError(f"no [tckdb] table header in {PIN_FILE}")
    nxt = re.search(r"^\[", text[header.end():], re.M)
    end = header.end() + nxt.start() if nxt else len(text)
    body, n = re.subn(r'^(sha\s*=\s*")[0-9a-f]{40}(")', lambda m: f"{m[1]}{sha}{m[2]}",
                      text[header.end():end], count=1, flags=re.M)
    if n != 1:
        raise DriftError(f'no sha = "..." line under [tckdb] in {PIN_FILE}')
    return text[:header.end()] + body + text[end:]


def rewrite_sha(text: str, repo: str, sha: str, where: str) -> str:
    """Replace the commit in ``<repo>@<sha>`` install lines (``repo`` from the pin file)."""
    new, n = re.subn(rf"({re.escape(repo)}@)[0-9a-f]{{40}}", lambda m: f"{m[1]}{sha}", text)
    if n == 0:
        raise DriftError(f"no pinned {repo}@<sha> install line to rewrite in {where}")
    return new


def rewrite_readme(text: str, repo: str, sha: str, schemas_line: Line, client_line: Line) -> str:
    text = rewrite_sha(text, repo, sha, README)
    new, n = re.subn(
        r"(`tckdb-client` )\d+\.\d+(\.x with `tckdb-schemas` )\d+\.\d+(\.x)",
        lambda m: f"{m[1]}{fmt_line(client_line)}{m[2]}{fmt_line(schemas_line)}{m[3]}",
        text, count=1,
    )
    if n != 1:
        raise DriftError(f"no 'tested contract' sentence to rewrite in {README}")
    return new


def rewrite_pkg_readme(text: str, schemas_line: Line, client_line: Line, where: str) -> str:
    new, n = re.subn(
        r"(`tckdb-client` )\d+\.\d+(\.x and `tckdb-schemas` )\d+\.\d+(\.x)",
        lambda m: f"{m[1]}{fmt_line(client_line)}{m[2]}{fmt_line(schemas_line)}{m[3]}",
        text, count=1,
    )
    if n != 1:
        raise DriftError(f"no version sentence to rewrite in {where}")
    return new


def add_changelog_line(text: str, line: str, where: str) -> str:
    if CHANGELOG_MARKER not in text:
        raise DriftError(f"no {CHANGELOG_MARKER} marker in {where}")
    if line in text:
        return text
    return text.replace(CHANGELOG_MARKER, f"{CHANGELOG_MARKER}\n{line}", 1)


def add_changelog_section(text: str, version: str, body: str, where: str) -> str:
    """Insert ``## <version>`` above the newest *version* heading (not ``## Unreleased`` and the like)."""
    if re.search(rf"^## {re.escape(version)}\s*$", text, re.M):
        raise DriftError(f"{where} already has a ## {version} section")
    section = f"## {version}\n\n{body}\n\n"
    first = re.search(r"^## v?\d+\.\d+\.\d+", text, re.M)
    if first is None:
        return text.rstrip("\n") + "\n\n" + section.rstrip("\n") + "\n"
    return text[:first.start()] + section + text[first.start():]


def release_body(pkg: Package, old_text: str, new_text: str, previous: str, sha: str,
                 schemas: str, client: str, schemas_line: Line) -> str:
    """The prose of a ``sections``-style changelog entry for a pin bump."""
    now, was = [], []
    for name in ("tckdb-client", "tckdb-schemas", CORE_DIST):
        new_spec, old_spec = _dependency_spec(new_text, name), _dependency_spec(old_text, name)
        if new_spec is not None and new_spec != old_spec:
            now.append(f"`{name}{new_spec}`")
            was.append(f"`{old_spec}`")
    parts = [f"Tracks TCKDB {sha[:7]} (tckdb-schemas {schemas}, tckdb-client {client})."]
    if now:
        parts.append(f"Requires {' and '.join(now)} ({'was' if len(now) == 1 else 'were'} {' and '.join(was)}).")
    parts.append(pkg.note.format(schemas_line=fmt_line(schemas_line), previous=previous))
    return textwrap.fill(" ".join(p for p in parts if p), width=100, break_long_words=False, break_on_hyphens=False)


def run_bump(root: Path, sha: str, schemas: str, client: str, allow_downgrade: bool = False) -> int:
    if not SHA_RE.match(sha):
        raise DriftError(f"--sha must be a full 40-character lowercase hex commit, got {sha!r}")
    schemas, client = "{}.{}.{}".format(*parse_version(schemas)), "{}.{}.{}".format(*parse_version(client))
    schemas_line, client_line = line_of(schemas), line_of(client)

    old = {rel: (root / rel).read_text() for rel in bumped_files()}
    if not allow_downgrade:
        for pkg in PACKAGES:
            for package, new_line in (("tckdb-schemas", schemas_line), ("tckdb-client", client_line)):
                if not states_dependency(old[pkg.pyproject], package):
                    continue
                current = pinned_line(old[pkg.pyproject], package)
                if new_line < current:
                    raise DriftError(
                        f"refusing to move {package} in {pkg.pyproject} back from {fmt_line(current)} "
                        f"to {fmt_line(new_line)}; pass --allow-downgrade if that is intended"
                    )
    repo = read_pin_file(old[PIN_FILE])["repo"]
    new = dict(old)
    for pkg in PACKAGES:
        new[pkg.pyproject] = rewrite_pyproject(old[pkg.pyproject], schemas_line, client_line, pkg.pyproject)
        if pkg.readme_sentence:
            new[pkg.readme] = rewrite_pkg_readme(old[pkg.readme], schemas_line, client_line, pkg.readme)
    new[CONTRACT_PY] = rewrite_contract_py(old[CONTRACT_PY], schemas_line)
    new[PIN_FILE] = rewrite_pin_file(old[PIN_FILE], sha)
    new[README] = rewrite_readme(old[README], repo, sha, schemas_line, client_line)

    def own_files(pkg: Package) -> list[str]:
        files = [pkg.pyproject]
        if pkg.readme_sentence:
            files.append(pkg.readme)
        if pkg.dist == CORE_DIST:
            files.append(CONTRACT_PY)
        return files

    # A package is released only when one of its own files changed apart from the version itself.
    released = {pkg.directory: any(new[rel] != old[rel] for rel in own_files(pkg)) for pkg in PACKAGES}
    core = core_package()
    versions: dict[str, tuple[str, str]] = {}  # directory -> (old, new)

    def release(pkg: Package) -> None:
        before = package_version_of(old[pkg.pyproject])
        new[pkg.pyproject] = bump_patch(new[pkg.pyproject], pkg.pyproject)
        versions[pkg.directory] = (before, package_version_of(new[pkg.pyproject]))

    if released[core.directory]:
        release(core)
        # The new core carries the new bounds, so whoever requires the core must not be
        # released against one that predates them: raise its floor (which releases it).
        for pkg in PACKAGES:
            if pkg is core:
                continue
            raised = rewrite_core_floor(new[pkg.pyproject], versions[core.directory][1], pkg.pyproject)
            if raised != new[pkg.pyproject]:
                new[pkg.pyproject] = raised
                released[pkg.directory] = True
    for pkg in PACKAGES:
        if pkg is not core and released[pkg.directory]:
            release(pkg)

    for pkg in PACKAGES:
        if pkg.directory not in versions:
            continue
        before, after = versions[pkg.directory]
        if pkg.version_in_init:
            new[pkg.init] = set_module_version(old[pkg.init], after, pkg.init)
        if pkg.changelog_style == "marker":
            new[pkg.changelog] = add_changelog_line(
                old[pkg.changelog],
                f"- Adapter {after}: tracked TCKDB {sha[:7]} (schemas {schemas}, client {client}).",
                pkg.changelog,
            )
        else:
            body = release_body(pkg, old[pkg.pyproject], new[pkg.pyproject], before, sha,
                                schemas, client, schemas_line)
            new[pkg.changelog] = add_changelog_section(old[pkg.changelog], after, body, pkg.changelog)

    changed = [rel for rel in new if new[rel] != old[rel]]
    for rel in changed:
        (root / rel).write_text(new[rel])
    if not changed:
        print("already up to date; nothing changed")
        return 0
    print(
        f"schemas -> {bound_for(schemas_line)} (target {fmt_line(schemas_line)}), "
        f"client -> {bound_for(client_line)}, TCKDB {sha[:7]}"
    )
    for pkg in PACKAGES:
        if pkg.directory in versions:
            print(f"  {pkg.dist} {versions[pkg.directory][0]} -> {versions[pkg.directory][1]}")
        else:
            print(f"  {pkg.dist} unchanged")
    for rel in changed:
        print(f"  changed {rel}")
    return 0


def run_install_specs(root: Path, sha: str | None) -> int:
    pin = read_pin_file((root / PIN_FILE).read_text())
    sha = sha or pin["sha"]
    if not SHA_RE.match(sha):
        raise DriftError(f"--sha must be a full 40-character lowercase hex commit, got {sha!r}")
    base = f"git+{pin['repo']}@{sha}#subdirectory="
    print(f"TCKDB_CLIENT_SPEC=tckdb-client @ {base}{pin['client_subdir']}")
    print(f"TCKDB_SCHEMAS_SPEC=tckdb-schemas @ {base}{pin['schemas_subdir']}")
    return 0


# ---------------------------------------------------------------------------
# --since-output
# ---------------------------------------------------------------------------


def run_since_output(root: Path, since: str | None) -> int:
    if since is None:
        line = lowest_line(read_pyprojects(root), "tckdb-schemas")
        since = f"{fmt_line(line)}.0"
    parse_version(since)
    proc = subprocess.run(
        [sys.executable, "-m", "tckdb_schemas.contract", "--since", since],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        print(proc.stdout, end="")
        print(proc.stderr, end="", file=sys.stderr)
        raise DriftError(f"tckdb_schemas.contract --since {since} exited {proc.returncode}")
    print(proc.stdout, end="")
    return 0


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="report drift vs TCKDB's main (exit 10 on drift)")
    mode.add_argument("--bump", action="store_true", help="rewrite the pins (needs --sha --schemas --client)")
    mode.add_argument("--since-output", action="store_true", help="print tckdb_schemas.contract --since <pin>")
    mode.add_argument("--install-specs", action="store_true", help="print pip specifiers from tckdb-pin.toml")
    mode.add_argument("--list-packages", action="store_true", help="print the package directories --bump moves")
    mode.add_argument("--bumped-files", action="store_true", help="print every file --bump may rewrite")
    p.add_argument("--repo", default=None, help="TCKDB repository, overriding the repo in tckdb-pin.toml")
    p.add_argument("--ref", default=DEFAULT_REF, help="branch, tag or SHA to resolve (default: %(default)s)")
    p.add_argument("--sha", help="--bump: full commit SHA to pin")
    p.add_argument("--schemas", help="--bump: available tckdb-schemas version X.Y.Z")
    p.add_argument("--client", help="--bump: available tckdb-client version A.B.C")
    p.add_argument("--allow-downgrade", action="store_true", help="--bump: allow moving a line backwards")
    p.add_argument("--from", dest="since", help="--since-output: base version (default: pinned line .0)")
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    try:
        if args.check:
            return run_check(args.root, args.repo, args.ref)
        if args.bump:
            if not (args.sha and args.schemas and args.client):
                p.error("--bump needs --sha, --schemas and --client")
            return run_bump(args.root, args.sha, args.schemas, args.client, args.allow_downgrade)
        if args.install_specs:
            return run_install_specs(args.root, args.sha)
        if args.list_packages:
            print("\n".join(pkg.directory for pkg in PACKAGES))
            return 0
        if args.bumped_files:
            print("\n".join(bumped_files()))
            return 0
        return run_since_output(args.root, args.since)
    except DriftError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
