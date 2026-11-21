"""Statmech helpers of the ARC adapter.

Classifies the treatment of a rotor or mode, coerces the coordinates of a torsion, and lists the
calculations a statmech block cites. The statmech block itself is built in ``statmech_payloads.py``.
"""

from collections.abc import Mapping
from typing import Any

from tckdb_arc.calculations import _CALC_KEY_FREQ, _CALC_KEY_OPT, _CALC_KEY_SP, _ROLE_COMPOSITE


# StatmechCalculationRole values that ARC's three-stage opt/freq/sp
# workflow can declare. Order is fixed (opt → freq → sp) so the
# emitted source_calculations list is byte-stable across runs of the
# same content. ``opt_coarse`` is intentionally excluded — it's an
# intermediate optimization stage, not a statmech input (the schema's
# StatmechCalculationRole enum has no ``opt_coarse`` value, and the
# task spec is explicit that opt_coarse is not a statmech source).
_STATMECH_CALC_ROLES: tuple[tuple[str, str], ...] = (
    (_CALC_KEY_OPT, "opt"),
    (_CALC_KEY_FREQ, "freq"),
    (_CALC_KEY_SP, "sp"),
    (_ROLE_COMPOSITE, _ROLE_COMPOSITE),
)


def _classify_statmech_treatment(
    torsions: Any,
    *,
    emitted_torsions: list[dict[str, Any]] | None = None,
) -> str | None:
    """Map ARC's torsion list to a TCKDB ``StatmechTreatmentKind`` value.

    ``emitted_torsions`` is the list :func:`_build_slim_torsions` actually
    produced for this record. Pass it whenever the payload will carry
    torsions: a rotor-aware treatment is *defined* by the rotors it treats,
    so TCKDB rejects one that lists none, and the builder drops rotors whose
    ARC ``treatment`` has no TCKDB equivalent. Classifying from ARC's raw
    input alone would therefore claim ``rrho_1d`` over an empty list.

    Rules:

    * ``None`` torsions input (no statmech subdict on the species
      record) → ``None``: ARC didn't emit a statmech evaluation, so the
      treatment is genuinely unknown. Don't fabricate one.
    * Empty list (statmech ran, no successful rotors) → ``"rrho"``.
    * Rotors present but none survived into the payload → ``None``.
      ``"rrho"`` would assert that the species was treated as a rigid
      rotor harmonic oscillator, and ARC's own record says otherwise --
      it treated rotors this adapter could not express. Omitting the
      field says "we are not naming a treatment", which is the only
      claim the evidence supports.
    * ≥1 1D rotor (each entry's ``atom_indices`` is a flat 4-int list)
      and no ND → ``"rrho_1d"``.
    * ≥1 ND rotor (entry's ``atom_indices`` is a list of 4-int lists)
      and no 1D → ``"rrho_nd"``.
    * Mix of 1D and ND → ``"rrho_1d_nd"``.
    * Anything we can't classify confidently → ``None`` (omitted).

    The ``rrho_ad``/``rrao`` enum values are reserved for treatments
    ARC doesn't currently produce; we never emit them.
    """
    if torsions is None:
        return None
    if not isinstance(torsions, list):
        return None
    if not torsions:
        return "rrho"
    if emitted_torsions is not None:
        if not emitted_torsions:
            return None
        # Classify from the emitted entries' ``dimension``, which the builder
        # sets only when it resolved real coordinate quartets. An entry
        # without one was emitted as a summary from unusable atom_indices;
        # its dimensionality is unknown, so no treatment can be named.
        dimensions = [t.get("dimension") for t in emitted_torsions]
        if any(not isinstance(d, int) or d < 1 for d in dimensions):
            return None
        has_1d = any(d == 1 for d in dimensions)
        has_nd = any(d > 1 for d in dimensions)
        if has_1d and has_nd:
            return "rrho_1d_nd"
        if has_nd:
            return "rrho_nd"
        return "rrho_1d"
    has_1d = False
    has_nd = False
    for t in torsions:
        if not isinstance(t, Mapping):
            return None
        atom_indices = t.get("atom_indices")
        if (
            isinstance(atom_indices, list)
            and len(atom_indices) == 4
            and all(isinstance(x, int) for x in atom_indices)
        ):
            has_1d = True
        elif (
            isinstance(atom_indices, list)
            and atom_indices
            and all(isinstance(x, list) for x in atom_indices)
        ):
            has_nd = True
        else:
            return None
    if has_1d and has_nd:
        return "rrho_1d_nd"
    if has_nd:
        return "rrho_nd"
    if has_1d:
        return "rrho_1d"
    return None


def _coerce_torsion_coordinates(
    atom_indices: Any,
) -> list[dict[str, int]] | None:
    """Validate and project ARC's atom_indices into TCKDB coordinate dicts.

    Returns the coordinate list when ``atom_indices`` is well-formed:

      * Flat list of 4 distinct positive ints → one coordinate.
      * List of N flat-4-int lists (each distinct, 4 atoms each) → N
        coordinates with ``coordinate_index`` running 1..N.

    Returns ``None`` when the input is missing, malformed, contains
    non-positive integers, or has duplicate atoms within a quartet —
    callers fall back to a coordinate-less torsion summary so a single
    bad rotor never blocks the whole upload. The 4-distinct-atoms check
    mirrors the server-side ``StatmechTorsionCoordinateIn`` validator;
    failing here gives a clearer producer-side message than letting the
    server 422.
    """
    if atom_indices is None:
        return None
    if not isinstance(atom_indices, list) or not atom_indices:
        return None
    # 1D shape: a flat list of exactly 4 ints.
    if all(isinstance(x, int) for x in atom_indices):
        if len(atom_indices) != 4:
            return None
        if not all(x >= 1 for x in atom_indices):
            return None
        if len(set(atom_indices)) != 4:
            return None
        a1, a2, a3, a4 = atom_indices
        return [{
            "coordinate_index": 1,
            "atom1_index": a1, "atom2_index": a2,
            "atom3_index": a3, "atom4_index": a4,
        }]
    # ND shape: a list of 4-int sub-lists.
    if all(isinstance(x, list) for x in atom_indices):
        coords: list[dict[str, int]] = []
        for i, quartet in enumerate(atom_indices, start=1):
            if not all(isinstance(x, int) for x in quartet):
                return None
            if len(quartet) != 4:
                return None
            if not all(x >= 1 for x in quartet):
                return None
            if len(set(quartet)) != 4:
                return None
            a1, a2, a3, a4 = quartet
            coords.append({
                "coordinate_index": i,
                "atom1_index": a1, "atom2_index": a2,
                "atom3_index": a3, "atom4_index": a4,
            })
        return coords or None
    return None


def _build_statmech_source_calculations(
    *,
    calc_keys_by_role: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Compose statmech ``source_calculations`` from the bundle's emitted calcs.

    Walks ``_STATMECH_CALC_ROLES`` in order (opt → freq → sp) and emits
    one entry per role whose key is present in the caller-supplied
    ``calc_keys_by_role`` map. ``opt_coarse`` is excluded by design —
    it's not a ``StatmechCalculationRole`` enum value.

    The role keys (``"opt"`` / ``"freq"`` / ``"sp"``) are the
    ``StatmechCalculationRole`` enum strings. Values are the bundle-local
    calculation keys the server should resolve. Both computed-species
    and computed-reaction call this with their own scoped values; this
    helper is mode-agnostic.
    """
    return [
        {"calculation_key": calc_keys_by_role[role], "role": role}
        for _, role in _STATMECH_CALC_ROLES
        if role in calc_keys_by_role and calc_keys_by_role[role]
    ]
