# Adapters

An adapter reads the output of one tool and turns it into TCKDB upload payloads. Each adapter
is its own package, built on the shared [core](../core/index.md), with its own README,
changelog and mapping document.

| Adapter | Package | Reads | What it builds |
|---|---|---|---|
| [ARC](arc/index.md) | `tckdb-arc` | an ARC project's `output.yml` | species, reaction and transition-state uploads |

The warning codes every adapter emits are listed on one page, [Warning codes](../contract/WARNING_CODES.md).
To add an adapter, see [Writing an adapter](../writing_an_adapter.md).
