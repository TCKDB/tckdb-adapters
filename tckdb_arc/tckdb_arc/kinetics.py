"""Kinetics helpers of the ARC adapter.

The determination key and the visibly stated interpretation of an Arkane-stamped fit
(``_kinetics_declarations``), the tunneling-model label, the calculation roles a kinetics block
cites, and the electron counts the one-electron exemption reads. The kinetics block itself is
built in ``kinetics_payloads.py``.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from tckdb_arc import arc13
from tckdb_arc.calculations import _CALC_KEY_FREQ, _CALC_KEY_IRC, _CALC_KEY_SP
from tckdb_arc.geometry import _species_element_symbols
from tckdb_arc.self_check import _self_check
from tckdb_arc.warning_codes import ArcWarning
from tckdb_core import composition as _composition


# Roles defined by TCKDB's ``KineticsCalculationRole`` enum; mirrored
# here so the adapter stays loud about unknown roles instead of
# bouncing through a 422 at upload time.
_KINETICS_ROLE_REACTANT_ENERGY = "reactant_energy"
_KINETICS_ROLE_PRODUCT_ENERGY = "product_energy"
_KINETICS_ROLE_TS_ENERGY = "ts_energy"
_KINETICS_ROLE_TS_FREQ = "freq"
_KINETICS_ROLE_TS_IRC = "irc"


def _coerce_optional_float(value: object) -> float | None:
    """Return value as float when possible, otherwise None."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# Preferred lookup order for extracting a canonical tunneling-model
# label out of a structured value. Mappings are checked first by key,
# then arbitrary objects by attribute. Keep the two lists aligned;
# ``class_name`` is the attribute analogue of the ``class`` mapping
# key (which would be a builtin shadow on attribute access).
_TUNNELING_MODEL_KEYS = ("method", "model", "type", "name", "class")
_TUNNELING_MODEL_ATTRS = ("method", "model", "type", "name", "class_name")


def _stringify_tunneling_model(value: object) -> str | None:
    """Return a concise, stable tunneling-model label for TCKDB.

    Tunneling metadata reaches the adapter from output.yml as a free-form
    value (string today, possibly a structured object tomorrow). TCKDB
    stores ``tunneling_model`` as a free-form ``str | None``, so the
    adapter's job is to turn whatever ARC supplies into a stable,
    human-meaningful label without ever crashing payload construction
    on a malformed entry.
    """
    if not value:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, Mapping):
        for key in _TUNNELING_MODEL_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    else:
        for attr in _TUNNELING_MODEL_ATTRS:
            candidate = getattr(value, attr, None)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = str(value).strip()
    return encoded or None


#: The source-scoped key prefix of the determination a fit of ARC's Arkane kinetics run is one
#: representation of (``KineticsDeterminationDeclaration.key``); see ``_kinetics_declarations``.
_KINETICS_DETERMINATION_KEY = "arkane_kinetics"

#: Substrings of a level's free-form ``args`` (an object, a string or null) that say a solvent was
#: requested: ``scrf`` (Gaussian), ``cpcm`` and ``smd`` (ORCA, Gaussian), ``pcm``, ``cosmo``, and
#: ``solvent`` and ``solvation`` (not the bare ``solv``, which matches Psi4's ``solver_maxiter``). The text is lowercased first. A false positive only withholds
#: ``phase``, the safe direction.
_SOLVATION_ARGS_TOKENS = ("scrf", "cpcm", "smd", "pcm", "cosmo", "solvent", "solvation")


def _level_states_solvation(level: Any) -> bool:
    """Whether an output.yml level dict states solvation.

    By ``solvent``, ``solvation_method`` or ``solvation_scheme_level``, or by a ``args`` whose
    text (dicts and lists stringified, lowercased) contains one of ``_SOLVATION_ARGS_TOKENS``.
    """
    if not isinstance(level, Mapping):
        return False
    if any(level.get(k) for k in ("solvent", "solvation_method", "solvation_scheme_level")):
        return True
    args = level.get("args")
    if args:
        text = json.dumps(args, default=str).lower()
        return any(token in text for token in _SOLVATION_ARGS_TOKENS)
    return False


def _any_level_states_solvation(node: Any) -> bool:
    """``_level_states_solvation`` for a mapping or any level dict nested in a list or mapping."""
    if isinstance(node, Mapping):
        return _level_states_solvation(node) or any(
            _any_level_states_solvation(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_any_level_states_solvation(v) for v in node)
    return False


def _participant_levels_state_solvent(calculations: Iterable[Mapping[str, Any]]) -> bool:
    """Whether any built calculation's level of theory states a ``solvent`` or ``solvent_model``."""
    for calc in calculations:
        level = calc.get("level_of_theory") if isinstance(calc, Mapping) else None
        if isinstance(level, Mapping) and (level.get("solvent") or level.get("solvent_model")):
            return True
    return False


def _header_levels(output_doc: Mapping[str, Any] | None) -> dict[str, Any]:
    """output.yml's header-level dicts (``*_level`` keys and ``composite_method``), verbatim."""
    if not isinstance(output_doc, Mapping):
        return {}
    return {k: v for k, v in output_doc.items()
            if isinstance(v, Mapping) and (k == "composite_method" or str(k).endswith("_level"))}


def _source_levels_state_solvation(
    output_doc: Mapping[str, Any] | None, source_records: Iterable[tuple[Any, Any]],
) -> bool:
    """Whether output.yml states solvation for the header levels or any participant's own ``levels``.

    Read from the source, not from the calculations the adapter built: a calculation skipped
    for a missing program still had the level ARC stated for it. Read: the header levels, every
    level dict inside ``adaptive_levels`` (a list of ``{atom_range, levels: {job types: level}}``,
    which also covers pre-1.3 documents with no record ``levels``) and each participant's
    ``levels``. A level states solvation as ``_level_states_solvation`` defines it.
    """
    if any(_level_states_solvation(v) for v in _header_levels(output_doc).values()):
        return True
    if isinstance(output_doc, Mapping) and _any_level_states_solvation(output_doc.get("adaptive_levels")):
        return True
    for _, record in source_records:
        levels = arc13.levels_of(record)
        if levels is not None and any(_level_states_solvation(v) for v in levels.values()):
            return True
    return False


def _kinetics_determination_key(
    *, project: Any, ts_label: Any, kinetics_record: Mapping[str, Any],
    kinetics_block: Mapping[str, Any], output_doc: Mapping[str, Any] | None = None,
    source_records: Iterable[tuple[Any, Any]] = (),
) -> str:
    """``arkane_kinetics:<project>:<ts_label>:<digest>``, scoped to the computation.

    TCKDB identifies a determination by reaction entry, direction, target, source and key, and
    fits of one determination are not independent support for each other, so two runs (a rerun
    under the same project name and ARC release) must not share a key, and neither may two
    different fits of the same energies. The digest (12 hex of SHA-256) covers output.yml
    values only, not the adapter's own projections of them, so a later change to the adapter's
    mapping, or its bundle-local calculation keys, does not give one ARC run a new key: the fit (``tunneling``, ``A``, ``A_units``,
    ``n``, ``Ea``, ``Ea_units``, ``T0_k``, ``Tmin_k``, ``Tmax_k``, ``n_data_points``, whichever
    the record states), the header-level dicts, and each participant's label, ``levels`` object and stated energies
    (``sp_energy_hartree``, ``opt_final_energy_hartree``). It is deterministic for one bundle.
    A key over 128 characters is replaced by the prefix and a digest of all its parts.
    """
    fit = {k: kinetics_record.get(k) for k in _KINETICS_FIT_KEYS if kinetics_record.get(k) is not None}
    participants = [
        {"label": str(label),
         "levels": arc13.levels_of(record),
         "sp_energy_hartree": record.get("sp_energy_hartree"),
         "opt_final_energy_hartree": record.get("opt_final_energy_hartree")}
        for label, record in source_records if isinstance(record, Mapping)]
    digest = hashlib.sha256(json.dumps(
        {"fit": fit, "participants": participants,
         "header_levels": _header_levels(output_doc)},
        sort_keys=True, default=str).encode()).hexdigest()[:12]
    parts = [str(project).strip() if isinstance(project, str) and project.strip() else None,
             str(ts_label) if ts_label else None]
    key = ":".join([_KINETICS_DETERMINATION_KEY, *(p for p in parts if p), digest])
    if len(key) > 128:
        key = f"{_KINETICS_DETERMINATION_KEY}:" + hashlib.sha256(key.encode()).hexdigest()[:16]
    return key


#: The kinetics record's fitted content, digested into the determination key.
_KINETICS_FIT_KEYS = ("tunneling", "A", "A_units", "n", "Ea", "Ea_units", "T0_k", "Tmin_k", "Tmax_k",
                      "n_data_points")


def _kinetics_declarations(
    kinetics_record: Mapping[str, Any],
    *,
    project: Any,
    has_workflow_tool: bool,
    ts_label: Any = None,
    kinetics_block: Mapping[str, Any] | None = None,
    calculations: Mapping[str, Mapping[str, Any]] | None = None,
    output_doc: Mapping[str, Any] | None = None,
    source_records: Iterable[tuple[Any, Any]] = (),
) -> dict[str, Any]:
    """The ``direction``, ``determination`` and ``applicability`` of a fit ARC's Arkane run produced.

    Only a fit whose ``tunneling`` ARC stamped is declared: output.yml states that key only for
    parameters fitted by the Arkane kinetics run (null for user-supplied, restored or
    non-Arkane kinetics). A fit without the stamp declares none.

    None of what is declared is a field of output.yml. The stamp is the only output.yml fact
    used, as the gate. What is sent is the adapter's interpretation of ARC's documented
    provenance, accepted for this producer: ARC's kinetics run is Arkane's
    ``compute_high_p_rate_coefficient`` job on a reaction written as ``reactants -> products``
    (``arc/statmech/arkane.py``), and Arkane's statistical mechanics is gas-phase.

    * ``applicability``: observable ``rate_coefficient``, coefficient basis
      ``elementary_coefficient`` (output.yml's ``T0_k`` row writes ``k = A (T/T0)**n
      exp(-Ea/RT)``), ``pressure_dependence: high_pressure_limit`` (the job computes the
      high-pressure-limit rate coefficient), and ``phase: gas`` only when no level states
      solvation: neither a built calculation's (``solvent``, ``solvent_model``) nor, read from
      output.yml, a header level's or a participant's own ``levels`` entry (``solvent``,
      ``solvation_method``, ``solvation_scheme_level``), so a calculation skipped for a missing
      program still withholds it (a solvated level says nothing of the phase here). Its ``claim_origin`` is ``depositor_interpretation``, which marks the
      pressure dependence and the phase as interpretation;
    * ``direction: forward``: the fit is of the reaction as written, never a reverse fit;
    * ``determination``: one complete determination of the whole reaction, keyed within the
      bundle's ``workflow_tool_release`` (TCKDB refuses a determination without a literature or
      workflow tool, so with no release none is sent); the key is scoped to the computation
      (``_kinetics_determination_key``).

    ``direction`` and ``determination`` have no origin field in the schema, so the caller marks
    the interpretation, determination included, on the block's ``note`` and with
    ``kinetics_declarations_interpreted``
    (``_note_kinetics_declarations_interpreted``).

    Not sent, because ARC states none of it: the reaction order, the method kind, the barrier
    basis, the zero-point, rotor, conformer and path treatments (no ``protocol``).
    """
    tunneling = kinetics_record.get("tunneling")
    if not isinstance(tunneling, str) or not tunneling.strip():
        return {}
    applicability: dict[str, Any] = {
        "version": 1,
        "observable": "rate_coefficient",
        "coefficient_basis": "elementary_coefficient",
        "claim_origin": "depositor_interpretation",
    }
    out: dict[str, Any] = {"applicability": applicability}
    out["direction"] = "forward"
    applicability["pressure_dependence"] = "high_pressure_limit"
    calcs = calculations or {}
    records = list(source_records)
    if not (_participant_levels_state_solvent(calcs.values())
            or _source_levels_state_solvation(output_doc, records)):
        applicability["phase"] = "gas"
    if has_workflow_tool:
        out["determination"] = {
            "key": _kinetics_determination_key(
                project=project, ts_label=ts_label, kinetics_record=kinetics_record,
                kinetics_block=kinetics_block or {}, output_doc=output_doc,
                source_records=records),
            "target_kind": "whole_reaction", "representation_role": "complete"}
    return out


_PRESSURE_DEPENDENCE_WORDS = {"high_pressure_limit": "high-pressure limit"}


def _kinetics_interpretation_sentence(declarations: Mapping[str, Any]) -> str | None:
    """The sentence naming what ``_kinetics_declarations`` sent as the adapter's interpretation.

    ``None`` when no direction was declared. Names the direction, the pressure dependence, the
    phase when sent and the determination when sent. The note and the warning both use it.
    """
    direction = declarations.get("direction")
    if direction is None:
        return None
    applicability = declarations.get("applicability") or {}
    pressure = applicability.get("pressure_dependence")
    facts = [f"direction ({direction})",
             f"pressure dependence ({_PRESSURE_DEPENDENCE_WORDS.get(pressure, pressure)})"]
    if applicability.get("phase") is not None:
        facts.append(f"phase ({applicability['phase']})")
    if declarations.get("determination") is not None:
        facts.append("the determination (one complete determination of the whole reaction)")
    sentence = (
        f"{', '.join(facts[:-1])} and {facts[-1]} are the adapter's interpretation of ARC's "
        "Arkane high-pressure-limit kinetics run; output.yml states none of them.")
    return sentence[0].upper() + sentence[1:]


def _note_kinetics_declarations_interpreted(
    kinetics_block: dict[str, Any],
    declarations: Mapping[str, Any],
    *,
    warnings: list[dict[str, Any]] | None,
    field: str,
) -> None:
    """Mark the declared direction, pressure dependence, phase and determination as interpretation.

    ``KineticsDirection`` and ``KineticsDeterminationDeclaration`` carry no origin field, so
    when ``_kinetics_declarations`` sent a direction this appends
    ``_kinetics_interpretation_sentence`` to the block's ``note`` (never altering what is already
    there; ``BundleKineticsIn.note`` has no length limit) and reports
    ``kinetics_declarations_interpreted`` on ``field`` with the same sentence. Nothing is done
    for a block that declared nothing.
    """
    sentence = _kinetics_interpretation_sentence(declarations)
    if sentence is None:
        return
    existing = kinetics_block.get("note")
    kinetics_block["note"] = f"{existing}\n{sentence}" if existing else sentence
    if warnings is not None:
        applicability = declarations.get("applicability") or {}
        context: dict[str, Any] = {
            "action": "kinetics_declarations_sent_as_interpretation",
            "direction": declarations["direction"],
            "pressure_dependence": applicability.get("pressure_dependence")}
        if applicability.get("phase") is not None:
            context["phase"] = applicability["phase"]
        key = (declarations.get("determination") or {}).get("key")
        if key:
            context["determination_key"] = key
        warnings.append(_self_check(
            ArcWarning.KINETICS_DECLARATIONS_INTERPRETED.value, sentence, field, context))


def _calculation_multiplicities(
    species_blocks: Sequence[Mapping[str, Any]], ts_block: Mapping[str, Any] | None,
) -> dict[str, int]:
    """``{calculation key: multiplicity of the participant that owns it}`` for a reaction bundle."""
    out: dict[str, int] = {}
    for block in species_blocks:
        multiplicity = (block.get("species_entry") or {}).get("multiplicity")
        if multiplicity is None:
            continue
        for calc in [block["conformers"][0]["calculation"], *block.get("calculations", ())]:
            if calc.get("key"):
                out[calc["key"]] = multiplicity
    if ts_block is not None and ts_block.get("multiplicity") is not None:
        for calc in ts_block.get("calculations") or ():
            if calc.get("key"):
                out[calc["key"]] = ts_block["multiplicity"]
    return out


def _participant_electron_count(record: Mapping[str, Any] | None) -> int | None:
    """A participant's electrons from its own record: its composition and its stated ``charge``.

    Atomic numbers (the vendored element table) of the atoms ``_species_element_symbols`` reads
    from the xyz, else the formula, minus the stated net charge. ``None`` when the composition or
    the charge is not stated; no producer flag is trusted.
    """
    if not isinstance(record, Mapping):
        return None
    return _composition.electron_count(_species_element_symbols(record), record.get("charge"))


def _calculation_electron_counts(
    species_blocks: Sequence[Mapping[str, Any]],
    ts_block: Mapping[str, Any] | None,
    *,
    key_by_label: Mapping[str, str],
    species_index: Mapping[str, Mapping[str, Any]],
    ts_record: Mapping[str, Any] | None,
) -> dict[str, int | None]:
    """``{calculation key: electrons of the participant that owns it}``; a participant with none known maps to ``None``."""
    labels_by_key = {key: label for label, key in key_by_label.items()}
    out: dict[str, int | None] = {}
    for block in species_blocks:
        count = _participant_electron_count(species_index.get(labels_by_key.get(block.get("key"))))
        for calc in [block["conformers"][0]["calculation"], *block.get("calculations", ())]:
            if calc.get("key"):
                out[calc["key"]] = count
    if ts_block is not None:
        count = _participant_electron_count(ts_record)
        for calc in ts_block.get("calculations") or ():
            if calc.get("key"):
                out[calc["key"]] = count
    return out


def _build_kinetics_source_calculations(
    *,
    reactant_keys: list[str],
    product_keys: list[str],
    actor_calc_keys: Mapping[str, Mapping[str, str]],
    ts_calc_keys: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Compose the kinetics ``source_calculations`` list explicitly.

    Producer policy: declare exactly the links we have evidence for.

    - Each reactant/product contributes its sp calc (if present) under
      ``reactant_energy``/``product_energy``.
    - The TS contributes ts_sp under ``ts_energy``.
    - The TS contributes ts_freq under ``freq`` (kinetics ``freq`` role
      is the *TS* frequency in v0, not reactant freq).
    - The TS contributes ts_irc under ``irc`` only when ARC produced
      and persisted an IRC calc.

    Anything missing is omitted — the spec is explicit that a missing
    source link is preferable to a fabricated one.
    """
    links: list[dict[str, Any]] = []
    # A species may fill several slots (H + H, or one species on both
    # sides); its sp is linked once per role because the schema refuses a
    # repeated (calculation_key, role) pair. Reactant and product roles
    # differ, so a species on both sides keeps both links.
    seen_links: set[tuple[str, str]] = set()
    for role, keys in (
        (_KINETICS_ROLE_REACTANT_ENERGY, reactant_keys),
        (_KINETICS_ROLE_PRODUCT_ENERGY, product_keys),
    ):
        for actor_key in keys:
            sp_key = actor_calc_keys.get(actor_key, {}).get(_CALC_KEY_SP)
            if sp_key and (sp_key, role) not in seen_links:
                seen_links.add((sp_key, role))
                links.append({"calculation_key": sp_key, "role": role})
    ts_sp = ts_calc_keys.get(_CALC_KEY_SP)
    if ts_sp:
        links.append({"calculation_key": ts_sp, "role": _KINETICS_ROLE_TS_ENERGY})
    ts_freq = ts_calc_keys.get(_CALC_KEY_FREQ)
    if ts_freq:
        links.append({"calculation_key": ts_freq, "role": _KINETICS_ROLE_TS_FREQ})
    ts_irc = ts_calc_keys.get(_CALC_KEY_IRC)
    if ts_irc:
        links.append({"calculation_key": ts_irc, "role": _KINETICS_ROLE_TS_IRC})
    return links
