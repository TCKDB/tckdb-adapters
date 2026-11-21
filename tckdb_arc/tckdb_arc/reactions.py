"""Reaction and transition-state helpers of the ARC adapter.

Participant slots of a reaction, the TS atom-map slots read from the route geometries, and the
SMILES handle of a transition state whose atoms ARC did not map.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from tckdb_arc.geometry import _geometry_payload, _normalize_xyz_text


def _ts_unmapped_smiles_handle(
    *,
    ts_record: Mapping[str, Any],
    reaction_record: Mapping[str, Any],
    species_index: Mapping[str, Mapping[str, Any]],
) -> str | None:
    """Derive a deterministic textual handle for the TS, or ``None``.

    Resolution order, lifted directly from the spec:

    1. ``ts_record['smiles']`` when ARC already attached a textual TS
       identifier (rare in production — TS records usually have SMILES
       null because there's no Lewis structure).
    2. A canonical reaction-SMILES handle ``"<r1>.<r2>>><p1>.<p2>"``
       built from the reactant/product species' SMILES looked up
       through ``species_index``. ``.`` joins species; ``>>`` separates
       reactant and product sides — the standard interchange format
       and unambiguous from ARC's data. This is *not* a claim that the
       TS itself is a single molecule; it's the same data the field
       intends to carry — a traceability handle.
    3. ``None`` when any reactant or product lacks a SMILES, or when
       the reaction has no reactant/product labels. The producer
       refuses to emit a misleading half-handle; TCKDB stores NULL.

    Determinism: the helper is a pure function of the input mappings.
    No paths, timestamps, or run identifiers leak in. Idempotency keys
    will move only if the upstream species SMILES change.
    """
    direct = ts_record.get("smiles")
    if direct:
        text = str(direct).strip()
        if text:
            return text

    def _smiles_for_labels(labels: list[str] | None) -> list[str] | None:
        out: list[str] = []
        for label in labels or []:
            record = species_index.get(label)
            if not isinstance(record, Mapping):
                return None
            smiles = record.get("smiles")
            if not smiles:
                return None
            out.append(str(smiles).strip())
        return out or None

    reactant_smiles = _smiles_for_labels(reaction_record.get("reactant_labels"))
    if reactant_smiles is None:
        return None
    product_smiles = _smiles_for_labels(reaction_record.get("product_labels"))
    if product_smiles is None:
        return None

    return f"{'.'.join(reactant_smiles)}>>{'.'.join(product_smiles)}"


def _participant_slots(labels: Sequence[Any]) -> list[tuple[str, int]]:
    """``(label, occurrence)`` of each participant slot, in the order of ``labels``.

    TCKDB numbers a side's participants by their position in ``reactant_keys``
    / ``product_keys``, which the adapter builds from the reaction record's
    ``reactant_labels`` / ``product_labels``. ARC numbers the participants of
    ``irc_participant_mapping`` in the order of ``atom_map_reactant_labels`` /
    ``atom_map_product_labels`` (``r_species`` order), and ``reactant_labels``
    is sorted instead, so the two orders differ and ARC's ``position`` is not
    TCKDB's ``participant_index``. The label and its occurrence (the 1-based
    count of that label up to the slot, as ARC states it) identify the same
    participant in both.
    """
    seen: dict[str, int] = {}
    slots: list[tuple[str, int]] = []
    for label in labels:
        seen[str(label)] = seen.get(str(label), 0) + 1
        slots.append((str(label), seen[str(label)]))
    return slots


def _ts_route_atom_map_slots(
    reaction_record: Mapping[str, Any], species_index: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The participant slots of a standalone TS upload, with the key and geometry each would carry.

    Keys (``reactant_1``) and geometry keys (``reactant_1_geom``) are unique per slot, as the route
    requires, even when one species fills several slots. A slot whose species has no usable geometry
    has ``geometry: None``.
    """
    slots: list[dict[str, Any]] = []
    for side, labels_key in (("reactant", "reactant_labels"), ("product", "product_labels")):
        for position, label in enumerate(reaction_record.get(labels_key) or [], start=1):
            record = species_index.get(label) or {}
            key = f"{side}_{position}"
            text = _normalize_xyz_text(record.get("xyz"), label) if record.get("xyz") else None
            geometry = _geometry_payload(
                record, text, record.get("xyz_isotopes"), key=f"{key}_geom") if text else None
            slots.append({"side": side, "index": position, "species_key": key,
                          "label": label, "geometry": geometry})
    return slots


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
