# Live TCKDB integration gate

The unit suites validate payloads against the shipped schemas, but they never show what
TCKDB stores. The live gate in `tckdb_arc/tests/integration/` uploads the test corpora
through the adapter's real transport (`tckdb-client`) to a TCKDB backend that you run
locally, then reads the rows back through the REST API.

The gate is opt-in. Without `TCKDB_INTEGRATION_URL` its live tests skip and only the
offline guard tests run.

## Safety rules

The gate writes to whatever answers at the URL, so the fixture refuses a target unless all
three of these hold:

- the URL's host is `127.0.0.1`, `localhost` or `::1`;
- its port is not 8010;
- `GET /status` reports an artifact bucket whose name starts with `tckdb-integ`.

Loopback alone does not prove isolation. A TCKDB deployment and a development stack both
listen on 127.0.0.1:8010, and an SSH tunnel makes a remote deployment loopback too. That
is why the port is refused and the bucket name is checked. Deployments use the bucket
`tckdb-artifacts`.

A target that fails a check fails the run; it does not skip. The checks are made by a
client that holds no API key, and the key leaves the process only after they pass. The
adapter reads its key from `TCKDB_INTEGRATION_API_KEY`, so an ambient `TCKDB_API_KEY` is
never used. Never point the gate at a shared or production TCKDB.

The guards and the fixture are `tckdb_core.testing.live`. A new adapter's gate registers
the same fixture and inherits the same rules.

## Prerequisites

- A TCKDB checkout at the pinned commit (`tckdb-pin.toml`), with its backend installed in
  an environment that has Alembic and uvicorn (`pip install -e "$TCKDB/backend"`).
- An environment with this repository's two packages and the pinned `tckdb-client` and
  `tckdb-schemas`.
- Docker, for the throwaway database and object store.

## Bring up an isolated backend

Use a throwaway database and object store on ports that do not collide with a
development stack (which uses 5432, 9000 and 8010). Keep generated files out
of the TCKDB checkout.

```bash
export TCKDB=/path/to/TCKDB WORK=$(mktemp -d)

docker run -d --name tckdb-integ-db -p 127.0.0.1:55432:5432 \
  -e POSTGRES_USER=tckdb -e POSTGRES_PASSWORD=integpw -e POSTGRES_DB=tckdb_integ \
  -e LANG=C.UTF-8 -e POSTGRES_INITDB_ARGS=--encoding=UTF8 \
  informaticsmatters/rdkit-cartridge-debian:Release_2025_03_3
docker run -d --name tckdb-integ-minio -p 127.0.0.1:59000:9000 \
  -e MINIO_ROOT_USER=integ -e MINIO_ROOT_PASSWORD=integsecret123 \
  minio/minio server /data
until docker exec tckdb-integ-db pg_isready -q -U tckdb -d tckdb_integ; do sleep 1; done
until docker exec tckdb-integ-minio mc ready local >/dev/null 2>&1; do sleep 1; done
docker exec tckdb-integ-minio sh -c \
  'mc alias set l http://127.0.0.1:9000 integ integsecret123 && mc mb l/tckdb-integ-artifacts'

cat > "$WORK/integ.env" <<'EOF'
export DB_HOST=127.0.0.1 DB_PORT=55432 DB_NAME=tckdb_integ DB_USER=tckdb DB_PASSWORD=integpw
export S3_ENDPOINT_URL=http://127.0.0.1:59000 S3_ACCESS_KEY=integ S3_SECRET_KEY=integsecret123
export S3_BUCKET=tckdb-integ-artifacts S3_REGION=us-east-1
export DEPLOYMENT_MODE=local TCKDB_INLINE_WORKER=true
export RATE_LIMIT_ENABLED=false SESSION_COOKIE_SECURE=false
EOF
source "$WORK/integ.env"

cd "$TCKDB/backend"
alembic upgrade head
uvicorn main:app --host 127.0.0.1 --port 58010 > "$WORK/api.log" 2>&1 &
until curl -sf http://127.0.0.1:58010/api/v1/readyz >/dev/null; do sleep 1; done
PYTHONPATH=. TCKDB_BOOTSTRAP_PASSWORD=integ-pass \
  python scripts/bootstrap_admin.py --username integ --email integ@local

B=http://127.0.0.1:58010/api/v1
curl -s -c "$WORK/cookies" -H 'Content-Type: application/json' \
  -d '{"username":"integ","password":"integ-pass"}' "$B/auth/login"
# Retried: the new session may not be committed yet when login returns (see T4 below).
until curl -sf -b "$WORK/cookies" -H 'Content-Type: application/json' \
  -d '{"label":"integ"}' "$B/auth/api-keys" > "$WORK/key.json"; do sleep 1; done
```

Set every `DB_*` and `S3_*` variable explicitly: Alembic loads
`backend/.env` for any variable left unset, which points at the development
database. The API must see `SESSION_COOKIE_SECURE=false` for the plain-HTTP
login above, and `RATE_LIMIT_ENABLED=false` because the gate makes more than
30 authenticated writes a minute.

## Run the gate

```bash
cd /path/to/tckdb-adapters
export TCKDB_INTEGRATION_URL=http://127.0.0.1:58010/api/v1
export TCKDB_INTEGRATION_API_KEY=$(python -c "import json; print(json.load(open('$WORK/key.json'))['key'])")
python -m pytest tckdb_arc/tests/integration -q -rx
```

Project labels are fixed, so a second run against the same database replays every upload
and adds no rows. The checks allow for that, and a run should pass both on an empty
database and on replay.

## What it checks

The corpora are defined in `tckdb_arc/tests/integration/_live.py`. For each one the gate
uploads in every mode that applies and reads the stored rows back:

| Area | Read back |
|---|---|
| Species, conformers, calculations | Calculation owners and types, the stored level of theory and its hash, Hessian values and frame, the Hessian parser stamp. |
| Thermo | Which enthalpy content was kept or stripped, the reference kind, an unstated reference pressure stored as null, source calculations. |
| Energy corrections | Atom-energy and bond-correction totals, units, components and the source calculation. |
| Kinetics | The Arrhenius parameters with `t0_k`, the source-calculation roles, the declared energy level, the mixed-spin and one-electron Hartree-Fock cases. |
| Transition states | Composition against both sides of the reaction, the IRC result, the three kinds of validation evidence, the atom map. |
| Isotopes | Labelled Hessian and scan-point geometries of a deuterated species. |
| Artifacts | Each stored artifact's SHA-256 and size against the local file. |
| Deliberate refusals | A geometry that contradicts its SMILES stereo, and one that contradicts its composition, are refused with 422 and the refusal is recorded in the sidecar. |

Every successful mode is also run as a whole sweep twice. The second pass must replay each
upload with the same key and body, and the row counts of calculations, thermo, kinetics,
conformer observations, transition states, energy corrections and geometries must not
move.

A known gap in the adapter is pinned as a strict `xfail` with `raises=AssertionError`.
Fixing the gap then makes the gate fail until the marker is removed, and a setup failure,
which raises `RuntimeError`, fails the test instead of satisfying the marker.

## Reads after writes (T4)

TCKDB has answered an upload before its transaction was visible to the next request: the
write session commits in the teardown of a FastAPI dependency, which can run after the 201
is sent. A read issued the moment an upload returns can then get 404 for rows the response
just named. The gate therefore waits for each upload's rows to become visible before it
reads them, and takes row counts only once two consecutive snapshots agree. The same
effect is why the bring-up script retries the API-key request after logging in.

## Tear down

```bash
fuser -k 58010/tcp        # stops uvicorn itself
docker rm -f -v tckdb-integ-db tckdb-integ-minio
rm -rf "$WORK"
```
