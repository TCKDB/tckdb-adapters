"""Transition-state validation evidence of the ARC adapter.

The IRC, energy-ordering and imaginary-mode evidence blocks of a transition state, and the IRC participant
mappings the IRC evidence carries.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from tckdb_arc.warning_codes import ArcWarning
from tckdb_core import ts_evidence as _ts_evidence
from tckdb_arc.self_check import _self_check
from tckdb_arc.geometry import _xyz_element_symbols
from tckdb_arc.reactions import _participant_slots
from tckdb_arc._logging import get_logger

from tckdb_arc.core_aliases import _finite_float

logger = get_logger()


_W_TS_IRC_EVIDENCE_WITHOUT_IRC_CALCULATION = ArcWarning.TS_IRC_EVIDENCE_WITHOUT_IRC_CALCULATION.value


def _ts_irc_validation_evidence(
    ts_record: Mapping[str, Any],
    *,
    irc_calc_key: str | None,
    ts_label: Any,
    warnings: list[dict[str, Any]] | None = None,
    reaction_record: Mapping[str, Any] | None = None,
    species_index: Mapping[str, Mapping[str, Any]] | None = None,
    ts_xyz_text: str | None = None,
) -> list[dict[str, Any]]:
    """Map ARC's ``ts_checks['IRC']`` verdict to TCKDB ``validation_evidence``.

    ``ts_checks['IRC']`` is ``True`` (the IRC connects the declared
    reactants and products), ``False`` (it does not) or ``None`` (not run, or
    no conclusion; arc/output.py ``_ts_checks_to_dict``). Only a bool is
    evidence; ``None`` or an absent ``ts_checks`` sends nothing, and the
    verdict is never derived from ``irc_converged``, which reports that the IRC
    jobs completed, not that the IRC validated anything.

    TCKDB binds evidence to an ``irc`` calculation of this transition state
    (``source_calculation_key``; the standalone route omits the key and binds
    to its single irc calculation). Without one there is nothing to bind to,
    so a verdict with no IRC calculation is reported and not sent. ``rationale``
    is required and non-empty: it states the source of ``passed``. ARC's
    ``ts_checks['warnings']`` are not used: they come only from the E0/e_elect
    and normal-mode-displacement checks (``arc/checks/ts.py``,
    ``arc/checks/nmd.py``), never from the IRC.

    ARC output schema 1.3 states the participant mappings
    (``irc_participant_mapping``); :func:`_irc_participant_mappings` turns them
    into ``reactant_participant_mapping`` / ``product_participant_mapping`` on
    a passed record, or sends neither side and says why.
    """
    checks = ts_record.get("ts_checks")
    verdict = checks.get("IRC") if isinstance(checks, Mapping) else None
    if not isinstance(verdict, bool):
        return []
    if irc_calc_key is None:
        message = (
            f"ARC recorded ts_checks['IRC'] = {verdict} for {ts_label!r} but the "
            "TS has no IRC calculation in this upload, so there is no irc "
            "calculation for validation evidence to bind to; the evidence was "
            "not sent."
        )
        logger.warning("TCKDB %s: %s", _W_TS_IRC_EVIDENCE_WITHOUT_IRC_CALCULATION, message)
        if warnings is not None:
            warnings.append(_self_check(
                _W_TS_IRC_EVIDENCE_WITHOUT_IRC_CALCULATION, message, "transition_state.validation_evidence",
                {"action": "validation_evidence_omitted", "ts_checks_irc": str(verdict).lower()}))
        return []
    mappings = None
    if verdict is True and reaction_record is not None and species_index is not None:
        mappings = _irc_participant_mappings(
            ts_record,
            reaction_record=reaction_record,
            species_index=species_index,
            ts_xyz_text=ts_xyz_text,
            ts_label=ts_label,
            warnings=warnings,
        )
    return [_ts_evidence.irc_record(
        verdict,
        source_calculation_key=irc_calc_key,
        rationale=f"ARC ts_checks['IRC'] = {verdict}",
        participant_mappings=mappings,
    )]


_W_TS_ENERGY_ORDERING_NOT_SENT = ArcWarning.TS_ENERGY_ORDERING_NOT_SENT.value
_W_TS_IMAGINARY_MODE_NOT_SENT = ArcWarning.TS_IMAGINARY_MODE_NOT_SENT.value
_W_TS_NMD_FORCED_CONTRADICTS_INDEX = ArcWarning.TS_NMD_FORCED_CONTRADICTS_INDEX.value
# The e_elect check's margin (``arc/checks/ts.py``), in kJ/mol; hartree convert with the vendored ``E_h_kJmol``.
_ENERGY_ORDERING_MARGIN_KJ_MOL = 1.0


def _warn_ts_evidence_not_sent(
    warnings: list[dict[str, Any]] | None, *, code: str, ts_label: Any, message: str,
    context: Mapping[str, Any] | None = None,
) -> None:
    """Report a TS validation verdict ARC states that is not sent (the adapter never guesses around it)."""
    logger.warning("TCKDB %s: %s", code, message)
    if warnings is not None:
        warnings.append(_self_check(
            code, message, "transition_state.validation_evidence",
            {"action": "validation_evidence_omitted", "ts_label": str(ts_label), **(context or {})}))


def _ts_energy_ordering_validation_evidence(
    ts_record: Mapping[str, Any],
    *,
    ts_sp_key: str | None,
    reactant_labels: Sequence[str],
    product_labels: Sequence[str],
    reactant_keys: Sequence[str],
    product_keys: Sequence[str],
    actor_calc_keys: Mapping[str, Mapping[str, str]],
    species_index: Mapping[str, Mapping[str, Any]],
    ts_label: Any,
    warnings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Map ARC's ``ts_checks['e_elect']`` verdict to a TCKDB ``energy_ordering`` record (bundle route only).

    ARC's check (``arc/checks/ts.py::check_rxn_e_elect``) is ``ts_e_elect > sum(reactants) + 1 kJ/mol``
    and ``> sum(products) + 1 kJ/mol`` over the participants (each with its stoichiometric multiplier).
    It leaves ``e_elect`` unset (``None``) whenever the zero-point-corrected ``E0`` check passed, and a
    record's ``passed`` is required, so ``None`` sends nothing. Only the *electronic* energies are sent
    (each participant's ``sp_energy_hartree``, cited to its own ``sp`` calculation); ``e0`` is not:
    ARC's ``e0_kj_mol`` carries corrections and is not one calculation's absolute energy. ARC's ``E0``
    verdict is only mentioned in ``rationale``.

    Participants are numbered by their position in ``reactant_keys`` / ``product_keys`` (a repeated
    species repeats its entry, as the bundle repeats the key). TCKDB refuses a passing record whose
    stated numbers do not put the saddle point above each side, so a ``True`` verdict is first
    re-derived from the stated hartree values with ARC's margin; when they contradict it the record is
    not sent. A ``False`` verdict whose stated numbers do satisfy the ordering was computed on stale
    energies and is likewise not sent. A participant without a stated finite non-positive energy, or without an ``sp``
    calculation in the upload, also leaves the record out (a passing record needs every participant).
    """
    checks = ts_record.get("ts_checks")
    verdict = checks.get("e_elect") if isinstance(checks, Mapping) else None
    if not isinstance(verdict, bool):
        return []
    context = {"ts_checks_e_elect": str(verdict).lower()}

    def omit(reason: str, *, reason_code: str | None = None) -> list[dict[str, Any]]:
        _warn_ts_evidence_not_sent(
            warnings, code=_W_TS_ENERGY_ORDERING_NOT_SENT, ts_label=ts_label,
            context={**context, **({"reason": reason_code} if reason_code else {})},
            message=(f"ARC recorded ts_checks['e_elect'] = {verdict} for {ts_label!r}, but {reason}; "
                     "the energy_ordering evidence was not sent."))
        return []

    entries: list[tuple[str, str, Any, str | None]] = [
        ("ts", str(ts_label), ts_record.get("sp_energy_hartree"), ts_sp_key)]
    for side, labels, keys in (("reactant", reactant_labels, reactant_keys),
                               ("product", product_labels, product_keys)):
        if len(labels) != len(keys):
            return omit(f"the {side} labels and keys disagree")
        for position, (label, key) in enumerate(zip(labels, keys), start=1):
            entries.append((f"{side}:{position}", str(label),
                            (species_index.get(label) or {}).get("sp_energy_hartree"),
                            (actor_calc_keys.get(key) or {}).get("sp")))
    energies: list[dict[str, Any]] = []
    for participant, label, raw, key in entries:
        energy = _finite_float(raw)
        if energy is None:
            return omit(f"{participant} ({label!r}) has no stated finite sp_energy_hartree")
        if energy > 0:
            return omit(f"{participant} ({label!r}) has a positive sp_energy_hartree ({energy}); "
                        "TCKDB takes absolute (non-positive) energies")
        if not key:
            return omit(f"{participant} ({label!r}) has no sp calculation in this upload to cite")
        energies.append(_ts_evidence.energy_entry(participant, energy, key))
    # TCKDB refuses a pass its own numbers contradict; a failure they contradict is stale.
    contradiction = _ts_evidence.energy_ordering_contradiction(
        verdict, energies, margin_kj_mol=_ENERGY_ORDERING_MARGIN_KJ_MOL)
    if contradiction is not None:
        reason, reason_code = contradiction
        return omit(reason, reason_code=reason_code)
    rationale = f"ARC ts_checks['e_elect'] = {verdict} (electronic energies; sp_energy_hartree of each participant)"
    e0_verdict = checks.get("E0")
    if isinstance(e0_verdict, bool):
        rationale += f"; ARC ts_checks['E0'] = {e0_verdict} (not sent: not one calculation's energy)"
    return [_ts_evidence.energy_ordering_record(verdict, rationale, energies)]


def _ts_imaginary_mode_validation_evidence(
    ts_record: Mapping[str, Any],
    *,
    freq_calc_key: str | None,
    freq_result: Mapping[str, Any] | None,
    ts_label: Any,
    warnings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Map ARC's ``ts_checks['freq']`` verdict to a TCKDB ``imaginary_mode`` record.

    ``passed`` is ``ts_checks['freq']`` when it is a bool, nothing otherwise. The record states what the
    TS frequency calculation found, read from the ``freq_result`` this upload sends for it, so the
    record cannot disagree with the result it cites (TCKDB refuses a count that differs, or a frequency
    more than 1 cm^-1 apart): ``imaginary_frequency_count`` is its ``n_imag``;
    ``imaginary_frequency_cm1`` is the designated reaction-coordinate mode's frequency (ARC's 1.3
    ``reaction_coordinate_mode_index`` into ``freq_frequencies_cm1_ess_order``, else tau / the window rule),
    or, with exactly one imaginary mode, ``imag_freq_cm1``; both are negated to the negative convention.
    ``mode_displacement_agrees`` is ``True`` only when ARC's own ``reaction_coordinate_mode_index`` is
    what designates the mode (ARC states it only for a genuine, non-forced normal-mode-displacement pass)
    and the TS's ``nmd_forced`` (output 1.3, ARC #1059 ebc88ec8) is not ``True``,
    ``False`` when ``ts_checks['NMD']`` is ``False`` with no index stated, or when ``nmd_forced`` is ``True``
    (ARC forces only a check that ran and failed, so the displacement disagreed), otherwise omitted (not
    assessed: an unrun or undecided check). ``nmd_forced: true`` never yields ``True``, and an index stated beside it is a contradiction that
    is reported (``ts_nmd_forced_contradicts_reaction_coordinate_index``) and not used. A passing record with more
    than one imaginary mode needs the cited result to designate the coordinate; otherwise it is not sent.
    On the standalone route the key is dropped by the caller (the record binds to the single freq).
    """
    checks = ts_record.get("ts_checks")
    verdict = checks.get("freq") if isinstance(checks, Mapping) else None
    if not isinstance(verdict, bool):
        return []
    context = {"ts_checks_freq": str(verdict).lower()}

    def omit(reason: str) -> list[dict[str, Any]]:
        _warn_ts_evidence_not_sent(
            warnings, code=_W_TS_IMAGINARY_MODE_NOT_SENT, ts_label=ts_label, context=context,
            message=(f"ARC recorded ts_checks['freq'] = {verdict} for {ts_label!r}, but {reason}; "
                     "the imaginary_mode evidence was not sent."))
        return []

    if freq_calc_key is None or freq_result is None:
        return omit("the TS has no frequency calculation in this upload, so there is no freq "
                    "calculation for the evidence to bind to")
    # What the cited frequency result states (a finite float or None for the frequency).
    n_imag, designated, value = _ts_evidence.freq_result_imaginary_facts(freq_result)
    if verdict and n_imag == 0:
        return omit("its frequency result reports no imaginary mode")
    if verdict and n_imag is not None and n_imag > 1 and designated is None:
        return omit(f"the frequency result has {n_imag} imaginary modes and designates no reaction "
                    "coordinate, which TCKDB requires of a passing record")
    mode_displacement_agrees: bool | None = None
    stated = ts_record.get("reaction_coordinate_mode_index")
    stated_index = stated if isinstance(stated, int) and not isinstance(stated, bool) else None
    nmd = checks.get("NMD")
    nmd_forced = ts_record.get("nmd_forced")
    rationale = f"ARC ts_checks['freq'] = {verdict}"
    if nmd_forced is True:
        # ARC sets ``nmd_forced`` only after ``check_normal_mode_displacement`` returned False and
        # ``skip_nmd`` overwrote ts_checks['NMD'] to True (arc/checks/ts.py); a None ("could not
        # decide") is never forced. So the displacement check ran and found the mode inconsistent
        # with the reaction: that is a ``False`` verdict, never ``True``. (TCKDB has no rule tying
        # ``mode_displacement_agrees`` to ``passed``.)
        mode_displacement_agrees = False
        rationale += ("; mode_displacement_agrees = False: ARC's normal mode displacement check failed "
                      "and the pass in ts_checks['NMD'] was forced (skip_nmd; nmd_forced = true)")
        if stated_index is not None:
            message = (
                f"ARC states reaction_coordinate_mode_index = {stated_index} for {ts_label!r} but also "
                "nmd_forced = true (the normal mode displacement check failed and the pass was forced); "
                "ARC states the index only for a genuine pass, so the index was not used as an NMD "
                "designation and mode_displacement_agrees was sent as False.")
            logger.warning("TCKDB %s: %s", _W_TS_NMD_FORCED_CONTRADICTS_INDEX, message)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_TS_NMD_FORCED_CONTRADICTS_INDEX, message, "transition_state.validation_evidence",
                    {"action": "reaction_coordinate_index_not_used", "ts_label": str(ts_label), "nmd_forced": "true", "reaction_coordinate_mode_index": str(stated_index)}))
    elif stated_index is not None and stated_index == designated and nmd is not False:
        mode_displacement_agrees = True
        rationale += (f"; mode_displacement_agrees from ARC's reaction_coordinate_mode_index = {stated_index} "
                      "(set only by a genuine normal mode displacement pass)")
    elif nmd is False and stated_index is None:
        mode_displacement_agrees = False
        rationale += "; mode_displacement_agrees from ARC ts_checks['NMD'] = False"
    return [_ts_evidence.imaginary_mode_record(
        verdict,
        source_calculation_key=freq_calc_key,
        imaginary_frequency_count=n_imag,
        frequency_cm1=value,
        mode_displacement_agrees=mode_displacement_agrees,
        rationale=rationale,
    )]


_W_IRC_PARTICIPANT_MAPPING_NOT_SENT = ArcWarning.IRC_PARTICIPANT_MAPPING_NOT_SENT.value


def _irc_participant_mappings(
    ts_record: Mapping[str, Any],
    *,
    reaction_record: Mapping[str, Any],
    species_index: Mapping[str, Mapping[str, Any]],
    ts_xyz_text: str | None,
    ts_label: Any,
    warnings: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, list[int]], dict[str, list[int]]] | None:
    """Translate ARC's ``irc_participant_mapping`` to TCKDB's two participant mappings.

    TCKDB's ``reactant_participant_mapping`` / ``product_participant_mapping``
    are ``{"reactant:N": [1-based transition-state atom indices]}`` and cover
    every transition-state atom exactly once on each side. ARC's mapping
    (output schema 1.3, ``irc_participant_mapping``) gives 0-based indices into
    each optimized IRC endpoint geometry, and those are transition-state atom
    indices only when ``atom_order_matches_ts`` is ``True``. Nothing is sent
    unless every claim TCKDB checks can be stated from what ARC says:

    * ``atom_order_matches_ts`` is ``True`` (otherwise the indices are not
      transition-state atoms);
    * ``sides_distinguishable`` is ``True`` (otherwise which endpoint is the
      reactants is only a convention ARC states it made);
    * each side's participants are exactly the participant slots of the
      reaction the upload declares, matched by label and occurrence, never by
      ARC's ``position``;
    * each side's indices partition the transition-state atoms, and each
      participant's atoms have the element counts of its species.

    Both sides are sent or neither (TCKDB refuses one side). ARC states the
    mapping only when the IRC verdict was established by graph isomorphism, so
    an absent mapping (``null``, or a pre-1.3 document) sends nothing and is not
    reported.
    """
    mapping = ts_record.get("irc_participant_mapping")
    if not isinstance(mapping, Mapping):
        return None

    def refuse(reason: str, **context: Any) -> None:
        message = (
            f"ARC states an IRC participant mapping for {ts_label!r} but it cannot be "
            f"sent as TCKDB participant mappings: {reason}. Neither side was sent."
        )
        logger.warning("TCKDB %s: %s", _W_IRC_PARTICIPANT_MAPPING_NOT_SENT, message)
        if warnings is not None:
            warnings.append(_self_check(
                _W_IRC_PARTICIPANT_MAPPING_NOT_SENT, message, "transition_state.validation_evidence",
                {"action": "irc_participant_mapping_omitted", **({key: str(value) for key, value in context.items()})}))
        return None

    if mapping.get("atom_order_matches_ts") is not True:
        return refuse(
            "atom_order_matches_ts is not true, so the endpoint atom indices are not "
            "transition-state atom indices",
            atom_order_matches_ts=mapping.get("atom_order_matches_ts"))
    if mapping.get("sides_distinguishable") is not True:
        return refuse(
            "the reactants and products are graph-isomorphic, so which IRC endpoint is the "
            "reactants is a convention, not something ARC established",
            sides_distinguishable=mapping.get("sides_distinguishable"))
    ts_symbols = _xyz_element_symbols(ts_xyz_text)
    if ts_symbols is None:
        return refuse("the transition-state geometry has no readable atoms")

    sides: list[dict[str, list[int]]] = []
    for side_name, prefix, labels_key in (
            ("reactants", "reactant", "reactant_labels"),
            ("products", "product", "product_labels")):
        side = mapping.get(side_name)
        participants = side.get("participants") if isinstance(side, Mapping) else None
        if not isinstance(participants, list) or not participants:
            return refuse(f"the {side_name} side states no participants")
        slots = _participant_slots(reaction_record.get(labels_key) or [])
        by_slot: dict[tuple[str, int], Mapping[str, Any]] = {}
        for participant in participants:
            if not isinstance(participant, Mapping):
                return refuse(f"a {side_name} participant is not a record")
            key = (str(participant.get("label")), participant.get("occurrence"))
            if key in by_slot:
                return refuse(f"{side_name} participant {key[0]!r} occurrence {key[1]} is listed twice")
            by_slot[key] = participant
        if set(by_slot) != set(slots):
            return refuse(
                f"the {side_name} participants (label, occurrence) "
                f"{sorted(map(str, by_slot))} are not the participants of the uploaded reaction "
                f"{sorted(map(str, slots))}")
        result: dict[str, list[int]] = {}
        claimed: list[int] = []
        for index, slot in enumerate(slots, start=1):
            atoms = by_slot[slot].get("atom_indices")
            if (not isinstance(atoms, list) or not atoms
                    or not all(isinstance(i, int) and not isinstance(i, bool)
                               and 0 <= i < len(ts_symbols) for i in atoms)):
                return refuse(f"{side_name} participant {slot[0]!r} has atom indices that are not "
                              f"0-based indices into the {len(ts_symbols)}-atom transition state")
            species_symbols = _xyz_element_symbols(
                (species_index.get(slot[0]) or {}).get("xyz"))
            if species_symbols is None or Counter(species_symbols) != Counter(
                    ts_symbols[i] for i in atoms):
                return refuse(f"the atoms assigned to {side_name} participant {slot[0]!r} are not "
                              "the elements of that species")
            result[f"{prefix}:{index}"] = sorted(i + 1 for i in atoms)
            claimed.extend(atoms)
        if sorted(claimed) != list(range(len(ts_symbols))):
            return refuse(f"the {side_name} participants do not cover every transition-state "
                          "atom exactly once")
        sides.append(result)
    return sides[0], sides[1]
