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
    --body) printf '%s' "${args[$((i + 1))]}" > "$FAKE/bodies/$(date +%s%N)-$$" ;;
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
        (self.tmp / "summary.md").write_text(summary)
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
    assert "tckdb-pin.toml" in changed
    # Everything the bump rewrote was staged and committed: nothing is left over in the work tree.
    assert git(env.work, "status", "--porcelain") == ""
    assert {"adapters/tckdb_chemkin/pyproject.toml", "adapters/tckdb_qcschema/CHANGELOG.md", "tckdb_core/tckdb_core/__init__.py"} <= changed
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
    assert "the drift job's own setup failed before any suite ran" in body
    assert env.remote_branch() is None


def test_the_pull_request_body_describes_each_changelog_style_and_the_floor_raise(env):
    env.run()
    (body,) = env.bodies()
    assert "raised the `tckdb-adapters-core` floor of each package that requires it" in body
    item = next(line for line in body.splitlines() if "changelog entry is complete" in line)
    assert "writes a section that names the TCKDB commit, the bounds that moved and that the package's code is unchanged" in item
    assert "writes one line under the tracked-releases marker" in item and "says nothing about the code" in item
    assert "different lines" not in body  # the packages agree here
    # The branch's ARC changelog line is what the body says it is, floor included.
    changelog = git(env.remote, "show", f"{env.remote_branch()}:adapters/tckdb_arc/CHANGELOG.md")
    assert "requires tckdb-adapters-core>=0.1.1." in changelog


def test_a_package_ahead_of_the_bump_is_left_alone_and_the_pull_request_says_so(tmp_path):
    def ahead(seed):
        path = seed / "adapters/tckdb_qcschema/pyproject.toml"
        path.write_text(path.read_text().replace(">=0.54,<0.55", ">=0.60,<0.61"))

    env = Env(tmp_path, seed_edit=ahead)
    env.run()
    (body,) = env.bodies()
    assert "### The packages are on different lines" in body
    assert "tckdb-schemas lines: 0.58 in tckdb_core, adapters/tckdb_arc; 0.60 in adapters/tckdb_qcschema" in body
    assert "leaves one that is already ahead where it is" in body
    assert env.remote_branch()  # not refused: the run went on to push the branch
    qcschema = git(env.remote, "show", f"{env.remote_branch()}:adapters/tckdb_qcschema/pyproject.toml")
    assert "tckdb-schemas>=0.60,<0.61" in qcschema and "tckdb-client>=0.98,<0.99" in qcschema
