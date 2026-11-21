"""Unit tests for tools/tckdb_drift.py: pin parsing and rewriting. No network."""

import difflib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

import _drift_fixture as fx

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("tckdb_drift", REPO / "tools" / "tckdb_drift.py")
drift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drift)

NEW_SHA = "74aae9a2" + "0" * 32
FILES = fx.BUMPED
UNTOUCHED = [rel for rel in fx.FILES if rel not in fx.BUMPED]


@pytest.fixture
def root(tmp_path):
    """A synthetic repo with fixed pins (0.54 / 0.95); never the checkout's real files."""
    return fx.build(tmp_path)


def _snapshot(root):
    return {rel: (root / rel).read_text() for rel in FILES}


def test_line_and_bound_helpers():
    assert drift.line_of("0.58.3") == (0, 58)
    assert drift.bound_for((0, 58)) == ">=0.58,<0.59"
    with pytest.raises(drift.DriftError):
        drift.parse_version("0.58")


def test_read_pins_from_the_synthetic_repo(root):
    assert drift.read_pins(root) == {
        "schemas_line": fx.PINNED_SCHEMAS, "client_line": fx.PINNED_CLIENT, "sha": fx.OLD_SHA,
    }


@pytest.mark.parametrize("spec", ["tckdb-schemas>=0.54", "tckdb-schemas>=0.54,<0.56", "tckdb-schemas==0.54.0"])
def test_pinned_line_refuses_anything_but_one_minor_line(spec):
    text = f'[project]\ndependencies = ["{spec}"]\n'
    with pytest.raises(drift.DriftError):
        drift.pinned_line(text, "tckdb-schemas")


# The one deliberate read of the real files: a structural check that holds for any pin value.
def test_real_pin_file_parses_and_no_workflow_has_a_literal_sha():
    pin = drift.read_pin_file((REPO / drift.PIN_FILE).read_text())
    assert drift.SHA_RE.match(pin["sha"])
    assert pin["repo"].startswith("https://") and pin["schemas_subdir"] and pin["client_subdir"]
    # ci.yml reads the pin at runtime, so a bump never edits a workflow file. An action pinned
    # by commit (``uses: owner/repo@<sha>``, as publish-python.yml pins the PyPI upload) is not
    # a TCKDB revision and is allowed.
    for workflow in (REPO / ".github" / "workflows").glob("*.yml"):
        text = re.sub(r"uses:\s*[\w.-]+/[\w./-]+@[0-9a-f]{40}\b", "uses: <action>", workflow.read_text())
        assert not re.search(r"\b[0-9a-f]{40}\b", text), workflow.name


@pytest.mark.parametrize("bad", ['sha = "abc"', 'repo = ""', "[other]\nx = 1"])
def test_pin_file_rejects_bad_content(bad):
    text = "[tckdb]\n" + bad + "\n" if not bad.startswith("[") else bad
    with pytest.raises(drift.DriftError):
        drift.read_pin_file(text)


def _pins():
    return {"schemas_line": (0, 54), "client_line": (0, 95), "sha": "f" * 40}


def _summary(schemas, client):
    return drift.build_summary(_pins(), "r", "main", "a" * 40, schemas, client)


def test_summary_no_drift_inside_the_pinned_lines():
    s = _summary("0.54.9", "0.95.4")
    assert s["drift"] is False and not s["schemas"]["newer"] and not s["client"]["newer"]
    assert s["sha_changed"] is True  # a new commit alone is not drift


def test_summary_drift_is_per_package():
    s = _summary("0.54.0", "0.98.0")
    assert (s["schemas"]["newer"], s["client"]["newer"], s["drift"]) == (False, True, True)
    s = _summary("0.58.0", "0.95.1")
    assert (s["schemas"]["newer"], s["client"]["newer"], s["drift"]) == (True, False, True)


def test_summary_older_upstream_is_not_drift():
    assert _summary("0.53.0", "0.94.0")["drift"] is False


ARC, CORE, CHEMKIN, QCSCHEMA = (drift.package(d) for d in ("adapters/tckdb_arc", "tckdb_core", "adapters/tckdb_chemkin", "adapters/tckdb_qcschema"))
SECTION_PACKAGES = (CORE, CHEMKIN, QCSCHEMA)


def _version(text):
    return drift.parse_version(drift.package_version_of(text))


def test_bump_rewrites_only_the_pins_and_versions(root):
    before = {rel: (root / rel).read_text() for rel in FILES + UNTOUCHED}
    assert drift.run_bump(root, NEW_SHA, "0.58.0", "0.98.0") == 0
    after = {rel: (root / rel).read_text() for rel in FILES + UNTOUCHED}

    changed = {rel for rel in before if before[rel] != after[rel]}
    assert changed == set(FILES)  # no workflow file, no AGENTS.md or CLAUDE.md, no chemkin/qcschema README

    for pkg in drift.PACKAGES:
        text = after[pkg.pyproject]
        if pkg is not CHEMKIN:
            assert drift.pinned_line(text, "tckdb-schemas") == (0, 58), pkg.directory
        assert drift.pinned_line(text, "tckdb-client") == (0, 98), pkg.directory
    assert "TARGET_SCHEMAS_LINE = (0, 58)" in after[drift.CONTRACT_PY]
    assert drift.read_pin_file(after[drift.PIN_FILE])["sha"] == NEW_SHA
    assert NEW_SHA in after[drift.README]
    assert "`tckdb-client` 0.98.x with `tckdb-schemas` 0.58.x" in after[drift.README]
    assert "`tckdb-client` 0.98.x and `tckdb-schemas` 0.58.x" in after[ARC.readme]
    assert drift.CHANGELOG_MARKER not in after[ARC.readme]

    # Every package moves one patch, in pyproject and (where it has one) in __version__.
    for pkg in drift.PACKAGES:
        old_v, new_v = _version(before[pkg.pyproject]), _version(after[pkg.pyproject])
        assert new_v == (old_v[0], old_v[1], old_v[2] + 1), pkg.directory
        if pkg.version_in_init:
            assert f'__version__ = "{new_v[0]}.{new_v[1]}.{new_v[2]}"' in after[pkg.init], pkg.directory

    # The ARC adapter's changelog gets a marker line; the others get a new section on top.
    arc_v = ".".join(map(str, _version(after[ARC.pyproject])))
    entry = f"- Adapter {arc_v}: tracked TCKDB 74aae9a (schemas 0.58.0, client 0.98.0)."
    assert f"{drift.CHANGELOG_MARKER}\n{entry}\n" in after[ARC.changelog]
    for pkg in SECTION_PACKAGES:
        v = ".".join(map(str, _version(after[pkg.pyproject])))
        old_v = ".".join(map(str, _version(before[pkg.pyproject])))
        text, old_text = after[pkg.changelog], before[pkg.changelog]
        first = re.search(r"^## (.*)$", text, re.M)
        assert first[1] == v, pkg.directory  # newest first, above the previous release
        section = text[first.start():text.index(f"## {old_v}")]
        assert "Tracks TCKDB 74aae9a (tckdb-schemas 0.58.0, tckdb-client 0.98.0)." in section
        assert "`tckdb-client>=0.98,<0.99`" in section
        assert ("(was `" if pkg is CHEMKIN else "(were `") in section  # one bound moved, or two
        assert text.startswith(old_text.split("## ")[0])  # the heading and intro are untouched
        assert text.endswith(old_text[old_text.index("## "):])  # so is the history
    assert "`TARGET_SCHEMAS_LINE` is 0.58" in after[CORE.changelog]
    assert "`tckdb-schemas>=0.58,<0.59`" in after[QCSCHEMA.changelog].split("## 0.9.9")[0]

    # The ARC adapter's floor on the core follows the core's new version.
    assert '"tckdb-adapters-core>=0.1.1,<0.2",  # pipeline' in after[ARC.pyproject]

    # Nothing else moved, in any bumped file.
    for rel in FILES:
        _assert_only_expected_lines_changed(rel, before[rel], after[rel])


KEYWORDS = ("tckdb-", "version =", "__version__", "TARGET_SCHEMAS_LINE", "tckdb.git@", "sha =")


def _assert_only_expected_lines_changed(rel, before, after):
    """A changelog only gains lines. Every other file keeps its line count, and each line that
    changed was a pin, the sha, a version, the core floor or a version sentence."""
    ops = [op for op in difflib.SequenceMatcher(None, before.splitlines(), after.splitlines()).get_opcodes()
           if op[0] != "equal"]
    assert ops, rel
    old, new = before.splitlines(), after.splitlines()
    for tag, i1, i2, j1, j2 in ops:
        if rel.endswith("CHANGELOG.md"):
            assert tag == "insert", (rel, tag, old[i1:i2])
        else:
            assert tag == "replace" and i2 - i1 == j2 - j1, (rel, tag, old[i1:i2], new[j1:j2])
            for o in old[i1:i2]:
                assert any(k in o for k in KEYWORDS), (rel, o)


@pytest.mark.parametrize("pkg", drift.PACKAGES, ids=lambda p: p.directory)
def test_a_stale_package_reports_drift_in_check(root, monkeypatch, pkg):
    """Every package that states a bound counts: the oldest pin is the pin."""
    path = root / pkg.pyproject
    text = path.read_text()
    for package in drift.TCKDB_PACKAGES:
        if drift.states_dependency(text, package):
            line = drift.pinned_line(text, package)
            text = re.sub(rf'"{package}[^"]*"', f'"{package}{drift.bound_for((line[0], line[1] - 1))}"', text)
    path.write_text(text)
    reported = drift.read_pins(root)
    for package, key, pinned in (("tckdb-schemas", "schemas_line", fx.PINNED_SCHEMAS),
                                 ("tckdb-client", "client_line", fx.PINNED_CLIENT)):
        stale = drift.states_dependency(text, package)
        assert reported[key] == ((pinned[0], pinned[1] - 1) if stale else pinned), (pkg.directory, key)
    pin = drift.read_pin_file((root / drift.PIN_FILE).read_text())
    available = {
        f"{pin['schemas_subdir']}/pyproject.toml": '[project]\nversion = "%d.%d.9"\n' % fx.PINNED_SCHEMAS,
        f"{pin['client_subdir']}/pyproject.toml": '[project]\nversion = "%d.%d.9"\n' % fx.PINNED_CLIENT,
    }
    monkeypatch.setattr(drift, "resolve_sha", lambda repo, ref: fx.OLD_SHA)
    monkeypatch.setattr(drift, "fetch_files", lambda repo, sha, paths: available)
    assert drift.run_check(root, None, "main") == drift.EXIT_DRIFT


def test_bump_repairs_a_package_that_lags(root):
    path = root / QCSCHEMA.pyproject
    path.write_text(path.read_text().replace(">=0.54,<0.55", ">=0.53,<0.54").replace(">=0.95,<0.96", ">=0.94,<0.95"))
    assert drift.read_pins(root) == {"schemas_line": (0, 53), "client_line": (0, 94), "sha": fx.OLD_SHA}
    drift.run_bump(root, fx.OLD_SHA, "0.54.0", "0.95.0")
    assert drift.read_pins(root) == {"schemas_line": (0, 54), "client_line": (0, 95), "sha": fx.OLD_SHA}


def test_the_tools_file_list_is_the_fixtures():
    assert sorted(drift.bumped_files()) == sorted(fx.BUMPED)


def _code_lines(path):
    """The file without its comment lines (a comment may point at a test that will move)."""
    return "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))


def test_the_publish_script_stages_what_the_tool_lists_and_names_no_package():
    script = _code_lines(REPO / "tools" / "tckdb_drift_publish.sh")
    assert "tools/tckdb_drift.py --bumped-files" in script
    for pkg in drift.PACKAGES:
        assert pkg.directory not in script, pkg.directory  # the tool is the one source of the file list


def test_the_workflow_takes_its_packages_from_the_tool():
    text = _code_lines(REPO / ".github" / "workflows" / "tckdb-drift.yml")
    assert "tools/tckdb_drift.py --list-packages" in text
    for pkg in drift.PACKAGES:
        assert pkg.directory not in text, pkg.directory


def test_list_modes_print_the_tables(capsys):
    assert drift.main(["--list-packages"]) == 0
    assert capsys.readouterr().out.split() == [pkg.directory for pkg in drift.PACKAGES]
    assert drift.main(["--bumped-files"]) == 0
    assert capsys.readouterr().out.split() == drift.bumped_files()


def test_the_release_tool_and_the_drift_tool_name_the_same_package_directories():
    spec = importlib.util.spec_from_file_location("python_release", REPO / "tools" / "python_release.py")
    release = importlib.util.module_from_spec(spec)
    sys.modules["python_release"] = release
    spec.loader.exec_module(release)
    assert {p.directory for p in release.PACKAGES} == {p.directory for p in drift.PACKAGES}
    assert {p.directory: p.module for p in release.PACKAGES} == {p.directory: p.module for p in drift.PACKAGES}


SKIPPED_DIRS = {".git", ".claude", ".venv", "venv", "env", "build", "dist", "site", "node_modules", "__pycache__"}


def depends_on_a_tckdb_package(pyproject: Path) -> bool:
    import tomllib

    deps = tomllib.loads(pyproject.read_text()).get("project", {}).get("dependencies", [])
    names = {re.split(r"[\s<>=!~;\[]", dep.strip(), maxsplit=1)[0].lower().replace("_", "-") for dep in deps}
    return bool(names & set(drift.TCKDB_PACKAGES))


def packages_depending_on_tckdb(root: Path) -> list[str]:
    """Repository-relative directories of every pyproject.toml, at any depth, that depends on
    tckdb-client or tckdb-schemas. Virtualenvs, build output and the like are not searched."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIPPED_DIRS and not d.endswith(".egg-info") and not (Path(dirpath) / d / "pyvenv.cfg").exists()
        )
        if "pyproject.toml" in filenames and depends_on_a_tckdb_package(Path(dirpath) / "pyproject.toml"):
            found.append(Path(dirpath).relative_to(root).as_posix())
    return sorted(found)


def test_the_guard_finds_nested_packages_and_skips_virtualenvs_and_build_output(tmp_path):
    dep = '[project]\nname = "x"\ndependencies = ["tckdb-client>=0.1,<0.2"]\n'
    for rel in ("adapters/new_one", "tckdb_flat", "deep/er/pkg"):
        (tmp_path / rel).mkdir(parents=True)
        (tmp_path / rel / "pyproject.toml").write_text(dep)
    for rel in (".venv/lib/x", "build/lib", "dist", "site", "node_modules/y", ".claude/worktrees/w", ".git/z"):
        (tmp_path / rel).mkdir(parents=True)
        (tmp_path / rel / "pyproject.toml").write_text(dep)
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "pyproject.toml").write_text('[project]\nname = "o"\ndependencies = ["rdkit"]\n')
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    assert packages_depending_on_tckdb(tmp_path) == ["adapters/new_one", "deep/er/pkg", "tckdb_flat"]


def test_every_package_that_depends_on_a_tckdb_package_is_covered_by_the_bump():
    """The next adapter cannot be forgotten: any pyproject.toml in the repository that depends on
    tckdb-client or tckdb-schemas must belong to a row of ``PACKAGES``, so ``--bump`` moves it."""
    found = packages_depending_on_tckdb(REPO)
    assert found, "found no package directory; the search is wrong"
    covered = {pkg.directory for pkg in drift.PACKAGES}
    missing = [d for d in found if d not in covered]
    assert not missing, (
        f"{missing} depend on tckdb-client or tckdb-schemas but are not in PACKAGES in "
        "tools/tckdb_drift.py, so a pin bump would leave them behind (AGENTS.md, Scaffolding step 2)"
    )
    for pkg in drift.PACKAGES:  # and every row is a package on disk
        for rel in (pkg.pyproject, pkg.changelog, f"{pkg.directory}/{pkg.module}/__init__.py"):
            assert (REPO / rel).is_file(), f"{pkg.directory}: {rel} does not exist"


def test_module_version_flags_match_the_real_packages():
    """``version_in_init`` says which packages hold a ``__version__`` that must equal pyproject's."""
    for pkg in drift.PACKAGES:
        init = REPO / pkg.init
        has = init.is_file() and re.search(r"^__version__\s*=", init.read_text(), re.M) is not None
        assert has == pkg.version_in_init, pkg.directory


def test_bump_is_idempotent(root):
    drift.run_bump(root, NEW_SHA, "0.58.0", "0.98.0")
    once = _snapshot(root)
    drift.run_bump(root, NEW_SHA, "0.58.2", "0.98.1")  # same lines, same sha
    assert _snapshot(root) == once


def test_bump_only_the_client_line(root):
    pins = drift.read_pins(root)
    schemas = f"{pins['schemas_line'][0]}.{pins['schemas_line'][1]}.3"
    drift.run_bump(root, NEW_SHA, schemas, "0.98.0")
    after = _snapshot(root)
    assert drift.pinned_line(after[ARC.pyproject], "tckdb-schemas") == pins["schemas_line"]
    assert drift.pinned_line(after[ARC.pyproject], "tckdb-client") == (0, 98)
    # The changelog entries name only the bound that moved.
    section = after[QCSCHEMA.changelog].split("## 0.9.9")[0]
    assert "`tckdb-client>=0.98,<0.99` (was `>=0.95,<0.96`)" in " ".join(section.split())
    assert "tckdb-schemas>=" not in section


def test_bump_rejects_a_short_sha_and_writes_nothing(root):
    before = _snapshot(root)
    with pytest.raises(drift.DriftError):
        drift.run_bump(root, "74aae9a2", "0.58.0", "0.98.0")
    assert _snapshot(root) == before


def test_bump_fails_before_writing_when_a_file_lacks_its_anchor(root):
    (root / drift.CONTRACT_PY).write_text("nothing here\n")
    before = _snapshot(root)
    with pytest.raises(drift.DriftError):
        drift.run_bump(root, NEW_SHA, "0.58.0", "0.98.0")
    assert _snapshot(root) == before


def test_resolve_sha_passes_a_full_sha_through_without_the_network():
    assert drift.resolve_sha("https://invalid.invalid/x.git", "c" * 40) == "c" * 40


def test_package_version_reads_project_version():
    assert drift.package_version('[project]\nversion = "0.58.0"\n', "p") == "0.58.0"
    with pytest.raises(drift.DriftError):
        drift.package_version("[tool]\n", "p")


def test_version_comparison_is_numeric_not_textual():
    assert drift.line_of("0.10.0") > drift.line_of("0.9.4")
    pins = {"schemas_line": (0, 9), "client_line": (0, 9), "sha": "f" * 40}
    s = drift.build_summary(pins, "r", "main", "a" * 40, "0.10.0", "0.9.3")
    assert (s["schemas"]["newer"], s["client"]["newer"]) == (True, False)
    assert drift.bound_for((0, 9)) == ">=0.9,<0.10"


def test_package_version_is_normalised_and_ascii_only():
    assert drift.package_version('[project]\nversion = " 0.58.0 "\n', "p") == "0.58.0"
    with pytest.raises(drift.DriftError):
        drift.package_version('[project]\nversion = "0.58.0rc1"\n', "p")
    with pytest.raises(drift.DriftError):
        drift.package_version('[project]\nversion = "0.\u0665\u0668.0"\n', "p")  # Arabic-Indic digits


def test_run_check_reports_drift_and_the_json(root, monkeypatch, capsys):
    pin = drift.read_pin_file((root / drift.PIN_FILE).read_text())
    seen = {}

    def fake_resolve(repo, ref):
        seen["repo"], seen["ref"] = repo, ref
        return NEW_SHA

    def fake_fetch(repo, sha, paths):
        assert sha == NEW_SHA
        assert paths == [f"{pin['schemas_subdir']}/pyproject.toml", f"{pin['client_subdir']}/pyproject.toml"]
        return {paths[0]: '[project]\nversion = "0.58.0"\n', paths[1]: '[project]\nversion = "0.98.0"\n'}

    monkeypatch.setattr(drift, "resolve_sha", fake_resolve)
    monkeypatch.setattr(drift, "fetch_files", fake_fetch)
    assert drift.run_check(root, None, "main") == drift.EXIT_DRIFT
    assert seen["repo"] == pin["repo"]  # the pin file is the source of the repo
    out = json.loads(capsys.readouterr().out)
    assert out["drift"] is True and out["available_sha"] == NEW_SHA
    assert out["schemas"]["available_version"] == "0.58.0" and out["client"]["newer"] is True

    assert drift.run_check(root, "https://example.invalid/x.git", "main") == drift.EXIT_DRIFT
    assert seen["repo"] == "https://example.invalid/x.git"  # explicit override wins

    pins = drift.read_pins(root)
    same = {
        (f"{pin['schemas_subdir']}/pyproject.toml"): "[project]\nversion = \"%d.%d.9\"\n" % pins["schemas_line"],
        (f"{pin['client_subdir']}/pyproject.toml"): "[project]\nversion = \"%d.%d.9\"\n" % pins["client_line"],
    }
    monkeypatch.setattr(drift, "fetch_files", lambda repo, sha, paths: same)
    assert drift.run_check(root, None, "main") == 0


def test_pin_file_rewrite_touches_only_the_tckdb_table_sha():
    other = 'sha = "%s"\n[tckdb]\nrepo = "https://x/y.git"\nsha = "%s"\nschemas_subdir = "s"\nclient_subdir = "c"\n[z]\nsha = "%s"\n'
    old, mid, tail = "1" * 40, "2" * 40, "3" * 40
    text = other % (old, mid, tail)
    new = drift.rewrite_pin_file(text, NEW_SHA)
    assert new == other % (old, NEW_SHA, tail)


def test_bump_refuses_a_downgrade_unless_allowed(root):
    before = {rel: (root / rel).read_text() for rel in FILES}
    pins = drift.read_pins(root)
    lower = f"{pins['schemas_line'][0]}.{pins['schemas_line'][1] - 1}.0"
    with pytest.raises(drift.DriftError, match="--allow-downgrade"):
        drift.run_bump(root, NEW_SHA, lower, "0.98.0")
    assert {rel: (root / rel).read_text() for rel in FILES} == before
    drift.run_bump(root, NEW_SHA, lower, "0.98.0", allow_downgrade=True)
    assert drift.pinned_line((root / ARC.pyproject).read_text(), "tckdb-schemas")[1] == pins["schemas_line"][1] - 1


def test_a_lagging_package_does_not_let_the_others_move_backwards(root):
    """The refusal is per package: with one package behind, the older target is still a downgrade
    for every package that is already ahead."""
    path = root / QCSCHEMA.pyproject
    path.write_text(path.read_text().replace(">=0.54,<0.55", ">=0.53,<0.54"))
    before = _snapshot(root)
    with pytest.raises(drift.DriftError, match="back from 0.54 to 0.53"):
        drift.run_bump(root, NEW_SHA, "0.53.0", "0.95.0")
    assert _snapshot(root) == before


def test_main_exits_2_on_a_refused_bump(root, capsys):
    rc = drift.main(["--root", str(root), "--bump", "--sha", NEW_SHA, "--schemas", "0.1.0", "--client", "0.98.0"])
    assert rc == 2 and "--allow-downgrade" in capsys.readouterr().err


def test_bump_keeps_extras_and_markers_in_specifiers():
    text = (
        '[project]\ndependencies = [\n'
        '    "tckdb-client[fast]>=0.95,<0.96 ; python_version >= \'3.11\'",\n'
        '    "tckdb-schemas >=0.54,<0.55",\n]\n'
    )
    new = drift.rewrite_pyproject(text, (0, 58), (0, 98), "p")
    assert '"tckdb-client[fast]>=0.98,<0.99 ; python_version >= \'3.11\'"' in new
    assert '"tckdb-schemas>=0.58,<0.59"' in new
    assert drift.pinned_line(new, "tckdb-client") == (0, 98)


def test_rewrite_sha_uses_the_pin_repo_not_a_hardcoded_one():
    repo = "https://example.org/other.git"
    text = f"pip install git+{repo}@{'1' * 40}#x\n pip install git+https://github.com/example/other.git@{'2' * 40}#x\n"
    new = drift.rewrite_sha(text, repo, NEW_SHA, "README")
    assert f"{repo}@{NEW_SHA}" in new and "2" * 40 in new
    with pytest.raises(drift.DriftError):
        drift.rewrite_sha("nothing\n", repo, NEW_SHA, "README")


def test_install_specs_come_from_the_pin_file(root, capsys):
    assert drift.run_install_specs(root, None) == 0
    out = capsys.readouterr().out
    pin = drift.read_pin_file((root / drift.PIN_FILE).read_text())
    assert f"TCKDB_CLIENT_SPEC=tckdb-client @ git+{pin['repo']}@{pin['sha']}#subdirectory={pin['client_subdir']}" in out
    assert f"TCKDB_SCHEMAS_SPEC=tckdb-schemas @ git+{pin['repo']}@{pin['sha']}#subdirectory={pin['schemas_subdir']}" in out
    assert drift.run_install_specs(root, NEW_SHA) == 0
    assert NEW_SHA in capsys.readouterr().out


def test_a_sha_only_move_releases_nothing(root):
    before = {rel: (root / rel).read_text() for rel in FILES}
    drift.run_bump(root, NEW_SHA, "0.54.2", "0.95.3")  # the same release lines
    after = {rel: (root / rel).read_text() for rel in FILES}
    assert {rel for rel in FILES if before[rel] != after[rel]} == {drift.PIN_FILE, drift.README}
    assert drift.read_pin_file(after[drift.PIN_FILE])["sha"] == NEW_SHA


def test_a_schemas_only_move_releases_every_package_that_states_the_bound_and_not_chemkin(root):
    before = {rel: (root / rel).read_text() for rel in FILES}
    drift.run_bump(root, NEW_SHA, "0.58.0", "0.95.0")
    after = {rel: (root / rel).read_text() for rel in FILES}
    changed = {rel for rel in FILES if before[rel] != after[rel]}
    for pkg in drift.PACKAGES:
        own = {pkg.pyproject, pkg.changelog} | ({pkg.init} if pkg.version_in_init else set())
        if pkg is CHEMKIN:
            assert not (changed & {pkg.pyproject, pkg.init, pkg.changelog}), "chemkin has no schemas bound"
        else:
            assert own <= changed, pkg.directory
    assert before[CHEMKIN.pyproject] == after[CHEMKIN.pyproject]
    # The core was released, so its dependant's floor followed it.
    assert '"tckdb-adapters-core>=0.1.1,<0.2"' in after[ARC.pyproject]
    for rel in FILES:
        if before[rel] != after[rel]:
            _assert_only_expected_lines_changed(rel, before[rel], after[rel])


def test_the_core_floor_is_left_alone_when_the_core_is_not_released(root):
    path = root / CORE.pyproject
    old_floor = '"tckdb-adapters-core>=0.1.0,<0.2"'
    assert old_floor in (root / ARC.pyproject).read_text()
    drift.run_bump(root, NEW_SHA, "0.54.1", "0.95.1")  # sha only
    assert old_floor in (root / ARC.pyproject).read_text()
    assert path.read_text() == fx.FILES[CORE.pyproject]


@pytest.mark.parametrize("spec", [
    '"tckdb-adapters-core >= 0.1.0 , <0.2"',
    '"tckdb-adapters-core[x]>=0.1,<0.2 ; python_version >= \'3.11\'"',
    '"tckdb-adapters-core>=0.1.0"',
])
def test_core_floor_is_rewritten_with_spaces_and_extras(spec):
    text = f"[project]\ndependencies = [\n    {spec},  # core\n]\n"
    new = drift.rewrite_core_floor(text, "0.5.19", "p")
    assert ">=0.5.19" in new.replace(" >= 0.5.19", ">=0.5.19") and "# core" in new
    assert new.count("0.5.19") == 1


@pytest.mark.parametrize("spec", ['"tckdb-adapters-core==0.1.0"', '"tckdb-adapters-core<0.2"', '"tckdb-adapters-core"'])
def test_core_floor_that_cannot_be_rewritten_is_an_error(spec):
    text = f"[project]\ndependencies = [{spec}]\n"
    with pytest.raises(drift.DriftError, match="lower bound"):
        drift.rewrite_core_floor(text, "0.5.19", "p")


def test_core_floor_is_a_noop_for_a_package_that_does_not_require_the_core():
    text = '[project]\ndependencies = ["tckdb-client>=0.1,<0.2"]\n'
    assert drift.rewrite_core_floor(text, "0.5.19", "p") == text


def test_a_bump_fails_without_writing_when_a_dependants_floor_cannot_be_rewritten(root):
    path = root / ARC.pyproject
    path.write_text(path.read_text().replace(">=0.1.0,<0.2", "==0.1.0"))
    before = _snapshot(root)
    with pytest.raises(drift.DriftError, match="lower bound"):
        drift.run_bump(root, NEW_SHA, "0.58.0", "0.98.0")
    assert _snapshot(root) == before


def test_changelog_section_goes_above_the_newest_version_not_above_unreleased():
    text = "# Changelog\n\nintro\n\n## Unreleased\n\nsoon\n\n## 1.0.0\n\nold\n"
    new = drift.add_changelog_section(text, "1.0.1", "body", "c")
    assert new == "# Changelog\n\nintro\n\n## Unreleased\n\nsoon\n\n## 1.0.1\n\nbody\n\n## 1.0.0\n\nold\n"
    assert drift.add_changelog_section("# C\n\ntext\n", "1.0.1", "body", "c") == "# C\n\ntext\n\n## 1.0.1\n\nbody\n"


def test_changelog_section_that_already_exists_is_an_error():
    with pytest.raises(drift.DriftError, match="already has a ## 1.0.1"):
        drift.add_changelog_section("# C\n\n## 1.0.1\n\nx\n", "1.0.1", "body", "c")


def test_release_body_says_was_for_one_bound_and_were_for_several():
    old = '[project]\ndependencies = ["tckdb-client>=0.1,<0.2", "tckdb-schemas>=0.3,<0.4"]\n'
    one = old.replace("0.1,<0.2", "0.2,<0.3")
    two = one.replace("0.3,<0.4", "0.4,<0.5")
    kw = dict(previous="1.0.0", sha="a" * 40, schemas="0.4.0", client="0.2.0", schemas_line=(0, 4))
    assert "(was `>=0.1,<0.2`)" in drift.release_body(CHEMKIN, old, one, **kw)
    assert "(were `>=0.1,<0.2` and `>=0.3,<0.4`)" in " ".join(drift.release_body(CHEMKIN, old, two, **kw).split())
