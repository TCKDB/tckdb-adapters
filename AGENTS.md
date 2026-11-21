# tckdb-adapters

Producer adapters that turn a tool's output into TCKDB upload payloads. `tckdb_core/`
(`tckdb-adapters-core`) is the shared, producer-agnostic core. `tckdb_arc/` is the ARC
adapter. `tckdb_chemkin/` (`tckdb-chemkin`) and `tckdb_qcschema/` (`tckdb-qcschema`) are
file-format adapters for CHEMKIN mechanisms and MolSSI QCSchema documents; they import
`tckdb-client` and `tckdb-schemas` directly and do not use the core. A new producer adapter is a
sibling package `tckdb_<producer>/`.

TCKDB decides the contract. The adapter conforms.

## Pointers

- Building a new adapter, or adding a route to the core: read `docs/writing_an_adapter.md`
  first. It has the scaffold, the submit sequence and the table of routes the core covers.
- Changing what the ARC adapter sends: read `docs/contract/ARC_MAPPING.md`, then update it
  in the same change.
- Moving the TCKDB pin, or reviewing a `chore/tckdb-drift-*` pull request: read
  `docs/tckdb_pin.md`.
- Running the live gate: read `docs/contract/INTEGRATION_GATE.md`.
- Releasing a package to PyPI, or changing `.github/workflows/publish-python.yml` or
  `tools/python_release.py`: read `docs/publishing.md`.
- Adding a documentation page: add it to `nav` in `mkdocs.yml`. `mkdocs build --strict` is
  the check; it must pass with no warnings. The site is built from `docs/`, `mkdocs.yml`,
  the READMEs, the changelogs and `CONTRIBUTING.md`. Install `requirements-docs.txt` in a
  separate virtualenv, never in the one the test suite uses.

## Read the producer contract before changing a mapping

Before changing any mapping from a producer's output to a TCKDB payload, read the contract
from the installed tckdb-schemas:

```bash
python -m tckdb_schemas.contract --print
python -m tckdb_schemas.contract --since <the tckdb-schemas version this adapter last targeted>
```

The second command shows what moved. The contract is generated from TCKDB's own routes,
models and refusal-code catalogue. It is the authority on what TCKDB accepts; memory and
ARC or RMG conventions are not.

## Send only what the source states

- When the source does not state a convention the contract requires (for example
  `enthalpy_reference_kind` or `reference_pressure_bar`), refuse to build that block or
  omit the field, and report a registered warning code. Add no new default.
- An interpretation may be sent only when it is visibly marked as one in the payload and
  in the sidecar. The pattern is `_kinetics_declarations` in
  `tckdb_arc/tckdb_arc/kinetics.py`: `claim_origin: depositor_interpretation`, a closing
  sentence in the kinetics `note`, and the `kinetics_declarations_interpreted` warning.
  Sending a new interpretation is the repository owner's decision. Ask before adding one.
- The ARC adapter has three documented exceptions to the two rules above: `formation_298k`
  for pre-1.2 output under the heuristic checks, `reversible: true` on a standalone
  transition state whose reaction states none, and the opt level on an IRC calculation
  (`irc_level_assumed_opt_level`). `docs/contract/ARC_MAPPING.md`, "Exceptions to rules 1
  and 3", lists them. Keep the list complete when one is added or removed.
- A minimum with an imaginary frequency stays refused. TCKDB's soft-mode tolerance is not
  copied into an adapter.

## Dependency direction

`tckdb_<producer>` imports `tckdb_core`; `tckdb_core` imports `tckdb-client` and
`tckdb-schemas`. Core source names no producer: no `arc` or `tckdb_arc` import, and no
`output.yml`, `parser_evidence`, `restart.yml` or `arc.` string, comments included (write
"the ARC adapter"). `tckdb_core/tests/test_core_is_producer_agnostic.py` is the guard; a
second producer adds its own names to it.

Code that reads a producer's keys lives in the producer package and passes plain values to
the core.

## Reuse the core

A producer package uses these as they are, and extends the core with a parameter when one
almost fits:

| Need | Core module |
|---|---|
| Upload, readiness probe, sidecars, strict mode, artifact batches | `uploader.TCKDBUploaderBase` (subclass it; set `PRODUCER_TAG`, `PRODUCER_NAME`, `IDEMPOTENCY_NAMESPACE`, `LOGGER_NAME`) |
| Posting a finished thermo, kinetics, statmech, transport, reaction or network request | `TCKDBUploaderBase.submit_standalone` (`standalone`); validates against the shipped schema, builds nothing |
| Payload and sidecar files | `payload_writer.PayloadWriter` (`self._writer`) |
| Idempotency keys | `idempotency` (pass the producer's namespace) |
| Checking a request against the shipped contract before the POST; the endpoint-to-schema table | `validation` (`ROUTE_SCHEMAS`; `TCKDBUploaderBase._upload` runs it; `config.validate_before_upload`) |
| Config fields, API-key resolution | `config` |
| Endpoints, payload kinds | `constants` |
| Warnings | `adapter_warnings.WarningSink`, `warning_codes.CodedEnum` |
| Level-of-theory identity | `level_rules`, `method_families` |
| Numerics and units | `thermo_numerics`, `rmg_units`, `composition`, `xyz`, `isotopes`, `reaction_flatten` |
| TCKDB rule pre-checks | `rules`, `ts_evidence`, `ts_evidence_rules`, `route_level` |
| Tests | `tckdb_core.testing`: `contract`, `contract_hook`, `contract_pin`, `stubs`, `backend_import`, `live` |

Submit sequence for every route: build the payload with a `WarningSink`, run the
pre-checks, then `self._write_and_upload(...)`. It attaches `config.rights` when set, builds
the idempotency key from the finished payload, writes with `self._writer.write(...)`, and
finishes with `_finalize_skipped` or `_upload`. The write always precedes the request.

Import a rule, enum or model from `tckdb_schemas` whenever it ships one. A replica in this
repository is allowed only for a rule that exists solely in TCKDB's backend, and it carries
a parity test (`tckdb_core.testing.backend_import`). Build thermo blocks in the adapter;
`tckdb_client.builders` fills a default `enthalpy_reference_kind`.

## Scaffolding `tckdb_<producer>/`

1. Create `pyproject.toml` (depends on `tckdb-adapters-core`; bounds `tckdb-schemas` and
   `tckdb-client` to the core's minor lines), `README.md`, `CHANGELOG.md`, the package and
   `tests/`.
2. Add the package to `pythonpath` and `testpaths` under `[tool.pytest.ini_options]` in the
   root `pyproject.toml` (that is what makes a root `pytest` run find it), to
   `.github/workflows/ci.yml`, to `tools/tckdb_drift.py --bump` and to the `git add` list
   in `tools/tckdb_drift_publish.sh`.
3. Write the `TCKDBUploaderBase` subclass, one submit method per route, and the builders.
4. Register warning codes and wire the tests below.

Done when the package's suite passes, every item in the checklist at the end of
`docs/writing_an_adapter.md` holds, and the full suite passes once.

## Warning codes

Every code is a member of the package's `CodedEnum` registry (`tckdb_arc/warning_codes.py`
is `ArcWarning`; the core's is `CoreWarning`) and is emitted through the registry member.
After adding or changing a code, run `python tools/gen_warning_codes.py` and commit
`docs/contract/WARNING_CODES.md` with it. `tckdb_arc/tests/test_warning_registry.py` fails
on a stale document, an unregistered code or a registered code nothing emits. A new
package gets its own registry, generated table and registry test.

## Contract tests and the pin

Validate every payload the adapter builds against the shipped JSON Schema for its route,
e.g.
`jsonschema.Draft202012Validator(tckdb_schemas.contract.json_schema("ThermoUploadRequest")).validate(payload)`,
and pin the tckdb-schemas version the tests ran against.

- The kit is `tckdb_core.testing.contract` and `.contract_hook`. The pin,
  `TARGET_SCHEMAS_LINE`, is one line in `tckdb_core/tckdb_core/testing/contract.py`.
- ARC wiring: `tckdb_arc/tests/_contract.py`, `tckdb_arc/tests/conftest.py` and
  `tckdb_arc/tests/test_contract_pin.py` (twin: `tckdb_core/tests/test_core_contract_pin.py`).
  A new package needs its own `conftest.py` hook registration and `test_contract_pin.py`,
  built from `tckdb_core.testing.contract_pin`.
- The conftest hook checks the four whole-request builders (conformer, computed-species,
  computed-reaction, transition-state) and the artifact request bodies with the route's
  published pydantic model and the shipped JSON Schema. Use `contract_validate` where a
  test checks a payload or fragment itself.
- The hook does not run the route handlers' rules (e.g. `thermo_energy_level_*`,
  `calculation_geometry_composition_mismatch`) or check headers, and JSON Schema `format`
  keywords are not enforced. Only the live gate reaches those.

A scheduled workflow proposes pin bumps as a pull request or issue. The contract-reading
rule applies to that pull request: a green suite shows only that today's payloads still
validate. Move `TARGET_SCHEMAS_LINE`, both `pyproject.toml` bounds and `tckdb-pin.toml`
together with `python tools/tckdb_drift.py --bump`, never by hand in one place.

## Live integration gate

`tckdb_arc/tests/integration` uploads to a TCKDB backend and reads the rows back. It is
skipped unless `TCKDB_INTEGRATION_URL` is set. The target must be loopback, on a port other
than 8010, with an artifact bucket named `tckdb-integ*`; the fixture in
`tckdb_core.testing.live` fails the run otherwise. Keep those guards intact in any new
gate. Tests, tools and agents in this repository contact only such an isolated local
backend, and an agent starts one only when the user asks.

## Running tests

- Run serially: `python -m pytest -p no:xdist -p no:cacheprovider <paths>`.
- While working, run only the test files your change touches. Run the full suite once at
  the end, from the repository root.
- In a git worktree whose editable installs point at another checkout, prefix
  `PYTHONPATH=$PWD/tckdb_core:$PWD/tckdb_arc`.
- Install order from a checkout: `pip install -e tckdb_core -e tckdb_arc`.
- `tckdb_chemkin` and `tckdb_qcschema` run from their own directories
  (`cd tckdb_chemkin && python -m pytest`). They are not in the root run: their
  `tests/conftest.py` and `test_uploader.py` share module names with the core's and the ARC
  adapter's, and collection fails.
- A new package with its own `pyproject.toml` also goes into `PACKAGES` in
  `tools/python_release.py` and the publish workflow's choices; `tools/tests` fails until it is.

## Versions and changelogs

A change to package code bumps that package's patch version and adds an entry to its
`CHANGELOG.md`. When `tckdb_arc` needs the new core, raise its `tckdb-adapters-core`
bound. Say in the entry when a release changes a payload: the payload hash is part of the
idempotency key, so TCKDB records a re-upload as a new deposit. `tckdb_core/__init__.py`,
`tckdb_chemkin/__init__.py` and `tckdb_qcschema/__init__.py` hold `__version__`; keep each equal
to its `pyproject.toml` version. A `tckdb-qcschema` payload records the adapter version, so any
version bump changes its request body (not its idempotency keys).

## ARC-specific decisions

- **Pre-1.2 enthalpies (a maintainer-approved interpretation).** Without output.yml 1.2's
  `atom_corrections_applied` flags, the adapter still declares
  `enthalpy_reference_kind: formation_298k` when the non-finite, magnitude, header-level,
  dispersion/solvation and light-species checks pass. This is a deliberate interpretation,
  not a default: Arkane's corrected H298 is a formation enthalpy by construction. The
  magnitude, light-species, header-level and dispersion/solvation checks are heuristics
  that make an uncorrected enthalpy very unlikely to pass; they are not proof that the
  corrections ran at the right level. The 1.2 flags supersede them when present.
- **`reference_pressure_bar` is omitted, never defaulted,** when ARC does not record
  `thermo.standard_state_pressure_pa` (or records an unusable value). The adapter reports
  `thermo_reference_pressure_not_stated` instead of filling 1.01325 bar.
- **Typed level-of-theory qualifiers are not sent** (`relativistic_hamiltonian`,
  `pno_truncation`, `ri_approximation`, `core_treatment: window`). ARC's `args` do not
  reliably describe the deck a job ran with.
