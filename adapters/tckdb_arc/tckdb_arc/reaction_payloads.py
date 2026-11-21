"""Reaction payload helpers of the ARC adapter.

The participants a reaction states, and the atom map of a reaction with the findings reported when it is
not sent.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from tckdb_arc.warning_codes import ArcWarning
from tckdb_arc.self_check import _self_check
from tckdb_arc.geometry import _xyz_element_symbols
from tckdb_arc.reactions import _is_plain_int
from tckdb_arc._logging import get_logger

logger = get_logger()


_W_ATOM_MAP_TS_ORDER_NOT_STATED = ArcWarning.ATOM_MAP_TS_ORDER_NOT_STATED.value
_W_REACTION_STOICHIOMETRY_NOT_STATED = ArcWarning.REACTION_STOICHIOMETRY_NOT_STATED.value
_W_REACTION_SPECIES_LABELS_CONTRADICTED = ArcWarning.REACTION_SPECIES_LABELS_CONTRADICTED.value


def _with_stated_participants(
    reaction_record: Mapping[str, Any],
    *,
    ts_record: Mapping[str, Any] | None,
    species_index: Mapping[str, Mapping[str, Any]],
    warnings: list[dict[str, Any]] | None = None,
) -> Mapping[str, Any]:
    """The reaction record with one label per participant occurrence.

    ARC's ``reactant_labels`` / ``product_labels`` are ``list(rxn.reactants)``
    after ``remove_dup_species`` sorted and de-duplicated them, so ``HO2 + HO2
    <=> H2O2 + O2`` is exported as ``['HO2'] <=> ['H2O2', 'O2']`` and TCKDB
    refuses the unbalanced reaction. The occurrences are taken, in this
    order, from (0) ``reactant_species_labels`` / ``product_species_labels`` (output 1.3 at
    ARC #1059 ebc88ec8: one entry per occurrence, in ``get_reactants_and_products`` order, always
    stated for a reaction ARC ran; they are THE order of ``reactant_keys`` / ``product_keys``), else,
    for the earlier 1.3 draft that lacked them, from (a) ``atom_map_reactant_labels`` /
    ``atom_map_product_labels`` (one label per occurrence, present when ``atom_map`` is), then
    (b) the participants of the TS's ``irc_participant_mapping`` (repeats
    expanded, in ``position`` order); each must name exactly the species of
    the collapsed lists. Otherwise (c) the collapsed lists are used when their
    elements balance (from the species geometries; not checked when a geometry
    is missing), and the reaction is refused when they do not, with
    ``reaction_stoichiometry_not_stated``. The reaction label string is never
    parsed, and no repeat is guessed from a label.

    When (0) is stated it is refused (``reaction_species_labels_contradicted``) if it disagrees with
    ``atom_map_*_labels`` (which ARC says it equals when ``atom_map`` is not null), or names other
    species than the collapsed ``reactant_labels`` / ``product_labels``, or is not a list of labels.
    """
    collapsed = {side: [str(x) for x in reaction_record.get(key) or []]
                 for side, key in (("reactants", "reactant_labels"), ("products", "product_labels"))}

    species_labels = {side: reaction_record.get(f"{side[:-1]}_species_labels") for side in collapsed}
    if any(isinstance(v, list) and v for v in species_labels.values()):
        def contradicted(reason: str) -> ValueError:
            message = (
                f"reaction {reaction_record.get('label')!r}: ARC's reactant_species_labels / "
                f"product_species_labels {species_labels} are contradicted: {reason}; the "
                "reaction was not uploaded.")
            logger.warning("TCKDB %s: %s", _W_REACTION_SPECIES_LABELS_CONTRADICTED, message)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_REACTION_SPECIES_LABELS_CONTRADICTED, message, "reactant_keys",
                    {"action": "reaction_not_uploaded"}))
            return ValueError(message)

        if not all(isinstance(v, list) and v and all(isinstance(x, str) and x for x in v)
                   for v in species_labels.values()):
            raise contradicted("one side is missing, empty, or not a list of labels")
        for side in collapsed:
            map_labels = reaction_record.get(f"atom_map_{side[:-1]}_labels")
            if map_labels is not None and list(map_labels) != species_labels[side]:
                raise contradicted(f"the {side} differ from atom_map_{side[:-1]}_labels {map_labels}, "
                                   "which ARC states they equal when atom_map is not null")
            if collapsed[side] and sorted(set(species_labels[side])) != sorted(set(collapsed[side])):
                raise contradicted(f"the {side} name other species than {side[:-1]}_labels "
                                   f"{collapsed[side]}")
        return {**reaction_record, "reactant_labels": list(species_labels["reactants"]),
                "product_labels": list(species_labels["products"])}

    def from_atom_map() -> dict[str, list[str]] | None:
        found = {side: reaction_record.get(f"atom_map_{side[:-1]}_labels")
                 for side in collapsed}
        return found if all(isinstance(v, list) and v for v in found.values()) else None

    def from_irc() -> dict[str, list[str]] | None:
        mapping = (ts_record or {}).get("irc_participant_mapping")
        if not isinstance(mapping, Mapping):
            return None
        found: dict[str, list[str]] = {}
        for side in collapsed:
            part = (mapping.get(side) or {}).get("participants") if isinstance(
                mapping.get(side), Mapping) else None
            if not isinstance(part, list) or not part or not all(
                    isinstance(p, Mapping) and isinstance(p.get("position"), int) for p in part):
                return None
            found[side] = [p.get("label") for p in sorted(part, key=lambda p: p["position"])]
        return found

    for source in (from_atom_map, from_irc):
        stated = source()
        if stated is None:
            continue
        stated = {side: [str(x) for x in labels] for side, labels in stated.items()}
        if all(sorted(set(stated[side])) == sorted(set(collapsed[side])) for side in collapsed):
            if all(Counter(stated[side]) == Counter(collapsed[side]) for side in collapsed):
                return reaction_record       # no repeat was lost; keep the exported order
            return {**reaction_record, "reactant_labels": stated["reactants"],
                    "product_labels": stated["products"]}

    counts = {}
    for side, labels in collapsed.items():
        symbols = [_xyz_element_symbols((species_index.get(label) or {}).get("xyz")) for label in labels]
        if not labels or any(sym is None for sym in symbols):
            return reaction_record           # nothing to check against
        counts[side] = Counter(sym for syms in symbols for sym in syms)
    if counts["reactants"] == counts["products"]:
        return reaction_record
    message = (
        f"reaction {reaction_record.get('label')!r}: ARC's reactant_labels {collapsed['reactants']} and "
        f"product_labels {collapsed['products']} are sorted and de-duplicated, are not atom-balanced, and "
        "ARC states no per-occurrence participants (atom_map_*_labels and irc_participant_mapping are "
        "absent), so a repeated species cannot be recovered; the reaction was not uploaded."
    )
    logger.warning("TCKDB %s: %s", _W_REACTION_STOICHIOMETRY_NOT_STATED, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_REACTION_STOICHIOMETRY_NOT_STATED, message, "reactant_keys",
            {"action": "reaction_not_uploaded"}))
    raise ValueError(message)


def _warn_atom_map_not_sent(
    reaction_record: Mapping[str, Any],
    *,
    ts_label: Any,
    warnings: list[dict[str, Any]] | None,
) -> None:
    """Report ARC's reaction ``atom_map`` (schema 1.3) that cannot be sent as TCKDB's ``atom_map``.

    TCKDB's ``ReactionAtomMapIn`` is ``{participant atom: transition-state atom}``
    for every participant, both sides 1-based, against the transition state
    geometry. ARC's ``atom_map`` is reactant-atom to product-atom, and its
    schema states that it "says nothing about the atom order of the transition
    state". No ARC key relates any participant atom to a transition-state atom
    one by one: ``irc_participant_mapping`` records only which atoms belong to
    which participant ("only atom-set membership is recorded, not the
    atom-to-atom correspondence inside a participant"). Writing TS atom ``i`` =
    reactant atom ``i`` would claim a TS order ARC does not state, and TCKDB
    never derives a map (ADR 0011). So no ``atom_map`` is built, and TCKDB
    reports ``reaction_atom_map_absent`` for the reaction.
    """
    atom_map = reaction_record.get("atom_map")
    if not isinstance(atom_map, list) or not atom_map:
        return
    if "ts_atom_map" in reaction_record:
        # ARC #1059 ebc88ec8 states ``ts_atom_map`` and, when it cannot, why.
        unavailable = reaction_record.get("ts_atom_map_unavailable_reason")
        _warn_ts_atom_map_not_sent(
            reaction_record, ts_label=ts_label, warnings=warnings,
            why=("ARC states ts_atom_map = null with ts_atom_map_unavailable_reason "
                 f"{unavailable!r}"),
            action="atom_map_omitted")
        return
    reason = (
        f"TCKDB's atom_map maps each participant atom to an atom of the transition-state "
        f"geometry {ts_label!r} and ARC states no relation between its map and the "
        "transition-state atom order (the output document has no ts_atom_map; it predates ARC "
        "#1059 ebc88ec8)"
    )
    message = (
        f"ARC states a reactant-to-product atom_map for {reaction_record.get('label')!r} "
        f"(source {reaction_record.get('atom_map_source')!r}), but {reason}, so no atom_map "
        "was sent. TCKDB will report reaction_atom_map_absent."
    )
    logger.warning("TCKDB %s: %s", _W_ATOM_MAP_TS_ORDER_NOT_STATED, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_ATOM_MAP_TS_ORDER_NOT_STATED, message, "atom_map",
            {"action": "atom_map_omitted", "atom_map_source": str(reaction_record.get("atom_map_source")), "atom_map_method": str(reaction_record.get("atom_map_method"))}))


_W_REACTION_TS_ATOM_MAP_NOT_SENT = ArcWarning.REACTION_TS_ATOM_MAP_NOT_SENT.value
_TS_ATOM_MAP_METHOD = "irc_endpoint_cgr_isomorphism"
_TS_ATOM_MAP_NOTE = (
    "ARC ts_atom_map (output.yml 1.3, method irc_endpoint_cgr_isomorphism, IRC endpoint {endpoint} "
    "served as the reactants): the TS atom of every reactant and product atom, from a label-preserving "
    "isomorphism of the condensed graph of reaction of the reaction (its atom_map) and that of the TS "
    "and its two IRC endpoint geometries. It is a constitutional (2D) correspondence: symmetry-equivalent "
    "atoms, including diastereotopic ones, are assigned by a deterministic convention that no geometry "
    "decides, and a different but equally valid assignment exists."
)


def _warn_ts_atom_map_not_sent(
    reaction_record: Mapping[str, Any],
    *,
    ts_label: Any,
    warnings: list[dict[str, Any]] | None,
    why: str,
    action: str = "atom_map_omitted",
) -> None:
    """Report that no TCKDB ``atom_map`` was built from ARC's ``ts_atom_map`` (or its absence), and why."""
    message = (
        f"No atom_map was sent for {reaction_record.get('label')!r} (transition state {ts_label!r}): "
        f"{why}. TCKDB will report reaction_atom_map_absent."
    )
    logger.warning("TCKDB %s: %s", _W_REACTION_TS_ATOM_MAP_NOT_SENT, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_REACTION_TS_ATOM_MAP_NOT_SENT, message, "atom_map",
            {"action": action, "ts_atom_map_unavailable_reason": str(
                            reaction_record.get("ts_atom_map_unavailable_reason")), "atom_map_source": str(reaction_record.get("atom_map_source")), "atom_map_method": str(reaction_record.get("atom_map_method"))}))


def _tckdb_reaction_atom_map(
    reaction_record: Mapping[str, Any],
    *,
    ts_label: Any,
    ts_xyz_text: str | None,
    ts_geometry_key: str,
    participants: Sequence[Mapping[str, Any]],
    irc_evidence: Sequence[Mapping[str, Any]] = (),
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """TCKDB's ``ReactionAtomMapIn`` from ARC's ``ts_atom_map`` (output 1.3 at ARC #1059 ebc88ec8), or ``None``.

    ARC states, for every concatenated reactant atom and every concatenated product atom, the 0-based TS
    atom it is (``ts_atom_map.reactants`` / ``.products``), counting the participants in the order of
    ``atom_map_reactant_labels`` / ``atom_map_product_labels`` and each participant's atoms in the atom
    order of its exported geometry. ``participants`` are the uploaded slots, in upload order, each
    ``{side, index (1-based), species_key, label, geometry: {key, xyz_text} | None}``; for each, its block
    of the map becomes ``atom_to_ts`` ({participant geometry atom: TS atom}, both 1-based) and the map is
    ``source: inferred`` naming ARC's method and its symmetry-equivalent-atom convention (the note).

    Nothing is sent unless every check passes; a failure reports ``reaction_ts_atom_map_not_sent`` and
    sends no map (never a partial one): ``ts_atom_map`` is stated and well formed; the uploaded slot labels
    are exactly ``atom_map_*_labels``; each block length is the participant's uploaded geometry atom count;
    TS indices are in range and cover every TS atom once per side; each participant atom has the element of
    its TS atom; ``products[atom_map[i]] == reactants[i]``; and, when the IRC evidence sent a participant
    mapping, each participant's TS atoms are the ones that mapping gives it. When ARC states
    ``ts_atom_map = null`` (or the document predates the key) the omission is reported with ARC's reason.
    """
    atom_map = reaction_record.get("atom_map")
    if not isinstance(atom_map, list) or not atom_map:
        return None
    ts_map = reaction_record.get("ts_atom_map")
    if "ts_atom_map" not in reaction_record or ts_map is None:
        _warn_atom_map_not_sent(reaction_record, ts_label=ts_label, warnings=warnings)
        return None

    def refuse(why: str) -> None:
        _warn_ts_atom_map_not_sent(
            reaction_record, ts_label=ts_label, warnings=warnings,
            why=f"ARC's ts_atom_map cannot be sent as TCKDB's atom_map: {why}")
        return None

    if not isinstance(ts_map, Mapping):
        return refuse("it is not an object")
    reactants, products = ts_map.get("reactants"), ts_map.get("products")
    endpoint = ts_map.get("reactant_endpoint")
    if (ts_map.get("method") != _TS_ATOM_MAP_METHOD or not _is_plain_int(endpoint)
            or endpoint not in (1, 2) or not isinstance(ts_map.get("ts_atom_order_follows_reactants"), bool)
            or not all(isinstance(side, list) and all(_is_plain_int(i) for i in side)
                       for side in (reactants, products))):
        return refuse("it does not have the documented shape (method "
                      f"{ts_map.get('method')!r}, reactant_endpoint {endpoint!r})")
    if ts_map.get("ts_label") != ts_label:
        return refuse(f"it is for transition state {ts_map.get('ts_label')!r}, not {ts_label!r}")
    ts_symbols = _xyz_element_symbols(ts_xyz_text)
    if ts_symbols is None:
        return refuse("the transition-state geometry has no readable atoms")
    n_ts = len(ts_symbols)
    if not all(_is_plain_int(i) for i in atom_map):
        return refuse("ARC's atom_map is not a list of integers")
    sides: dict[str, list[Mapping[str, Any]]] = {"reactant": [], "product": []}
    for participant in participants:
        sides[participant["side"]].append(participant)
    for side, key in (("reactant", "atom_map_reactant_labels"), ("product", "atom_map_product_labels")):
        if [p["label"] for p in sides[side]] != list(reaction_record.get(key) or []):
            return refuse(f"the uploaded {side} slots {[p['label'] for p in sides[side]]} are not "
                          f"ARC's {key} {reaction_record.get(key)}, the order its map counts in")
    mapped = {"reactant": reactants, "product": products}
    symbols: dict[str, list[list[str]]] = {"reactant": [], "product": []}
    for side in sides:
        for participant in sides[side]:
            geometry = participant.get("geometry")
            found = _xyz_element_symbols(geometry.get("xyz_text") if isinstance(geometry, Mapping) else None)
            if found is None:
                return refuse(f"{side} {participant['index']} ({participant['label']!r}) has no readable "
                              "uploaded geometry")
            symbols[side].append(list(found))
        total = sum(len(block) for block in symbols[side])
        if len(mapped[side]) != total or len(atom_map) != total:
            return refuse(f"the {side} block lengths do not agree: ts_atom_map.{side}s has "
                          f"{len(mapped[side])} entries, atom_map has {len(atom_map)}, and the uploaded "
                          f"{side} geometries have {total} atoms")
        if sorted(mapped[side]) != list(range(n_ts)):
            return refuse(f"ts_atom_map.{side}s is not a one-to-one map onto the {n_ts} transition-state "
                          "atoms (an index is out of range, repeated, or a TS atom is not covered)")
    n = len(atom_map)
    if sorted(atom_map) != list(range(n)) or any(products[atom_map[i]] != reactants[i] for i in range(n)):
        return refuse("it is not consistent with ARC's atom_map (products[atom_map[i]] != reactants[i] "
                      "for some reactant atom i)")
    if ts_map["ts_atom_order_follows_reactants"] != (list(reactants) == list(range(n))):
        return refuse("ts_atom_order_follows_reactants disagrees with its own reactants list")

    entries: list[dict[str, Any]] = []
    claimed: dict[tuple[str, int], set[int]] = {}
    for side in ("reactant", "product"):
        offset = 0
        for participant, block in zip(sides[side], symbols[side]):
            atom_to_ts: dict[str, int] = {}
            for atom_index, element in enumerate(block):
                ts_atom = mapped[side][offset + atom_index]
                if ts_symbols[ts_atom].capitalize() != element.capitalize():
                    return refuse(
                        f"{side} {participant['index']} ({participant['label']!r}) atom {atom_index + 1} is "
                        f"{element} but the transition-state atom {ts_atom + 1} it maps to is "
                        f"{ts_symbols[ts_atom]}")
                atom_to_ts[str(atom_index + 1)] = ts_atom + 1
            offset += len(block)
            claimed[(side, participant["index"])] = set(atom_to_ts.values())
            entries.append({
                "side": side,
                "species_key": participant["species_key"],
                "participant_index": participant["index"],
                "geometry_key": participant["geometry"]["key"],
                "atom_to_ts": atom_to_ts,
            })
    for evidence in irc_evidence:
        if evidence.get("kind") != "irc":
            continue
        for side in ("reactant", "product"):
            given = evidence.get(f"{side}_participant_mapping")
            if not isinstance(given, Mapping):
                continue
            for slot, atoms in given.items():
                index = int(str(slot).split(":")[1])
                if claimed.get((side, index)) != set(atoms):
                    return refuse(
                        f"it contradicts the IRC participant mapping that is sent: {slot} has transition-state "
                        f"atoms {sorted(claimed.get((side, index), ()))} here and {sorted(atoms)} there")
    return {
        "source": "inferred",
        "ts_geometry_key": ts_geometry_key,
        "participants": entries,
        "note": _TS_ATOM_MAP_NOTE.format(endpoint=endpoint),
    }
