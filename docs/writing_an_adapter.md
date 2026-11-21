# Writing a new adapter

An adapter turns one tool's output into TCKDB upload payloads and sends them. This guide
is for someone adding the second adapter to this repository, next to the ARC one. It says
what `tckdb_core` already does for you, what you write, and where the core stops.

The ARC adapter (`tckdb_arc/`) is the working example throughout. It is large because ARC
produces a great deal of data, and most of its size is mapping code that you will not
copy.

## Before you start

Read TCKDB's producer contract. It ships inside `tckdb-schemas` and is generated from
TCKDB's routes, models and refusal codes, so it is what the server accepts:

```bash
python -m tckdb_schemas.contract --print      # the whole contract
python -m tckdb_schemas.contract --schemas    # one JSON Schema per upload payload
python -m tckdb_schemas.contract --codes      # path of the refusal-code reference
python -m tckdb_schemas.contract --warnings   # path of the warning-code reference
```

Decide which routes your tool's results fit (the table under "Routes" below), and check
that the tool states everything those routes require.

Three rules shape every adapter here.

1. **TCKDB decides the contract and the adapter conforms.** The contract is the reference,
   not memory and not another tool's conventions.
2. **Send only what the source states.** If the tool does not state a convention that the
   contract requires, such as `enthalpy_reference_kind` or `reference_pressure_bar`, do not
   build that block, or omit that field, and report it with a warning code. A plausible
   default would state something the source never said.
3. **Mark an interpretation as one.** Sometimes the tool's documentation settles a fact
   that its output file does not repeat. You may send that reading if the payload says it
   is the adapter's interpretation and the sidecar carries a warning. The pattern is
   `_kinetics_declarations` in `tckdb_arc/tckdb_arc/adapter.py` with the
   `kinetics_declarations_interpreted` warning: the applicability carries
   `claim_origin: depositor_interpretation`, and the kinetics `note` ends with a sentence
   that names what was interpreted.

## What the core gives you

| Need | Use |
|---|---|
| Upload pipeline: readiness probe, POST, sidecar bookkeeping, strict mode, artifact batches | `tckdb_core.uploader.TCKDBUploaderBase` |
| Writing the payload and its sidecar before any request | `tckdb_core.payload_writer.PayloadWriter` (the base class holds one as `self._writer`) |
| Idempotency keys | `tckdb_core.idempotency` (`IdempotencyInputs`, `build_idempotency_key`, `build_artifact_idempotency_key`); the namespace is a required argument |
| Configuration fields and API-key resolution | `tckdb_core.config.TCKDBConfig`, `TCKDBArtifactConfig` |
| Route endpoints and payload kinds | `tckdb_core.constants` |
| Outcome objects | `tckdb_core.outcomes` (`UploadOutcome`, `ArtifactUploadOutcome`) |
| Warnings in the sidecar's shape | `tckdb_core.adapter_warnings` (`AdapterWarning`, `WarningSink`) and `tckdb_core.warning_codes` (`CodedEnum`) |
| Level-of-theory identity, as TCKDB hashes it | `tckdb_core.level_rules` |
| NASA polynomials and thermo point tables | `tckdb_core.thermo_numerics` |
| RMG and Arkane unit strings | `tckdb_core.rmg_units` |
| Elements, formulas, xyz text, isotopes | `tckdb_core.composition`, `xyz`, `isotopes` |
| Bundle-local keys | `tckdb_core.keys` |
| Pre-checks of TCKDB rules, run on your facts before upload | `tckdb_core.rules`, `ts_evidence`, `ts_evidence_rules`, `route_level` |
| Software banners split the way TCKDB splits them | `tckdb_core.software_release` |
| Posting a request you built to a standalone record route (thermo, kinetics, statmech, transport, reaction, network) | `TCKDBUploaderBase.submit_standalone`, `tckdb_core.standalone` |
| Tests: contract validation, the pin, client stubs, the live gate | `tckdb_core.testing` |

Reuse these as they are. If one almost fits, extend it in the core with a parameter; do not
copy it into your package.

The core never imports an adapter and never names a tool's file format.
`tckdb_core/tests/test_core_is_producer_agnostic.py` enforces that. Anything that reads
your tool's keys stays in your package and passes plain values to the core.

## What you write

### 1. The package

Create a sibling of `tckdb_arc/`:

```text
tckdb_mytool/
  pyproject.toml
  README.md
  CHANGELOG.md
  tckdb_mytool/
    __init__.py
    adapter.py          # the TCKDBUploaderBase subclass and the payload builders
    config.py           # where the tckdb settings live in your tool's input
    warning_codes.py    # your CodedEnum registry
    cli.py              # optional
  tests/
    conftest.py
    test_contract_pin.py
    test_warning_registry.py
    ...
```

In `pyproject.toml`, depend on `tckdb-adapters-core` and bound `tckdb-schemas` and
`tckdb-client` to the same minor lines the core does. Add the package to
`pythonpath` and `testpaths` under `[tool.pytest.ini_options]` in the root
`pyproject.toml`, which is what lets `pytest` find it from the repository root, and to the
CI workflow.

`tools/tckdb_drift.py --bump` currently rewrites the bounds of `tckdb_core` and
`tckdb_arc` only. Add your `pyproject.toml` to it, and to the file list in
`tools/tckdb_drift_publish.sh`, so the three packages move together.

### 2. The uploader subclass

```python
from tckdb_core.uploader import TCKDBUploaderBase

class MyToolAdapter(TCKDBUploaderBase):
    PRODUCER_TAG = "tckdb_mytool"
    PRODUCER_NAME = "MyTool"
    IDEMPOTENCY_NAMESPACE = "mytool"
    LOGGER_NAME = "tckdb_mytool"
```

`PRODUCER_TAG` appears in every warning as `context.source = "tckdb_mytool_self_check"`.
`IDEMPOTENCY_NAMESPACE` is the first part of every key. Choose both once: changing the
namespace later changes every key, and TCKDB then treats old uploads as new deposits.

### 3. A submit method per route

Each one does the same things in the same order. `tckdb_core/README.md` has a complete
example.

1. Return `None` when the config is disabled.
2. Create a `WarningSink(PRODUCER_TAG)` and build the payload, adding a warning for each
   thing omitted or interpreted.
3. Run the pre-checks that apply (`tckdb_core.rules`).
4. Return `self._write_and_upload(payload=..., kind=..., endpoint=..., label=...,
   project_label=..., species_label=..., conformer_label=..., subdir=..., warnings=...)`.
   It sets `config.rights` on the payload when the user configured it (overwriting any
   `rights` already there), builds the idempotency key from the finished payload, writes the
   payload and sidecar with the warnings, logs the write, and then finishes as skipped when
   upload is off or uploads with `_upload`. `log_name` words the log line; `is_partial` writes
   a record that is never posted.

`_upload` checks the finished payload against the shipped contract for its endpoint before it
does anything else (no API-key lookup, no readiness probe, no POST), with `tckdb_core.validation`:
the route's published pydantic model where tckdb-schemas has one (the server runs that model, in
lax mode, so it alone decides), otherwise the shipped JSON Schema. A payload that fails is not
posted. Its file stays on disk, the sidecar is `failed` with `last_error` starting
`payload_failed_local_validation:` followed by the first finding, and a warning of that code lists
the findings in full and any stable refusal codes in them (no `response_status_code`, which is how
it differs from a server refusal), the outcome is `failed`, and strict mode raises
`PayloadContractError`. A checker that itself raises does not block the upload: the request is
posted unchecked and the sidecar says `payload_local_validation_unavailable`. The endpoint you pass must be a key of
`ROUTE_SCHEMAS`, or there is nothing to check against and the request is posted unchecked.
Your users can turn the check off with `config.validate_before_upload = False`; give your
config parser that key.

The payload is written before the request so that a crash leaves a `pending` sidecar on
disk, and so that the bytes can be replayed. The idempotency key includes a hash of the
payload, so the same input gives the same key and TCKDB replays its first answer. A change
to your mapping changes the hash. Say so in your changelog when it happens.

The ARC adapter's four methods and `submit_standalone` all end in `_write_and_upload`; a new
route's method differs from them only in how it builds the payload and the key's labels. For the
standalone record routes (thermo, kinetics, statmech, transport, reaction, network) the base
class has the whole method, `submit_standalone`; see "Posting a standalone record" under
"Routes".

The artifact route is the exception: the base class already has the whole submit path.
`submit_artifacts_for_calculation(species_label=..., calculation_ref=..., file_path=...)`
uploads one file and `submit_artifact_batch_for_calculation(...)` uploads several, using the
`calc_` ref that the upload response returned. They apply the config's artifact settings,
write one sidecar per file and post one request per calculation, cut into several when
its files total more than the client's 64 MiB inline budget, so a producer does not
rewrite them. If request k of N fails, the sidecars of the earlier requests stay `uploaded`,
those of request k and of the requests not sent after it are `failed`. Override
`_project_label_for(output_doc)` if the project label comes from your tool's output when the
config names none. You may wrap the public methods with a signature
of your own: the base class's single-file method calls a private batch function, not the
public batch method, so an override does not break it.

### 4. The mapping

The mapping is the part only you can write. It reads your tool's records and builds the
request dictionaries the contract describes. Keep each builder of a whole request a method
of the adapter class, because the contract hook (below) wraps the builder methods you
name.

Check the server's rules before you post where the core has a pre-check for them:
`rules.energy_level_declaration`, `rules.kinetics_energy_level_assessment`,
`rules.geometry_anchor_refusals`, `rules.undeclarable_input_geometries`, the reaction
coordinate designation, the isotope reconciliation and the transition-state evidence
rules. A payload that TCKDB would refuse is cheaper to catch locally.

Where `tckdb-schemas` ships a rule or an enum, import it. Examples already in use:
`tckdb_schemas.enthalpy_reference.enthalpy_reference_error`,
`tckdb_schemas.method_families`, `tckdb_schemas.bundle_source_rules`,
`tckdb_schemas.rights.DepositRights`, `tckdb_schemas.enums`. A replica in this repository
is justified only for a rule that exists solely in TCKDB's backend, and it then needs a
parity test against the backend (`tckdb_core.testing.backend_import`).

Do not use `tckdb_client.builders` for thermo. Its defaults declare an enthalpy reference
kind, which breaks rule 2.

### 5. Warning codes

Define a `CodedEnum` in `warning_codes.py`, one member per code, each with a one-line
description:

```python
from tckdb_core.warning_codes import CodedEnum

class MyToolWarning(CodedEnum):
    REFERENCE_PRESSURE_NOT_STATED = (
        "thermo_reference_pressure_not_stated",
        "The source records no standard-state pressure, so reference_pressure_bar is omitted.",
    )
```

Emit them through the sink: `warnings.add(MyToolWarning.X, "message", "field.path",
action="...")`. Never write a code as a bare string at the call site.

The ARC adapter generates `docs/contract/WARNING_CODES.md` from its registry with
`python tools/gen_warning_codes.py`, and `tckdb_arc/tests/test_warning_registry.py` fails
when the document is stale, when a code is unregistered, or when a registered code is
never emitted. Give your package the same: a `render_markdown()`, a branch in the
generator that writes your table, and a registry test. Regenerate the document in the same
commit that adds a code.

### 6. Configuration and a command

`TCKDBConfig` holds the fields. Your package decides where they come from in your tool's
input and parses them. `tckdb_arc/tckdb_arc/config.py` is a parser you can follow; it
validates `rights` with `DepositRights` and rejects unknown artifact kinds. API keys are
never read from the input file, only from the environment or a key file named there.

## Tests your package must have

| Test | How |
|---|---|
| Every built request validates against the contract | In `tests/conftest.py`, register `tckdb_core.testing.contract_hook.contract_checks_fixture` with your adapter class and a map of builder-method name to schema name. It runs the published pydantic model and the shipped JSON Schema on every request any test builds. Call `contract_validate` yourself where a test checks a fragment. |
| The contract pin | `tests/test_contract_pin.py` with the four assertions of `tckdb_core.testing.contract_pin`, pointed at your `pyproject.toml` and your routes. `assert_routes_have_shipped_schemas` takes the set of endpoints your adapter posts to, which can be any subset of the kit's `ROUTE_SCHEMAS`. |
| Warning registry | Codes are unique and registered, the generated document is current. |
| Upload behaviour without a network | `tckdb_core.testing.stubs.StubClient`, passed as `client_factory=lambda config, key: client`. |
| Refusals | For each convention the source may not state, a test that the block is omitted and the warning is present. |

The contract hook checks shape. It does not run the route handlers' rules (energy-level
ownership, geometry and composition checks), it does not check headers, and JSON Schema
`format` keywords are not enforced. Only a live server reaches those.

Run tests serially and only the files you touched while working:

```bash
python -m pytest -p no:xdist tckdb_mytool/tests/test_thermo.py -q
```

Run the whole suite once before you open a pull request.

### The live gate

`tckdb_core.testing.live` provides the fixture for an opt-in gate that uploads to a TCKDB
backend on your own machine and reads the rows back.
[`contract/INTEGRATION_GATE.md`](contract/INTEGRATION_GATE.md) explains how to bring the
backend up. The fixture refuses any target that is not loopback, that uses port 8010, or
whose artifact bucket is not named `tckdb-integ*`. Keep those guards. Tests in this
repository never contact a shared or production TCKDB.

## Routes

The core's endpoint constants, payload writer, uploader and test kit cover twelve of
TCKDB's producer routes. The others are accepted by TCKDB and listed in the contract, but
the core has no constant, payload kind or test-kit entry for them.

There are two levels of coverage. The first five routes are **built and sent**: the adapter
builds the request from its tool's output, and the core handles the sidecar, upload and
idempotency around it. The seven standalone record routes are **sent and validated only**:
the core takes a request you have already built, checks it against the route's shipped JSON
Schema, writes it with a sidecar and uploads it. It builds none of them: the core has no
thermo, kinetics, statmech, transport, reaction or network builder, and the adapter writes it.

| Route | Request | Core |
|---|---|---|
| `POST /uploads/conformers` | `ConformerUploadRequest` | covered |
| `POST /uploads/computed-species` | `ComputedSpeciesUploadRequest` | covered |
| `POST /uploads/computed-reaction` | `ComputedReactionUploadRequest` | covered |
| `POST /uploads/transition-states` | `TransitionStateUploadRequest` | covered |
| `POST /calculations/{calculation_ref}/artifacts` | `ArtifactsUploadRequest` | covered |
| `POST /uploads/thermo` | `ThermoUploadRequest` | transport and validation (`submit_standalone("thermo", ...)`). Not built: `thermo_numerics` builds NASA-7 and point blocks; NASA-9, Wilhoit and group-additivity records have no helper. |
| `POST /uploads/kinetics` | `KineticsUploadRequest` | transport and validation (`"kinetics"`). Not built: `rmg_units` maps A and Ea units; only the ARC adapter builds a modified-Arrhenius block, and no other rate form has a helper. |
| `POST /uploads/statmech` | `StatmechUploadRequest` | transport and validation (`"statmech"`). Not built. |
| `POST /uploads/transport` | `TransportUploadRequest` | transport and validation (`"transport"`). Not built. |
| `POST /uploads/reactions` | `ReactionUploadRequest` | transport and validation (`"reaction"`). Not built. |
| `POST /uploads/networks` | `NetworkUploadRequest` | transport and validation (`"network"`). Not built. |
| `POST /uploads/networks/pdep` | `NetworkPDepUploadRequest` | transport and validation (`"network_pdep"`). Not built. |
| `POST /uploads/thermoml` | `ThermoMLUploadRequest` | not covered. It uploads a ThermoML XML document (base64 in the body) and requires `rights` and an `Idempotency-Key`; it is a file ingest, not a record a tool builds. |
| `POST /bundles/dry-run`, `/bundles/submit` | `ContributionBundle` | not covered |
| `POST /submissions/{ref}/rights-attestations`, `/submissions/{ref}/supersede` | `RightsAttestationCreate`, `SubmissionSupersedeRequest` | not covered. They act on an existing submission by its ref. The `rights` fragment inside an upload is supported through `TCKDBConfig.rights`. |
| `POST /jobs/*` (asynchronous twins of the upload routes) | same requests | not covered |

### Posting a standalone record

The standalone routes share one method, `TCKDBUploaderBase.submit_standalone`
(`tckdb_core.standalone`):

```python
outcome = adapter.submit_standalone(
    "thermo",                    # a key of tckdb_core.standalone.STANDALONE_ROUTES
    request,                     # the finished request dictionary
    label="ethane",              # file name and the species-or-reaction part of the key
    qualifier="ccsd-t",          # optional: tells apart several records of one kind under one label
    warnings=sink,               # optional: your findings, written to the sidecar
)
```

It follows the submit sequence above, minus the build step: `config.rights` is added to a copy
of the request when the request has none (a request whose `rights` differ from the configured
ones is refused), the request is validated against the shipped schema (here, and again by `_upload`
before the POST), and the finished request
goes to `_write_and_upload`, which builds the key
(`<namespace>:<project>:<label>:<qualifier>:<kind>:<hash>`), writes the payload and a `pending`
sidecar, and skips or uploads exactly as the other routes do (readiness probe, POST, sidecar
status, strict mode). It returns `None` when the
config is disabled.

An invalid request raises `StandalonePayloadError` before any file is written, in strict mode
or not, and whatever `validate_before_upload` says. A payload that never passed the contract has
nothing worth replaying, so no sidecar is left for it. (The pre-POST check in `_upload` is the one
the switch governs; it reports through the sidecar instead.)

What validation covers: the shipped JSON Schema, which checks shape; its `format` keywords are
not enforced. tckdb-schemas publishes
no pydantic model for these requests (the models are backend-side), so there is no model check.
The route handlers' rules are not checked: ownership of cited calculations, composition
checks, `enthalpy_reference_kind` values (the schema takes any string; TCKDB refuses an unknown
one), and the rest of the "Will be refused if" lists in the contract. Only the live gate
reaches those. In your tests, put the request through
`tckdb_core.testing.contract.assert_matches_contract` as well, and use the example that
tckdb-schemas ships in each schema's `examples` as a starting point.

Sidecars of these kinds carry `payload_kind` `thermo`, `kinetics`, and so on. Like every sidecar
the core writes, they are not replayable by `tckdb-client` (see "Client replay" below); re-run the
adapter instead.

Two records under one label need a `qualifier`: it is part of the idempotency key and of the file
name (`<label>.<qualifier>`), so they do not overwrite each other. A label, qualifier and project
label that make the key longer than the server's 200 characters raise `StandalonePayloadError`
before anything is written.

If your tool's output also needs a route that is not in the table, add it in the core, in its
own pull request, before the adapter that needs it: an endpoint and kind constant, a subdirectory
constant in `PayloadWriter`, a row in `tckdb_core.standalone.STANDALONE_ROUTES`, a
`ROUTE_SCHEMAS` entry in `tckdb_core.validation` (the one table; the test kit and the runtime
check both read it, so the new route is checked before its POST from the start), and tests. A new `ROUTE_SCHEMAS` entry
does not affect another adapter's pin test, which checks only the routes that adapter posts to;
the core's own pin test requires an endpoint constant for every entry.

Other limits of the core that an adapter author should know:

- **Retries.** The client the core builds carries a `tckdb_client.retry.RetryPolicy` made from
  `config.retry_max_attempts` (3, the first attempt included; 1 to 10) and
  `config.retry_backoff_seconds` (1.0; each wait is jittered and capped at 30 s, the first between
  0.5 and 1 times this, and the un-jittered wait doubles each time; each wait is jittered
  separately). The client replays a `GET`, or a `POST` that carries
  an `Idempotency-Key` (every upload does), after a connection failure, a timeout, or a 429, 502,
  503 or 504; a refusal is not replayed, nor is a status whose error `code` is on the client's
  non-retryable list. Every replay carries the same key and payload. All of that is the client's
  logic with its own defaults; the core only builds the policy. `retry_max_attempts = 1` passes
  none. A custom `client_factory` builds the whole client, so the core sets nothing on it. The
  readiness probe has its own attempts loop and is not retried by the client: when the core builds
  the client it sends `GET /readyz` through a second client without a policy (a factory's client
  does the probe itself). The wait between replays goes through
  `TCKDBUploaderBase._sleep_between_retries`; a test rebinds it, as it does
  `_sleep_between_probes`. Give your config parser both keys.
  `timeout_seconds` applies to each phase of a request (connect, each read), not to the whole
  request. A request that makes no progress is given up after `timeout_seconds`, so with the
  defaults one upload can then take about three such timeouts plus the waits; a slow but progressing
  large upload can run longer on each attempt. Each wait is jittered separately and capped at 30 s:
  the first is between 0.5 and 1 times `retry_backoff_seconds`, and the un-jittered wait doubles
  each time. When the server sends `Retry-After` the client waits at least that long, up to 120 s
  per wait, and does not replay at all above 120 s. The readiness probe runs once per uploader, so a
  server that degrades mid-run costs this on every remaining upload. TCKDB does not lock an
  idempotency key while a request is in progress. After a client-side timeout both the original
  request and the replay can run, and whichever commits second is refused with `409
  idempotency_conflict` and rolled back, so no duplicate record is created. Either the original or
  the replay can be the one refused, so the sidecar can be `failed` with `server_error_code:
  idempotency_conflict` although the data was stored. Running the upload again later returns the
  stored response when the server stored a receipt for the write; a response that carries
  `Idempotency-Receipt: failed` means it did not, and a later run executes the write again.
  Inject a client through `client_factory`, which is the supported way: a subclass that
  overrides `_make_client` instead gets no second client for the probe, so its own client does it.
- **Failure codes.** When a failed upload's exception carries a `code` (`TCKDBHTTPError.code`,
  the server's own error code), the sidecar's `server_error_code` and the outcome's
  `server_error_code` hold it, for conformer and bundle uploads, the standalone routes and
  artifact batches alike. It is `null` for a connection failure, a readiness failure and a
  request the adapter refused locally (those carry no server code), and for a success. The
  client does not report how many attempts it made, so the sidecar does not record a count.
- **Client replay.** The sidecars are replayed by re-running the adapter. `tckdb-client`'s own
  replay tool (0.208) does not replay any sidecar the core writes: it accepts only an integer
  `bundle_format_version` of 1, and the core writes the string `"0"`. It would also report the
  transition-state and standalone kinds as unknown, since it dispatches on four kinds only.
- **Artifacts.** Only the `output_log` and `input` kinds are implemented. The size limit is
  per file. A calculation whose files total more than 64 MiB (the client's inline budget) is sent
  as several requests, each stored or refused whole by the server. Only the first request's
  idempotency key carries the digest of the whole plan; a later request is keyed by the kind and
  the sha256 prefix of its first file (`...:artifact-batch-2`, `-3`). So a later run that sends a
  different set of files in a later request, with the same first file, repeats that key with a
  different body, and TCKDB answers 409 `idempotency_conflict` for it. A calculation over 64 MiB
  that an earlier core version stored as one request meets the same 409 on its first request,
  now a smaller body under the old key. `tckdb-client`'s `upload_artifacts` derives its keys the
  same way. In strict mode a failure on a later request raises although the sidecars of the
  earlier requests say `uploaded`.
- **Payload validation at run time.** Every request is checked before its POST, artifact batches
  included. For the conformer, computed-species, computed-reaction and transition-state routes and
  for the items of an artifact batch the published pydantic model alone decides, because that is
  what the server runs and it runs it in lax mode (`species_entry.charge: "0"` passes). The
  standalone routes have no published model, so for them the shipped JSON Schema decides (shape
  only), and `submit_standalone` checks the same schema once more before it writes anything. The
  test kit is stricter on purpose: `assert_request_is_accepted` runs the model and then the JSON
  Schema, so a builder is held to the schema in tests even where the server would accept looser
  input. A check is as
  strict as the *installed* tckdb-schemas, which may be older or newer than the server: that is
  the reason for `validate_before_upload`. It does not run the route handlers' rules (ownership
  of cited calculations, composition checks, the energy-level rules), headers, or `format`
  keywords, so a request can pass and still be refused; only the live gate reaches those. With
  `upload = false` nothing is checked: `_finalize_skipped` writes the sidecar as it always did.
  A check costs milliseconds (about 11 ms for a 78 KB computed-reaction request, the largest ARC
  golden payload); the first check of each route builds its validator, 10 to 300 ms, once per
  process. An artifact plan is read twice, once to check its bodies and once to post them.

## Checklist

- [ ] The contract is read for every route you post to.
- [ ] The package depends on `tckdb-adapters-core` and pins the same TCKDB lines.
- [ ] Nothing in `tckdb_core` names your tool.
- [ ] Every required convention the source does not state is omitted or refused, with a
      registered warning code and a test.
- [ ] Every interpretation is marked in the payload and in the sidecar.
- [ ] The contract hook, the pin test and the registry test are in place.
- [ ] The warning-code document is regenerated.
- [ ] The package has a README and a changelog, and the changelog marks releases that
      change payloads.
