# tckdb-adapters

Producer adapters for [TCKDB](https://github.com/TCKDB/TCKDB), a database of computed
thermochemistry and kinetics. An adapter reads the output of one tool and turns it into
TCKDB upload payloads. Every payload is written to disk before any request is made, next
to a sidecar file that records the idempotency key, the server's answer and every warning.

TCKDB decides what it accepts. An adapter conforms to the contract and sends only what its
source states.

## Packages

| Package | What it is | Documentation |
|---|---|---|
| `tckdb-arc` | The ARC adapter. It reads an ARC project's `output.yml` and builds species, reaction and transition-state uploads. | [Using the ARC adapter](arc.md), [ARC mapping](contract/ARC_MAPPING.md), [Warning codes](contract/WARNING_CODES.md) |
| `tckdb-adapters-core` | The part every adapter shares: payload writer, upload pipeline, idempotency keys, configuration, pre-checks of TCKDB's rules and a test kit. It reads no tool's output format. | [The core](core.md) |

Dependencies point one way: `tckdb_arc` imports `tckdb_core`, and `tckdb_core` imports
`tckdb-client` and `tckdb-schemas`.

## Where to start

- To upload an ARC project, read [Using the ARC adapter](arc.md).
- To see what the ARC adapter sends and what it leaves out, read
  [ARC mapping](contract/ARC_MAPPING.md).
- To write an adapter for another tool, read [Writing an adapter](writing_an_adapter.md).
- To see how the supported TCKDB release line is tracked, read
  [The TCKDB pin](tckdb_pin.md).
- To send a change, read [Contributing](contributing.md).

The [repository README](../README.md) has the install commands and the TCKDB versions the
adapters are tested against.
