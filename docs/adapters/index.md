# Adapters

An adapter reads the output of one tool and turns it into TCKDB upload payloads. Each
adapter is its own package. A producer adapter, such as ARC's, is built on the shared
[core](../core/index.md). The CHEMKIN and QCSchema adapters read a file format and use
`tckdb-client` directly.

| Adapter | Package |
|---|---|
| [ARC](arc/index.md) | `tckdb-arc` |
| [CHEMKIN](chemkin/index.md) | `tckdb-chemkin` |
| [QCSchema](qcschema/index.md) | `tckdb-qcschema` |

To add an adapter, see [Writing an adapter](../writing_an_adapter.md).
