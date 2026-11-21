"""Run the drift workflow's test step with fake ``pip`` and ``pytest``.

The step decides what the pull request or the issue says, so its shell is exercised here: every
package is installed and tested from its own directory, a failure in any suite makes the run a
failing one (an issue, not a pull request) and the failing tests are reported one per line
starting with ``FAILED`` or ``ERROR``, which is what the publish script hashes. No network.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import _drift_fixture as fx

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "tckdb-drift.yml"
DIRECTORIES = ["tckdb_core", "tckdb_arc", "tckdb_chemkin", "tckdb_qcschema"]

PYTHON_SHIM = """#!/bin/bash
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then echo "python $*" >> "$FAKE/pip.log"; exit 0; fi
exec "{python}" "$@"
"""
PIP_SHIM = """#!/bin/bash
echo "pip $*" >> "$FAKE/pip.log"
[ -e "$FAKE/pip_fails" ] && { echo "ERROR: no matching distribution"; exit 1; }
exit 0
"""
PYTEST_SHIM = """#!/bin/bash
echo "$PWD $*" >> "$FAKE/pytest.log"
if [ -e "$FAKE/fail_$(basename "$PWD")" ]; then
  echo "FAILED tests/test_x.py::test_a - boom"
  echo "ERROR tests/test_y.py"
  echo "===== 2 failed, 3 passed in 0.1s ====="
  exit 1
fi
echo "4 passed in 0.1s"
"""


def _test_step() -> dict:
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    steps = [s for s in jobs["test"]["steps"] if s.get("id") == "test"]
    assert len(steps) == 1
    return steps[0]


def test_the_test_job_keeps_its_read_only_token_and_no_commit_is_written_in_the_workflow():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["test"]["permissions"] == {"contents": "read"}
    assert not re.search(r"\b[0-9a-f]{40}\b", re.sub(r"uses:\s*[\w.-]+/[\w./-]+@[0-9a-f]{40}\b", "", WORKFLOW.read_text()))


class Run:
    def __init__(self, tmp: Path):
        self.root = fx.build(tmp / "repo")
        (self.root / "tools").mkdir()
        shutil.copy(REPO / "tools" / "tckdb_drift.py", self.root / "tools" / "tckdb_drift.py")
        self.fake = tmp / "fake"
        self.fake.mkdir()
        self.runner_temp = tmp / "runner"
        self.runner_temp.mkdir()
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name, text in (("python", PYTHON_SHIM.format(python=sys.executable)), ("pip", PIP_SHIM),
                           ("pytest", PYTEST_SHIM)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.bin = bin_dir
        self.output = tmp / "github_output"

    def run(self) -> dict:
        script = _test_step()["run"].replace("${{ steps.check.outputs.old_schemas }}", "0.54.0")
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE": str(self.fake),
               "RUNNER_TEMP": str(self.runner_temp), "GITHUB_OUTPUT": str(self.output)}
        proc = subprocess.run(["bash", "-e", "-c", script], cwd=self.root, env=env, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return dict(line.split("=", 1) for line in self.output.read_text().splitlines())

    def log(self, name: str) -> str:
        path = self.fake / name
        return path.read_text() if path.exists() else ""

    def summary(self) -> str:
        return (self.runner_temp / "summary.md").read_text()


@pytest.fixture
def run(tmp_path):
    return Run(tmp_path)


def test_every_package_is_installed_and_its_suite_run_from_its_own_directory(run):
    assert run.run() == {"result": "pass"}
    installs = [line for line in run.log("pip.log").splitlines() if " -e " in line]
    assert len(installs) == 1  # one resolve, so a conflict between the packages shows up
    for directory in DIRECTORIES:
        assert f"-e ./{directory}[test]" in installs[0]
    ran = [Path(line.split()[0]).name for line in run.log("pytest.log").splitlines()]
    assert ran == DIRECTORIES
    assert "FAILED" not in run.summary()


@pytest.mark.parametrize("failing", DIRECTORIES)
def test_a_failure_in_any_suite_is_a_failed_run_and_the_others_still_run(run, failing):
    (run.fake / f"fail_{failing}").write_text("")
    assert run.run() == {"result": "fail"}
    assert len(run.log("pytest.log").splitlines()) == len(DIRECTORIES)  # no suite is skipped
    lines = run.summary().splitlines()
    # The publish script hashes the lines that start with FAILED or ERROR; the package is in the path.
    assert f"FAILED {failing}/tests/test_x.py::test_a - boom" in lines
    assert f"ERROR {failing}/tests/test_y.py" in lines
    assert [line for line in lines if re.match(r"(FAILED|ERROR) ", line)] == [
        f"FAILED {failing}/tests/test_x.py::test_a - boom", f"ERROR {failing}/tests/test_y.py"]
    assert f"{failing}: ===== 2 failed, 3 passed in 0.1s =====" in lines


def test_failures_in_two_packages_are_both_reported(run):
    for failing in ("tckdb_chemkin", "tckdb_qcschema"):
        (run.fake / f"fail_{failing}").write_text("")
    assert run.run() == {"result": "fail"}
    lines = run.summary().splitlines()
    assert "FAILED tckdb_chemkin/tests/test_x.py::test_a - boom" in lines
    assert "FAILED tckdb_qcschema/tests/test_x.py::test_a - boom" in lines


def test_an_install_failure_runs_no_suite_and_is_reported_as_one(run):
    (run.fake / "pip_fails").write_text("")
    assert run.run() == {"result": "install-failed"}
    assert run.log("pytest.log") == ""
    assert "Installing the new packages" in run.summary() and "no matching distribution" in run.summary()
