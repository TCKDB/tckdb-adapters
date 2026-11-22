"""Kinetics payload builders of the ARC adapter.

The kinetics block of a reaction and the energy level its source calculations ran at.
"""

import math
from collections.abc import Mapping, Sequence
from typing import Any

from tckdb_schemas.utils import normalize_tunneling_model
from tckdb_arc.warning_codes import ArcWarning
from tckdb_core.level_rules import method_identity_key
from tckdb_core import rules as _rules
from tckdb_arc.self_check import _self_check
from tckdb_arc.levels import _thermo_energy_level
from tckdb_arc.kinetics import (
    _build_kinetics_source_calculations,
    _participant_electron_count,
    _stringify_tunneling_model,
)
from tckdb_arc._logging import get_logger

from tckdb_arc.core_aliases import arc_to_tckdb_a_units, arc_to_tckdb_ea_units
from tckdb_arc.level_payloads import _arc_level_to_tckdb_lot

logger = get_logger()


# ``BundleKineticsIn.t0_k``: 0 < t0_k <= 10000 K (tckdb-schemas 0.63).
_MAX_ARRHENIUS_T0_K = 10000.0


def _build_kinetics_block(
    *,
    kinetics_record: Mapping[str, Any],
    reactant_keys: list[str],
    product_keys: list[str],
    actor_calc_keys: Mapping[str, Mapping[str, str]],
    ts_calc_keys: Mapping[str, str],
    long_kinetic_description: str | None = None,
    schema_1_3: bool = False,
) -> dict[str, Any] | None:
    """Build a ``BundleKineticsIn``-shaped dict from ARC kinetics.

    ``schema_1_3`` (an output.yml 1.3 document) also reads the kinetics run's
    ``comment`` (Arkane's comment on the fitted expression, verbatim),
    ``ts_validation`` (ARC's summary for a rate computed from a TS that failed
    its IRC check) and ``atom_corrections_applied`` (the atom-energy switch of
    the Arkane kinetics run) into the kinetics ``note``: ``BundleKineticsIn`` has
    a free-text ``note`` and no structured field for any of the three.

    Mapping (ARC → TCKDB):
        A, T0_k, n  → a = A (unnormalised), t0_k = T0_k when it is not 1 K, n
                      (RMG uses (T/T0)**n; TCKDB's ``t0_k``, 0.63, carries the T0)
        A_units     → a_units (via :func:`arc_to_tckdb_a_units`)
        n           → n
        Ea          → reported_ea
        Ea_units    → reported_ea_units (via :func:`arc_to_tckdb_ea_units`)
        Tmin_k      → tmin_k
        Tmax_k      → tmax_k
        dn          → n_uncertainty
        dEa         → d_reported_ea (only when dEa_units == Ea_units;
                      the live schema has no d_reported_ea_units field,
                      so dEa is only safe to emit when its units agree
                      with Ea's)
        dA          → a_uncertainty (+ a_uncertainty_kind="multiplicative")
                      Arkane/ARC dA is a multiplicative factor f, with
                      the true value bracketed by [A/f, A*f]. TCKDB's
                      ``KineticsUncertaintyKind`` exposes that semantic
                      explicitly, so the producer preserves dA verbatim
                      rather than dropping or re-encoding it. dA < 1.0
                      is omitted (server rejects multiplicative factors
                      below 1).

    ``source_calculations`` is populated from each actor's emitted
    sp/ts_sp/ts_freq/ts_irc keys. Reactant/product freq calcs are
    deliberately *not* linked as ``role=freq`` — in v0 kinetics that
    role is reserved for the TS frequency.

    Returns ``None`` when neither A nor Ea is populated — TCKDB's
    ``BundleKineticsIn`` allows empty kinetics, but a totally empty
    record is just noise and easier to omit than to send.

    When ``A`` (or ``Ea``) is present but its unit string doesn't map to
    a TCKDB enum (missing or unrecognized), the magnitude is *never*
    deposited without its unit — but the failure is scoped to that one
    field, not to the whole bundle. ``a``/``a_units`` (or
    ``reported_ea``/``reported_ea_units``) are both omitted and a
    WARNING is logged; ``n``, the other of A/Ea, ``Tmin_k``/``Tmax_k``
    and the rest of the kinetics block are still built and returned.
    Raising here would abort ``_build_computed_reaction_payload``
    entirely (the call site is unguarded) and discard the reaction's
    species blocks, TS block, geometries, IRC and path-search data for
    the sake of one optional rate-coefficient field — a
    disproportionate response to an unresolvable unit string.
    """
    has_substantive_field = any(
        kinetics_record.get(k) is not None
        for k in ("A", "n", "Ea", "Tmin_k", "Tmax_k")
    )
    if not has_substantive_field:
        return None

    block: dict[str, Any] = {
        "reactant_keys": list(reactant_keys),
        "product_keys": list(product_keys),
        "model_kind": "modified_arrhenius",
    }

    # A rate coefficient's pre-exponential factor is meaningless without
    # its unit. ``arc_to_tckdb_a_units`` returns None both when A_units is
    # absent and when it's a string the map doesn't recognize; either way,
    # depositing ``a`` alone would ship a unitless number that reads as
    # correct and silently isn't. Note this is a routine, *valid* ARC
    # shape, not just a hand-written/third-party edge case: ARC's own
    # ``output.yml`` schema requires ``A_units`` to be present but allows
    # it to be ``null`` (``arc/schemas/output_yml_schema.json``:
    # ``"A_units": {"type": ["string", "null"]}``, in ``required``), and
    # ``arc/output.py`` deliberately emits ``null`` whenever ``A`` isn't a
    # ``(value, unit)`` tuple. So omit ``a``/``a_units`` and log a WARNING
    # rather than raise — a single unresolvable unit on an optional field
    # must not abort the whole reaction bundle (species, TS, geometries,
    # IRC, path-search) via the unguarded call site in
    # ``_build_computed_reaction_payload``. ``a_units`` has no coupling
    # validator on ``BundleKineticsIn`` (unlike ``reported_ea``/
    # ``reported_ea_units`` — see the Ea handling below), so omitting both
    # is guaranteed to still validate.
    a = kinetics_record.get("A")
    if a is not None:
        try:
            a_value = float(a)
        except (TypeError, ValueError) as exc:
            logger.warning("TCKDB kinetics: malformed A=%r (%s)", a, exc)
        else:
            a_units = arc_to_tckdb_a_units(kinetics_record.get("A_units"))
            if a_units is None:
                logger.warning(
                    "TCKDB kinetics: A=%r has no recognized TCKDB unit "
                    "(A_units=%r); omitting a/a_units rather than "
                    "depositing a pre-exponential factor with no unit. "
                    "Add the ARC string to _ARC_TO_TCKDB_A_UNITS if it is "
                    "a legitimate unit, or fix the producer. The rest of "
                    "the kinetics block is still built.",
                    a_value, kinetics_record.get("A_units"),
                )
            else:
                block["a"] = a_value
                block["a_units"] = a_units

    n = kinetics_record.get("n")
    if n is not None:
        try:
            block["n"] = float(n)
        except (TypeError, ValueError) as exc:
            logger.warning("TCKDB kinetics: malformed n=%r (%s)", n, exc)

    # RMG/Arkane fits k = A * (T/T0)**n * exp(-Ea/RT). TCKDB (tckdb-schemas
    # 0.63) states the same law: ``t0_k`` is "the reference temperature T0 of the
    # Arrhenius expression, in K, meaning k = A * (T / T0)**n * exp(-Ea / (R * T))",
    # it defaults to 1 K ("the plain A T^n form and what every record deposited
    # before this release meant") and "the server stores ``a`` as sent (it is A at
    # T0, not A rescaled)". So ARC's A is sent unnormalised beside ARC's own T0_k
    # (the adapter used to send A / T0**n and lose the T0 it was fitted with).
    # Older exports omitted T0 entirely and used the 1 K convention: their rates
    # come from Arkane's ``Arrhenius().fit_to_data(...)`` (RMG-Py
    # arkane/kinetics.py), called without T0, whose default is T0=1 K
    # (rmgpy/kinetics/arrhenius.pyx ``fit_to_data(..., double T0=1)``, stored as
    # ``self.T0 = (T0, "K")``; ``Arrhenius.__init__`` also defaults T0=(1.0, "K")).
    # Current output always writes ``T0_k``. A record with no T0_k key at all is
    # kept as 1 K by maintainer decision (adapter 0.6.0) for pre-contract output
    # only (no ``t0_k`` sent: 1 K is TCKDB's default); an explicit null or invalid
    # T0 is unknown, not evidence for 1 K, and so is one the contract refuses
    # (``0 < t0_k <= 10000``): ``a`` is then not sent, since A at an unstated
    # reference temperature would be read as A at 1 K.
    if "a" in block and "T0_k" in kinetics_record:
        raw_t0 = kinetics_record.get("T0_k")
        try:
            if isinstance(raw_t0, bool):
                raise ValueError("T0_k must be a number")
            t0 = float(raw_t0)
            if not math.isfinite(t0) or not 0 < t0 <= _MAX_ARRHENIUS_T0_K:
                raise ValueError(
                    f"T0_k must satisfy 0 < t0_k <= {_MAX_ARRHENIUS_T0_K:g} K (tckdb-schemas 0.63)")
            if t0 != 1.0:
                block["t0_k"] = t0
        except (TypeError, ValueError, OverflowError) as exc:
            logger.warning(
                "TCKDB kinetics: cannot state T0_k=%r (%s); omitting a/a_units.",
                raw_t0, exc,
            )
            block.pop("a", None)
            block.pop("a_units", None)

    # Same policy as A/A_units above, and for the same reason: an
    # activation energy with no unit is not a smaller version of the
    # right answer, it's an unrecoverable one. But same fix as above too
    # — omit the pair and warn rather than raise. TCKDB's own
    # ``validate_ea_pair`` (``BundleKineticsIn`` model validator) already
    # requires ``reported_ea``/``reported_ea_units`` to be both provided
    # or both omitted, so omitting both here is exactly what the schema
    # wants when only one half is resolvable, and it costs nothing extra
    # server-side (no failed round trip) since the adapter never sends
    # the broken half. ``ea_units`` is computed unconditionally because
    # the dEa fallback below reuses it.
    ea = kinetics_record.get("Ea")
    ea_units = arc_to_tckdb_ea_units(kinetics_record.get("Ea_units"))
    if ea is not None:
        try:
            ea_value = float(ea)
        except (TypeError, ValueError) as exc:
            logger.warning("TCKDB kinetics: malformed Ea=%r (%s)", ea, exc)
        else:
            if ea_units is None:
                logger.warning(
                    "TCKDB kinetics: Ea=%r has no recognized TCKDB unit "
                    "(Ea_units=%r); omitting reported_ea/reported_ea_units "
                    "rather than depositing an activation energy with no "
                    "unit. The rest of the kinetics block is still built.",
                    ea_value, kinetics_record.get("Ea_units"),
                )
            else:
                block["reported_ea"] = ea_value
                block["reported_ea_units"] = ea_units

    for arc_key, payload_key in (("Tmin_k", "tmin_k"), ("Tmax_k", "tmax_k")):
        v = kinetics_record.get(arc_key)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            logger.warning("TCKDB kinetics: malformed %s=%r (%s)", arc_key, v, exc)
            continue
        if f <= 0:
            logger.warning(
                "TCKDB kinetics: %s=%r is non-positive; field will be omitted "
                "(server requires gt 0).", arc_key, v,
            )
            continue
        block[payload_key] = f

    dn = kinetics_record.get("dn")
    if dn is not None:
        try:
            block["n_uncertainty"] = float(dn)
        except (TypeError, ValueError) as exc:
            logger.warning("TCKDB kinetics: malformed dn=%r (%s)", dn, exc)

    # dEa policy: ARC's dEa_units may differ from Ea_units. TCKDB has
    # only one Ea unit per kinetics row (no separate d_reported_ea_units
    # column today), so we can only safely emit d_reported_ea when the
    # producer reported it in the same units as Ea *and* that unit is
    # actually on the wire as ``reported_ea_units`` — otherwise the
    # number would be silently misinterpreted as the wrong unit, or (the
    # "both unitless" case — same defect C-4 exists to prevent, applied
    # here too) shipped with no unit anywhere in the record at all.
    # ``ea_units is not None`` alone is not enough: it only proves the
    # string *resolved*, not that ``reported_ea``/``reported_ea_units``
    # actually made it into ``block`` (Ea itself may be absent or
    # malformed even when Ea_units resolves).
    dea = kinetics_record.get("dEa")
    if dea is not None:
        dea_units_raw = kinetics_record.get("dEa_units")
        dea_units = arc_to_tckdb_ea_units(dea_units_raw) if dea_units_raw else ea_units
        if (
            dea_units is not None
            and dea_units == ea_units
            and "reported_ea_units" in block
        ):
            try:
                block["d_reported_ea"] = float(dea)
            except (TypeError, ValueError) as exc:
                logger.warning("TCKDB kinetics: malformed dEa=%r (%s)", dea, exc)
        else:
            logger.warning(
                "TCKDB kinetics: dEa=%r has no unambiguous unit to ship "
                "with (dEa_units=%r, Ea_units=%r, reported_ea_units on "
                "wire=%s); omitting d_reported_ea rather than depositing "
                "an uncertainty with no unit or a mismatched one.",
                dea, dea_units_raw, kinetics_record.get("Ea_units"),
                "reported_ea_units" in block,
            )

    # dA policy: Arkane/ARC dA is a multiplicative uncertainty factor
    # (true A in [A/f, A*f]). TCKDB's ``KineticsUncertaintyKind.multiplicative``
    # encodes exactly that, so we forward dA verbatim — never re-derive
    # it as A*(dA-1) or fold it into an additive band. Pair the value
    # with ``a_uncertainty_kind="multiplicative"`` (the schema requires
    # both fields to appear together, and rejects multiplicative factors
    # below 1.0).
    da = kinetics_record.get("dA")
    if da is not None:
        try:
            da_f = float(da)
        except (TypeError, ValueError) as exc:
            logger.warning("TCKDB kinetics: malformed dA=%r (%s)", da, exc)
        else:
            if da_f < 1.0:
                logger.debug(
                    "TCKDB kinetics: dA=%r is below 1.0; multiplicative factors "
                    "must be >= 1, so a_uncertainty will be omitted.",
                    da,
                )
            else:
                block["a_uncertainty"] = da_f
                block["a_uncertainty_kind"] = "multiplicative"

    # Tunneling method ARC applied to the fit (currently always Eckart;
    # surfaced through output.yml so the adapter doesn't have to hardcode
    # the constant alongside the producer template). Absent → omit the
    # field for backward compat with older output.yml versions. The
    # contract's ``TunnelingModel`` enum holds lowercase tokens; send the
    # token the server's own ``normalize_tunneling_model`` would store
    # (ARC's "Eckart" is ``eckart``, an unknown label ``other``), so the
    # payload matches the shipped JSON Schema, not just the lenient model.
    tunneling_label = normalize_tunneling_model(
        _stringify_tunneling_model(kinetics_record.get("tunneling"))
    )
    if tunneling_label:
        block["tunneling_model"] = tunneling_label

    # ``degeneracy`` is not sent: ARC's kinetics record has no degeneracy key
    # (arc/output.py::_rxn_to_dict), TCKDB reads a missing value as NULL, and
    # the producer must not default it to 1 or infer it from stoichiometry.
    #
    # Free-form note: the reaction-level ``long_kinetic_description`` is what
    # ARC writes (there is no ``kinetics.note``); it feeds TCKDB's
    # ``KineticsCreate.note`` slot.
    note_parts: list[str] = []
    if isinstance(long_kinetic_description, str) and long_kinetic_description.strip():
        note_parts.append(long_kinetic_description.strip())
    if schema_1_3:
        # The comment is Arkane's, verbatim, and for a rate whose TS failed the
        # IRC check it already ends in the ``ts_validation`` marker; the marker
        # is added only when the comment does not carry it. The switch states
        # what the run's E0s are (an absolute energy plus ZPE when off); a
        # barrier fitted within one run is unaffected either way.
        comment = kinetics_record.get("comment")
        comment = comment.strip() if isinstance(comment, str) else ""
        if comment:
            note_parts.append(comment)
        ts_validation = kinetics_record.get("ts_validation")
        ts_validation = ts_validation.strip() if isinstance(ts_validation, str) else ""
        if ts_validation and ts_validation not in comment:
            note_parts.append(ts_validation)
        switch = kinetics_record.get("atom_corrections_applied")
        if isinstance(switch, bool):
            note_parts.append(
                "Arkane kinetics run atom energy corrections: "
                + ("applied." if switch else "not applied (E0 values are absolute "
                   "electronic energy plus ZPE)."))
    if note_parts:
        block["note"] = "\n".join(note_parts)

    sources = _build_kinetics_source_calculations(
        reactant_keys=reactant_keys,
        product_keys=product_keys,
        actor_calc_keys=actor_calc_keys,
        ts_calc_keys=ts_calc_keys,
    )
    if sources:
        block["source_calculations"] = sources

    return block


def _kinetics_energy_level(
    *,
    output_doc: Mapping[str, Any],
    kinetics_block: Mapping[str, Any],
    reactant_keys: Sequence[str],
    product_keys: Sequence[str],
    key_by_label: Mapping[str, str],
    species_index: Mapping[str, Mapping[str, Any]],
    ts_record: Mapping[str, Any] | None,
    calculations: Mapping[str, Mapping[str, Any]],
    multiplicities: Mapping[str, int],
    electron_counts: Mapping[str, int | None] | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """The ``energy_level_of_theory`` to declare on a kinetics block, or ``None``.

    The rule is TCKDB's (``kinetics_energy_level_violation``, tckdb-schemas 0.161) and lives in
    :func:`tckdb_core.rules.kinetics_energy_level_assessment`: spin treatment is judged per
    participant (closed-shell links never constrain it, the open-shell links must share one
    reference, which the declaration names). The ARC part is stating the level each linked energy
    came from: for the reactants and products the level their record states
    (``_thermo_energy_level``: the sp level, or the composite method), and for the transition
    state its own. One participant ARC cannot attribute a level to (adaptive runs) means nothing
    is declared. ``multiplicities`` (calculation key to the owning participant's multiplicity)
    and each sp calculation's own ``spin_diagnostic.s_squared`` are the facts the spin judgement
    needs; an unrestricted singlet for which ARC states no ``<S**2>`` counts as open-shell, as it
    does in TCKDB. Open-shell links computed with different references declare nothing and say so
    (``kinetics_energy_level_spin_mixed``).

    A one-electron participant computed at Hartree-Fock in the same basis (ORCA cannot run a
    correlated method on one electron, and every correlation term vanishes) may differ from the
    declared correlated level in ``method`` alone; ``electron_counts`` (calculation key to the
    owner's electrons) and the stated levels' own counts gate it, and using it is reported
    (``one_electron_hf_substitution``).
    """
    links = kinetics_block.get("source_calculations") or ()
    roles = {link.get("role") for link in links}
    stated: list[Mapping[str, Any] | None] = []
    stated_counts: list[int | None] = []
    labels_by_key = {key: label for label, key in key_by_label.items()}
    for key in {*reactant_keys, *product_keys}:
        if not any(r in roles for r in ("reactant_energy", "product_energy")):
            break
        record = species_index.get(labels_by_key.get(key))
        level = _thermo_energy_level(output_doc, record) if record is not None else None
        stated.append(_arc_level_to_tckdb_lot(level) if isinstance(level, Mapping) else None)
        stated_counts.append(_participant_electron_count(record))
    if "ts_energy" in roles:
        level = _thermo_energy_level(output_doc, ts_record) if ts_record is not None else None
        stated.append(_arc_level_to_tckdb_lot(level) if isinstance(level, Mapping) else None)
        stated_counts.append(_participant_electron_count(ts_record))
    assessment = _rules.kinetics_energy_level_assessment(
        links, calculations=calculations, stated_levels=stated, multiplicities=multiplicities,
        electron_counts=electron_counts, stated_electron_counts=stated_counts)
    if assessment.one_electron_hf_calculations and assessment.level is not None and warnings is not None:
        context: dict[str, Any] = {
            "action": "one_electron_hf_energy_accepted",
            "calculation_keys": list(assessment.one_electron_hf_calculations)}
        message = (
            "The kinetics energy level is declared as the correlated level the other participants "
            "share; the energies of the one-electron participants (calculations "
            f"{', '.join(assessment.one_electron_hf_calculations)}) were computed at Hartree-Fock in "
            "the same basis, which equals the correlated energy for one electron because every "
            "correlation term vanishes.")
        if "f12" in method_identity_key(str(assessment.level.get("method") or "")):
            context["cabs_singles_correction"] = "absent"
            message += (" The HF energy omits the F12 CABS singles correction "
                        "(about 0.05 to 0.1 mEh for H).")
        warnings.append(_self_check(
            ArcWarning.ONE_ELECTRON_HF_SUBSTITUTION.value, message,
            "kinetics[0].energy_level_of_theory", context))
    if assessment.qualifier_differing_calculations and warnings is not None:
        warnings.append(_self_check(
            ArcWarning.KINETICS_ENERGY_LEVEL_QUALIFIERS_DIFFER.value,
            "No energy level of theory was declared on the kinetics block: the energy levels of "
            f"its links (calculations {', '.join(assessment.qualifier_differing_calculations)}) "
            "differ from the others' only in a typed level qualifier (pno_truncation, "
            "ri_approximation, relativistic_hamiltonian or core_treatment) that the deck of one "
            "job states and the other's does not. TCKDB resolves them as different levels and "
            "refuses a declared kinetics level that is not every link's "
            "(kinetics_energy_level_contradiction).",
            "kinetics[0].energy_level_of_theory",
            {"action": "energy_level_not_declared",
             "calculation_keys": list(assessment.qualifier_differing_calculations)}))
    if assessment.mixed_spin_calculations and warnings is not None:
        warnings.append(_self_check(
            ArcWarning.KINETICS_ENERGY_LEVEL_SPIN_MIXED.value,
            "No energy level of theory was declared on the kinetics block: its open-shell "
            "participants' energies were computed with different spin treatments "
            f"(calculations {', '.join(assessment.mixed_spin_calculations)}), and unrestricted and "
            "restricted-open references are different levels for an open-shell species, which TCKDB "
            "refuses (kinetics_energy_level_contradiction, open_shell_reference_mixed).",
            "kinetics[0].energy_level_of_theory",
            {"action": "energy_level_not_declared",
             "calculation_keys": list(assessment.mixed_spin_calculations)}))
    return assessment.level
