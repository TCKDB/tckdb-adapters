#!/usr/bin/env bash
# Fetch the TCKDB backend modules the comparison tests import, at the commit of a pip install spec:
# the identity rules (backend/app/chemistry) and the ORCA and Gaussian deck parsers
# (backend/app/services/{orca,gaussian}_parameter_parser.py with what they import at module level:
# parameter_closed_values.py, app/parser_registry.py, app/versioning.py and the empty package
# __init__.py files). None of them needs sqlalchemy or fastapi; the heavier services modules
# (calculation_resolution, ...) are not fetched, and the tests that need them skip or are told so.
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
# Non-cone patterns name the few files wanted; cone mode would take all of backend/app/services.
git -C "$dest" sparse-checkout set --no-cone \
  /backend/app/chemistry/ \
  /backend/app/__init__.py /backend/app/parser_registry.py /backend/app/versioning.py \
  /backend/app/services/__init__.py /backend/app/services/parameter_closed_values.py \
  /backend/app/services/orca_parameter_parser.py /backend/app/services/gaussian_parameter_parser.py >&2
git -C "$dest" checkout --quiet "$sha" >&2
if [ ! -d "$dest/backend/app/chemistry" ]; then
  echo "fetch_tckdb_backend.sh: $repo at $sha has no backend/app/chemistry" >&2
  exit 1
fi
for file in app/__init__.py app/parser_registry.py app/versioning.py app/services/__init__.py \
    app/services/parameter_closed_values.py app/services/orca_parameter_parser.py \
    app/services/gaussian_parameter_parser.py; do
  if [ ! -f "$dest/backend/$file" ]; then
    echo "fetch_tckdb_backend.sh: $repo at $sha has no backend/$file (the deck parsers the comparison tests import)" >&2
    exit 1
  fi
done
echo "TCKDB_BACKEND_PATH=$dest/backend"
echo "TCKDB_REQUIRE_BACKEND=1"
