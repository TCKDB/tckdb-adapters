# tckdb-adapters

Producer adapters for [TCKDB](https://github.com/TCKDB/TCKDB), a database of computed
thermochemistry and kinetics. An adapter reads the output of one tool and turns it into
TCKDB upload payloads. Today there is one, for
[ARC](https://github.com/ReactionMechanismGenerator/ARC), and a shared core that the next
adapter builds on.

TCKDB publishes its upload contract in two packages, and this repository depends on both:

- `tckdb-schemas` holds the request models, the JSON Schema of every upload route and the
  producer contract generated from TCKDB's own routes.
- `tckdb-client` is the HTTP client: authentication, idempotency keys and replay.

TCKDB decides what it accepts. An adapter conforms to the contract and sends only what its
source states.

## Packages

| Package | Path | What it is |
|---|---|---|
| `tckdb-adapters-core` | [`tckdb_core/`](tckdb_core/README.md) | Everything an adapter needs after it has built a payload, and every helper two adapters would write the same way: the payload writer, the upload pipeline with its sidecar files, idempotency keys, configuration, shared numerics, pre-checks of TCKDB's rules, the warning-code registry and a test kit. It reads no tool's output format. |
| `tckdb-arc` | [`tckdb_arc/`](tckdb_arc/README.md) | The ARC adapter. It reads an ARC project's `output.yml` and builds species, reaction and transition-state uploads. Its command is `tckdb-arc-upload`. |

Dependencies point one way: `tckdb_arc` imports `tckdb_core`, and `tckdb_core` imports
`tckdb-client` and `tckdb-schemas`. The core never imports an adapter or the tool behind
it, and a test in `tckdb_core/tests` fails if it does.

## Install

Python 3.11 or newer. Neither package is on PyPI yet, so install from a checkout. The two
TCKDB packages come from the TCKDB commit this repository is tested against:

```bash
pip install \
  "tckdb-client @ git+https://github.com/calvinp0/tckdbv2.git@d4d53462db2b75ed699cb6bd7b39964b2b645254#subdirectory=clients/python" \
  "tckdb-schemas @ git+https://github.com/calvinp0/tckdbv2.git@d4d53462db2b75ed699cb6bd7b39964b2b645254#subdirectory=schemas/python/tckdb-schemas"
pip install -e tckdb_core -e tckdb_arc
```

Install `tckdb_core` before `tckdb_arc`, as above, because the ARC adapter depends on it. With a
local TCKDB checkout you can install the two TCKDB packages from its `clients/python` and
`schemas/python/tckdb-schemas` directories instead.

## Upload an ARC run

Add a `tckdb` block to the ARC project's `input.yml`:

```yaml
tckdb:
  enabled: true
  base_url: https://tckdb.example.org/api/v1
  upload_mode: computed_species   # or conformer, computed_reaction, computed_ts
  upload: true                    # false writes the payloads and sends nothing
```

Put the API key in the `TCKDB_API_KEY` environment variable, then run the adapter on the
finished project:

```bash
tckdb-arc-upload /path/to/project/input.yml --offline   # build and inspect, no network
tckdb-arc-upload /path/to/project/input.yml             # upload
```

Each payload is written under `<project>/tckdb_payloads/` before any request is made, next
to a sidecar file that records the idempotency key, the server's answer and every warning.
Because running the command again sends the same keys, a repeated upload does not create
a second deposit. The [ARC adapter README](tckdb_arc/README.md) lists the other settings, and
[`docs/contract/ARC_MAPPING.md`](docs/contract/ARC_MAPPING.md) says what each ARC field
becomes in TCKDB and what is left out.

## Run the tests

```bash
pip install -e "tckdb_core[test]" -e "tckdb_arc[test]"
python -m pytest                                    # both suites, from the repository root
python -m pytest tckdb_arc/tests/test_sweep.py -q   # one file
```

The suites run without ARC and without a TCKDB server. Tests that need ARC skip when it is
not importable. The tests that compare the level-of-theory hash with TCKDB's own code need
a TCKDB `backend/` directory at the pinned commit, named by `TCKDB_BACKEND_PATH`; they
skip without it, and `TCKDB_REQUIRE_BACKEND=1` turns the skip into a failure, as CI does.

Every payload the tests build is validated against the JSON Schema that the installed
`tckdb-schemas` ships for its route. An opt-in live gate uploads the test corpora to a
TCKDB backend running on the same machine and reads the stored rows back. See
[`docs/contract/INTEGRATION_GATE.md`](docs/contract/INTEGRATION_GATE.md).

## The TCKDB pin

The tested contract is `tckdb-client` 0.208.x with `tckdb-schemas` 0.175.x. Both packages
bound the two dependencies to those release lines, and `tckdb-pin.toml` names the TCKDB
commit that CI installs them from.

A scheduled workflow compares the pin with TCKDB's main branch every day. When a newer
release line appears it moves the pin in a scratch checkout, runs the suites, and opens a
pull request if they pass or an issue if they fail. A green run shows only that today's
payloads still validate. The pull request body therefore carries the contract changelog,
and a person reads it before merging. [`docs/tckdb_pin.md`](docs/tckdb_pin.md) describes the
workflow, the `tools/tckdb_drift.py` commands and the one-time repository settings.

## Write a new adapter

A new adapter is a sibling package, `tckdb_<tool>/`, that subclasses the core's uploader
and maps its tool's output to the payloads. Start with
[`docs/writing_an_adapter.md`](docs/writing_an_adapter.md). It lists what the core gives
you, what you write yourself, and which TCKDB routes the core does not cover yet.
[`AGENTS.md`](AGENTS.md) holds the same rules in the short form coding agents read, and
[`CONTRIBUTING.md`](CONTRIBUTING.md) covers pull requests.

## Licence

MIT. See [`LICENSE`](LICENSE).
