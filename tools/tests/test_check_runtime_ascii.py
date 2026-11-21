"""tools/check_runtime_ascii.py: the rule, the targets and the audit.

The target list is written out below as literals, so a test cannot follow a
target repointed at the wrong directory. Each package also gets one landed
mutation: an em dash put into a real emitted string must be reported, which a
target that resolves nowhere would not do.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load():
    name = "check_runtime_ascii_under_test"
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "tools" / "check_runtime_ascii.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


checker = _load()

REQUIRED_TREES = (
    "tckdb_core/tckdb_core",
    "tckdb_arc/tckdb_arc",
    "tckdb_chemkin/tckdb_chemkin",
    "tckdb_qcschema/tckdb_qcschema",
)


def _findings(source: str):
    return checker.check_source(source, Path("probe.py"))


def test_every_package_is_a_default_target():
    targets = {p.resolve() for p in checker.default_target_paths()}
    for tree in REQUIRED_TREES:
        assert (REPO_ROOT / tree).resolve() in targets, tree


def test_every_package_tree_is_a_default_target():
    """A new ``tckdb_<name>/tckdb_<name>`` package is scanned or the audit says why not."""
    on_disk = sorted(p.parent.relative_to(REPO_ROOT).as_posix() for p in REPO_ROOT.glob("tckdb_*/tckdb_*/__init__.py"))
    assert sorted(checker.DEFAULT_TARGETS) == on_disk


@pytest.mark.parametrize("tree", REQUIRED_TREES)
def test_the_shipped_packages_are_clean(tree):
    assert checker.check_path(REPO_ROOT / tree) == []


def test_the_audit_accounts_for_every_source_file():
    undeclared, counts = checker.audit()
    assert undeclared == []
    assert all(count > 0 for count in counts.values()), counts


def test_the_audit_names_an_undeclared_file(tmp_path, monkeypatch):
    (tmp_path / "tckdb_x" / "tckdb_x").mkdir(parents=True)
    (tmp_path / "tckdb_x" / "tckdb_x" / "a.py").write_text("x = 1\n")
    (tmp_path / "loose.py").write_text("x = 1\n")
    monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(checker, "DEFAULT_TARGETS", ("tckdb_x/tckdb_x",))
    monkeypatch.setattr(checker, "UNSCANNED_BY_DESIGN", ())
    undeclared, _ = checker.audit()
    assert undeclared == ["loose.py"]


@pytest.mark.parametrize(
    "source, kind",
    [
        ('raise ValueError("bad — input")\n', "raised message"),
        ('logger.warning("skipped — %s", x)\n', "log message"),
        ('get_logger(__name__).info("a → b")\n', "log message"),
        ('warnings.append({"message": "x"} if y else "a — b")\n', "accumulated warning"),
        ('sink.add("code", "a …")\n', "accumulated warning"),
        ('warn(code="c", message="a — b")\n', "message= argument"),
    ],
)
def test_each_emission_site_is_caught(source, kind):
    (finding,) = _findings(source)
    assert finding.kind == kind


@pytest.mark.parametrize(
    "source",
    [
        '"""A docstring — prose."""\n',
        "# a comment — prose\nx = 1\n",
        'label = "Ångström"\n',
        'raise ValueError("bad — input")  # tckdb: allow-non-ascii\n',
        'seen.add("é")\n',
    ],
)
def test_prose_data_and_marked_lines_are_left_alone(source):
    assert _findings(source) == []


@pytest.mark.parametrize(
    "path, clean, mutated",
    [
        (
            "tckdb_core/tckdb_core/thermo_numerics.py",
            "NASA block skipped -- temperature bounds incomplete.",
            "NASA block skipped — temperature bounds incomplete.",
        ),
        (
            "tckdb_arc/tckdb_arc/sweep.py",
            "missing/non-converged -- writing ",
            "missing/non-converged \u2014 writing ",
        ),
        (
            "tckdb_chemkin/tckdb_chemkin/parser.py",
            "raise ChemkinParseError(",
            None,
        ),
        (
            "tckdb_qcschema/tckdb_qcschema/reader.py",
            "raise QCSchemaAdapterError(",
            None,
        ),
    ],
)
def test_a_reintroduced_character_in_each_package_is_caught(path, clean, mutated):
    file = REPO_ROOT / path
    source = file.read_text(encoding="utf-8")
    assert clean in source, f"the string this mutation targets has moved in {path}"
    assert checker.check_source(source, file) == []
    if mutated is None:
        # Put an em dash into the first string literal of the first matching raise.
        start = source.index(clean)
        quote = min(i for i in (source.find('"', start), source.find("'", start)) if i != -1)
        mutated_source = source[: quote + 1] + "— " + source[quote + 1 :]
    else:
        mutated_source = source.replace(clean, mutated)
    found = checker.check_source(mutated_source, file)
    assert len(found) == 1, found
    assert found[0].characters == "—"


def test_a_target_with_no_python_is_refused(tmp_path, capsys):
    assert checker.main([str(tmp_path)]) == 2
    assert "contains no *.py" in capsys.readouterr().err


def test_the_default_run_names_every_tree(capsys):
    assert checker.main([]) == 0
    out = capsys.readouterr().out
    for tree in REQUIRED_TREES:
        line = next(line for line in out.splitlines() if line.endswith(tree))
        assert int(line.split()[1]) > 0, line
