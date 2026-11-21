# tckdb-adapters

[![ci](https://github.com/calvinp0/tckdb-adapters/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/calvinp0/tckdb-adapters/actions/workflows/ci.yml)
[![tckdb-adapters-core](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Fcalvinp0%2Ftckdb-adapters%2Fmain%2Ftckdb_core%2Fpyproject.toml&query=%24.project.version&label=tckdb-adapters-core)](tckdb_core/CHANGELOG.md)
[![tckdb-arc](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Fcalvinp0%2Ftckdb-adapters%2Fmain%2Ftckdb_arc%2Fpyproject.toml&query=%24.project.version&label=tckdb-arc)](tckdb_arc/CHANGELOG.md)
[![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](.github/workflows/ci.yml)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![docs](https://img.shields.io/badge/docs-mkdocs-blue)](https://calvinp0.github.io/tckdb-adapters/)

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

The documentation website is at <https://calvinp0.github.io/tckdb-adapters/>.

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
  "tckdb-client @ git+https://github.com/TCKDB/TCKDB.git@d4d53462db2b75ed699cb6bd7b39964b2b645254#subdirectory=clients/python" \
  "tckdb-schemas @ git+https://github.com/TCKDB/TCKDB.git@d4d53462db2b75ed699cb6bd7b39964b2b645254#subdirectory=schemas/python/tckdb-schemas"
pip install -e tckdb_core -e tckdb_arc
```

Install `tckdb_core` before `tckdb_arc`, as above, because the ARC adapter depends on it. With a
local TCKDB checkout you can install the two TCKDB packages from its `clients/python` and
`schemas/python/tckdb-schemas` directories instead.

## Use an adapter

Each adapter has its own README with its settings and its command; for ARC that is
[`tckdb_arc/README.md`](tckdb_arc/README.md). Every adapter writes each payload to disk
before it makes any request, next to a sidecar file that records the idempotency key, the
server's answer and every warning. Running the same adapter release on the same output again
sends the same keys, so a repeated upload does not create a second deposit. Each adapter's
README links the document that says what each source field becomes in TCKDB and what is
left out.

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

MIT. See [`LICENSE`](LICENSE). [`NOTICE`](NOTICE) lists the code and data ported from ARC,
which is also MIT-licensed.
