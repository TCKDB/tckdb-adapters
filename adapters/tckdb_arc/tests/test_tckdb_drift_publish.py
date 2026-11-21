"""Exercise tools/tckdb_drift_publish.sh against a bare remote and a fake ``gh``.

Covers the branch-safety rules (never wipe human commits, skip a push that would
change nothing, lease-protected push otherwise) and the issue/PR fallbacks. No
network: the "remote" is a local bare repo and ``gh`` only records its arguments.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import _drift_fixture as fx

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "tools" / "tckdb_drift_publish.sh"
BRANCH = "chore/tckdb-drift-schemas-0.58-client-0.98"
SHA_1 = "1" * 40
SHA_2 = "2" * 40
GH_SHIM = """#!/bin/bash
echo "$@" >> "$FAKE/gh.log"
# Keep every body that is sent (--body-file contents and --body values), one file each.
mkdir -p "$FAKE/bodies"
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    --body-file) cp "${args[$((i + 1))]}" "$FAKE/bodies/$(date +%s%N)-$$" ;;
    --body|--comment) printf '%s' "${args[$((i + 1))]}" > "$FAKE/bodies/$(date +%s%N)-$$" ;;
  esac
done
case "$1 $2" in
  "label list") cat "$FAKE/labels" 2>/dev/null ;;
  "issue list") cat "$FAKE/issues" 2>/dev/null ;;
  "issue view") cat "$FAKE/issue_body" 2>/dev/null ;;
  "pr list") if [[ " $* " == *" --head "* ]]; then cat "$FAKE/pr_head" 2>/dev/null; else cat "$FAKE/pr_all" 2>/dev/null; fi ;;
  "pr view") cat "$FAKE/pr_comments" 2>/dev/null ;;
  "pr create") [ -e "$FAKE/pr_create_fails" ] && exit 1 ;;
esac
exit 0
"""

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(cwd, *args, **kw):
    env = {**os.environ, "GIT_AUTHOR_NAME": "h", "GIT_AUTHOR_EMAIL": "h@x", "GIT_COMMITTER_NAME": "h", "GIT_COMMITTER_EMAIL": "h@x"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env, **kw).stdout.strip()


class Env:
    def __init__(self, tmp: Path, seed_edit=None):
        self.tmp = tmp
        self.remote = tmp / "remote.git"
        self.fake = tmp / "fake"
        self.fake.mkdir()
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(GH_SHIM)
        (bin_dir / "python").write_text(f'#!/bin/bash\nexec "{sys.executable}" "$@"\n')
        for f in ("gh", "python"):
            (bin_dir / f).chmod(0o755)
        self.bin = bin_dir
        seed = tmp / "seed"
        seed.mkdir()
        fx.build(seed)  # synthetic pins (0.54 / 0.95): the suite never depends on the real ones
        if seed_edit is not None:
            seed_edit(seed)
        for rel in ("tools/tckdb_drift.py", "tools/tckdb_drift_publish.sh"):  # code under test, not data
            dest = seed / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / rel, dest)
        git(seed, "init", "-q", "-b", "main")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "base")
        git(tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        git(seed, "push", "-q", str(self.remote), "main")
        self.runs = 0

    def set(self, name, text):
        (self.fake / name).write_text(text)

    def run(self, sha=SHA_2, result="pass", drift="true", summary="```\n1 passed\n```\n",
            since="# Changes\nsome text\n", extra_env=None):
        self.runs += 1
        work = self.tmp / f"work{self.runs}"
        git(self.tmp, "clone", "-q", str(self.remote), str(work))
        (self.tmp / "since.md").write_text(since)
        (self.tmp / "summary.md").write_bytes(summary if isinstance(summary, bytes) else summary.encode())
        (self.fake / "gh.log").write_text("")
        shutil.rmtree(self.fake / "bodies", ignore_errors=True)
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE": str(self.fake),
            "PUSH_URL": str(self.remote),
            "GH_TOKEN": "t", "REPO": "o/r", "RUN_URL": "http://run",
            "DRIFT": drift, "RESULT": result, "SHA": sha,
            "SCHEMAS": "0.58.0", "CLIENT": "0.98.0", "SCHEMAS_LINE": "0.58", "CLIENT_LINE": "0.98",
            "OLD_SCHEMAS": "0.54.0",
            "SINCE_FILE": str(self.tmp / "since.md"), "SUMMARY_FILE": str(self.tmp / "summary.md"),
            **(extra_env or {}),
        }
        self.work = work
        proc = subprocess.run(["bash", str(work / "tools" / "tckdb_drift_publish.sh")], cwd=work, env=env,
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return proc

    def bodies(self) -> list[str]:
        """Every body the run sent to gh (issue, pull request or comment), as text."""
        folder = self.fake / "bodies"
        return [path.read_text() for path in sorted(folder.iterdir())] if folder.exists() else []

    def gh_log(self):
        return (self.fake / "gh.log").read_text()

    def remote_branch(self):
        out = git(self.tmp, "ls-remote", "--heads", str(self.remote), f"refs/heads/{BRANCH}")
        return out.split()[0] if out else None


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_first_run_pushes_a_bot_branch_opens_the_pr_and_dispatches_ci(env):
    env.run()
    tip = env.remote_branch()
    assert tip
    assert git(env.remote, "log", "-1", "--format=%ae", tip) == "41898282+github-actions[bot]@users.noreply.github.com"
    changed = set(git(env.remote, "diff", "--name-only", "main", tip).split())
    assert ".github/workflows/ci.yml" not in changed
    # A full bump rewrites every file the tool lists, so the pushed commit holds exactly those (every
    # changed file is committed, and nothing else is) ...
    assert changed == set(fx.drift.bumped_files())
    # ... and nothing is left over in the work tree.
    assert git(env.work, "status", "--porcelain") == ""
    assert sorted(git(env.work, "diff", "--name-only", "main", "HEAD").split()) == sorted(changed)
    log = env.gh_log()
    assert "pr create" in log and "workflow run ci.yml --ref " + BRANCH in log


def test_unchanged_tree_is_not_pushed_or_redispatched_but_the_pr_is_updated(env):
    env.run()
    tip = env.remote_branch()
    env.set("pr_head", "7\n")
    env.run()
    assert env.remote_branch() == tip
    log = env.gh_log()
    assert "pr edit 7" in log and "workflow run" not in log and "pr create" not in log


def test_branch_with_human_commits_is_never_overwritten(env):
    env.run(sha=SHA_1)
    human = env.tmp / "human"
    git(env.tmp, "clone", "-q", "-b", BRANCH, str(env.remote), str(human))
    (human / "README.md").write_text((human / "README.md").read_text() + "\nhuman note\n")
    git(human, "commit", "-qam", "my fix")
    git(human, "push", "-q", "origin", BRANCH)
    tip = env.remote_branch()
    env.set("pr_head", "7\n")
    env.run(sha=SHA_2)
    assert env.remote_branch() == tip
    log = env.gh_log()
    assert "pr comment 7" in log and "--bump --sha" in log and "workflow run" not in log


def test_bot_only_stale_branch_is_updated_with_a_lease(env):
    env.run(sha=SHA_1)
    old = env.remote_branch()
    env.set("pr_head", "7\n")
    env.run(sha=SHA_2)
    new = env.remote_branch()
    assert new and new != old
    assert SHA_2 in git(env.remote, "show", f"{new}:tckdb-pin.toml")
    assert "workflow run ci.yml" in env.gh_log()


def test_pr_creation_failure_after_the_push_opens_a_ready_to_adopt_issue(env):
    env.set("pr_create_fails", "")
    env.run()
    assert env.remote_branch()
    assert "issue create --title TCKDB drift: schemas 0.58 / client 0.98 is ready to adopt" in env.gh_log()


def test_failing_tests_open_one_issue_per_line_pair_and_comment_only_on_change(env):
    env.run(result="fail", summary="```\nFAILED tests/a.py::t\n1 failed\n```\n")
    assert "issue create --title TCKDB drift: schemas 0.58 / client 0.98 breaks the adapter" in env.gh_log()
    assert env.remote_branch() is None
    env.set("issues", "5\n")
    env.set("issue_body", "body <!-- tckdb-drift-state:stale -->")
    env.run(result="fail", summary="```\nFAILED tests/a.py::t\n1 failed\n```\n")
    log = env.gh_log()
    assert "issue edit 5" in log and "issue comment 5" in log  # marker differs from the stored one

    # Same state as stored (marker = hash of versions, result, failing tests only): no comment.
    blob = "0.58.0\n0.98.0\nfail\nFAILED tests/a.py::t\n"
    marker = hashlib.sha256(blob.encode()).hexdigest()[:16]
    env.set("issue_body", f"body <!-- tckdb-drift-state:{marker} -->")
    env.run(result="fail", summary="```\nFAILED tests/a.py::t\n5 failed in 3.1s\n```\n")
    log = env.gh_log()
    assert "issue edit 5" in log and "issue comment" not in log


def test_install_failure_is_titled_install_failed(env):
    env.run(result="install-failed", summary="pip blew up\n")
    assert "TCKDB drift: schemas 0.58 / client 0.98 install failed" in env.gh_log()


def test_no_drift_closes_open_drift_issues(env):
    env.set("issues", "5\n6\n")
    env.run(drift="false")
    log = env.gh_log()
    assert "issue close 5" in log and "issue close 6" in log


def test_a_passing_bump_closes_drift_issues_and_marks_older_prs_superseded(env):
    env.set("issues", "5\n")
    env.set("pr_all", "3:chore/tckdb-drift-schemas-0.56-client-0.96\n")
    env.run()
    log = env.gh_log()
    assert "issue close 5" in log
    assert "pr comment 3" in log and "Superseded" in log


# ---- body size: whatever the inputs, nothing over GitHub's limit is sent ------------------------

GITHUB_LIMIT = 65536
MARKER = re.compile(r"tckdb-drift-state:([0-9a-f]{16})")


def failing_summary(count=160, width=300, last_changed=False):
    """What a drift run with four failing packages produces at its caps: 40 lines of 300 characters each."""
    lines = []
    for i in range(count):
        line = f"FAILED pkg{i % 4}/tests/test_module_{i}.py::test_case - "
        lines.append(line + "y" * (width - len(line)))
    if last_changed:
        lines[-1] = lines[-1][:-1] + "z"
    return "```\n" + "\n".join(lines) + "\n" + "\n".join(f"pkg{i}: 1 failed in 3s" for i in range(4)) + "\n```\n"


def oversized_since():
    return "# Contract changes\n" + "".join(f"## Route {i}\n" + "a long line of the changelog " * 20 + "\n" for i in range(2000))


def assert_all_under_the_limit(env):
    bodies = env.bodies()
    assert bodies, "nothing was sent"
    for body in bodies:
        assert len(body) <= GITHUB_LIMIT and len(body.encode()) <= GITHUB_LIMIT, len(body)
    return bodies


def test_an_oversized_failure_issue_is_cut_with_a_marker_and_keeps_its_state_marker(env):
    env.run(result="fail", summary=failing_summary(), since=oversized_since(), extra_env={"RUN_URL": "http://run/" + "u" * 100000})
    (body,) = assert_all_under_the_limit(env)
    assert "[... truncated:" in body  # the whole body was cut at its limit
    assert MARKER.search(body) and body.rstrip().endswith("-->")  # the dedup marker survives the cut
    assert body.count("```") % 2 == 0


def test_an_oversized_failure_issue_with_normal_urls_keeps_the_whole_tail(env):
    env.run(result="fail", summary=failing_summary(), since=oversized_since())
    (body,) = assert_all_under_the_limit(env)
    assert "[... truncated:" in body and MARKER.search(body)  # the summary and the --since output were cut
    assert "Read the contract diff above" in body and "fetch_tckdb_backend.sh" in body
    assert "### Failing summary" in body and "```bash" in body and body.count("```") % 2 == 0


def test_an_oversized_pull_request_body_is_cut_and_keeps_its_checklist(env):
    env.run(summary=failing_summary(), since=oversized_since(), extra_env={"RUN_URL": "http://run/" + "u" * 100000})
    (body,) = assert_all_under_the_limit(env)
    assert "[... truncated:" in body
    env.set("pr_head", "7\n")  # an edit of the existing pull request: the same limit
    env.run(summary=failing_summary(), since=oversized_since())
    (body,) = assert_all_under_the_limit(env)
    assert "### Before merging (AGENTS.md)" in body and "Each package's changelog entry is complete" in body


def test_a_comment_is_capped_too(env):
    env.set("issues", "5\n")
    env.set("issue_body", "body <!-- tckdb-drift-state:stale -->")
    env.run(result="fail", summary=failing_summary(), extra_env={"RUN_URL": "http://run/" + "u" * 100000})
    bodies = assert_all_under_the_limit(env)
    assert len(bodies) == 2 and "issue comment 5" in env.gh_log()  # the edited body and the comment
    assert any(body.startswith("Updated for TCKDB") and "[... truncated:" in body for body in bodies)


def state_of(env, **kwargs):
    env.run(result="fail", **kwargs)
    (body,) = env.bodies()
    return MARKER.search(body).group(1)


def test_the_dedup_marker_covers_every_failing_line_not_only_the_displayed_ones(env):
    full = failing_summary()
    lines = "\n".join(line for line in full.splitlines() if line.startswith("FAILED"))
    expected = hashlib.sha256(f"0.58.0\n0.98.0\nfail\n{lines}\n".encode()).hexdigest()[:16]
    assert state_of(env, summary=full) == expected  # the same bytes as before the cap existed
    assert state_of(env, summary=full) == expected  # and stable from run to run
    # A failure that differs only in a line beyond the displayed part still changes it.
    assert state_of(env, summary=failing_summary(last_changed=True)) != expected
    # The marker ignores what is not a failing line (the tail lines carry timings).
    assert state_of(env, summary=full.replace("1 failed in 3s", "1 failed in 9s")) == expected


def test_a_stored_marker_of_an_oversized_run_stops_the_repeat_comment(env):
    full = failing_summary()
    marker = state_of(env, summary=full)
    env.set("issues", "5\n")
    env.set("issue_body", f"body <!-- tckdb-drift-state:{marker} -->")
    env.run(result="fail", summary=full)
    assert "issue edit 5" in env.gh_log() and "issue comment" not in env.gh_log()


# ---- what the bodies say ------------------------------------------------------------------------


def test_the_failure_issue_gives_reproduction_steps_that_fit_four_suites(env):
    env.run(result="fail", summary="```\nFAILED a\n```\n")
    (body,) = env.bodies()
    assert "install both packages" not in body
    for needle in (
        "python tools/tckdb_drift.py --bump --sha " + SHA_2,
        "python tools/tckdb_drift.py --install-specs",
        "python tools/tckdb_drift.py --list-packages",
        "bash tools/fetch_tckdb_backend.sh",
        "python -m pytest -p no:xdist -p no:cacheprovider",
        'cd "$d"',
        "not part of a root `pytest` run",
    ):
        assert needle in body, needle


def test_a_setup_failure_is_its_own_kind_of_issue(env):
    env.run(result="setup-failed", summary="```\nthe list was empty\n```\n")
    assert "TCKDB drift: schemas 0.58 / client 0.98 setup failed" in env.gh_log()
    (body,) = env.bodies()
    assert "the drift job stopped before it ran any suite" in body and "the list was empty" in body
    # A person has to look, and the text names what a moved backend directory means for the adapter.
    assert "a person has to look" in body and "backend/app/chemistry" in body and "would then have to follow" in body
    assert "says nothing about the adapter" not in body
    assert "To reproduce locally" not in body and "fetch_tckdb_backend.sh" not in body  # nothing to rerun
    for claim in ("install failed", "could not be installed", "fetch failed"):
        assert claim not in body and claim not in env.gh_log(), claim
    assert env.remote_branch() is None


def test_a_setup_failure_for_packages_on_different_lines_says_which_and_claims_no_install_or_fetch_failure(env):
    summary = (
        "The drift job stopped before it ran any suite. This is not a failing test, but a person has to look:\n```\n"
        "The packages in this repository already state different TCKDB lines, so the bump was not tried and no suite was run:\n"
        "The packages are on different tckdb-schemas lines: 0.54 in tckdb_core; 0.60 in adapters/tckdb_qcschema.\n"
        "A person must bring them onto one line by hand.\n```\n"
    )
    env.run(result="setup-failed", summary=summary)
    assert "TCKDB drift: schemas 0.58 / client 0.98 setup failed" in env.gh_log()
    (body,) = env.bodies()
    assert "0.60 in adapters/tckdb_qcschema" in body and "A person must bring them onto one line by hand" in body
    for claim in ("install failed", "could not be installed", "fetch failed", "Fetching"):
        assert claim not in body, claim
    assert env.remote_branch() is None


@pytest.mark.parametrize("result", ["install-failed", "setup-failed", "fail", "", "PASS", "passed", "pass "])
def test_only_an_exact_pass_reaches_the_pull_request_path(env, result):
    env.run(result=result, summary="```\n1 failed\n```\n")
    assert env.remote_branch() is None
    assert "pr create" not in env.gh_log() and "workflow run" not in env.gh_log()
    assert "issue create" in env.gh_log()


def test_the_pull_request_body_describes_each_changelog_style_and_the_floor_raise(env):
    env.run()
    (body,) = env.bodies()
    assert "raised the `tckdb-adapters-core` floor of each package that requires it" in body
    item = next(line for line in body.splitlines() if "changelog entry is complete" in line)
    assert "writes a section that names the TCKDB commit, the bounds that moved and that the package's code is unchanged" in item
    assert "writes one line under the tracked-releases marker" in item and "says nothing about the code" in item
    assert "different lines" not in body and "keeps its line" not in body
    # The branch's ARC changelog line is what the body says it is, floor included.
    changelog = git(env.remote, "show", f"{env.remote_branch()}:adapters/tckdb_arc/CHANGELOG.md")
    assert "requires tckdb-adapters-core>=0.1.1." in changelog


# ---- the reproduction steps in the failure issue, run -------------------------------------------

REPRO_PYTHON_SHIM = """#!/bin/bash
if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
  echo "$PWD|${TCKDB_BACKEND_PATH:-unset}|${TCKDB_REQUIRE_BACKEND:-unset}|$(cat "${TCKDB_BACKEND_PATH:-/nonexistent}/app/chemistry/marker.txt" 2>/dev/null)" >> "$FAKE/pytest.log"
  exit 0
fi
exec "{python}" "$@"
"""


def reproduction_block(env) -> list[str]:
    """The lines of the failure issue's bash block that follow the --bump command."""
    env.run(result="fail", summary="```\n1 failed\n```\n")
    (body,) = env.bodies()
    block = re.search(r"```bash\n(.*?)```", body, re.S).group(1).splitlines()
    assert block[0].startswith("python tools/tckdb_drift.py --bump")
    return block[1:]


class Repro:
    """A checkout with the tools and a local stand-in for TCKDB, where the issue's block can be run."""

    def __init__(self, tmp: Path, block: list[str]):
        self.block = "\n".join(block)
        self.root = fx.build(tmp / "checkout")
        (self.root / "tools").mkdir()
        for name in ("tckdb_drift.py", "fetch_tckdb_backend.sh"):
            shutil.copy(REPO / "tools" / name, self.root / "tools" / name)
        self.url, _, self.new_sha = fx.make_tckdb(tmp)
        self.set_pin(self.url)
        self.fake = tmp / "fake"
        self.fake.mkdir()
        self.tmpdir = tmp / "tmpdir"  # where mktemp puts its files, so a second run can be compared
        self.tmpdir.mkdir()
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name, text in (("python", REPRO_PYTHON_SHIM.replace("{python}", sys.executable)),
                           ("pip", '#!/bin/bash\necho "pip $*" >> "$FAKE/pip.log"\n')):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.bin = bin_dir

    def set_pin(self, url: str) -> None:
        (self.root / fx.drift.PIN_FILE).write_text(fx.pin_text(url, self.new_sha))

    def run(self, shell: str = "bash") -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE": str(self.fake), "TMPDIR": str(self.tmpdir)}
        for name in ("TCKDB_BACKEND_PATH", "TCKDB_REQUIRE_BACKEND"):
            env.pop(name, None)
        return subprocess.run([shell, "-c", self.block], cwd=self.root, env=env, capture_output=True, text=True)

    def suites(self) -> list[list[str]]:
        path = self.fake / "pytest.log"
        return [line.split("|") for line in path.read_text().splitlines()] if path.exists() else []


def test_the_reproduction_block_has_no_comment_lines_and_no_unbalanced_quote(env):
    """An interactive zsh does not treat `#` as a comment (interactive_comments is off by default), so
    a comment line pasted into it is run as a command, and an apostrophe in one opens a quote that
    swallows the rest. The block's claim to paste into zsh therefore rests on this check: the zsh
    tests below run it with `zsh -c`, which accepts comments, and cannot catch that."""
    for line in reproduction_block(env):
        assert not line.lstrip().startswith("#"), line
        assert " #" not in line, line  # nor a trailing comment
        assert line.count("'") % 2 == 0, line


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_the_reproduction_block_exports_the_backend_variables_every_time_it_is_pasted(env, tmp_path, shell):
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is not installed here")
    repro = Repro(tmp_path / "r", reproduction_block(env))
    backends = []
    for attempt in range(2):  # a second reproduction on the same machine must work too
        proc = repro.run(shell)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        runs = repro.suites()[attempt * len(fx.DIRECTORIES):]
        assert len(runs) == len(fx.DIRECTORIES)  # every suite, each from its own directory
        assert [Path(cwd).relative_to(repro.root).as_posix() for cwd, *_ in runs] == fx.DIRECTORIES
        for _cwd, backend, require, marker in runs:
            assert backend != "unset" and require == "1" and marker == "NEW"  # both variables, at the new commit
        backends.append(runs[0][1])
    assert backends[0] != backends[1] and all(Path(b).is_dir() for b in backends)  # a fresh directory each time
    # The install line gives every package its own extra, whatever the shell does with brackets.
    installs = (repro.fake / "pip.log").read_text()
    for directory in fx.DIRECTORIES:
        assert f"-e ./{directory}[test]" in installs


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_a_failing_fetch_stops_the_reproduction_block_before_any_suite(env, tmp_path, shell):
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is not installed here")
    repro = Repro(tmp_path / "r", reproduction_block(env))
    repro.set_pin(f"file://{tmp_path}/no-such-repository")
    proc = repro.run(shell)
    assert proc.returncode != 0
    assert repro.suites() == []  # nothing ran against an unset backend


# ---- the dedup marker is what it always was -----------------------------------------------------

MAIN_HASH = r"""
SUMMARY="$(cat "$1")"
printf '%s\n%s\n%s\n%s\n' "$2" "$3" "$4" "$(grep -E '^(FAILED|ERROR) ' <<<"$SUMMARY" || true)" | sha256sum | cut -c1-16
"""


def main_marker(path: Path, result: str = "fail") -> str:
    """The marker as this script computed it before it capped any body: the same lines, in bash."""
    out = subprocess.run(["bash", "-c", MAIN_HASH, "x", str(path), "0.58.0", "0.98.0", result], capture_output=True, text=True)
    return out.stdout.strip()


SUMMARIES = {
    "failures": "```\npkg: 2 failed in 1s\nFAILED pkg/tests/a.py::t - boom\nERROR pkg/tests/b.py\n```\n",
    "ordinary": failing_summary(count=40, width=120),
    "near the test job's cut": failing_summary(count=125, width=300),
    "none": "```\npkg: 4 passed in 0.1s\n```\n",
    "empty": "",
    "no trailing newline": "FAILED a::t\nERROR b",
    "trailing blank lines": "FAILED a::t\n\n\n",
    "crlf": "FAILED a::t\r\nERROR b\r\n",
    "indented lines are not failures": "  FAILED a::t\nxFAILED b\nFAILED c\n",
    "non-ascii": "FAILED a::t - café ✓\n",
}


@pytest.mark.parametrize("name", sorted(SUMMARIES))
def test_the_dedup_marker_is_byte_identical_to_the_one_computed_before_bodies_were_capped(env, name):
    """No open issue gets a spurious comment because of the cap: for any summary the test job can
    write under its own cuts, the marker is the one the script computed before it capped anything."""
    summary = SUMMARIES[name]
    path = env.tmp / "expected-summary.md"
    path.write_bytes(summary.encode())
    assert len(summary.encode()) <= 40000  # under the test job's cut
    assert state_of(env, summary=summary) == main_marker(path)


def test_the_dedup_marker_of_a_summary_with_a_nul_byte_is_still_the_old_one(env):
    """The one input where a naive grep of the file would differ from the old computation."""
    summary = b"FAILED a::t\nERROR \x00b\n"
    path = env.tmp / "expected-summary.md"
    path.write_bytes(summary)
    assert state_of(env, summary=summary) == main_marker(path)


# ---- the comment on closing an issue is capped too ----------------------------------------------


def test_the_comment_on_closing_an_issue_is_capped(env):
    env.set("issues", "5\n")
    env.run(drift="false", extra_env={"RUN_URL": "http://run/" + "u" * 100000})
    bodies = assert_all_under_the_limit(env)
    assert "issue close 5" in env.gh_log()
    assert len(bodies) == 1 and bodies[0].startswith("No TCKDB drift as of") and "[... truncated:" in bodies[0]
