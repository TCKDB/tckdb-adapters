#!/usr/bin/env bash
# Fetch TCKDB's identity rules (backend/app/chemistry) at the commit of a pip install spec.
#
#   fetch_tckdb_backend.sh <tckdb-schemas pip spec> <destination directory>
#
# The spec is the TCKDB_SCHEMAS_SPEC that `python tools/tckdb_drift.py --install-specs`
# prints (`tckdb-schemas @ git+<repo>@<sha>#subdirectory=...`), so the backend is read from
# the same repository and commit the tckdb packages are installed from, and a --bump moves
# both. Used by ci.yml and by tckdb-drift.yml.
#
# Prints, on stdout and nothing else, the two variables the suites need:
#   TCKDB_BACKEND_PATH=<destination>/backend
#   TCKDB_REQUIRE_BACKEND=1
# (append them to $GITHUB_ENV, or export them). TCKDB_REQUIRE_BACKEND=1 makes the comparison
# tests fail, not skip, when the backend cannot be imported. Exits non-zero, printing nothing
# on stdout, when the repository, the commit or the directory is missing.
set -euo pipefail

spec="${1:?usage: fetch_tckdb_backend.sh <tckdb-schemas pip spec> <destination directory>}"
dest="${2:?usage: fetch_tckdb_backend.sh <tckdb-schemas pip spec> <destination directory>}"

spec="${spec#*git+}"
repo="${spec%%@*}"
sha="${spec#*@}"
sha="${sha%%#*}"
if [ -z "$repo" ] || [ -z "$sha" ] || [ "$repo" = "$spec" ] || [ "$sha" = "$spec" ]; then
  echo "fetch_tckdb_backend.sh: cannot read <repo>@<sha> from the spec: $1" >&2
  exit 1
fi

git clone --quiet --filter=blob:none --no-checkout "$repo" "$dest" >&2
git -C "$dest" sparse-checkout set backend/app/chemistry >&2
git -C "$dest" checkout --quiet "$sha" >&2
if [ ! -d "$dest/backend/app/chemistry" ]; then
  echo "fetch_tckdb_backend.sh: $repo at $sha has no backend/app/chemistry" >&2
  exit 1
fi
echo "TCKDB_BACKEND_PATH=$dest/backend"
echo "TCKDB_REQUIRE_BACKEND=1"
