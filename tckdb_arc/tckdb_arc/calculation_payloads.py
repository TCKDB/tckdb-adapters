"""Calculation payload builders of the ARC adapter.

The frequency and single-point result payloads, the serialised constraints of a calculation, the
route-versus-level contradiction check, and the geometry anchoring of Hessians and point geometries.
"""

import math
from collections.abc import Mapping
from dataclasses import field
from typing import Any

from tckdb_arc import arc13
from tckdb_arc.warning_codes import ArcWarning
from tckdb_core.constraints import serialize_constraints
from tckdb_core import isotopes as _isotopes
from tckdb_core import rules as _rules
from tckdb_arc.self_check import _self_check
from tckdb_arc.geometry import _geometry_payload, _isotopic_substitutions, _normalize_xyz_text
from tckdb_arc.calculations import _CALC_KEY_IRC, _designate_reaction_coordinate_index
from tckdb_arc.thermo import _is_composite_atom_record
from tckdb_arc._logging import get_logger

logger = get_logger()


def _serialize_calc_constraints(source) -> list[dict]:
    """Translate a parser-shaped constraint list into the TCKDB payload shape.

    ``source`` is whatever ``arc/output.py`` attached to the record (a
    list of parser dicts) or what the caller passed explicitly. Empty /
    None / unrecognised input produces ``[]``. Wraps
    ``tckdb_core.constraints.serialize_constraints`` so per-calc parse
    failures never bubble up into payload generation.
    """
    if not source:
        return []
    if not isinstance(source, (list, tuple)):
        logger.warning("TCKDB constraints: expected list, got %s; emitting [].",
                       type(source).__name__)
        return []
    try:
        return serialize_constraints(source, log=logger)
    except Exception as exc:  # defensive — serializer logs internally too
        logger.warning("TCKDB constraints: serialization failed: %s; emitting [].",
                       exc)
        return []


_W_LEVEL_CONTRADICTED_BY_ROUTE = ArcWarning.LEVEL_CONTRADICTED_BY_ROUTE.value


def _route_contradiction(
    record: Mapping[str, Any], calc_type: str, ess_job_key: str, level: Mapping[str, Any],
) -> str | None:
    """Description of a clear contradiction between a 1.3 record's level and the route the job ran with.

    Gaussian only. The route parser (``route_level.route_method_basis``) reads the one
    ``method/basis`` token of a Gaussian ``#`` line. An ORCA keyword line carries slash tokens
    that are not a method/basis pair (auxiliary basis sets: ``aug-cc-pvtz/c``, ``def2/J``,
    ``def2-TZVP/C``), and ARC exports ORCA routes without the leading ``!``, so the text alone
    does not say which program wrote it. The program of the job is stated by the record
    (``ess_software[<job>]``, via ``arc13.software_job_key``); a job that is not stated to be
    Gaussian is never compared, so an ORCA route is not read as a Gaussian one and the level
    is not contradicted by it.
    """
    if not arc13.has_levels(record):
        return None
    ess_software = record.get("ess_software")
    ess_software = ess_software if isinstance(ess_software, Mapping) else {}
    if calc_type == _CALC_KEY_IRC:
        if ess_software.get("irc") != "gaussian":
            return None
        routes = record.get("irc_log_routes")
        for route in routes if isinstance(routes, list) else ():
            found = arc13.route_contradicts_level(route, level)
            if found:
                return found
        return None
    if calc_type != ess_job_key or calc_type not in arc13.ROUTE_FIELDS:
        return None
    software_key = arc13.software_job_key(record, calc_type)
    if software_key == "composite":
        return None
    if ess_software.get(software_key) != "gaussian":
        if not ess_software.get(software_key) and arc13.route_for_job(record, calc_type):
            logger.debug(
                "TCKDB route check: the %s route of %r is not compared with the level: ess_software "
                "states no program for job %r", calc_type, record.get("label"), software_key)
        return None
    return arc13.route_contradicts_level(arc13.route_for_job(record, calc_type), level)


_FREQ_FIELD_SPECS = (
    # (record_key, payload_key, coerce)
    ("freq_n_imag", "n_imag", int),
    ("imag_freq_cm1", "imag_freq_cm1", float),
    ("zpe_hartree", "zpe_hartree", float),
)


# TCKDB's noise floor for an imaginary mode when no protocol is recorded
# (tckdb_schemas.stationary_point.TAU_PROTOCOL_NOT_RECORDED_CM1): deliberately the
# finite-difference-from-gradients value, since assuming the better case would
# flag modes that are only noise. ARC states no tau, so this is used.
_TS_TAU_PROTOCOL_NOT_RECORDED_CM1 = _rules.TAU_PROTOCOL_NOT_RECORDED_CM1
_W_TS_REACTION_COORDINATE_NOT_DESIGNATED = ArcWarning.TS_REACTION_COORDINATE_NOT_DESIGNATED.value


class TSReactionCoordinateNotDesignated(ValueError):
    """A 1.3 TS whose reaction coordinate neither ARC nor tau can designate.

    ``warning`` is the structured record (code, message, field, context) of
    what the sidecar would carry; the message starts with its code so that a
    sweep, which only reports the exception text, names it too.
    """

    def __init__(self, *, label: str, n_imag: int, imaginary_cm1: list[float],
                 stated_index: Any, n_above_tau: int):
        reason = (
            "no imaginary mode is at or above tau" if n_above_tau == 0
            else f"{n_above_tau} imaginary modes are at or above tau")
        message = (
            f"[{_W_TS_REACTION_COORDINATE_NOT_DESIGNATED}] label={label!r} is a "
            f"transition state with {n_imag} imaginary modes {imaginary_cm1} cm-1; "
            f"ARC states no usable reaction_coordinate_mode_index "
            f"({stated_index!r}) and {reason} (tau = "
            f"{_TS_TAU_PROTOCOL_NOT_RECORDED_CM1:.0f} cm-1, TCKDB's value when no "
            f"protocol is recorded). TCKDB requires exactly one designated reaction "
            f"coordinate (transition_state_reaction_coordinate_not_designated) and "
            f"refuses to guess; refusing to deposit this record."
        )
        super().__init__(message)
        self.warning = _self_check(
            _W_TS_REACTION_COORDINATE_NOT_DESIGNATED, message, "transition_state.freq_result.reaction_coordinate_mode_index",
            {"action": "record_refused", "label": label, "imaginary_cm1": imaginary_cm1, "tau_cm1": _TS_TAU_PROTOCOL_NOT_RECORDED_CM1, "n_above_tau": n_above_tau})
        logger.warning("TCKDB %s: %s", _W_TS_REACTION_COORDINATE_NOT_DESIGNATED, message)


def _freq_result_payload(
    record: Mapping[str, Any], *, schema_1_3: bool = False,
) -> dict[str, Any] | None:
    """Build a FreqResultPayload-shaped dict from an output.yml record.

    ``schema_1_3`` (an output.yml 1.3 document): a TS record's
    ``freq_frequencies_cm1_ess_order`` lists every frequency of the job as the
    ESS printed it, imaginary modes (negative) in place, and
    ``reaction_coordinate_mode_index`` is the 1-based position in it of the mode
    ARC's normal-mode-displacement check validated as the reaction coordinate
    (``null`` unless that check genuinely passed). The ``modes`` are then that
    list, numbered by their position, and the designation is ARC's index, so
    neither the re-insertion of imaginary frequencies nor the (75, 10000) cm-1
    window of ``_designate_reaction_coordinate_index`` is needed (both remain
    for earlier documents). With no stated index, TCKDB's tau (50 cm-1 when no
    protocol is recorded) decides: exactly one imaginary mode at or above it is the
    reaction coordinate and the rest are ``unassigned``; otherwise the record is
    refused (``TSReactionCoordinateNotDesignated``).

    Returns ``None`` when no freq fields are populated. Returns ``None``
    and logs a warning when any present field cannot be coerced to the
    expected numeric type — the task spec mandates skipping the whole
    additional calculation rather than uploading a partial freq row.

    Raises ``ValueError`` on a genuine contradiction between ARC's own
    record and the stationary-point claim this freq result will be
    uploaded under. Callers deliberately do NOT catch it (unlike the
    ``ValueError`` a downstream calc builder can raise for e.g. a missing
    level of theory, which every call site wraps in a local
    try/except-and-skip): it is left to propagate out of the enclosing
    ``submit_*`` method, matching the documented "build failures raise;
    the caller wraps the per-record call in a try/except" contract those
    methods already advertise. That caller is ``sweep.py``, whose
    per-record ``except Exception`` counts the record ``failed`` and
    prints ``failed: <label> — <error>`` — a loud, attributable failure
    the operator cannot miss, unlike a ``logger.warning`` buried in the
    run log.

    TCKDB 0.22.0 cross-checks ``freq_n_imag`` against the stationary-
    point claim this record is uploaded under: species_entry_kind=
    'minimum' for every species/reactant/product path (``_species_entry_
    payload`` now refuses any record with ``is_ts=True`` before it can
    reach this function at all — see its own docstring — so 'minimum' is
    the only value that ever gets here), and "this is the
    transition_state block" for a TS. ``_species_entry_payload`` derives
    that claim from ARC's ``is_ts`` flag completely independently of the
    n_imag read here, so nothing guarantees they agree.

    No concrete path inside ARC's own pipeline is known to produce this
    contradiction. ``Scheduler.check_negative_freq`` (arc/scheduler.py)
    refuses to mark a non-TS species converged while any negative
    frequency remains, and refuses to mark a TS converged with zero. A
    species loaded from a pre-existing Arkane YAML
    (``species.yml_path``) looked like a candidate bypass —
    ``Scheduler.schedule_jobs`` sets ``self.output[label]['convergence']
    = True`` unconditionally for it ("Species is loaded from an Arkane
    YAML file (no need to execute any job)"), with no frequency sanity
    check at all — but it isn't one: ``ARCSpecies.from_yml_file``
    (arc/species/species.py) loads only ``final_xyz``, ``mol``,
    ``multiplicity``, ``charge``, and a handful of Arkane-conformer
    fields; it never sets ``spc.freqs``. ``_get_imaginary_freqs``
    (arc/output.py) reads only ``spc.freqs`` and ``spc.
    ts_guesses[chosen].imaginary_freqs``, neither of which the YAML load
    touches, so a YAML-loaded species reports ``freq_n_imag=None`` — the
    check below is skipped for it, not contradicted.

    These checks are therefore defensive validation for an ``output.yml``
    document this adapter did not produce — hand-written, third-party,
    or from a future/older ARC — which the adapter genuinely accepts:
    ``evidence.py::validate_output_schema`` checks only
    ``schema_version``, never the document shape, so nothing upstream
    would stop a record like that from reaching this function. If a
    concrete producer inside ARC's own pipeline is later found to trigger
    this, update this note with it; until then, treat these raises as
    precautionary rather than evidence of a live bug.

    Uploading the contradiction verbatim would 422 a transition state
    (``transition_state_no_imaginary_mode`` /
    ``transition_state_reaction_coordinate_not_designated``). For a
    minimum the reason is different: since TCKDB 0.142 a minimum whose
    imaginary modes all lie below ``tau`` is accepted, but the adapter
    does not copy that tau table, and ARC never marks such a minimum
    converged, so the path is unreachable on ARC output and the
    adapter refuses it (``n_imag_contradicts_minimum`` is the
    pre-0.142 name of the refusal).
    Quietly dropping just the offending field(s) is not a fix: it
    defeats TCKDB's own guard (``evaluate_transition_state_frequency``
    and ``evaluate_species_entry_frequency`` both report nothing when
    n_imag is absent — "absence is never contradiction") and deposits
    the record under a classification its own evidence disputes, with
    the disproving evidence quietly removed. Refusing the whole record
    is the honest alternative: nothing is deposited that asserts
    something ARC's own frequency evidence contradicts.
    """
    statmech = record.get("statmech") or {}
    raw_freqs = statmech.get("harmonic_frequencies_cm1")
    ess_order = record.get("freq_frequencies_cm1_ess_order") if (
        schema_1_3 and record.get("is_ts")) else None
    ess_order_used = isinstance(ess_order, list) and bool(ess_order)
    if ess_order_used:
        raw_freqs = ess_order
    has_modes_source = bool(raw_freqs)
    if not has_modes_source and all(
        record.get(rkey) is None for rkey, _, _ in _FREQ_FIELD_SPECS
    ):
        return None
    out: dict[str, Any] = {}
    for record_key, payload_key, coerce in _FREQ_FIELD_SPECS:
        value = record.get(record_key)
        if value is None:
            continue
        try:
            out[payload_key] = coerce(value)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "TCKDB freq additional calculation skipped for label=%s: "
                "malformed %s=%r (%s)",
                record.get("label"), record_key, value, exc,
            )
            return None

    label = record.get("label")
    is_ts = bool(record.get("is_ts"))
    n_imag = out.get("n_imag")

    if n_imag is not None and not is_ts and n_imag > 0:
        raise ValueError(
            f"TCKDB freq: label={label!r} is_ts=False (species_entry_kind="
            f"'minimum') but ARC reports freq_n_imag={n_imag}. ARC never "
            f"marks a minimum converged with an imaginary mode, so this "
            f"record is not current ARC output; the adapter refuses it "
            f"rather than silently drop the disproving evidence "
            f"(n_imag_contradicts_minimum; the adapter does not copy "
            f"TCKDB's tau table of tolerable soft modes). "
            f"No path inside ARC's own pipeline is known to produce this; "
            f"likely cause is a hand-written, third-party, or non-current-"
            f"ARC output.yml whose is_ts and frequency evidence disagree. "
            f"Re-optimise on a tighter integration grid, fix a stale "
            f"is_ts, or declare the entry as what it actually is."
        )
    if n_imag is not None and is_ts and n_imag <= 0:
        raise ValueError(
            f"TCKDB freq: label={label!r} is_ts=True (transition state) "
            f"but ARC reports freq_n_imag={n_imag} — TCKDB 0.22.0 refuses "
            f"a transition state with no imaginary mode "
            f"(transition_state_no_imaginary_mode). Refusing to deposit "
            f"this record rather than silently drop the disproving "
            f"evidence. No path inside ARC's own pipeline is known to "
            f"produce this; likely cause is a hand-written, third-party, "
            f"or non-current-ARC output.yml whose is_ts and frequency "
            f"evidence disagree. Re-run the saddle-point search, or "
            f"deposit the structure as a species entry."
        )

    modes: list[dict[str, Any]] = []
    if has_modes_source:
        try:
            for f in raw_freqs:
                freq = float(f)
                modes.append({"frequency_cm1": freq, "is_imaginary": freq < 0})
        except (TypeError, ValueError) as exc:
            logger.warning(
                "TCKDB freq additional calculation skipped for label=%s: "
                "malformed harmonic_frequencies_cm1=%r (%s)",
                label, raw_freqs, exc,
            )
            return None

    # ARC's statmech ``harmonic_frequencies_cm1`` lists only the REAL
    # vibrational modes for a TS (arc/output.py excludes every negative
    # frequency — not just the reaction coordinate — whenever ``spc.
    # is_ts``, and may be entirely absent even though imaginary
    # frequencies were recorded — see the ``ts_guesses`` fallback in
    # ``arc/output.py::_get_imaginary_freqs``). That is the common case,
    # but not the only one: a record can also already carry every
    # imaginary value inline in ``harmonic_frequencies_cm1`` (e.g. a
    # hand-written/foreign output.yml, or a producer that doesn't follow
    # ARC's REAL-modes-only convention). Re-insert the imaginary mode(s)
    # from whatever source is available only when none are present yet —
    # independent of whether ``has_modes_source`` — so ``modes`` is
    # internally consistent with ``n_imag``: the TCKDB FreqResultPayload
    # validator requires count(is_imaginary) == n_imag whenever both are
    # present.
    if n_imag and not any(m["is_imaginary"] for m in modes) and not ess_order_used:
        all_imaginary = record.get("imaginary_frequencies_cm1")
        imag = record.get("imag_freq_cm1")
        if isinstance(all_imaginary, (list, tuple)) and len(all_imaginary) == n_imag:
            try:
                imaginary_values = [float(f) for f in all_imaginary]
            except (TypeError, ValueError):
                imaginary_values = None
            if imaginary_values is not None:
                imaginary_modes = [
                    {"frequency_cm1": -abs(v), "is_imaginary": True}
                    for v in imaginary_values
                ]
                modes = imaginary_modes + modes
        elif imag is not None and n_imag == 1:
            # Older/malformed records without the plural field: fall back
            # to reinserting only the one scalar ARC reports. Only
            # reconciles n_imag == 1 — no designation is required there.
            try:
                modes = [{
                    "frequency_cm1": -abs(float(imag)),
                    "is_imaginary": True,
                }] + modes
            except (TypeError, ValueError):
                pass
        elif n_imag > 1:
            # No plural imaginary-frequency list, or its length disagrees
            # with n_imag: there is no honest way to designate a unique
            # reaction coordinate. Refuse rather than deposit an
            # undesignated multi-imaginary TS TCKDB would 422 anyway.
            raise ValueError(
                f"TCKDB freq: label={label!r} is a transition state with "
                f"freq_n_imag={n_imag} but ARC's "
                f"imaginary_frequencies_cm1={all_imaginary!r} does not "
                f"give exactly one value per imaginary mode, so the "
                f"reaction coordinate cannot be designated (TCKDB "
                f"0.22.0: transition_state_reaction_coordinate_not_"
                f"designated). Refusing to deposit this record rather "
                f"than guess."
            )

    # Designation is a separate concern from reinsertion above, and must
    # run whenever n_imag > 1 regardless of WHERE the imaginary mode(s) in
    # ``modes`` came from: reinserted just now, or already present in
    # ``harmonic_frequencies_cm1`` before this function ever ran.
    # Gating designation on "reinsertion happened" left a hole — a record
    # whose harmonic list already included every imaginary value (see the
    # comment above) skipped this block entirely and reached TCKDB with
    # ``modes`` populated but no ``reaction_coordinate_mode_index``,
    # tripping the exact ``transition_state_reaction_coordinate_not_
    # designated`` finding this function exists to prevent.
    reaction_coordinate_mode_index: int | None = None
    if ess_order_used:
        imaginary_entries = [
            (i, m) for i, m in enumerate(modes, start=1) if m["is_imaginary"]
        ]
        stated_index = record.get("reaction_coordinate_mode_index")
        index_is_usable = (
            isinstance(stated_index, int) and not isinstance(stated_index, bool)
            and 1 <= stated_index <= len(modes) and modes[stated_index - 1]["is_imaginary"]
        )
        if record.get("nmd_forced") is True:
            # ``nmd_forced`` (ARC #1059 ebc88ec8): the normal mode displacement check failed and
            # ``skip_nmd`` forced the pass. ARC states the index only for a genuine pass, so an index
            # beside ``nmd_forced: true`` is a contradiction, and the index is not a designation by
            # the check. TCKDB's tau rule below designates instead.
            if stated_index is not None:
                logger.warning(
                    "TCKDB freq: %r states reaction_coordinate_mode_index=%r and nmd_forced=true; "
                    "the index is not used as the NMD designation.", label, stated_index)
            index_is_usable = False
        if index_is_usable:
            reaction_coordinate_mode_index = stated_index
            # Every other imaginary mode is "extra" once one is designated
            # (ADR 0012); ARC does not say what it is, so it is declared
            # ``unassigned`` (see the pre-1.3 branch below for why).
            for i, m in imaginary_entries:
                if i != stated_index:
                    m["imaginary_disposition"] = "unassigned"
        elif n_imag and n_imag > 1:
            # ARC states no index (its normal mode displacement check did not
            # genuinely pass, e.g. a ``skip_nmd`` run, or no frequency matched).
            # TCKDB's own noise floor decides instead: with exactly one
            # imaginary mode at or above tau and every other below it, the
            # others are numerical noise and the one real mode is the reaction
            # coordinate. Two or more at or above tau is a genuine higher-order
            # saddle TCKDB blocks without a designation: refused.
            tau_index, n_above_tau = _rules.designate_by_tau(
                [(i, m["frequency_cm1"]) for i, m in imaginary_entries], n_imag)
            if tau_index is not None:
                reaction_coordinate_mode_index = tau_index
                for i, m in imaginary_entries:
                    if i != tau_index:
                        m["imaginary_disposition"] = "unassigned"
            else:
                values = [m["frequency_cm1"] for _, m in imaginary_entries]
                raise TSReactionCoordinateNotDesignated(
                    label=str(label), n_imag=n_imag, imaginary_cm1=values,
                    stated_index=stated_index, n_above_tau=n_above_tau,
                )
    elif n_imag and n_imag > 1:
        imaginary_entries = [
            (i, m) for i, m in enumerate(modes, start=1) if m["is_imaginary"]
        ]
        if len(imaginary_entries) == n_imag:
            # ADR 0012 requires exactly one designated reaction coordinate
            # whenever n_imag > 1. See
            # ``_designate_reaction_coordinate_index`` for why this is
            # ARC's own (75, 10000) cm-1 window plus largest-magnitude
            # tie-break among in-window candidates, not "most negative
            # wins" over every mode.
            designated_values = [m["frequency_cm1"] for _, m in imaginary_entries]
            designated_position = _designate_reaction_coordinate_index(designated_values)
            if designated_position is None:
                raise ValueError(
                    f"TCKDB freq: label={label!r} is a transition state "
                    f"with {n_imag} imaginary modes {designated_values} "
                    f"cm-1, and no single one is the unique major TS mode "
                    f"in ARC's own (75, 10000) cm-1 window "
                    f"(arc/checks/ts.py::"
                    f"check_imaginary_frequencies) — zero candidates "
                    f"qualify, or more than one ties for the largest "
                    f"magnitude. TCKDB 0.22.0 requires exactly one "
                    f"designated reaction coordinate "
                    f"(transition_state_reaction_coordinate_not_"
                    f"designated) and refuses to guess; refusing to "
                    f"deposit this record rather than pick one "
                    f"arbitrarily."
                )
            reaction_coordinate_mode_index = imaginary_entries[designated_position - 1][0]
            # Every OTHER imaginary mode is "extra" once one is designated
            # (ADR 0012), and stationary_point.py's ambiguity check blocks
            # whenever an undeclared extra is at least as stiff as the
            # designated one — which the excluded, out-of-window mode(s)
            # that motivated this window criterion often are (that is the
            # whole point: an SCF/parse artifact can be *more* negative
            # than the real reaction coordinate). Declare them
            # ``unassigned`` — ImaginaryModeDisposition's honest "I do not
            # know what this mode is, but it is not the reaction
            # coordinate" — rather than leave them undeclared and let a
            # stiffer artifact, or a smaller distinct-magnitude in-window
            # sibling, block the upload. ``unassigned`` still keeps ADR
            # 0012's structural flag (via the below-designation-magnitude/
            # tau warning path); it only lifts the hard block.
            for i, m in imaginary_entries:
                if i != reaction_coordinate_mode_index:
                    m["imaginary_disposition"] = "unassigned"
        # else: the imaginary-mode count found in ``modes`` doesn't match
        # n_imag (malformed/insufficient data). No honest designation is
        # possible; fall through to the reconciliation check below, which
        # omits ``modes`` entirely with a warning rather than guess.

    imag_count = sum(1 for m in modes if m["is_imaginary"])
    if n_imag == 0 and imag_count > 0:
        # is_ts=True with n_imag<=0 already raised above, so reaching
        # here with n_imag==0 means is_ts=False (species_entry_kind=
        # 'minimum'). ARC's own harmonic_frequencies_cm1 shows an
        # imaginary mode that freq_n_imag=0 says doesn't exist — the same
        # n_imag_contradicts_minimum contradiction guarded above, just
        # surfaced through the frequency list instead of the freq_n_imag
        # field itself. Quietly stripping the negative value(s) and
        # depositing freq_n_imag=0 anyway would defeat TCKDB's guard with
        # the disproving evidence removed — the same failure mode this
        # function's docstring rejects for every other contradiction.
        raise ValueError(
            f"TCKDB freq: label={label!r} is_ts=False (species_entry_kind="
            f"'minimum') reports freq_n_imag=0, but ARC's own "
            f"statmech.harmonic_frequencies_cm1 contains {imag_count} "
            f"negative value(s) "
            f"{[m['frequency_cm1'] for m in modes if m['is_imaginary']]} — "
            f"ARC never marks a minimum converged with an imaginary mode, "
            f"so this record is not current ARC output. Refusing to deposit "
            f"this record rather than silently drop the disproving evidence "
            f"(n_imag_contradicts_minimum; the adapter does not copy "
            f"TCKDB's tau table of tolerable soft modes)."
        )
    if n_imag is not None and imag_count != n_imag:
        # Cannot reconcile modes with n_imag (e.g. a higher-order saddle
        # with a single stored imag_freq_cm1, or malformed statmech data).
        # Emit the scalar n_imag/imag_freq_cm1 without ``modes`` rather
        # than a payload the backend validator would reject.
        logger.warning(
            "TCKDB freq modes omitted for label=%s: could not reconcile "
            "n_imag=%s with %d imaginary mode(s) from statmech.",
            label, n_imag, imag_count,
        )
    elif modes:
        for i, m in enumerate(modes, start=1):
            m["mode_index"] = i
        out["modes"] = modes
        if reaction_coordinate_mode_index is not None:
            out["reaction_coordinate_mode_index"] = reaction_coordinate_mode_index
            # Keep the scalar in agreement with the designation: without
            # this, ``imag_freq_cm1`` can carry ARC's raw min() (which may
            # be an out-of-window artifact, e.g. -12000.0) while
            # ``reaction_coordinate_mode_index`` names a different mode
            # (e.g. -1320.5) — two different "the reaction coordinate"
            # answers in the same payload. TCKDB does not cross-check
            # them, but ``imag_freq_cm1`` is documented as "Value of the
            # imaginary frequency" and is what a tunneling consumer reads.
            designated_mode = next(
                m for m in modes if m["mode_index"] == reaction_coordinate_mode_index
            )
            out["imag_freq_cm1"] = designated_mode["frequency_cm1"]
    return out or None


def _sp_result_payload(record: Mapping[str, Any]) -> dict[str, Any] | None:
    # ``sp_energy_hartree`` is the only key ARC's output.yml writes for a
    # species' single-point energy; the TCKDB-side name
    # ``electronic_energy_hartree`` is not an ARC key and is not read.
    record_key = "sp_energy_hartree"
    energy = record.get(record_key)
    if energy is None:
        return None
    try:
        return {"electronic_energy_hartree": float(energy)}
    except (TypeError, ValueError) as exc:
        logger.warning(
            "TCKDB sp additional calculation skipped for label=%s: "
            "malformed %s=%r (%s)",
            record.get("label"), record_key, energy, exc,
        )
        return None


def _monatomic_sp_result_payload(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """``sp_result`` of a single atom's one job: ``sp_energy_hartree``, else ``opt_final_energy_hartree``.

    An atom has no optimisation job; ARC runs only its single point, whose log
    output.yml 1.3 also exports as the atom's ``opt_log`` (so ``sp_log == opt_log``).
    Output written before that, and some records after it, state the atom's energy
    only as ``opt_final_energy_hartree``, which ARC parses from that same log. It is
    the energy of the atom's single job whichever key carries it, so it is used when
    ``sp_energy_hartree`` is not stated; nothing else is read.

    Not for a composite-run atom (``composite_log`` or a composite level stated): its
    ``opt_final_energy_hartree`` is parsed from the composite log and may be an
    intermediate SCF energy rather than the composite energy, so only
    ``sp_energy_hartree`` is accepted and the atom is otherwise refused.
    """
    result = _sp_result_payload(record)
    if result is not None:
        return result
    if _is_composite_atom_record(record):
        return None
    fallback = record.get("opt_final_energy_hartree")
    if isinstance(fallback, bool) or not isinstance(fallback, (int, float)) or not math.isfinite(fallback):
        return None
    return {"electronic_energy_hartree": float(fallback)}


_W_GEOMETRY_ISOTOPES_NOT_STATED = ArcWarning.GEOMETRY_ISOTOPES_NOT_STATED.value


_W_CALCULATION_INPUT_GEOMETRY_NOT_DECLARED = ArcWarning.CALCULATION_INPUT_GEOMETRY_NOT_DECLARED.value


def _enforce_geometry_anchoring(payload: dict[str, Any], route: str, warnings: list[dict[str, Any]]) -> None:
    """Never send a payload whose calculations contradict their conformer's geometry (tckdb-schemas 0.121).

    Two rules, both from TCKDB #635:

    * keys: a calculation's ``conformer_key`` and ``geometry_key`` must name one conformer, a
      transition state's calculations carry neither a ``conformer_key`` nor a species
      geometry (:func:`tckdb_core.rules.geometry_anchor_refusals`, the shipped
      ``bundle_source_rules``). The adapter builds its keys from one conformer, so a finding here
      is a defect: the build is refused (``ValueError``) instead of posting what TCKDB refuses.
    * input geometries: a ``freq``/``sp`` that declares ``input_geometries`` declares its owner's
      geometry or is refused (``calculation_input_geometry_mismatch``). ARC runs both on the
      optimisation's output, which is the conformer's own ``xyz`` the adapter sends, so the two
      are the same text. A declaration that is not (:func:`tckdb_core.rules.undeclarable_input_geometries`)
      is left out, with a warning, and TCKDB takes the owner's geometry: ARC's re-printed log
      coordinates are never offered as the job's input.
    """
    refusals = _rules.geometry_anchor_refusals(payload, route)
    if refusals:
        raise ValueError(
            f"{route} payload would be refused by TCKDB: "
            + "; ".join(f"{code}: {detail}" for code, detail in refusals))
    for calc, field in _rules.undeclarable_input_geometries(payload, route):
        calc.pop("input_geometries", None)
        message = (
            f"{field} ({calc.get('type')}) declared an input geometry other than its conformer's own, "
            "which TCKDB refuses as calculation_input_geometry_mismatch; the declaration was left out "
            "and TCKDB takes the conformer's geometry. ARC runs freq and sp on the optimisation's "
            "output, so the conformer's own xyz is the only input geometry the adapter can state.")
        logger.warning("TCKDB %s: %s", _W_CALCULATION_INPUT_GEOMETRY_NOT_DECLARED, message)
        warnings.append(_self_check(
            _W_CALCULATION_INPUT_GEOMETRY_NOT_DECLARED, message, f"{field}.input_geometries",
            {"action": "input_geometries_omitted", "calculation_key": calc.get("key")}))


def _hessian_geometry(
    record: Mapping[str, Any], xyz_text: str, warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """The Hessian's ``geometry`` block, or ``None`` when it cannot be stated honestly (tckdb-schemas 0.94).

    TCKDB checks a Hessian's geometry against the isotopes of the subject its calculation is
    filed under (``calculation_geometry_isotope_mismatch``), because it reads the Hessian back
    with masses taken from that geometry. For a species with no substitutions the block is the
    frame's xyz as before. For an isotopically substituted record the frame (the freq job's own
    input orientation: the same atoms in the same order as the record's final geometry) is labelled
    with the record's ``xyz_isotopes``; a frame in another atom order, or one the stated list does
    not reconcile with, is not labelled by guesswork: the Hessian (which is optional) is omitted,
    logged and, when ``warnings`` is given, recorded as ``geometry_isotopes_not_stated``.
    """
    substitutions = _isotopic_substitutions(record)
    if not substitutions:
        return {"xyz_text": xyz_text}
    record_symbols = _isotopes._xyz_symbols(_normalize_xyz_text(record.get("xyz"), record.get("label")))
    geometry = None
    if record_symbols is not None and _isotopes._xyz_symbols(xyz_text) == record_symbols:
        geometry = _geometry_payload(record, xyz_text, record.get("xyz_isotopes"))
    if geometry is None:
        message = (
            f"the Hessian of the isotopically substituted {record.get('label')!r} was left out: its frame is "
            "not the record's own atoms in the record's order, so ARC's isotope list cannot label it.")
        logger.warning("TCKDB %s: %s", _W_GEOMETRY_ISOTOPES_NOT_STATED, message)
        if warnings is not None:
            warnings.append(_self_check(
                _W_GEOMETRY_ISOTOPES_NOT_STATED, message, "hessian.geometry",
                {"action": "hessian_omitted"}))
    return geometry


def _withhold_unlabelled_point_geometries(
    payload: dict[str, Any], route: str, warnings: list[dict[str, Any]],
) -> None:
    """Label, or else leave out, the point geometries of an isotopically substituted subject (0.94).

    TCKDB checks every ``irc_result`` / ``scan_result`` / ``path_search_result`` point geometry
    against the isotopes its calculation's subject declares (a species' SMILES; a transition
    state's reactants'), and a geometry with no ``isotopes`` is the unsubstituted molecule.
    Under a subject that declares a substitution:

    * a scan or IRC point is labelled like the Hessian is: with the isotopes of the structure the
      job started from (the owner's geometry, ARC's ``xyz_isotopes``) when its frame has that
      structure's element sequence in order and the calculation's program keeps atom order
      (:func:`tckdb_core.isotopes.label_point_geometries`); ARC's scan and IRC jobs run from the
      record's own geometry. The element sequence does not prove the permutation of identical
      elements, which is the stated caveat;
    * any other point (a path-search point, a frame of another element order, a program that may
      renumber atoms) has its geometry dropped (the point's energy and coordinate stay), with
      ``geometry_isotopes_not_stated``, rather than deposited as an unsubstituted structure.

    A subject without substitutions is untouched.
    """
    def smiles_of(entry: Any) -> Any:
        return (entry.get("species_entry") or {}).get("smiles") if isinstance(entry, Mapping) else None

    def first_geometry(conformers: Any) -> Any:
        return next((c.get("geometry") for c in conformers or () if isinstance(c, Mapping)), None)

    # (subject isotope multiset, [(path, calculation, geometry the calculation started from)])
    groups: list[tuple[dict[tuple[str, int], int], list[tuple[str, Any, Any]]]] = []
    if route == "computed_species":
        subject = _isotopes.subject_isotope_multiset(smiles_of(payload))
        calcs = []
        for ci, conf in enumerate(payload.get("conformers") or ()):
            reference = conf.get("geometry")
            calcs.append((f"conformers[{ci}].primary_calculation", conf.get("primary_calculation"), reference))
            calcs += [(f"conformers[{ci}].additional_calculations[{ai}]", a, reference)
                      for ai, a in enumerate(conf.get("additional_calculations") or ())]
        groups.append((subject, calcs))
    elif route == "computed_reaction":
        by_key = {sp.get("key"): sp for sp in payload.get("species") or ()}
        for si, sp in enumerate(payload.get("species") or ()):
            reference = first_geometry(sp.get("conformers"))
            groups.append((_isotopes.subject_isotope_multiset(smiles_of(sp)),
                           [(f"species[{si}].calculations[{ai}]", a, reference)
                            for ai, a in enumerate(sp.get("calculations") or ())]))
        ts = payload.get("transition_state")
        if isinstance(ts, Mapping):
            subject = _isotopes.subject_isotope_multiset(
                *(smiles_of(by_key.get(k)) for k in payload.get("reactant_keys") or ()))
            groups.append((subject, [(f"transition_state.calculations[{ai}]", a, ts.get("geometry"))
                                     for ai, a in enumerate(ts.get("calculations") or ())]))
    elif route == "transition_state":
        subject = _isotopes.subject_isotope_multiset(
            *(smiles_of(r) for r in (payload.get("reaction") or {}).get("reactants") or ()))
        reference = payload.get("geometry")
        calcs = [("primary_opt", payload.get("primary_opt"), reference)]
        calcs += [(f"additional_calculations[{ai}]", a, reference)
                  for ai, a in enumerate(payload.get("additional_calculations") or ())]
        groups.append((subject, calcs))
    for subject, calcs in groups:
        if not subject:
            continue
        for where, calc, reference in calcs:
            if not isinstance(calc, dict):
                continue
            software = (calc.get("software_release") or {}).get("name")
            for field, point in _isotopes.label_point_geometries(
                    calc, reference_geometry=reference, software=software):
                point.pop("geometry", None)
                message = (
                    f"{where}.{field} was left out: its subject declares isotope substitution "
                    f"{sorted(subject)} and the point cannot be labelled from the structure the job started "
                    "from (not a scan or IRC point of an order-preserving program with that structure's "
                    "element sequence), so TCKDB would read it as the unsubstituted molecule "
                    "(calculation_geometry_isotope_mismatch).")
                logger.warning("TCKDB %s: %s", _W_GEOMETRY_ISOTOPES_NOT_STATED, message)
                warnings.append(_self_check(
                    _W_GEOMETRY_ISOTOPES_NOT_STATED, message, f"{where}.{field}",
                    {"action": "geometry_omitted"}))
