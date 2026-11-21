"""The drift tool's tests must not depend on the checkout's current pins.

tckdb-drift.yml bumps the real pin files to the newest TCKDB and then runs the
suite, so a test that reads those files as its fixture fails exactly when there is
drift (that happened: issue #17). This bumps a copy of the real files to a far
newer line and runs the drift tests there.
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
# The files the bump rewrites (read from the tool, so a new package is copied too), and the
# files the drift tests read besides.
_TOOL = importlib.util.spec_from_file_location("drift_real", REPO / "tools" / "tckdb_drift.py")
_real = importlib.util.module_from_spec(_TOOL)
_TOOL.loader.exec_module(_real)
COPIED = sorted({
    "tools/tckdb_drift.py",
    "tools/tckdb_drift_publish.sh",
    "tools/python_release.py",
    "adapters/tckdb_arc/tests/test_tckdb_drift.py",
    "adapters/tckdb_arc/tests/_drift_fixture.py",
    "AGENTS.md",
    "CLAUDE.md",
    ".github/workflows/ci.yml",
    ".github/workflows/tckdb-drift.yml",
    *_real.bumped_files(),
    *(pkg.pyproject for pkg in _real.PACKAGES),
    *(f"{pkg.directory}/{pkg.module}/__init__.py" for pkg in _real.PACKAGES),
})


def test_drift_tests_pass_with_the_real_pins_already_bumped(tmp_path):
    for rel in COPIED:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, dest)

    spec = importlib.util.spec_from_file_location("drift_copy", tmp_path / "tools" / "tckdb_drift.py")
    drift = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drift)
    # Far past any real release, so "already at the available line" holds for every line.
    drift.run_bump(tmp_path, "b" * 40, "9.99.0", "9.99.0")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--noconftest", "-p", "no:cacheprovider",
         str(tmp_path / "adapters" / "tckdb_arc" / "tests" / "test_tckdb_drift.py")],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-1000:]
