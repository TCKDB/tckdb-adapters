"""The PyPI publish workflow covers exactly the packages the release tool knows.

``.github/workflows/publish-python.yml`` maps a release tag to a package
directory through ``tools/python_release.py``. These tests pin that the
workflow's manual-run choices name the same packages, that every package's tag
matches the workflow's tag filter and resolves to its own directory and
version, that CI builds and tests every package, and that the publish jobs keep
the shape trusted publishing needs: OIDC permission, the named environments,
TestPyPI before PyPI, PyPI only for a pushed tag, and no stored secret.

Needs only the standard library, pytest and PyYAML.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish-python.yml"
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PUBLISH_ACTION = re.compile(r"^pypa/gh-action-pypi-publish@[0-9a-f]{40}$")
DOWNLOAD_ACTION = re.compile(r"^actions/download-artifact@")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: ``@dataclass`` looks its module up in ``sys.modules``.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


release = _load_module("python_release_under_test", REPO_ROOT / "tools" / "python_release.py")

NAMES = release.package_names()


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML reads the bare key ``on`` as the boolean True.
    return workflow.get("on", workflow.get(True))


def _project(package) -> dict:
    return tomllib.loads((REPO_ROOT / package.directory / "pyproject.toml").read_text())["project"]


def test_the_release_tool_knows_every_package_in_the_repository():
    """A ``tckdb_*/pyproject.toml`` or ``adapters/tckdb_*/pyproject.toml`` the tool does not know is a package nobody can release."""
    pyprojects = [*REPO_ROOT.glob("tckdb_*/pyproject.toml"), *REPO_ROOT.glob("adapters/tckdb_*/pyproject.toml")]
    on_disk = sorted(p.parent.relative_to(REPO_ROOT).as_posix() for p in pyprojects)
    assert sorted(package.directory for package in release.PACKAGES) == on_disk
    assert NAMES == ["tckdb-adapters-core", "tckdb-arc", "tckdb-chemkin", "tckdb-qcschema"]


@pytest.mark.parametrize("package", release.PACKAGES, ids=lambda p: p.name)
def test_each_table_row_matches_its_pyproject(package):
    project = _project(package)
    assert project["name"] == package.name
    assert (REPO_ROOT / package.directory / package.module / "__init__.py").is_file()


def test_manual_run_choices_are_the_released_packages():
    options = _triggers(_workflow())["workflow_dispatch"]["inputs"]["package"]["options"]
    assert options == NAMES, (
        f"publish-python.yml's package choices must equal python_release.PACKAGES; workflow {options}, tool {NAMES}"
    )


@pytest.mark.parametrize("package", release.PACKAGES, ids=lambda p: p.name)
def test_each_package_tag_matches_the_filter_and_resolves_to_its_directory(package):
    version = _project(package)["version"]
    tag = f"{package.name}-v{version}"
    patterns = _triggers(_workflow())["push"]["tags"]
    assert any(fnmatch.fnmatchcase(tag, pattern) for pattern in patterns), (tag, patterns)

    resolved = release.resolve(tag=tag)
    assert resolved["name"] == package.name
    assert resolved["version"] == version
    assert resolved["directory"] == package.directory
    assert resolved["module"] == package.module


@pytest.mark.parametrize("package", release.PACKAGES, ids=lambda p: p.name)
def test_a_tag_naming_another_version_is_refused(package):
    with pytest.raises(release.ReleaseError, match="tag the commit"):
        release.resolve(tag=f"{package.name}-v{_project(package)['version']}.999")


@pytest.mark.parametrize(
    "tag", ["v0.2.0", "tckdb-client-v0.208.2", "tckdb-core-v0.5.15", "tckdb-arc-0.1.0", "tckdb-arc-vx"]
)
def test_a_tag_outside_the_scheme_or_the_packages_is_refused(tag):
    with pytest.raises(release.ReleaseError):
        release.resolve(tag=tag)


def test_in_repo_dependencies_come_before_their_dependents():
    assert release.resolve(package_name="tckdb-adapters-core")["deps"] == ""
    assert release.resolve(package_name="tckdb-arc")["deps"] == "tckdb_core"
    # The file-format adapters build on tckdb-client and tckdb-schemas, which come from PyPI.
    assert release.resolve(package_name="tckdb-chemkin")["deps"] == ""
    assert release.resolve(package_name="tckdb-qcschema")["deps"] == ""


def test_the_build_job_resolves_builds_checks_and_smoke_tests():
    steps = _workflow()["jobs"]["build"]["steps"]
    script = "\n".join(str(step.get("run", "")) for step in steps)
    assert "python_release.py resolve --tag" in script
    assert "python_release.py resolve --package" in script
    assert "python -m build --outdir dist" in script
    assert "twine check --strict dist/*" in script
    assert "python_release.py smoke" in script
    assert "merge-base --is-ancestor" in script


def _publish_step(job: dict) -> dict:
    """The job's only steps: one artifact download, then one pinned publish."""
    steps = job["steps"]
    assert [step.get("run") for step in steps if "run" in step] == [], "a publish job runs no shell"
    downloads = [step for step in steps if DOWNLOAD_ACTION.match(step.get("uses", ""))]
    publishes = [step for step in steps if PUBLISH_ACTION.match(step.get("uses", ""))]
    assert len(downloads) == 1, downloads
    assert len(publishes) == 1, f"pypa/gh-action-pypi-publish must appear once, pinned by commit sha: {steps}"
    assert len(steps) == 2, f"a publish job only downloads and publishes the build job's dist: {steps}"
    return publishes[0]


def test_publish_jobs_use_trusted_publishing_in_the_named_environments():
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]

    testpypi = jobs["publish-testpypi"]
    assert testpypi["needs"] == "build"
    assert testpypi["environment"]["name"] == "testpypi"
    assert testpypi["permissions"] == {"id-token": "write"}
    publish = _publish_step(testpypi)
    assert publish["with"]["repository-url"] == "https://test.pypi.org/legacy/"
    assert publish["with"]["skip-existing"] is True

    pypi = jobs["publish-pypi"]
    assert set(pypi["needs"]) == {"build", "publish-testpypi"}
    assert pypi["environment"]["name"] == "pypi"
    assert pypi["permissions"] == {"id-token": "write"}
    assert "github.event_name == 'push'" in pypi["if"]
    assert "startsWith(github.ref, 'refs/tags/')" in pypi["if"]
    publish = _publish_step(pypi)
    options = publish.get("with") or {}
    assert "repository-url" not in options
    assert "skip-existing" not in options, "PyPI must refuse a version that already exists, not skip it"


def test_no_expression_is_spliced_into_a_shell_script():
    """Values reach ``run:`` through ``env:``, never as ``${{ }}`` text."""
    for name, job in _workflow()["jobs"].items():
        for step in job.get("steps", []):
            assert "${{" not in str(step.get("run", "")), f"{name}: {step.get('name')}"


def test_the_build_checkout_does_not_keep_the_token():
    (checkout,) = [
        step for step in _workflow()["jobs"]["build"]["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkout["with"]["persist-credentials"] is False


def _shell_command(workflow: dict) -> list[str]:
    """The shell GitHub runs a ``run:`` step with, given the workflow's default."""
    shell = (workflow.get("defaults") or {}).get("run", {}).get("shell")
    if shell == "bash":
        return ["bash", "--noprofile", "--norc", "-eo", "pipefail"]
    assert shell is None, f"unexpected default shell {shell!r}"
    return ["bash", "-e"]


def test_steps_run_under_bash_with_pipefail():
    assert _workflow()["defaults"]["run"]["shell"] == "bash"


def _run_resolve_step(tmp_path: Path, env_overrides: dict[str, str]) -> tuple[subprocess.CompletedProcess, Path]:
    workflow = _workflow()
    (step,) = [s for s in workflow["jobs"]["build"]["steps"] if s.get("id") == "resolve"]
    script = tmp_path / "step.sh"
    script.write_text(step["run"], encoding="utf-8")
    output = tmp_path / "github_output"
    env = {**os.environ, "GITHUB_OUTPUT": str(output), **env_overrides}
    result = subprocess.run(
        [*_shell_command(workflow), str(script)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False
    )
    return result, output


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("tag", ["tckdb-chemkin-v0.0.0", "v0.2.0"])
def test_the_resolve_step_fails_the_job_on_a_refused_tag(tmp_path, tag):
    """Run the step's own script the way GitHub would; a refused tag must stop the job."""
    result, _ = _run_resolve_step(tmp_path, {"EVENT_NAME": "push", "REF_NAME": tag, "PACKAGE": ""})
    assert result.returncode != 0, f"the resolve step passed a refused tag:\n{result.stdout}{result.stderr}"
    assert "::error::" in result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_resolve_step_writes_its_outputs_for_a_known_package(tmp_path):
    """The same harness passes a good run, so the refusal above is the tag's doing."""
    result, output = _run_resolve_step(
        tmp_path, {"EVENT_NAME": "workflow_dispatch", "REF_NAME": "main", "PACKAGE": "tckdb-arc"}
    )
    assert result.returncode == 0, result.stderr
    lines = output.read_text(encoding="utf-8").splitlines()
    assert "name=tckdb-arc" in lines
    assert "deps=tckdb_core" in lines


def test_the_workflow_stores_no_secret():
    assert "secrets." not in WORKFLOW_PATH.read_text(encoding="utf-8")


def _ci() -> dict:
    return yaml.safe_load(CI_PATH.read_text(encoding="utf-8"))


def test_ci_builds_and_checks_every_package():
    """Metadata rot fails a pull request, not the release."""
    job = _ci()["jobs"]["package-metadata"]
    script = "\n".join(str(step.get("run", "")) for step in job["steps"])
    (checkout,) = [step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")]
    assert checkout["with"]["persist-credentials"] is False
    assert "twine check --strict" in script
    assert "tools/tests" in script, "CI does not run this file"
    for package in release.PACKAGES:
        assert package.directory in script, f"{package.name} ({package.directory}) is not built by ci.yml"


def test_ci_runs_every_packages_test_suite():
    """Each package's suite runs in its own directory: a job default or a step's working-directory."""
    tested: set[str] = set()
    for job in _ci()["jobs"].values():
        default = (job.get("defaults") or {}).get("run", {}).get("working-directory")
        for step in job.get("steps", []):
            if "pytest" in str(step.get("run", "")):
                tested.add(step.get("working-directory", default))
    missing = [p.directory for p in release.PACKAGES if p.directory not in tested]
    assert missing == [], f"no CI step runs pytest in {missing}"
