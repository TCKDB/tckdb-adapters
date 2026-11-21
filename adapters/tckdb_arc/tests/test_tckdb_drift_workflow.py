"""Run the drift workflow's test step with fake ``pip`` and ``pytest``.

The step decides what the pull request or the issue says, so its shell is exercised here: every
package is installed and tested from its own directory, a failure in any suite makes the run a
failing one (an issue, not a pull request) and the failing tests are reported one per line
starting with ``FAILED`` or ``ERROR``, which is what the publish script hashes. No network.

TCKDB's repository is a local one here (the fixture's pin file names it): the step fetches the
backend identity rules from it with the real ``tools/fetch_tckdb_backend.sh`` and real ``git``,
at the commit the bumped pin names, and the fake ``pytest`` records what each suite was given.
Only ``pip`` and ``pytest`` are fakes.
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

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

REPO = Path(__file__).resolve().parents[3]
WORKFLOW = REPO / ".github" / "workflows" / "tckdb-drift.yml"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
FETCH_SCRIPT = REPO / "tools" / "fetch_tckdb_backend.sh"
DIRECTORIES = fx.DIRECTORIES  # the tool's table, so a new package needs no edit here

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
echo "${TCKDB_BACKEND_PATH:-unset}|${TCKDB_REQUIRE_BACKEND:-unset}|$(cat "${TCKDB_BACKEND_PATH:-/nonexistent}/app/chemistry/marker.txt" 2>/dev/null)" >> "$FAKE/backend.log"
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


def _git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


def make_tckdb(tmp: Path) -> tuple[str, str, str]:
    """A local stand-in for the TCKDB repository with two commits: ``(url, old sha, new sha)``.

    ``backend/app/chemistry/marker.txt`` says which commit a checkout is at (OLD, then NEW), and a
    file outside ``backend/app/chemistry`` is there to show the checkout is sparse.
    """
    src = tmp / "tckdb-src"
    (src / "backend" / "app" / "chemistry").mkdir(parents=True)
    (src / "schemas").mkdir()
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "uploadpack.allowFilter", "true")
    (src / "schemas" / "elsewhere.txt").write_text("not part of the backend\n")
    marker = src / "backend" / "app" / "chemistry" / "marker.txt"
    shas = []
    for text in ("OLD", "NEW"):
        marker.write_text(text)
        _git(src, "add", "-A")
        _git(src, "commit", "-q", "-m", text)
        shas.append(_git(src, "rev-parse", "HEAD"))
    return f"file://{src}", shas[0], shas[1]


class Run:
    def __init__(self, tmp: Path):
        self.root = fx.build(tmp / "repo")
        (self.root / "tools").mkdir()
        for name in ("tckdb_drift.py", "fetch_tckdb_backend.sh"):
            shutil.copy(REPO / "tools" / name, self.root / "tools" / name)
        # The bump step has already moved the pin to the NEW commit when this step runs.
        self.tckdb_src = tmp / "tckdb-src"
        self.tckdb_url, self.old_sha, self.new_sha = make_tckdb(tmp)
        self.set_pin(self.tckdb_url, self.new_sha)
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

    def set_pin(self, url: str, sha: str) -> None:
        text = fx.FILES[fx.drift.PIN_FILE].replace(fx.PIN_REPO, url).replace(fx.OLD_SHA, sha)
        (self.root / fx.drift.PIN_FILE).write_text(text)

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
    ran = [Path(line.split()[0]).relative_to(run.root).as_posix() for line in run.log("pytest.log").splitlines()]
    assert ran == DIRECTORIES
    assert "FAILED" not in run.summary()


@pytest.mark.parametrize("failing", DIRECTORIES)
def test_a_failure_in_any_suite_is_a_failed_run_and_the_others_still_run(run, failing):
    (run.fake / f"fail_{Path(failing).name}").write_text("")  # the shim names the failing suite by its directory name
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
    for failing in ("adapters/tckdb_chemkin", "adapters/tckdb_qcschema"):
        (run.fake / f"fail_{Path(failing).name}").write_text("")
    assert run.run() == {"result": "fail"}
    lines = run.summary().splitlines()
    assert "FAILED adapters/tckdb_chemkin/tests/test_x.py::test_a - boom" in lines
    assert "FAILED adapters/tckdb_qcschema/tests/test_x.py::test_a - boom" in lines


def test_an_install_failure_runs_no_suite_and_is_reported_as_one(run):
    (run.fake / "pip_fails").write_text("")
    assert run.run() == {"result": "install-failed"}
    assert run.log("pytest.log") == ""
    assert "Installing the new packages" in run.summary() and "no matching distribution" in run.summary()


def test_every_suite_gets_tckdbs_backend_at_the_new_commit_and_requires_it(run):
    """Without TCKDB_BACKEND_PATH the comparison tests skip, so a change to TCKDB's identity rules
    would pass the drift job: the step fetches them at the pin the bump moved and exports both
    variables to every suite, as ci.yml does."""
    assert run.run() == {"result": "pass"}
    backend = (run.runner_temp / "tckdb" / "backend").as_posix()
    assert run.log("backend.log").splitlines() == [f"{backend}|1|NEW"] * len(DIRECTORIES)  # the new commit, not OLD
    assert not (run.runner_temp / "tckdb" / "schemas" / "elsewhere.txt").exists()  # sparse, as in ci.yml


@pytest.mark.parametrize("breakage", ["unreachable repository", "commit without the backend directory"])
def test_a_failed_backend_fetch_is_a_failed_run_that_runs_no_suite(run, breakage):
    if breakage == "unreachable repository":
        run.set_pin(f"file://{run.runner_temp}/no-such-repository", run.new_sha)
    else:  # a TCKDB commit that has no backend/app/chemistry (the directory moved)
        _git(run.tckdb_src, "rm", "-q", "-r", "backend")
        _git(run.tckdb_src, "commit", "-q", "-m", "restructure")
        run.set_pin(run.tckdb_url, _git(run.tckdb_src, "rev-parse", "HEAD"))
    assert run.run() == {"result": "setup-failed"}  # never "pass": nothing was tested against the new backend
    assert run.log("pytest.log") == ""
    summary = run.summary()
    assert "could not set up its tests" in summary and "Fetching TCKDB's backend/app/chemistry" in summary
    assert not re.search(r"^(FAILED|ERROR) ", summary, re.M)


@pytest.mark.parametrize("tool", [
    "import sys\nsys.exit(3)\n",  # --list-packages fails
    "",                           # --list-packages prints nothing
], ids=["fails", "prints-nothing"])
def test_a_missing_package_list_is_a_distinct_setup_failure_not_an_install_failure(run, tool):
    (run.root / "tools" / "tckdb_drift.py").write_text(tool)
    assert run.run() == {"result": "setup-failed"}
    assert run.log("pytest.log") == "" and run.log("pip.log") == ""
    summary = run.summary()
    assert "--list-packages failed or printed no packages" in summary
    assert "Installing the new packages" not in summary


def test_the_fetch_script_prints_only_the_two_variables_and_fails_loudly(tmp_path):
    url, _, new_sha = make_tckdb(tmp_path)
    spec = f"tckdb-schemas @ git+{url}@{new_sha}#subdirectory=schemas/python/tckdb-schemas"
    dest = tmp_path / "out"
    proc = subprocess.run(["bash", str(FETCH_SCRIPT), spec, str(dest)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [f"TCKDB_BACKEND_PATH={dest}/backend", "TCKDB_REQUIRE_BACKEND=1"]
    assert (dest / "backend" / "app" / "chemistry" / "marker.txt").read_text() == "NEW"
    for bad in ("not a spec", f"tckdb-schemas @ git+{url}@{'0' * 40}#x"):
        failed = subprocess.run(["bash", str(FETCH_SCRIPT), bad, str(tmp_path / "again")], capture_output=True, text=True)
        assert failed.returncode != 0 and failed.stdout == "", bad


def test_ci_and_the_drift_workflow_fetch_the_backend_with_the_same_script():
    drift_text, ci_text = WORKFLOW.read_text(), CI_WORKFLOW.read_text()
    for name, text in (("tckdb-drift.yml", drift_text), ("ci.yml", ci_text)):
        assert "tools/fetch_tckdb_backend.sh" in text, name
        assert "sparse-checkout" not in text, name  # the fetch logic lives in the script only
    assert "sparse-checkout" in FETCH_SCRIPT.read_text()
    # ci.yml still turns the script's output into the two job-wide variables.
    assert re.search(r'fetch_tckdb_backend\.sh "\$TCKDB_SCHEMAS_SPEC" "\$RUNNER_TEMP/tckdb" >> "\$GITHUB_ENV"', ci_text)


def test_the_ci_step_still_writes_the_same_two_variables_to_github_env(tmp_path):
    """ci.yml's step, run as the runner runs it (from the ARC package directory), against a local
    TCKDB: the job-wide environment gets the backend path and the require flag, as before."""
    url, _, new_sha = make_tckdb(tmp_path)
    steps = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]["tckdb-arc"]["steps"]
    (step,) = [s for s in steps if "identity rules" in s.get("name", "")]
    runner_temp, github_env = tmp_path / "runner", tmp_path / "github_env"
    runner_temp.mkdir()
    env = {**os.environ, "RUNNER_TEMP": str(runner_temp), "GITHUB_ENV": str(github_env),
           "TCKDB_SCHEMAS_SPEC": f"tckdb-schemas @ git+{url}@{new_sha}#subdirectory=schemas/python/tckdb-schemas"}
    proc = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=REPO / "adapters" / "tckdb_arc", env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert github_env.read_text().splitlines() == [f"TCKDB_BACKEND_PATH={runner_temp}/tckdb/backend", "TCKDB_REQUIRE_BACKEND=1"]
    assert (runner_temp / "tckdb" / "backend" / "app" / "chemistry" / "marker.txt").read_text() == "NEW"


def test_the_summary_is_bounded_whatever_the_suites_print(run):
    """Every package fails with many long failing-test lines and a very long last line: each line
    is cut, and the summary as a whole stays under the cap, its code fence closed."""
    for directory in DIRECTORIES:
        (run.fake / f"fail_{Path(directory).name}").write_text("")
    shim = (run.bin / "pytest").read_text()
    shim = shim.replace('echo "FAILED tests/test_x.py::test_a - boom"',
                        'for i in $(seq 100); do echo "FAILED tests/test_$i.py::test_a - $(printf \'y%.0s\' $(seq 400))"; done')
    shim = shim.replace('echo "===== 2 failed, 3 passed in 0.1s ====="', f'echo "{"x" * 5000}"')
    (run.bin / "pytest").write_text(shim)
    assert run.run() == {"result": "fail"}
    summary = run.summary()
    assert all(len(line) <= 330 for line in summary.splitlines()), "a line was not cut"  # 300 plus the package prefix
    assert len(summary.encode()) <= 40000
    assert "truncated" in summary  # 4 x 40 lines of ~330 bytes is more than the cap
    assert sum(1 for line in summary.splitlines() if line.startswith("```")) % 2 == 0  # the fence is closed
