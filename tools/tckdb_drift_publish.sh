#!/usr/bin/env bash
# Publish step of .github/workflows/tckdb-drift.yml: open or update the bump PR, or
# the issue that explains why there is no PR. Kept out of the workflow so it can be
# exercised with a bare remote and a fake `gh` (adapters/tckdb_arc/tests/test_tckdb_drift_publish.py).
#
# Environment (all set by the workflow):
#   GH_TOKEN REPO RUN_URL DRIFT(true|false)
#   when DRIFT=true: RESULT(pass|fail|install-failed|setup-failed) SCHEMAS CLIENT SHA SCHEMAS_LINE
#                    CLIENT_LINE OLD_SCHEMAS SINCE_FILE SUMMARY_FILE
#   optional: PUSH_URL (default: https://x-access-token:$GH_TOKEN@github.com/$REPO.git)
set -euo pipefail

# GitHub rejects an issue, pull request or comment body of more than 65,536 characters. Every
# body this script sends to gh (an issue or pull request body, a comment, the comment on closing an
# issue) goes through cap_file or cap_arg first (tools/tckdb_drift.py --cap-text cuts by bytes,
# which is never fewer than characters), whatever the inputs are.
BODY_LIMIT=60000
SECTION_LIMIT=20000   # the failing-test summary and the --since output get this much each
TOOL="$(dirname "${BASH_SOURCE[0]}")/tckdb_drift.py"

BOT_NAME="github-actions[bot]"
BOT_EMAIL="41898282+github-actions[bot]@users.noreply.github.com"
ISSUE_PREFIX="TCKDB drift: "
LABEL=tckdb-drift

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

labels="$(gh label list --limit 200 --json name -q '.[].name' 2>/dev/null || true)"
label_args=()
if grep -qx "$LABEL" <<<"$labels"; then label_args=(--label "$LABEL"); fi

cap_text() {  # $1 = byte limit; stdin -> stdout, cut with a marker when longer
  python "$TOOL" --cap-text "$1"
}

cap_file() {  # $1 = file, $2 = limit (default BODY_LIMIT); rewritten in place
  cap_text "${2:-$BODY_LIMIT}" < "$1" > "$1.cap"
  mv "$1.cap" "$1"
}

cap_arg() {  # $1 = text; prints it capped to BODY_LIMIT (assign the result, so a failure stops the script)
  printf '%s' "$1" | cap_text "$BODY_LIMIT"
}

open_issue_numbers() {  # $1 = title prefix
  gh issue list --state open --limit 200 --json number,title \
    -q ".[] | select(.title | startswith(\"$1\")) | .number"
}

close_drift_issues() {  # $1 = reason
  local n comment
  for n in $(open_issue_numbers "$ISSUE_PREFIX"); do
    comment="$(cap_arg "$1")"
    gh issue close "$n" --comment "$comment"
  done
}

if [ "$DRIFT" != true ]; then
  close_drift_issues "No TCKDB drift as of ${RUN_URL}; closing."
  exit 0
fi

SHA7="${SHA:0:7}"
PAIR="schemas ${SCHEMAS_LINE} / client ${CLIENT_LINE}"
BRANCH="chore/tckdb-drift-schemas-${SCHEMAS_LINE}-client-${CLIENT_LINE}"
BUMP_CMD="python tools/tckdb_drift.py --bump --sha $SHA --schemas $SCHEMAS --client $CLIENT"
PUSH_URL="${PUSH_URL:-https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git}"
# The --since output carries its own headings; nest them under this page's sections.
SINCE="$(cap_text "$SECTION_LIMIT" < "$SINCE_FILE" | sed -E 's/^#{1,6}[[:space:]]+/#### /')"
SUMMARY="$(cap_text "$SECTION_LIMIT" < "$SUMMARY_FILE")"
# The dedup marker hashes the failing-test lines of the summary file as the test job wrote it, not
# of the display text above, so cutting the display changes nothing. (The test job cuts the summary
# itself at 40 lines per package and 40,000 bytes in all, and writes the tallies first; the marker
# covers the failing-test lines that survived those cuts.) For a summary under those cuts the lines
# are exactly the ones this script hashed before it capped anything, the same way: via a command
# substitution and a here-string.
SUMMARY_FULL="$(cat "$SUMMARY_FILE")"
FAILED_LINES="$(grep -E '^(FAILED|ERROR) ' <<<"$SUMMARY_FULL" || true)"

# Create the issue for this line pair, or update the open one (title and body);
# comment only when the versions or the failing tests changed since the last body.
upsert_issue() {  # $1 kind (breaks the adapter|install failed|is ready to adopt), $2 body file
  local title="${ISSUE_PREFIX}${PAIR} $1" n old state
  state="$(printf '%s\n%s\n%s\n%s\n' "$SCHEMAS" "$CLIENT" "$RESULT" \
           "$FAILED_LINES" | sha256sum | cut -c1-16)"
  cap_file "$2" $((BODY_LIMIT - 1000))   # room for the marker, which must survive the cut
  printf '\n<!-- tckdb-drift-state:%s -->\n' "$state" >> "$2"
  cap_file "$2"
  n="$(open_issue_numbers "${ISSUE_PREFIX}${PAIR} " || true)"
  n="${n%%$'\n'*}"
  if [ -n "$n" ]; then
    old="$(gh issue view "$n" --json body -q .body)"
    gh issue edit "$n" --title "$title" --body-file "$2"
    if ! grep -qF "tckdb-drift-state:${state}" <<<"$old"; then
      comment="$(cap_arg "Updated for TCKDB ${SHA7} (schemas ${SCHEMAS}, client ${CLIENT}): ${RUN_URL}")"
      gh issue comment "$n" --body "$comment"
    fi
  else
    gh issue create --title "$title" --body-file "$2" "${label_args[@]}" \
      || gh issue create --title "$title" --body-file "$2"
  fi
}

ready_issue() {  # $1 = why there is no PR
  cat > "$tmp/issue.md" <<BODY
TCKDB ${SHA7} (schemas ${SCHEMAS}, client ${CLIENT}) passes the adapter's tests, but $1

Run: ${RUN_URL}

To adopt it by hand: \`${BUMP_CMD}\`, read the contract diff below, then open a PR.

### \`python -m tckdb_schemas.contract --since ${OLD_SCHEMAS}\`
${SINCE}
BODY
  upsert_issue "is ready to adopt" "$tmp/issue.md"
}

reproduce() {  # prints the steps that rerun this job's suites on a developer machine
  local steps
  steps="$(cat <<'REPRO'
To reproduce locally, in a clean virtualenv at a checkout of this repository (paste into bash or zsh). The suites run one at a time and each from its own package directory, as the workflow runs them: the chemkin and qcschema suites are not part of a root `pytest` run (AGENTS.md, "Running tests"). The block has no comment lines, so it pastes into an interactive zsh as well. Its last command fetches TCKDB's backend identity rules at that commit into a fresh directory (the comparison tests skip without them) and runs the suites only if the fetch worked.

```bash
@BUMP_CMD@
specs="$(mktemp)" && python tools/tckdb_drift.py --install-specs > "$specs"
spec() { sed -n "s/^$1=//p" "$specs"; }
pip install "$(spec TCKDB_CLIENT_SPEC)" "$(spec TCKDB_SCHEMAS_SPEC)"
editable=(); for d in $(python tools/tckdb_drift.py --list-packages); do editable+=(-e "./${d}[test]"); done
pip install "${editable[@]}"
backend_env="$(mktemp)" && bash tools/fetch_tckdb_backend.sh "$(spec TCKDB_SCHEMAS_SPEC)" "$(mktemp -d)/tckdb" > "$backend_env" \
  && [ -s "$backend_env" ] && export $(cat "$backend_env") \
  && for d in $(python tools/tckdb_drift.py --list-packages); do
    (cd "$d" && python -m pytest -p no:xdist -p no:cacheprovider)
  done
```
REPRO
)"
  printf '%s\n' "${steps//@BUMP_CMD@/$BUMP_CMD}"
}

if [ "$RESULT" != pass ]; then
  case "$RESULT" in
    install-failed)
      kind="install failed"; desc="could not be installed with the adapter's pins" ;;
    setup-failed)
      kind="setup failed"; desc="could not be tested: the drift job stopped before it ran any suite (see the summary for why)" ;;
    *)
      kind="breaks the adapter"; desc="does not work with the adapter's tests" ;;
  esac
  if [ "$RESULT" = setup-failed ]; then
    steps=""
    closing="This is not a failing test, but a person has to look: the cause named in the summary may be in this repository's pins, in the drift job, or in TCKDB itself (for example a moved or removed \`backend/app/chemistry\` or deck parser module under \`backend/app/services\`, which the adapter's comparisons with TCKDB's own code would then have to follow). The next scheduled run, or a manual dispatch, tries again."
  else
    steps="$(reproduce)"
    closing="Read the contract diff above (AGENTS.md) and fix the adapter to conform before moving the pins."
  fi
  cat > "$tmp/issue.md" <<BODY
TCKDB ${SHA7} (schemas ${SCHEMAS}, client ${CLIENT}) ${desc}.

Run: ${RUN_URL}

### Failing summary
${SUMMARY}

### \`python -m tckdb_schemas.contract --since ${OLD_SCHEMAS}\`
${SINCE}

${steps}

${closing}
BODY
  upsert_issue "$kind" "$tmp/issue.md"
  exit 0
fi

# ---- tests passed: build the bump branch ---------------------------------
TITLE="Track TCKDB: schemas ${SCHEMAS} / client ${CLIENT} (TCKDB ${SHA7})"
git config user.name "$BOT_NAME"
git config user.email "$BOT_EMAIL"
BASE_SHA="$(git rev-parse HEAD)"
python "$TOOL" --bump --sha "$SHA" --schemas "$SCHEMAS" --client "$CLIENT"
cat > "$tmp/pr.md" <<BODY
Automated bump by \`tckdb-drift.yml\` (run: ${RUN_URL}).

- tckdb-schemas ${SCHEMAS}, tckdb-client ${CLIENT}, TCKDB ${SHA}
- \`tools/tckdb_drift.py --bump\` moved the pyproject bounds of every package that states them, \`TARGET_SCHEMAS_LINE\`, \`tckdb-pin.toml\` and the README install lines and version sentences. It released each package one of whose own files changed: patch version in \`pyproject.toml\` (and \`__version__\` where the package has one) and an entry in its \`CHANGELOG.md\`. When it released the core, it also raised the \`tckdb-adapters-core\` floor of each package that requires it to the new core version, which releases that package as well; the changelog line of \`tckdb-arc\` says so.

### What the bot already did
It ran the test suite of every package against the new TCKDB packages with the target already moved, with TCKDB's backend identity rules fetched at the new commit for the comparison tests, and each suite passed. It did not read the contract change: that is your job. Read the \`--since\` changelog below (and \`python -m tckdb_schemas.contract --print\`), decide whether the adapter handles what moved, then merge or close. CI on this PR re-runs the suite.

${SUMMARY}

### \`python -m tckdb_schemas.contract --since ${OLD_SCHEMAS}\`
${SINCE}

### Before merging (AGENTS.md)
- [ ] I read the \`--since\` changelog above.
- [ ] Every changed route, field or refusal code is handled by the adapter or judged irrelevant.
- [ ] No producer convention was defaulted to fit a new required field (refuse to build the block instead).
- [ ] Each package's changelog entry is complete. For \`tckdb-adapters-core\`, \`tckdb-chemkin\` and \`tckdb-qcschema\` the bot writes a section that names the TCKDB commit, the bounds that moved and that the package's code is unchanged (the \`tckdb-qcschema\` section also says its request body differs, because it records the adapter version). For \`tckdb-arc\` it writes one line under the tracked-releases marker that names the TCKDB commit and versions and, when it was raised, the core floor; that line says nothing about the code. The bot cannot know what the new contract does to a package's behaviour or payloads; add that (a new refusal, a changed rule, a changed payload and its idempotency-key consequence) to the entries it applies to.
BODY

git switch -C "$BRANCH"
# Stage every file tools/tckdb_drift.py --bump can rewrite (the tool names them; none is listed here).
mapfile -t bumped < <(python "$TOOL" --bumped-files)
[ "${#bumped[@]}" -gt 0 ] || { echo "tckdb_drift.py --bumped-files listed no files" >&2; exit 1; }
git add -- "${bumped[@]}"
git diff --cached --quiet || git commit -q -m "$TITLE"

pushed=false
remote_sha="$(git ls-remote --heads "$PUSH_URL" "refs/heads/${BRANCH}" | cut -f1)"
human_commits=false
if [ -n "$remote_sha" ]; then
  git fetch -q "$PUSH_URL" "+refs/heads/${BRANCH}:refs/remotes/drift/${BRANCH}"
  if [ "$(git rev-parse "${remote_sha}^{tree}")" = "$(git rev-parse 'HEAD^{tree}')" ]; then
    echo "remote ${BRANCH} already has this tree; not pushing"
  else
    # Commits on the branch that are not on main: any not made by the bot stop the push.
    others="$(git log --format='%ae %ce' "${BASE_SHA}..${remote_sha}" | grep -vxF "$BOT_EMAIL $BOT_EMAIL" || true)"
    if [ -n "$others" ]; then
      human_commits=true
    elif git push -q --force-with-lease="refs/heads/${BRANCH}:${remote_sha}" "$PUSH_URL" "HEAD:refs/heads/${BRANCH}"; then
      pushed=true
    else
      ready_issue "the workflow could not update ${BRANCH} (push failed)."
      exit 0
    fi
  fi
elif git push -q "$PUSH_URL" "HEAD:refs/heads/${BRANCH}"; then
  pushed=true
else
  ready_issue "the workflow could not push ${BRANCH} (check the repository's Actions permissions)."
  exit 0
fi

pr="$(gh pr list --head "$BRANCH" --state open --json number -q '.[0].number // empty')"

if [ "$human_commits" = true ]; then
  # Someone added commits to the bot branch: leave it alone and say so once per TCKDB commit.
  msg="TCKDB ${SHA7} (schemas ${SCHEMAS}, client ${CLIENT}) is newer than this branch, but the branch has commits not made by the bot, so it was not updated. To adopt the newer state: \`${BUMP_CMD}\`.<!-- tckdb-drift-newer:${SHA} -->"
  if [ -n "$pr" ]; then
    seen="$(gh pr view "$pr" --json comments -q '.comments[].body' || true)"
    if ! grep -qF "tckdb-drift-newer:${SHA}" <<<"$seen"; then
      msg="$(cap_arg "$msg")"
      gh pr comment "$pr" --body "$msg"
    fi
  else
    ready_issue "the existing branch ${BRANCH} has human commits and no open PR, so it was not updated."
  fi
  exit 0
fi

if [ -n "$pr" ]; then
  cap_file "$tmp/pr.md"
  gh pr edit "$pr" --title "$TITLE" --body-file "$tmp/pr.md"
else
  cap_file "$tmp/pr.md"
  if ! { gh pr create --base main --head "$BRANCH" --title "$TITLE" --body-file "$tmp/pr.md" "${label_args[@]}" \
         || gh pr create --base main --head "$BRANCH" --title "$TITLE" --body-file "$tmp/pr.md"; }; then
    ready_issue "the workflow pushed ${BRANCH} but could not open the PR."
    exit 0
  fi
fi

# A PR or push made with GITHUB_TOKEN does not trigger workflows; start CI once per real push.
if [ "$pushed" = true ]; then
  gh workflow run ci.yml --ref "$BRANCH" || echo "::warning::could not dispatch ci.yml on ${BRANCH}"
fi

close_drift_issues "TCKDB ${SHA7} (${PAIR}) now has a passing bump branch \`${BRANCH}\`; closing."

# Older bot PRs for other line pairs are superseded (commented once, not closed).
for row in $(gh pr list --state open --json number,headRefName \
      -q '.[] | select(.headRefName | startswith("chore/tckdb-drift-")) | "\(.number):\(.headRefName)"'); do
  n="${row%%:*}"; b="${row#*:}"
  [ "$b" = "$BRANCH" ] && continue
  seen="$(gh pr view "$n" --json comments -q '.comments[].body' || true)"
  if ! grep -qF "tckdb-drift-superseded:${BRANCH}" <<<"$seen"; then
    superseded="$(cap_arg "Superseded by \`${BRANCH}\` (schemas ${SCHEMAS}, client ${CLIENT}). Close this PR once that one is handled.<!-- tckdb-drift-superseded:${BRANCH} -->")"
    gh pr comment "$n" --body "$superseded"
  fi
done
