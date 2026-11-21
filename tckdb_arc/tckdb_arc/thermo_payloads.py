"""Thermo payload builders of the ARC adapter.

The applied energy corrections of a record, the thermo block with its enthalpy declaration and
reference pressure, and the withholding of energies a G4 loader shifted.
"""

import math
from collections.abc import Mapping
from typing import Any, Literal

from tckdb_schemas.enthalpy_reference import enthalpy_reference_error
from tckdb_schemas.fragments.refs import correction_table_method_stem
from tckdb_arc.warning_codes import ArcWarning
from tckdb_core.level_rules import method_identity_key
from tckdb_core.method_families import ONE_ELECTRON_EXEMPT_FAMILIES, method_family
from tckdb_arc.self_check import _self_check
from tckdb_arc.levels import _describe_level, _level_identity
from tckdb_arc.calculations import _CALC_KEY_FREQ, _CALC_KEY_OPT, _CALC_KEY_SP, _ROLE_COMPOSITE
from tckdb_arc.thermo import (
    _arkane_composite_key_halves,
    _arkane_key_software,
    _atom_params_from_reference_atom_energies,
    _ENERGY_WITHHELD_KEY,
    _enthalpy_magnitude_error,
    _g4_loader_energy_unreliable,
    _has_enthalpy_content,
    _level_field_unverifiable_error,
    _norm_level_token,
    _petersson_bac_omission_reason,
    _skipped_bonds_note,
    _strip_enthalpy_content,
    _unflagged_enthalpy_error,
    _W_ENTHALPY_ATOM_CORRECTIONS_LEVEL_MISMATCH,
)
from tckdb_arc.provenance import _RMG_DATABASE_SCHEME_KINDS
from tckdb_arc._logging import get_logger

from tckdb_arc.core_aliases import _build_nasa_block, _build_thermo_points
from tckdb_arc.level_payloads import _arc_level_to_tckdb_lot

logger = get_logger()


# ``AtomParamApplication`` (tckdb-schemas 0.62).
_ATOM_PARAM_APPLICATIONS = frozenset({"subtracted", "added"})


def _correction_records_from_record(
    record: Mapping[str, Any], *, legacy_1_0: bool = False, schema_1_3: bool = False,
) -> list[dict[str, Any]]:
    """Translate neutral ARC correction facts to the legacy adapter boundary.

    ``legacy_1_0`` (an output.yml 1.0 document) also reads an atom-energy
    ``parameter_table``, the 1.0 name of what 1.1 calls
    ``reference_atom_energies``. ``schema_1_3`` (an output.yml 1.3 document)
    reads a Petersson record's ``skipped_components`` and states the skipped
    bonds in the correction's note; the record's ``components`` are then the
    bonds applied, summing to ``total``."""
    legacy = record.get("applied_energy_corrections")
    if isinstance(legacy, list):
        return [dict(entry) for entry in legacy if isinstance(entry, Mapping)]
    neutral = record.get("energy_corrections")
    if not isinstance(neutral, list):
        return []
    out: list[dict[str, Any]] = []
    for correction in neutral:
        if not isinstance(correction, Mapping):
            continue
        correction_type = correction.get("correction_type")
        model = correction.get("model")
        total = correction.get("total")
        if not schema_1_3:
            # Before 1.3 ARC kept the record of a correction its thermo run did not
            # apply (1.3 drops it); the thermo block states which, so do not
            # deposit as applied what the same record says was switched off.
            thermo = record.get("thermo")
            switch = None
            if isinstance(thermo, Mapping):
                switch = thermo.get(
                    "atom_corrections_applied" if correction_type == "atom_energy"
                    else "bond_corrections_applied")
            if switch is False:
                continue
        if not isinstance(total, Mapping) or total.get("value") is None:
            continue
        if correction_type == "atom_energy":
            application_role = "aec_total"
            scheme_kind = "atom_energy"
        elif correction_type == "bond_additivity" and model in ("petersson", "melius"):
            application_role = "bac_total"
            scheme_kind = "bac_petersson" if model == "petersson" else "bac_melius"
        else:
            continue
        # Unit is read, never guessed. ARC's
        # producer (arc/scripts/get_species_corrections.py) always
        # hardcodes 'hartree' for atom-energy and 'kcal_mol' for both
        # bond-additivity models, so real ARC output never reaches the
        # missing-unit branch below — but ARC's own docs have stated the
        # wrong unit for this table before, and a hand-written/
        # third-party output.yml isn't schema-validated (evidence.py
        # only checks schema_version). ``value_unit`` is a *required*
        # field on TCKDB's AppliedEnergyCorrectionUploadPayload, so
        # there is no valid way to emit this record without one: omit
        # the whole correction rather than assert a unit ARC didn't
        # report.
        raw_unit = total.get("unit")
        if not raw_unit:
            logger.warning(
                "TCKDB energy correction: %s correction has no reported "
                "unit; omitting rather than guessing hartree/kcal_mol.",
                correction_type,
            )
            continue
        unit = str(raw_unit)
        scheme: dict[str, Any] = {
            "kind": scheme_kind,
            "name": scheme_kind,
            "level_of_theory": correction.get("level_of_theory"),
            "units": unit,
        }
        if correction_type == "atom_energy":
            # ARC writes Arkane's per-element reference atomic energies as
            # ``reference_atom_energies`` (never ``parameter_table``, which only
            # the Petersson record carries). TCKDB's ``scheme.atom_params[]``
            # for an ``atom_energy`` scheme is the element-keyed atomic-energy
            # table (backend ``EnergyCorrectionSchemeAtomParam``: "atom_hf,
            # atom_thermal, SOC, atom_energies"), which is what these are: the
            # bare atomic electronic energies Arkane subtracts. They are the
            # scheme's parameters, not per-atom corrections; the applied total
            # and its components are sent separately and never rebuilt from them.
            reference = correction.get("reference_atom_energies")
            if not isinstance(reference, Mapping):
                reference = {}
            atom_params, params_unit = _atom_params_from_reference_atom_energies(
                reference
            )
            legacy_table = correction.get("parameter_table") if legacy_1_0 else None
            if (
                not atom_params
                and isinstance(legacy_table, Mapping)
                and isinstance(legacy_table.get("values"), Mapping)
            ):
                # output.yml 1.0: the table carried no unit of its own; the
                # correction's unit is what the scheme's values are in.
                scheme["atom_params"] = [
                    {"element": str(key), "value": float(value)}
                    for key, value in sorted(legacy_table["values"].items())
                ]
            if atom_params:
                scheme["atom_params"] = atom_params
                applied_as = reference.get("applied_as")
                if applied_as in _ATOM_PARAM_APPLICATIONS:
                    # tckdb-schemas 0.62 ``EnergyCorrectionSchemeRef.atom_params_applied_as``:
                    # "How ``atom_params`` enter the corrected energy: ``subtracted`` or
                    # ``added``. Applies to every entry of ``atom_params`` and to nothing
                    # else... not inferred when omitted"; it requires ``atom_params``.
                    # ARC records it with the table, and Arkane's atom_energy tables are
                    # ``subtracted`` ("count * value is removed from the energy"), so it is
                    # sent exactly as ARC states it, only beside the atom_params it covers.
                    scheme["atom_params_applied_as"] = applied_as
                if applied_as == "subtracted":
                    # The atom_params are bare atomic energies. Arkane's full per-atom
                    # term (RMG-Py arkane/encorr/corr.py, get_atom_correction, steps 1-2)
                    # is -E_atom + (atom_hf - atom_thermal): the atom's gas-phase
                    # formation enthalpy (less its thermal correction, kcal/mol in
                    # RMG-database data.py) is added as well, and that addend is no
                    # atom_param, so ``atom_params_applied_as`` (which covers atom_params
                    # alone) does not state it. The subtraction itself is structured now,
                    # so the note keeps only the part it cannot carry.
                    scheme["note"] = (
                        "Arkane also adds each atom's gas-phase formation enthalpy less "
                        "its thermal correction (RMG-database atom_hf - atom_thermal); "
                        "those addends are not atom_params."
                    )
                # ``scheme.units`` is the unit the scheme's parameter values
                # are expressed in; the applied total keeps its own value_unit.
                scheme["units"] = params_unit
        else:
            table = correction.get("parameter_table")
            if (
                model == "petersson"
                and isinstance(table, Mapping)
                and isinstance(table.get("values"), Mapping)
            ):
                scheme["bond_params"] = [
                    {"bond_key": str(key), "value": float(value)}
                    for key, value in sorted(table["values"].items())
                ]
        converted: dict[str, Any] = {
            "application_role": application_role,
            "value": float(total["value"]),
            "value_unit": unit,
            "scheme": scheme,
            "components": correction.get("components") or [],
            # Arkane's database key the table came from; read by
            # ``_build_applied_energy_corrections`` for ``scheme.software``,
            # never sent itself.
            "matched_arkane_key": correction.get("matched_arkane_key"),
        }
        if schema_1_3 and scheme_kind == "bac_petersson":
            # Output 1.3: ``components`` are the bonds the BAC applied (they
            # sum to the total) and ``skipped_components`` the bonds without a
            # parameter. The skipped bonds belong to this species' total, not
            # to the scheme (a table's identity does not depend on the species
            # it was applied to), so they go in the correction's note.
            converted["components_are_applied_bonds"] = True
            skipped_note = _skipped_bonds_note(correction.get("skipped_components"))
            if skipped_note is not None:
                converted["note"] = skipped_note
        out.append(converted)
    return out


_APPLIED_CORRECTION_COMPONENT_FIELDS = (
    "component_kind", "key", "multiplicity", "parameter_value", "contribution_value",
)


_W_CORRECTION_TABLE_METHOD_SPLIT = ArcWarning.CORRECTION_TABLE_METHOD_SPLIT.value


def _scheme_level_of_theory(scheme: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project ARC's per-species scheme.level_of_theory dict onto TCKDB's
    ``LevelOfTheoryRef`` shape. The scheme dict comes from the same
    ``_level_to_dict(arkane_level_of_theory)`` producer as opt/freq/sp
    levels, so the same field-name translation applies."""
    return _arc_level_to_tckdb_lot(scheme.get("level_of_theory"), split_table=False)


#: Scheme kinds Arkane keys on ``CompositeLevelOfTheory(freq=..., energy=...)``.
#: Atom-energy schemes are keyed on the energy level alone and never carry a frequency level.
_FREQ_KEYED_SCHEME_KINDS = frozenset({"bac_petersson", "bac_melius"})


_W_BAC_FREQUENCY_LEVEL_CONFLICT = ArcWarning.BAC_FREQUENCY_LEVEL_CONFLICT.value


def _bac_key_frequency_level(
    matched_arkane_key: Any,
    arc_freq_level: Mapping[str, Any] | None,
    *,
    kind: str,
    warning_field: str,
    warnings: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """The ``frequency_level_of_theory`` of a BAC scheme: the freq half of Arkane's composite key.

    tckdb-schemas 0.66: only a scheme Arkane keys on ``CompositeLevelOfTheory(freq=..., energy=...)``
    carries a frequency level; "for an atom-energy scheme or a scheme keyed on one level, send
    nothing new". So the record's ``matched_arkane_key`` decides: a single-level key, no key or an
    unparseable one sends none. For a composite key the level is the key's freq half (the table's
    own key, not ARC's job level). If ARC states a frequency-job level (``arc_freq_level``) that
    disagrees with the key's freq half (method, basis, and software when both state it), which one
    keys the table is unclear, so none is sent and a warning says so.
    """
    halves = _arkane_composite_key_halves(matched_arkane_key)
    if halves is None:
        return None
    key_freq = halves["freq"]
    if isinstance(arc_freq_level, Mapping) and arc_freq_level.get("method"):
        mismatched = [
            f for f in ("method", "basis", "software")
            if f in key_freq and arc_freq_level.get(f)
            and _norm_level_token(key_freq[f]) != _norm_level_token(arc_freq_level.get(f))
        ]
        if mismatched:
            arc_stated = {k: arc_freq_level.get(k) for k in ("method", "basis", "software")}
            message = (
                f"The {kind} table's Arkane key {matched_arkane_key!r} has frequency level "
                f"{key_freq!r}, but ARC states the frequency job ran at {arc_stated!r} "
                f"(differs in {', '.join(mismatched)}). Which one keys the table is unclear, so "
                f"scheme.frequency_level_of_theory is omitted."
            )
            logger.warning("TCKDB %s: %s: %s", warning_field, _W_BAC_FREQUENCY_LEVEL_CONFLICT, message)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_BAC_FREQUENCY_LEVEL_CONFLICT, message, f"{warning_field}.scheme.frequency_level_of_theory",
                    {"action": "scheme_frequency_level_omitted", "scheme_kind": kind, "arkane_key_freq": dict(key_freq), "arc_freq_level": arc_stated}))
            return None
    return _arc_level_to_tckdb_lot(key_freq)


_W_ENERGY_CORRECTION_SCHEME_SOFTWARE_CONFLICT = ArcWarning.ENERGY_CORRECTION_SCHEME_SOFTWARE_CONFLICT.value


_W_ATOM_ENERGY_RECORD_NOT_DEPOSITED_AEC_YML = ArcWarning.ATOM_ENERGY_RECORD_NOT_DEPOSITED_AEC_YML.value
_W_BAC_CORRECTION_OMITTED_COMPONENTS_INCOMPLETE = ArcWarning.BAC_CORRECTION_OMITTED_COMPONENTS_INCOMPLETE.value


def _build_applied_energy_corrections(
    applied_records: Any,
    *,
    source_calculation_key: str | None = None,
    warnings: list[dict[str, Any]] | None = None,
    warning_field: str = "applied_energy_corrections",
    target_kind: Literal["species", "transition_state"] = "species",
    element_symbols: Any = None,
    target_label: str | None = None,
    arkane_release: Mapping[str, Any] | None = None,
    omitted_bac_reasons: list[str] | None = None,
    scheme_data_revisions: Mapping[str, str] | None = None,
    aec_yml_digest: str | None = None,
    bac_frequency_level: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Translate ``output.yml`` per-species ``applied_energy_corrections``
    into the TCKDB ``AppliedEnergyCorrectionUploadPayload`` shape.

    The output.yml shape is already very close to the upload schema (same
    ``application_role`` / ``value`` / ``value_unit`` / ``scheme`` fields),
    so this is mostly a passthrough plus three small adaptations:

    1. Drop output.yml-only fields from components (e.g. ``parameter_unit``)
       — TCKDB's ``AppliedCorrectionComponentPayload`` rejects unknowns.
    2. Drop component rows whose ``parameter_value`` is null — the upload
       schema requires a real number, and the producer marks them null
       precisely when reconstruction wasn't reliable.
    3. Attach ``source_calculation_key`` when the caller has resolved it
       to a real SP key in this bundle. AEC and BAC are corrections to the
       electronic-energy reference, so SP is the right anchor; the field
       is omitted when no key is supplied rather than guessed.

    The caller resolves the SP key against the relevant namespace —
    bundle-global (``"sp"``) for computed-species, scoped (``"r0_sp"`` /
    ``"p0_sp"`` / ``"ts_sp"``) for computed-reaction. The helper does
    not know about modes; it just takes the resolved key (or ``None``)
    and stamps it on every emitted entry.

    A Petersson ``bac_total`` is only sent with a complete bond
    decomposition (see ``_petersson_bac_omission_reason``): TCKDB refuses
    the whole upload for a componentless one (``bac_total_requires_components``)
    and does not check that a partial decomposition sums to the total. When
    the decomposition is unusable the BAC correction alone is omitted, with a
    ``bac_correction_omitted_components_incomplete`` warning; the AEC
    correction and the rest of the payload are unaffected. ``target_kind``
    and ``element_symbols`` (the species' composition, ``None`` when
    unknown) say whether a componentless total is honest: only a monatomic
    species has no bond to decompose. ``omitted_bac_reasons`` collects the
    reason of each omitted BAC so the caller can note it on the thermo record.

    ``bac_frequency_level`` (see ``_bac_frequency_level``) is ARC's stated frequency level
    behind a BAC; it is sent as ``scheme.frequency_level_of_theory`` on ``bac_petersson`` and
    ``bac_melius`` schemes that carry a ``level_of_theory`` (tckdb-schemas 0.66: the field is
    refused on any other kind and without the energy level) and never on an atom-energy
    scheme. It joins the scheme's identity: one new scheme row per BAC scheme the adapter
    sent before without it.

    ``arkane_release`` (see ``_arkane_workflow_tool_release``) is stamped as
    ``scheme.workflow_tool_release`` on ``atom_energy``, ``bac_petersson`` and
    ``bac_melius`` schemes, the kinds built from Arkane's tables. An output.yml
    1.3 document also identifies the tables themselves (the RMG-database
    ``quantum_corrections/data.py`` Arkane loaded); ``scheme_data_revisions`` (see
    ``_scheme_data_revisions``) then names that revision per scheme kind as
    ``scheme.data_revision`` (tckdb-schemas 0.62), which joins the scheme's
    identity and makes the Arkane build provenance only.

    ``aec_yml_digest`` (output 1.3's ``arc_aec_yml_sha256``, see
    ``_arc_aec_yml_digest``) says ARC rendered Arkane's ``atomEnergies`` from its own
    ``data/AEC.yml``. ARC recomputes the exported ``atom_energy`` record from
    ``data.py`` regardless, so for a level both files cover the record would
    describe a table other than the one Arkane applied: that record is then not
    deposited (``atom_energy_record_not_deposited_aec_yml``).
    """
    if not isinstance(applied_records, list):
        return []

    out: list[dict[str, Any]] = []
    for rec in applied_records:
        if not isinstance(rec, Mapping):
            continue
        if rec.get("value") is None or rec.get("scheme") is None:
            continue

        if aec_yml_digest and rec["scheme"].get("kind") == "atom_energy":
            message = (
                f"The atom_energy correction was not sent for "
                f"{target_label or 'this target'}: ARC rendered Arkane's atom energies "
                f"from its own data/AEC.yml (sha256 {aec_yml_digest}) but recomputes "
                f"the exported record from the RMG-database data.py, so the record may "
                f"describe a table other than the one Arkane applied.")
            logger.warning("TCKDB %s: %s: %s", warning_field,
                           _W_ATOM_ENERGY_RECORD_NOT_DEPOSITED_AEC_YML, message)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_ATOM_ENERGY_RECORD_NOT_DEPOSITED_AEC_YML, message, warning_field,
                    {"action": "atom_energy_correction_omitted", "arc_aec_yml_sha256": aec_yml_digest, "species": target_label}))
            continue
        components_in = rec.get("components") or []
        omission = _petersson_bac_omission_reason(
            rec, components_in,
            target_kind=target_kind, element_symbols=element_symbols,
        )
        if omission is not None:
            reason, detail = omission
            message = (
                f"The Petersson bac_total was not sent for "
                f"{target_label or 'this target'}: {detail} TCKDB refuses a "
                f"bac_total without a bond decomposition and does not check that "
                f"a partial one sums to the total, so the correction is omitted "
                f"rather than sent unsupported. The rest of the payload is unchanged."
            )
            logger.warning("TCKDB %s: %s: %s", warning_field,
                           _W_BAC_CORRECTION_OMITTED_COMPONENTS_INCOMPLETE, message)
            if omitted_bac_reasons is not None:
                omitted_bac_reasons.append(reason)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_BAC_CORRECTION_OMITTED_COMPONENTS_INCOMPLETE, message, warning_field,
                    {"action": "bac_correction_omitted", "reason": reason, "target_kind": target_kind, "species": target_label}))
            continue
        components_out: list[dict[str, Any]] = []
        for c in components_in:
            if not isinstance(c, Mapping):
                continue
            if c.get("parameter_value") is None or c.get("contribution_value") is None:
                continue
            components_out.append({
                k: c[k] for k in _APPLIED_CORRECTION_COMPONENT_FIELDS if k in c
            })

        scheme_in = rec["scheme"]
        scheme_out = {
            k: scheme_in[k] for k in scheme_in
            if k not in {"level_of_theory", "version"}
        }
        # Older ARC-shaped payloads carried a free-form scheme version.
        # Current TCKDB identifies schemes by scientific/software provenance;
        # retain a reported legacy label as a note, not an invented release.
        legacy_version = scheme_in.get("version")
        if legacy_version is not None:
            version_note = f"Legacy scheme version: {legacy_version}"
            scheme_out["note"] = "; ".join(
                str(value) for value in (scheme_out.get("note"), version_note) if value
            )
        lot_ref = _scheme_level_of_theory(scheme_in)
        if lot_ref is not None:
            table_stem = correction_table_method_stem(lot_ref["method"])
            if table_stem is not None:
                # ARC's Arkane level string names a correction table, not a method
                # (``cbs-qb3-paraskevas``, ``cbsqb32023``): the calculation that ran is
                # the stem. tckdb-schemas 0.67 warns on such a method and stores it as a
                # separate level of theory; it says to send the method and name the table on
                # the scheme. The scheme's ``name`` is that name (the table label, as ARC
                # states it); the level carries the method.
                table_name = lot_ref["method"]
                lot_ref = {**lot_ref, "method": table_stem}
                scheme_out["name"] = table_name
                message = (
                    f"ARC's Arkane level string {table_name!r} names a correction table, not a "
                    f"method. The {scheme_out.get('kind')} scheme is sent with level method "
                    f"{table_stem!r} and the table name as the scheme name."
                )
                logger.warning("TCKDB %s: %s: %s", warning_field, _W_CORRECTION_TABLE_METHOD_SPLIT, message)
                if warnings is not None:
                    warnings.append(_self_check(
                        _W_CORRECTION_TABLE_METHOD_SPLIT, message, f"{warning_field}.scheme.level_of_theory.method",
                        {"action": "correction_table_named_on_scheme", "table": table_name, "method": table_stem, "scheme_kind": str(scheme_out.get("kind"))}))
            scheme_out["level_of_theory"] = lot_ref
            if scheme_out.get("kind") in _FREQ_KEYED_SCHEME_KINDS:
                freq_ref = _bac_key_frequency_level(
                    rec.get("matched_arkane_key"), bac_frequency_level,
                    kind=str(scheme_out.get("kind")), warning_field=warning_field, warnings=warnings,
                )
                if freq_ref is not None:
                    scheme_out["frequency_level_of_theory"] = freq_ref
        # ``scheme.software`` is the program that computed the scheme's
        # parameters (contract: "The program release that computed this
        # scheme's parameters"). That is the ``software`` of the Arkane
        # database entry the table came from, recorded by ARC as
        # ``matched_arkane_key`` (e.g. LevelOfTheory(method='b3lyp2023',
        # basis='def2tzvp', software='gaussian')). The record's
        # ``level_of_theory`` is ARC's own level, not the table's: ARC's
        # matcher (arc/statmech/arkane.py) accepts a key without software
        # for any program, so crediting the table to the level's software
        # could name a program that never computed it. Hence: the key's
        # software, name only (ARC records no release); omitted when the
        # key is absent or names none; omitted with a warning when it
        # disagrees with the level's software. Never Arkane or ARC (they
        # looked the table up; that is ``workflow_tool_release``).
        if "software" not in scheme_out:
            key_software = _arkane_key_software(rec.get("matched_arkane_key"))
            scheme_level = scheme_in.get("level_of_theory")
            level_software = (
                scheme_level.get("software") if isinstance(scheme_level, Mapping) else None
            )
            if key_software is None:
                pass
            elif (isinstance(level_software, str) and level_software.strip()
                    and level_software.strip().lower() != key_software.lower()):
                message = (
                    f"The {scheme_out.get('kind')} table came from Arkane's "
                    f"{rec.get('matched_arkane_key')!r} (software={key_software!r}), "
                    f"but ARC's correction level names software={level_software!r}. "
                    f"Which program computed the table is unclear, so "
                    f"scheme.software is omitted."
                )
                logger.warning("TCKDB %s: %s: %s", warning_field,
                               _W_ENERGY_CORRECTION_SCHEME_SOFTWARE_CONFLICT, message)
                if warnings is not None:
                    warnings.append(_self_check(
                        _W_ENERGY_CORRECTION_SCHEME_SOFTWARE_CONFLICT, message, f"{warning_field}.scheme.software",
                        {"action": "scheme_software_omitted", "scheme_kind": str(scheme_out.get("kind")), "arkane_key_software": key_software, "level_software": level_software}))
            else:
                scheme_out["software"] = {"name": key_software}

        if scheme_out.get("kind") in _RMG_DATABASE_SCHEME_KINDS:
            if arkane_release is not None and "workflow_tool_release" not in scheme_out:
                scheme_out["workflow_tool_release"] = dict(arkane_release)
            revision = (scheme_data_revisions or {}).get(scheme_out["kind"])
            if revision is not None and "data_revision" not in scheme_out:
                scheme_out["data_revision"] = revision

        payload: dict[str, Any] = {
            "application_role": rec["application_role"],
            "value": float(rec["value"]),
            "value_unit": rec["value_unit"],
            "scheme": scheme_out,
            "components": components_out,
        }
        if source_calculation_key is not None:
            payload["source_calculation_key"] = source_calculation_key

        note = rec.get("note")
        if note is not None:
            payload["note"] = note

        out.append(payload)

    return out


# Explicit target field sets guard against future divergence of the two
# bundle roots. Both current roots accept calculation provenance.
_THERMO_FIELDS_BY_TARGET: dict[str, frozenset[str]] = {
    "ThermoInBundle": frozenset({
        "h298_kj_mol", "s298_j_mol_k", "tmin_k", "tmax_k",
        "nasa", "points", "source_calculations",
        "enthalpy_reference_kind", "reference_pressure_bar",
        "energy_level_of_theory",
    }),
    "BundleThermoIn": frozenset({
        "h298_kj_mol", "s298_j_mol_k", "tmin_k", "tmax_k",
        "nasa", "points", "source_calculations",
        "enthalpy_reference_kind", "reference_pressure_bar",
        "energy_level_of_theory",
    }),
}

# TCKDB #520: a thermo record carrying enthalpy content must declare its
# enthalpy basis. Arkane's H298 and NASA a6/b6 (and the H/G points ARC
# evaluates from that fit) are formation enthalpies from the elements at
# 298.15 K, which is exactly ``formation_298k``.
_THERMO_ENTHALPY_REFERENCE_KIND = "formation_298k"

# That holds only when Arkane subtracted the atom energies of the level the
# species' energies were computed at. With none for the level of theory,
# ARC runs Arkane with useAtomCorrections=False and every enthalpy (H298,
# NASA a6/b6, point H/G) is the raw absolute energy. output.yml 1.2 records
# that switch (``thermo.atom_corrections_applied``) and the level whose
# atom energies were used (``thermo.atom_corrections_level``), which can be
# a stand-in for the energy level (ARC only warns). Enthalpies failing
# either check are stripped from the block, keeping its entropy and Cp.
_W_ENTHALPY_ATOM_CORRECTIONS_NOT_APPLIED = ArcWarning.ENTHALPY_ATOM_CORRECTIONS_NOT_APPLIED.value
_W_ENTHALPY_NOT_FINITE = ArcWarning.ENTHALPY_NOT_FINITE.value

# TCKDB #529 and the producer contract: ``reference_pressure_bar`` is never
# defaulted, and "if your source does not say which convention a number
# follows, leave the field out". ARC states the standard-state pressure only
# as ``thermo.standard_state_pressure_pa`` (arc/output.py
# ``_thermo_to_dict``; ``None`` when the thermo did not come from a statmech
# run that recorded one). When it is absent or unusable the adapter omits
# ``reference_pressure_bar`` (TCKDB stores "not stated") and reports
# ``thermo_reference_pressure_not_stated``; it never fills RMG's 1 atm in.
_W_THERMO_REFERENCE_PRESSURE_NOT_STATED = ArcWarning.THERMO_REFERENCE_PRESSURE_NOT_STATED.value
# Plausible standard-state pressures, in bar: a validity check on the value
# ARC recorded, never a source of one. Every real convention (1 bar, 1 atm)
# sits well inside; a value in bar mistaken for Pa (1.01325 -> 1e-5 bar) or
# a YAML boolean (True -> 1e-5 bar) falls far outside.
_THERMO_REFERENCE_PRESSURE_WINDOW_BAR = (0.5, 2.0)


def _thermo_reference_pressure_bar(
    thermo_record: Mapping[str, Any],
) -> tuple[float | None, str | None]:
    """Return ``(pressure_bar, None)`` or ``(None, why it is not stated)``.

    The pressure is ARC's recorded ``standard_state_pressure_pa`` converted
    to bar, when it is a real number in Pa inside
    ``_THERMO_REFERENCE_PRESSURE_WINDOW_BAR``. Otherwise there is no
    pressure, and the second item says whether ARC recorded none
    (``not_recorded``) or an unusable one (``malformed``).
    """
    recorded = thermo_record.get("standard_state_pressure_pa")
    if recorded is None:
        return None, "not_recorded"
    pressure_bar = math.nan
    if isinstance(recorded, (int, float)) and not isinstance(recorded, bool):
        pressure_bar = float(recorded) / 1e5
    low, high = _THERMO_REFERENCE_PRESSURE_WINDOW_BAR
    if low <= pressure_bar <= high:
        return pressure_bar, None
    logger.warning(
        "TCKDB thermo: malformed standard_state_pressure_pa=%r; "
        "omitting reference_pressure_bar.", recorded,
    )
    return None, "malformed"


def _one_electron_hf_substitution(
    energy_level: Mapping[str, Any] | None,
    corrections_level: Mapping[str, Any] | None,
    electron_count: int | None,
) -> bool:
    """Whether a one-electron species' Hartree-Fock energy stands for the correlated corrections level.

    The same exemption the kinetics energy-level rule has (``tckdb_core.rules``): the species has
    exactly one electron (``electron_count``, ``None`` when not known, grants none), its energy
    level's method identifies as Hartree-Fock, the atom-corrections level's method classifies as
    ``wavefunction`` or ``multireference`` (``tckdb_core.method_families``), and every other
    identity field of the two levels (basis, auxiliary and CABS basis, dispersion, solvent and
    solvent model) is equal on TCKDB's projection. Every correlation term vanishes for one
    electron, so the atom energy subtracted at the correlated level is the HF energy in the same
    basis (an F12 level's CABS singles term aside).
    """
    if isinstance(electron_count, bool) or electron_count != 1:
        return False
    energy = _arc_level_to_tckdb_lot(energy_level)
    corrections = _arc_level_to_tckdb_lot(corrections_level)
    if energy is None or corrections is None or method_identity_key(energy["method"]) != "hf":
        return False
    if method_family(corrections.get("method")) not in ONE_ELECTRON_EXEMPT_FAMILIES:
        return False
    return ({k: v for k, v in energy.items() if k != "method"}
            == {k: v for k, v in corrections.items() if k != "method"})


def _enthalpy_validity_error(
    block: Mapping[str, Any],
    thermo_record: Mapping[str, Any],
    energy_level: Mapping[str, Any] | None,
    header_corrections_level: Any = None,
    element_symbols: Any = None,
    electron_count: int | None = None,
) -> tuple[str, str, dict[str, str]] | None:
    """Return why the block's enthalpies are not formation_298k, or ``None``.

    Returns ``(code, message, extra warning context)``. Reads ARC's recorded
    atom-correction switch and level first. A switch that was on still
    needs ``atom_corrections_level`` to be the energy level
    (``_level_identity``; a one-electron species at Hartree-Fock under a wavefunction or
    multireference corrections level is accepted, ``_one_electron_hf_substitution``), and a match cannot be verified when either level
    sets a dispersion or solvation field ARC's matching ignores; the
    non-finite and magnitude checks follow.

    A null or absent switch runs, first failure wins: non-finite, magnitude,
    the header ``arkane_level_of_theory`` against the energy level
    (mismatch; skipped when the header is null or has no method),
    dispersion/solvation on either level (unverifiable), and a light
    species the magnitude guard cannot catch. Refusals from the last three
    carry ``atom_corrections_applied: not_recorded`` and whether the header
    level was checked in their context.
    """
    applied = thermo_record.get("atom_corrections_applied")
    if applied is False:
        return (
            _W_ENTHALPY_ATOM_CORRECTIONS_NOT_APPLIED,
            "ARC recorded atom_corrections_applied=false: Arkane subtracted no "
            "atom energies, so H298, the NASA fit and point H/G are raw absolute "
            "energies, not formation enthalpies.",
            {},
        )
    if applied is True:
        corrections_level = thermo_record.get("atom_corrections_level")
        corrections_key = _level_identity(corrections_level)
        if (corrections_key is None or corrections_key != _level_identity(energy_level)) \
                and not _one_electron_hf_substitution(energy_level, corrections_level, electron_count):
            return (
                _W_ENTHALPY_ATOM_CORRECTIONS_LEVEL_MISMATCH,
                f"Arkane subtracted the atom energies of "
                f"{_describe_level(corrections_level)} from energies computed at "
                f"{_describe_level(energy_level)}, so the enthalpies mix two levels "
                f"and are not formation enthalpies.",
                {},
            )
        unverifiable = _level_field_unverifiable_error(
            (("energy level", energy_level), ("atom_corrections_level", corrections_level)),
            matched="a matching level",
        )
        if unverifiable is not None:
            return (*unverifiable, {})
    values = [block.get("h298_kj_mol"), *block.get("nasa", {}).values()]
    values += [p.get(key) for p in block.get("points", ()) for key in ("h_kj_mol", "g_kj_mol")]
    if any(v is not None and not math.isfinite(v) for v in values):
        return (
            _W_ENTHALPY_NOT_FINITE,
            "An enthalpy value (H298, NASA coefficient or bound, point H or G) "
            "is not finite.",
            {},
        )
    magnitude = _enthalpy_magnitude_error(block)
    if magnitude is not None:
        return (*magnitude, {})
    if applied is True:
        return None
    return _unflagged_enthalpy_error(energy_level, header_corrections_level, element_symbols)


def _build_thermo_block(
    thermo_record: Any,
    *,
    calc_keys_by_role: Mapping[str, str],
    target_model: Literal["ThermoInBundle", "BundleThermoIn"],
    warnings: list[dict[str, Any]] | None = None,
    warning_field: str = "thermo",
    energy_level: Mapping[str, Any] | None = None,
    header_corrections_level: Any = None,
    element_symbols: Any = None,
    energy_level_unattributable: bool = False,
    energy_declaration: tuple[str, Mapping[str, Any]] | None = None,
    legacy_cp_data: bool = False,
    electron_count: int | None = None,
) -> dict[str, Any] | None:
    """Build a ``target_model``-shaped thermo dict from ``output.yml`` thermo data.

    Returns ``None`` when no usable thermo content can be assembled. The
    server-side ``validate_has_scientific_content`` validator (present on
    both roots) rejects empty thermo blocks, so emitting one with
    nothing in it would just produce a 422; better to omit at the
    producer.

    Mapping (from ``arc/output.py::_thermo_to_dict``):
        h298_kj_mol  → h298_kj_mol
        s298_j_mol_k → s298_j_mol_k
        tmin_k       → tmin_k
        tmax_k       → tmax_k
        nasa_low.coeffs  → nasa.a1..a7
        nasa_high.coeffs → nasa.b1..b7
        nasa_low.tmin_k  → nasa.t_low
        nasa_low.tmax_k  → nasa.t_mid (cross-checked vs nasa_high.tmin_k)
        nasa_high.tmax_k → nasa.t_high
        thermo_points    → points (per-point validation; bad points dropped)

    Enthalpy content (h298, NASA, point H or G) is first checked by
    ``_enthalpy_validity_error`` against ARC's recorded atom-correction
    switch and level (``energy_level`` is the species' energy level, see
    ``_thermo_energy_level``); without that switch, against the document's
    ``arkane_level_of_theory`` (``header_corrections_level``) and the
    species' composition (``element_symbols``, see
    ``_species_element_symbols``). With ``energy_level_unattributable`` (an
    ``adaptive_levels`` run, ``_energy_level_unattributable``) the energy level
    is not known per species, so no enthalpy is verifiable and all of it is
    stripped (``enthalpy_adaptive_levels_unverifiable``). Enthalpy that is not a formation enthalpy is
    stripped, keeping S298, point S and Cp; the refusal is appended to
    ``warnings`` under ``warning_field`` with action
    ``thermo_enthalpy_omitted`` (``thermo_omitted`` when nothing is left).

    Remaining enthalpy content adds
    ``enthalpy_reference_kind="formation_298k"``; entropy content (s298,
    NASA, point S or G) adds ``reference_pressure_bar`` when ARC recorded
    a usable ``standard_state_pressure_pa`` (see
    ``_thermo_reference_pressure_bar``). Without one the field is omitted,
    never defaulted, and ``thermo_reference_pressure_not_stated`` is
    appended to ``warnings`` with action ``reference_pressure_omitted``;
    the block is still sent. Neither field is set on a Cp-only block. The
    finished block is checked with the shared ``enthalpy_reference_error``
    rule; a refused block is never emitted: ``None`` is returned and the
    refusal is appended to ``warnings`` with action ``thermo_omitted``.

    ``electron_count`` is the species' electrons from its own composition and charge
    (``_participant_electron_count``). A one-electron species computed at Hartree-Fock under
    a wavefunction or multireference ``atom_corrections_level`` that otherwise equals its
    energy level keeps its enthalpy (``_one_electron_hf_substitution``) and is reported as
    ``one_electron_hf_substitution``; every other level mismatch is refused as before.

    ``calc_keys_by_role`` maps roles to the actual bundle-local keys.
    Both roots accept source links; reaction participants use their own
    scoped keys (for example ``r0_opt``), never another participant's keys.

    ``energy_declaration`` is ``(role, level)`` from
    ``_energy_level_declaration``: the level of the linked energy calculation,
    sent as ``energy_level_of_theory`` only when that role is among this
    block's source links (TCKDB checks the declaration against those links).
    """
    if target_model not in _THERMO_FIELDS_BY_TARGET:
        raise ValueError(
            f"_build_thermo_block: unknown target_model={target_model!r}; "
            f"expected one of {sorted(_THERMO_FIELDS_BY_TARGET)}"
        )
    if not isinstance(thermo_record, Mapping):
        return None

    block: dict[str, Any] = {}

    h298 = thermo_record.get("h298_kj_mol")
    if h298 is not None:
        try:
            block["h298_kj_mol"] = float(h298)
        except (TypeError, ValueError) as exc:
            logger.warning("TCKDB thermo: malformed h298_kj_mol=%r (%s)", h298, exc)

    s298 = thermo_record.get("s298_j_mol_k")
    if s298 is not None:
        try:
            block["s298_j_mol_k"] = float(s298)
        except (TypeError, ValueError) as exc:
            logger.warning("TCKDB thermo: malformed s298_j_mol_k=%r (%s)", s298, exc)

    for tk in ("tmin_k", "tmax_k"):
        v = thermo_record.get(tk)
        if v is not None:
            try:
                block[tk] = float(v)
            except (TypeError, ValueError) as exc:
                logger.warning("TCKDB thermo: malformed %s=%r (%s)", tk, v, exc)

    nasa = _build_nasa_block(thermo_record.get("nasa_low"), thermo_record.get("nasa_high"))
    if nasa is not None:
        block["nasa"] = nasa

    points = _build_thermo_points(
        thermo_record.get("thermo_points")
        or (thermo_record.get("cp_data") if legacy_cp_data else None))
    if points:
        block["points"] = points

    # Thermo provenance: link every calculation that actually contributed
    # to the thermo result, not just the "physically produced" ones.
    # For ARC's standard opt/freq/sp pipeline that means:
    #   opt  — geometry that freq + sp were run on
    #   freq — vibrational modes / ZPE / thermal corrections
    #   sp   — electronic-energy reference for H298
    # The links should be self-sufficient; we don't want consumers to have
    # to traverse `calculation_dependency` to recover the opt calc.
    # Order is fixed (opt, freq, sp) so payloads are deterministic — same
    # inputs hash to the same idempotency key across runs.
    #
    sources: list[dict[str, str]] = []
    for role in (_CALC_KEY_OPT, _CALC_KEY_FREQ, _CALC_KEY_SP, _ROLE_COMPOSITE):
        key = calc_keys_by_role.get(role)
        if key:
            sources.append({"calculation_key": key, "role": role})
    if sources:
        block["source_calculations"] = sources

    def has_content() -> bool:
        return any(key in block for key in ("h298_kj_mol", "s298_j_mol_k", "nasa", "points"))

    if not has_content():
        # Server would 422 us; nothing usable here.
        return None

    # Refusals as (code, message, extra context), reported once the block's
    # fate is known.
    refusals: list[tuple[str, str, dict[str, str]]] = []
    if thermo_record.get(_THERMO_ENTHALPY_WITHHELD_KEY):
        # The record's E0 is one the adapter withheld (``_withhold_unreliable_composite_energies``,
        # which has warned): H298, NASA and point H/G derive from it; S298, Cp and point S do not.
        _strip_enthalpy_content(block)
    if _has_enthalpy_content(block):
        enthalpy_refusal = (
            (
                _W_ENTHALPY_ADAPTIVE_LEVELS_UNVERIFIABLE,
                "ARC ran with adaptive_levels, which chooses each species' "
                "energy level by heavy-atom count, while output.yml records one "
                "sp_level per run. The level this species' energy was computed "
                "at is not known, so it cannot be checked against the level "
                "Arkane's atom-energy corrections were applied at; H298, the "
                "NASA fit and point H/G may not be formation enthalpies and are "
                "omitted.",
                {"adaptive_levels": "detected"},
            )
            if energy_level_unattributable else
            _enthalpy_validity_error(
                block, thermo_record, energy_level, header_corrections_level, element_symbols,
                electron_count)
        )
        if enthalpy_refusal is not None:
            refusals.append(enthalpy_refusal)
            _strip_enthalpy_content(block)
    keep = has_content()
    # The enthalpy survived only because a one-electron HF energy stands for the correlated
    # corrections level: said once the block's fate is known.
    hf_substitution = (
        keep and _has_enthalpy_content(block)
        and thermo_record.get("atom_corrections_applied") is True
        and _level_identity(thermo_record.get("atom_corrections_level")) != _level_identity(energy_level)
        and _one_electron_hf_substitution(
            energy_level, thermo_record.get("atom_corrections_level"), electron_count))

    # Why the entropy's standard state is not stated, when it is not.
    pressure_unstated: str | None = None
    if keep:
        points_out = block.get("points", ())
        if _has_enthalpy_content(block):
            block["enthalpy_reference_kind"] = _THERMO_ENTHALPY_REFERENCE_KIND
        # G = H - T*S carries the entropy's standard state as well.
        if "s298_j_mol_k" in block or "nasa" in block or any(
            "s_j_mol_k" in p or "g_kj_mol" in p for p in points_out
        ):
            pressure_bar, pressure_unstated = _thermo_reference_pressure_bar(thermo_record)
            if pressure_bar is not None:
                block["reference_pressure_bar"] = pressure_bar
        if energy_declaration is not None and any(
                link["role"] == energy_declaration[0] for link in sources):
            block["energy_level_of_theory"] = dict(energy_declaration[1])

    # Belt-and-suspenders: this is the exact bug class that motivated
    # ``target_model`` in the first place (see
    # ``_THERMO_FIELDS_BY_TARGET``'s docstring) — a real raise here, not
    # just in the test suite, so a future field added to this builder
    # without updating the allow-list fails loudly the moment it's
    # exercised, rather than shipping a silent 422 to production. A bare
    # ``assert`` would vanish under ``python -O``, precisely when a
    # production guard is wanted most.
    disallowed = set(block) - _THERMO_FIELDS_BY_TARGET[target_model]
    if disallowed:
        raise ValueError(
            f"_build_thermo_block emitted field(s) not accepted by "
            f"{target_model}: {sorted(disallowed)}"
        )

    # TCKDB refuses the whole upload over an incoherent enthalpy
    # declaration. Never send a block the shared rule would refuse: drop
    # only the thermo block (the enclosing payload stays valid without
    # it). Record every refusal next to the server's own warnings.
    if keep:
        shared_refusal = enthalpy_reference_error(block)
        if shared_refusal is not None:
            refusals.append((*shared_refusal, {}))
            keep = False
    action = "thermo_enthalpy_omitted" if keep else "thermo_omitted"
    for code, message, extra_context in refusals:
        logger.warning(
            "TCKDB thermo %s: producer self-check refused %s (%s): %s",
            warning_field, "its enthalpy" if keep else "it", code, message,
        )
        if warnings is not None:
            warnings.append(_self_check(
                code, message, warning_field,
                {"action": action, **extra_context}))
    if keep and hf_substitution and warnings is not None:
        corrections = _arc_level_to_tckdb_lot(thermo_record.get("atom_corrections_level")) or {}
        context: dict[str, str] = {
            "action": "one_electron_hf_enthalpy_accepted",
            "energy_level": _describe_level(energy_level),
            "atom_corrections_level": _describe_level(thermo_record.get("atom_corrections_level"))}
        message = (
            "This one-electron species' energy was computed at Hartree-Fock in the same basis as the "
            "atom-corrections level, so the atom energy Arkane subtracted equals it (every correlation "
            "term vanishes for one electron) and the enthalpy is kept as a formation enthalpy.")
        if "f12" in method_identity_key(str(corrections.get("method") or "")):
            context["cabs_singles_correction"] = "absent"
            message += (" The HF energy omits the F12 CABS singles correction "
                        "(about 0.05 to 0.1 mEh for H).")
        warnings.append(_self_check(
            ArcWarning.ONE_ELECTRON_HF_SUBSTITUTION.value, message, warning_field, context))
    if keep and pressure_unstated is not None:
        message = (
            "ARC did not record the standard-state pressure its entropies were "
            "computed at (thermo.standard_state_pressure_pa is "
            + ("absent" if pressure_unstated == "not_recorded" else "not a usable pressure in Pa")
            + "), so reference_pressure_bar is omitted and TCKDB stores it as not "
            "stated rather than a guessed 1 atm."
        )
        logger.warning(
            "TCKDB thermo %s: %s: %s",
            warning_field, _W_THERMO_REFERENCE_PRESSURE_NOT_STATED, message,
        )
        if warnings is not None:
            warnings.append(_self_check(
                _W_THERMO_REFERENCE_PRESSURE_NOT_STATED, message, warning_field,
                {"action": "reference_pressure_omitted", "standard_state_pressure_pa": pressure_unstated}))
    return block if keep else None


_W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03 = ArcWarning.G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03.value
#: Marks a ``thermo`` record whose E0-derived content is withheld (``_build_thermo_block`` strips it).
_THERMO_ENTHALPY_WITHHELD_KEY = "_tckdb_thermo_enthalpy_withheld"


def _withhold_unreliable_composite_energies(
    output_doc: Mapping[str, Any],
    record: Mapping[str, Any],
    warnings: list[dict[str, Any]] | None,
) -> Mapping[str, Any]:
    """The record without the energies ARC read wrongly (G4 / G4MP2 on Gaussian 16 Rev A.03).

    Returns ``record`` itself when it is not that case. Otherwise a copy with
    ``sp_energy_hartree`` unset (so no sp energy and no sp link on the statmech energy), its
    ``thermo`` marked so the thermo block is built without its E0-derived content (H298, NASA,
    point H and G; ``_strip_enthalpy_content``, the path the enthalpy refusals use) and with S298,
    Cp and point S kept, and the record marked so the composite role (which would also link the
    composite job as the energy) is withheld too. Warns once per record.

    Only an output 1.3 record carries ``ess_versions.composite``, so an earlier document never
    matches ``_g4_loader_energy_unreliable`` and is not guarded.
    """
    hit = _g4_loader_energy_unreliable(output_doc, record) if isinstance(record, Mapping) else None
    if hit is None:
        return record
    method, banner = hit
    label = record.get("label") or record.get("original_label") or "<unlabeled>"
    message = (
        f"The energies of {label!r} were not sent: it is a {method.upper()} composite run on "
        f"{banner!r}, whose summary table shifts the {method.upper()}(0 K) label by one entry, so "
        "the energy ARC and Arkane read for it is the 298 K value, 9-11 kJ/mol from E0. What is derived "
        "from that energy is withheld until ARC exports E0 from the archive (ARC_TCKDB_EXPORT_BRIEF.md, Bug 8): "
        "the single-point energy, H298, the NASA polynomials, the point enthalpies and Gibbs energies, and the "
        "statmech energy links. S298, Cp and the point entropies, which come from statmech and not from E0, "
        "are kept."
    )
    if not any(w.get("code") == _W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03
               and (w.get("context") or {}).get("label") == label for w in (warnings or ())):
        logger.warning("TCKDB %s: %s", _W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03, message)
        if warnings is not None:
            warnings.append(_self_check(
                _W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03, message, f"{label}.energies",
                {"action": "record_energies_withheld", "label": label, "method": method, "ess_version": banner}))
    out = dict(record)
    out["sp_energy_hartree"] = None
    if isinstance(record.get("thermo"), Mapping):
        out["thermo"] = {**record["thermo"], _THERMO_ENTHALPY_WITHHELD_KEY: True}
    out[_ENERGY_WITHHELD_KEY] = _W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03
    return out


def _withhold_unreliable_energies_in_doc(
    output_doc: Mapping[str, Any], warnings: list[dict[str, Any]] | None,
) -> Mapping[str, Any]:
    """``output_doc`` with every affected species / TS record neutralised and the kinetics built on them dropped.

    Returns ``output_doc`` unchanged when no record is affected. See
    ``_withhold_reaction_kinetics`` for the kinetics.
    """
    changed = False

    def neutralise(records: Any) -> Any:
        nonlocal changed
        if not isinstance(records, list):
            return records
        out = []
        for record in records:
            fixed = _withhold_unreliable_composite_energies(output_doc, record, warnings)
            changed = changed or fixed is not record
            out.append(fixed)
        return out

    species = neutralise(output_doc.get("species"))
    transition_states = neutralise(output_doc.get("transition_states"))
    if not changed:
        return output_doc
    marked = dict(output_doc)
    if "species" in output_doc:
        marked["species"] = species
    if "transition_states" in output_doc:
        marked["transition_states"] = transition_states
    if isinstance(output_doc.get("reactions"), list):
        marked["reactions"] = [
            _withhold_reaction_kinetics(marked, reaction, warnings) for reaction in output_doc["reactions"]
        ]
    return marked


def _withhold_reaction_kinetics(
    output_doc: Mapping[str, Any], reaction: Mapping[str, Any], warnings: list[dict[str, Any]] | None,
) -> Mapping[str, Any]:
    """The reaction without its kinetics when one of its participants' energies was withheld.

    A rate coefficient is fitted from the energies of the reactants, products and the TS, so one
    built on a withheld record is withheld too, with the same warning code. ``output_doc`` is the
    document after ``_withhold_unreliable_energies_in_doc`` (its records carry the marker).
    """
    if not isinstance(reaction, Mapping) or reaction.get("kinetics") is None:
        return reaction
    withheld = {
        str(record.get("label") or record.get("original_label"))
        for key in ("species", "transition_states") for record in output_doc.get(key) or ()
        if isinstance(record, Mapping) and record.get(_ENERGY_WITHHELD_KEY)
    }
    labels = {str(x) for x in (reaction.get("reactant_labels") or ())}
    labels |= {str(x) for x in (reaction.get("product_labels") or ())}
    labels.add(str(reaction.get("ts_label")))
    hit = sorted(labels & withheld)
    if not hit:
        return reaction
    if not any(w.get("code") == _W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03 and w.get("field") == "kinetics"
               and (w.get("context") or {}).get("reaction") == reaction.get("label") for w in (warnings or ())):
        message = (
            f"The kinetics of reaction {reaction.get('label')!r} were not sent: they are fitted from the "
            f"energies of {hit}, which were withheld ({_W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03})."
        )
        logger.warning("TCKDB %s: %s", _W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03, message)
        if warnings is not None:
            warnings.append(_self_check(
                _W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03, message, "kinetics",
                {"action": "kinetics_withheld", "reaction": reaction.get("label"), "withheld": hit}))
    return {**reaction, "kinetics": None}


_W_ENTHALPY_ADAPTIVE_LEVELS_UNVERIFIABLE = ArcWarning.ENTHALPY_ADAPTIVE_LEVELS_UNVERIFIABLE.value
