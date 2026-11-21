"""Statmech payload builders of the ARC adapter.

The statmech block of a species with its torsions, the rejected torsions, the frequency scale factor
reference, and the scan entries a rotor scan is read from.
"""

from collections.abc import Mapping
from typing import Any, Literal

from tckdb_arc import arc13
from tckdb_arc.warning_codes import ArcWarning
from tckdb_arc.self_check import _self_check
from tckdb_arc.geometry import _geometry_payload, _isotopic_substitutions, _normalize_xyz_text
from tckdb_arc.levels import _describe_level, _opt_level_software, _resolve_level
from tckdb_arc.calculations import _CALC_KEY_SCAN
from tckdb_arc.provenance import _is_output_schema_1_3_or_later
from tckdb_arc.statmech import (
    _build_statmech_source_calculations,
    _classify_statmech_treatment,
    _coerce_torsion_coordinates,
)
from tckdb_arc._logging import get_logger

from tckdb_arc.level_payloads import _arc_level_to_tckdb_lot

logger = get_logger()


def _scan_entries_from_record(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return legacy TCKDB-shaped scan entries, translating neutral ARC scans.

    ARC output 1.1 records parser-native ``rotor_scans``. Older consolidated
    outputs carried ``additional_calculations`` already shaped like the TCKDB
    calculation model; retain that path for replay compatibility.
    """
    legacy = record.get("additional_calculations")
    if isinstance(legacy, list):
        return [entry for entry in legacy if isinstance(entry, Mapping)]
    scans = record.get("rotor_scans")
    if not isinstance(scans, list):
        return []
    translated: list[Mapping[str, Any]] = []
    for scan in scans:
        if not isinstance(scan, Mapping):
            continue
        key = scan.get("key")
        result = scan.get("result")
        if not isinstance(key, str) or not key or not isinstance(result, Mapping):
            continue
        scan_result = _neutral_scan_result_to_tckdb(result, record)
        if scan_result is None:
            continue
        translated.append({
            "key": key,
            "type": _CALC_KEY_SCAN,
            "scan_result": scan_result,
            "constraints": scan.get("constraints") or [],
            # ARC's ``rotor_scans[].source_log``: the scan's own ESS log, the
            # output_log artifact of this scan calculation.
            "source_log": scan.get("source_log"),
            # Output.yml 1.3: the program and banner of the scan's own log.
            "ess_software": scan.get("ess_software"),
            "ess_version": scan.get("ess_version"),
        })
    return translated


def _neutral_scan_result_to_tckdb(
    result: Mapping[str, Any], record: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    coordinate = result.get("coordinate")
    samples = result.get("samples")
    if not isinstance(coordinate, Mapping) or not isinstance(samples, list) or not samples:
        return None
    atoms = coordinate.get("atom_indices")
    if not isinstance(atoms, list) or not atoms:
        return None
    # index_base defaults to 1 when the key is absent (matches ARC's own
    # rotor-scan writer, which always emits index_base=1 for the only
    # coordinate kind it produces — a dihedral scan; ARC's schema pins
    # it to `const: 1`). Unlike the constraints[] route (C-1), a missing
    # key here is safe on every reachable path. But an out-of-range
    # *explicit* value must not be applied blindly
    # — guard it the same way constraints.py does, rather than only
    # bounding via the len<=4 check below.
    try:
        index_base = int(coordinate.get("index_base", 1))
    except (TypeError, ValueError):
        return None
    if index_base not in (0, 1):
        logger.warning(
            "TCKDB scan coordinate: unsupported index_base=%r; omitting "
            "scan result.", index_base,
        )
        return None
    try:
        one_based = [int(atom) - index_base + 1 for atom in atoms]
    except (TypeError, ValueError):
        return None
    kind = {
        "cartesian": "cartesian_atom",
        "distance": "bond",
        "angle": "angle",
        "dihedral": "dihedral",
    }.get(str(coordinate.get("coordinate_type") or ""))
    if kind is None or len(one_based) > 4:
        return None
    coord_out: dict[str, Any] = {
        "coordinate_index": 1,
        "coordinate_kind": kind,
        "step_count": len(samples),
        "value_unit": str(coordinate.get("unit") or "degree"),
    }
    for position, atom in enumerate(one_based, start=1):
        coord_out[f"atom{position}_index"] = atom
    optional_map = {
        "symmetry_number": "symmetry_number",
        "requested_step_size": "step_size",
        "requested_start": "start_value",
        "requested_end": "end_value",
    }
    for source_key, target_key in optional_map.items():
        if coordinate.get(source_key) is not None:
            coord_out[target_key] = coordinate[source_key]
    # ``resolution_degrees`` is a degree-specific echo of the generic
    # ``step_size`` above (TCKDB's CoordinateUnit is only {angstrom,
    # degree} — a bond/cartesian scan's step is in Angstrom, not
    # degrees). Only stamp it when the coordinate's own unit says
    # degree; otherwise a distance-kind scan's Angstrom step would land
    # in a field named degrees. Unreachable from
    # real ARC output today — ARC's rotor-scan writer only ever emits
    # dihedral/degree scans, and its schema pins both as consts — but
    # this function also serves hand-written/third-party output.yml,
    # which evidence.py does not validate against that schema.
    if (
        coordinate.get("requested_step_size") is not None
        and str(coordinate.get("unit") or "").strip().lower() == "degree"
    ):
        coord_out["resolution_degrees"] = coordinate["requested_step_size"]

    points: list[dict[str, Any]] = []
    for point_index, sample in enumerate(samples, start=1):
        if not isinstance(sample, Mapping) or sample.get("angle_degrees") is None:
            return None
        point: dict[str, Any] = {
            "point_index": point_index,
            "coordinate_values": [{
                "coordinate_index": 1,
                "coordinate_value": float(sample["angle_degrees"]),
                "value_unit": str(coordinate.get("unit") or "degree"),
            }],
        }
        for key in ("electronic_energy_hartree", "relative_energy_kj_mol"):
            if sample.get(key) is not None:
                point[key] = float(sample[key])
        geometry = _normalize_xyz_text(sample.get("geometry_xyz"), f"scan_point_{point_index}")
        if geometry is not None:
            # The sample's own stated isotopes (1.3 ``geometry_isotopes``). A sample whose scan
            # log states none (a Gaussian opt-type scan prints no masses) is sent bare under a
            # substituted species: ``_withhold_unlabelled_point_geometries`` labels it from the
            # structure the scan started from (order-preserving program, same element sequence) or
            # drops it with a warning. A sample that states a list that contradicts the record is
            # left out here.
            stated = sample.get("geometry_isotopes")
            payload = (
                {"xyz_text": geometry}
                if record is None or (stated is None and _isotopic_substitutions(record))
                else _geometry_payload(record, geometry, stated))
            if payload is not None:
                point["geometry"] = payload
        points.append(point)
    out: dict[str, Any] = {
        "dimension": int(result.get("dimension", 1)),
        "is_relaxed": bool(result.get("relaxed", True)),
        "coordinates": [coord_out],
        "points": points,
    }
    if result.get("zero_energy_reference_hartree") is not None:
        out["zero_energy_reference_hartree"] = float(result["zero_energy_reference_hartree"])
    return out


_W_FREQ_SCALE_FACTOR_FITTED_FOR_OTHER_LEVEL = ArcWarning.FREQ_SCALE_FACTOR_FITTED_FOR_OTHER_LEVEL.value


def _build_freq_scale_factor_ref(
    output_doc: Mapping[str, Any],
    *,
    workflow_tool_release: Mapping[str, Any] | None,
    record: Mapping[str, Any] | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Build a ``FreqScaleFactorRef``-shaped dict, or ``None``.

    Maps ARC run-level frequency-scale metadata to the unified TCKDB
    reference shape. Returns ``None`` when ARC didn't apply a scale
    factor for this run, or when the level of theory's ``method``
    isn't available — ``LevelOfTheoryRef.method`` is required by the
    schema, and emitting a ref with a missing method would fail
    server-side validation.

    Source-attribution policy (from
    ``FreqScaleFactorRef`` docstring + ARC's audit):

    * ``value`` ← ``output_doc['freq_scale_factor']``
    * ``level_of_theory`` ← ``freq_level`` (preferred — actual freq LOT),
      falling back to ``arkane_level_of_theory`` when freq_level is
      absent (common single-LOT runs).
    * ``software`` ← ``freq_level.software`` (or fallback chain to opt).
    * ``scale_kind`` ← not sent (adapter 0.6.0, maintainer decision).
      ARC does not state what kind of factor it applied: its
      ``data/freq_scale_factors.yml`` mixes CCCBDB fundamental factors
      and ZPE x 1.014 harmonic ones. The field is optional, so TCKDB
      applies its own default instead of the adapter claiming one.
    * ``note`` ← ``freq_scale_factor_source`` when present (it's a bare
      citation/URL string, not structured literature).
    * ``source_literature`` ← always ``None``. The schema explicitly
      forbids synthesizing literature rows from raw citation strings.
    * ``workflow_tool_release`` ← ARC release **only when ARC's
      curated data file was the proximate source** (i.e., the source
      string is non-null). When the user supplied the factor directly,
      omit ``workflow_tool_release`` — claiming ARC's release would
      fork the dedupe identity tuple ``(level, software, scale_kind,
      value, source_literature, workflow_tool_release)`` and create
      duplicate registry rows.
    """
    value = output_doc.get("freq_scale_factor")
    if value is None:
        return None
    try:
        value_f = float(value)
    except (TypeError, ValueError):
        logger.warning(
            "TCKDB statmech: malformed freq_scale_factor=%r; omitting FSF ref.",
            value,
        )
        return None
    if value_f <= 0:
        logger.warning(
            "TCKDB statmech: freq_scale_factor=%r is non-positive; omitting "
            "FSF ref (server requires gt 0).", value,
        )
        return None

    # Prefer freq_level, fall back to arkane_level_of_theory. Don't
    # fall further back to opt_level — opt and freq levels are
    # genuinely independent in some workflows, and an FSF mislabeled
    # against opt_level would dedupe incorrectly.
    level_source = output_doc.get("freq_level") or output_doc.get("arkane_level_of_theory")
    level_of_theory = _arc_level_to_tckdb_lot(level_source)
    # Arkane applies the single run-wide ``frequencyScaleFactor`` to every species,
    # so the factor was applied to this record whatever its ``levels.freq``; its
    # ``FreqScaleFactorRef.level_of_theory`` is the level the factor belongs to
    # (the header ``freq_level``). Under ``adaptive_levels`` a record whose
    # frequencies are from another level is still sent with the header level, and
    # reported.
    recorded = arc13.recorded_level(record, "freq") if record is not None else None
    # Level identity (the TCKDB projection), not job identity: the factor belongs to a level of theory.
    other_level = (
        recorded is not None and recorded.level is not None
        and isinstance(output_doc.get("freq_level"), Mapping)
        and _arc_level_to_tckdb_lot(recorded.level) != _arc_level_to_tckdb_lot(output_doc["freq_level"])
    )
    if other_level:
        message = (
            f"The run-wide frequency scale factor (fitted for header freq_level "
            f"{_describe_level(output_doc['freq_level'])}) was applied by Arkane to "
            f"{record.get('label')!r}, whose frequencies are from "
            f"{_describe_level(recorded.level)} (levels.freq). It is attached with its own level."
        )
        logger.warning("TCKDB %s: %s", _W_FREQ_SCALE_FACTOR_FITTED_FOR_OTHER_LEVEL, message)
        if warnings is not None:
            warnings.append(_self_check(
                _W_FREQ_SCALE_FACTOR_FITTED_FOR_OTHER_LEVEL, message, "statmech.freq_scale_factor",
                {"action": "freq_scale_factor_sent_with_header_level"}))
    if level_of_theory is None:
        logger.debug(
            "TCKDB statmech: freq/arkane level missing or has no 'method'; "
            "cannot build FreqScaleFactorRef.level_of_theory.",
        )
        return None

    ref: dict[str, Any] = {
        "level_of_theory": level_of_theory,
        "value": value_f,
    }

    # Software comes from freq_level when present; otherwise fall back
    # to opt_level's software (matches the existing ess_versions
    # fallback in _calculation_payload — opt and freq usually share an
    # ESS in practice).
    freq_software = (
        (level_source.get("software") if isinstance(level_source, Mapping) else None)
        or _opt_level_software(output_doc)
    )
    # Output.yml 1.3 states the program that ran the record's frequency job.
    if not other_level and recorded is not None and recorded.level is not None and recorded.job_key and isinstance(
            record.get("ess_software"), Mapping) and record["ess_software"].get(recorded.job_key):
        freq_software = record["ess_software"][recorded.job_key]
    if freq_software:
        ref["software"] = {"name": str(freq_software)}

    source_string = output_doc.get("freq_scale_factor_source")
    if source_string:
        # Bare citation string lands in note. Never synthesized into
        # source_literature.
        ref["note"] = str(source_string)
        # Only tag ARC as the proximate source when our data file was
        # actually the lookup origin (source_string non-null implies
        # _resolve_freq_scale_factor_source matched a row in
        # data/freq_scale_factors.yml). User-supplied factors leave
        # workflow_tool_release null to avoid forking the registry.
        if workflow_tool_release is not None:
            ref["workflow_tool_release"] = dict(workflow_tool_release)

    return ref


# TCKDB ``RigidRotorKind`` enum values (live as of this writing — see
# ``app/db/models/common.py``). Output 1.3 classifies the kind from the principal
# moments of the exported geometry (``linear``, ``symmetric_top``,
# ``spherical_top``, ``asymmetric_top``; ``atom`` never reaches output.yml, and
# ``null`` where it cannot be classified) and the adapter sends it as stated.
# Before 1.3 ARC emitted only ``atom`` / ``linear`` / ``asymmetric_top``, which
# mislabelled every symmetric top.
_TCKDB_RIGID_ROTOR_KINDS = frozenset({
    "atom", "linear", "spherical_top", "symmetric_top", "asymmetric_top",
})

# TCKDB ``TorsionTreatmentKind`` values that ARC produces today. The
# remaining values (``rigid_top``, ``hindered_rotor_dos``) aren't in
# ARC's lexicon, so they're silently dropped per spec rather than
# guessed at.
_ARC_TO_TCKDB_TORSION_TREATMENTS = frozenset({"free_rotor", "hindered_rotor"})


# Which statmech-block fields this adapter can emit, keyed by the
# *root* model the block is destined for. Unlike thermo, both statmech
# roots currently accept the full set the builder below is capable of
# producing:
#   - "StatmechInBundle" is the computed-SPECIES bundle root.
#   - "BundleStatmechIn" is the computed-REACTION per-species root.
# ``StatmechInBundle`` additionally accepts species-only fields this
# builder never emits today (``literature``, ``rotational_constant_a
# /b/c_cm1``, ``software_release``, ``workflow_tool_release``) — see
# ``ComputedSpeciesUploadRequest``'s schema. That means today's parity
# is incidental, not structural: adding an emission for any of those
# fields without also updating this allow-list (and gating it on
# ``target_model``, the same way ``_build_thermo_block`` gates
# ``source_calculations``) would silently 422 every computed-reaction
# upload that hits it — exactly the bug class this file's thermo fix
# addresses. The final assert below turns that into a loud failure
# instead. Kept honest against schema drift by
# ``tests/test_shared_builder_field_sets.py``, which imports the real
# ``tckdb_schemas`` models and asserts these sets are subsets of
# ``<Model>.model_fields``.
_STATMECH_FIELDS_BY_TARGET: dict[str, frozenset[str]] = {
    "StatmechInBundle": frozenset({
        "freq_scale_factor", "external_symmetry", "optical_isomers",
        "is_linear", "rigid_rotor_kind", "statmech_treatment",
        "torsions", "point_group", "source_calculations",
        "energy_level_of_theory",
    }),
    "BundleStatmechIn": frozenset({
        "freq_scale_factor", "external_symmetry", "optical_isomers",
        "is_linear", "rigid_rotor_kind", "statmech_treatment",
        "torsions", "point_group", "source_calculations",
        "energy_level_of_theory",
    }),
    # The conformer upload's nested statmech (ConformerUploadRequest.statmech).
    # Its torsions (StatmechTorsionIn) carry ``invalidated_reason``, the home of
    # ARC's rejected rotors, as the bundle torsions have since tckdb-schemas 0.61.
    "ConformerUploadStatmechPayload": frozenset({
        "freq_scale_factor", "external_symmetry", "optical_isomers",
        "is_linear", "rigid_rotor_kind", "statmech_treatment",
        "torsions", "point_group", "source_calculations",
        "energy_level_of_theory",
    }),
}


_W_TORSION_SCAN_NOT_BUILT = ArcWarning.TORSION_SCAN_NOT_BUILT.value
_W_STATMECH_TREATMENT_NOT_STATED = ArcWarning.STATMECH_TREATMENT_NOT_STATED.value


def _build_statmech_block_for_species(
    *,
    output_doc: Mapping[str, Any],
    species_record: Mapping[str, Any] | None = None,
    calc_keys_by_role: Mapping[str, str],
    workflow_tool_release: Mapping[str, Any] | None,
    target_model: Literal[
        "StatmechInBundle", "BundleStatmechIn", "ConformerUploadStatmechPayload"],
    scan_key_renames: Mapping[str, str] | None = None,
    unbuilt_scans: Mapping[str, str] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    warning_field: str = "statmech",
    freq_hessian_available: bool = False,
    energy_declaration: tuple[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Build a ``target_model``-shaped statmech dict, or ``None``.

    A rotor-aware ``statmech_treatment`` and each torsion's ``treatment_kind``
    are sent only when ``freq_hessian_available``; plain ``rrho`` (no rotors) is
    sent regardless. The reasoning:
    Arkane ignores every rotor when the frequency log has no force-constant
    matrix and runs the species as RRHO (RMG-Py ``arkane/statmech.py``
    647-667), so ARC's rotor list does not show which treatment was applied.
    Without Hessian evidence the field is omitted (TCKDB: "an absent field is
    honest where an invented one would not be") and
    ``statmech_treatment_not_stated`` is reported. The default is False, so a
    caller that has not looked never claims a treatment.

    ``unbuilt_scans`` maps each scan ARC exported (``rotor_scans``) that the
    caller could not build to the reason. A torsion naming one keeps its
    summary but loses ``source_scan_calculation_key``, which would dangle
    and make TCKDB refuse the whole upload; each dropped link is appended
    to ``warnings`` as ``torsion_scan_not_built``. Without the map (a
    direct call), scans are assumed unbuilt only when the run records no
    ``scan_level``. A reference to a scan ARC never exported is left as is,
    so it still reaches validation.

    Pulls per-species statmech metadata from
    ``species_record['statmech']`` (the dict that ``arc/output.py::
    _statmech_to_dict`` writes into ``output.yml``) and projects it onto
    the upload schema. The frequency-scale-factor handling is unchanged.

    ``energy_declaration`` is ``(role, level)`` from
    ``_energy_level_declaration``; it is sent as ``energy_level_of_theory``
    only when that role is among the block's source links, which TCKDB checks
    the declaration against.

    Every target adds ARC's rejected rotors (``statmech.rejected_torsions``) as
    torsions carrying ``invalidated_reason`` (the bundle torsion models have it
    since tckdb-schemas 0.61). See ``_build_rejected_torsions``.

    Both bundle endpoints (``StatmechInBundle`` for computed-species,
    ``BundleStatmechIn`` for computed-reaction per-species) currently
    accept the same field set this builder is capable of producing —
    see ``_STATMECH_FIELDS_BY_TARGET`` for why that's incidental rather
    than guaranteed, and why ``target_model`` is required here anyway.

    ``calc_keys_by_role`` is the role-to-bundle-local-key map for the
    *owning* species block. Computed-species passes the unscoped keys
    (``{"opt": "opt", ...}``); computed-reaction passes the species-
    scoped equivalents (``{"opt": "r0_opt", ...}``). The helper does
    not synthesize keys — it only writes a source_calculation entry per
    role that the caller declared.

    Returns ``None`` when no field survives filtering — emitting an
    empty statmech container would just create a useless server-side
    row. Per the project convention, no empty containers.
    """
    if target_model not in _STATMECH_FIELDS_BY_TARGET:
        raise ValueError(
            f"_build_statmech_block_for_species: unknown "
            f"target_model={target_model!r}; expected one of "
            f"{sorted(_STATMECH_FIELDS_BY_TARGET)}"
        )
    block: dict[str, Any] = {}

    fsf_ref = _build_freq_scale_factor_ref(
        output_doc, workflow_tool_release=workflow_tool_release,
        record=species_record, warnings=warnings,
    )
    if fsf_ref is not None:
        block["freq_scale_factor"] = fsf_ref

    statmech_input = (
        species_record.get("statmech") if isinstance(species_record, Mapping) else None
    )
    if isinstance(statmech_input, Mapping):
        external_symmetry = statmech_input.get("external_symmetry")
        if isinstance(external_symmetry, int) and external_symmetry >= 1:
            block["external_symmetry"] = external_symmetry

        optical_isomers = statmech_input.get("optical_isomers")
        if isinstance(optical_isomers, int) and optical_isomers >= 1:
            block["optical_isomers"] = optical_isomers

        is_linear = statmech_input.get("is_linear")
        if isinstance(is_linear, bool):
            block["is_linear"] = is_linear

        rotor_kind = statmech_input.get("rigid_rotor_kind")
        if isinstance(rotor_kind, str) and rotor_kind in _TCKDB_RIGID_ROTOR_KINDS:
            block["rigid_rotor_kind"] = rotor_kind

        torsions_input = statmech_input.get("torsions")
        # Build first, then classify from what survived. The treatment names
        # the rotors the record actually lists, and the builder drops rotors
        # this adapter cannot represent, so classifying from ARC's raw input
        # can claim a rotor-aware treatment the payload has no torsions to
        # support -- which TCKDB refuses as self-contradictory.
        # Only remove links to well-formed exported scans that were not
        # built (for any reason: no scan_level, a level without software,
        # ...). Unknown/malformed references still reach schema validation,
        # rather than being silently repaired.
        exported_scan_keys = {
            entry["key"] for entry in _scan_entries_from_record(species_record)
            if entry.get("type") == _CALC_KEY_SCAN
            and isinstance(entry.get("key"), str) and entry["key"]
            and isinstance(entry.get("scan_result"), Mapping)
        } if isinstance(species_record, Mapping) else set()
        if unbuilt_scans is None:
            unbuilt_scans = (
                {key: "no scan_level recorded" for key in exported_scan_keys}
                if _resolve_level(output_doc, "scan") is None else {}
            )
        omitted_scan_keys = {k: v for k, v in unbuilt_scans.items() if k in exported_scan_keys}
        dropped_links: list[tuple[int, str]] = []
        is_conformer_root = target_model == "ConformerUploadStatmechPayload"
        without_coordinates: list[int] = []
        schema_1_3 = _is_output_schema_1_3_or_later(output_doc)
        slim_torsions = _build_slim_torsions(
            torsions_input, scan_key_renames=scan_key_renames,
            omitted_scan_keys=set(omitted_scan_keys), dropped_links=dropped_links,
            require_coordinates=is_conformer_root,
            dropped_without_coordinates=without_coordinates,
            schema_1_3=schema_1_3,
        )
        if not is_conformer_root:
            # The bundle roots accept a torsion without coordinates, but it then
            # says nothing about which dihedral it is; report it.
            for torsion in slim_torsions:
                if "coordinates" in torsion:
                    continue
                message = (
                    f"Torsion #{torsion['torsion_index']} is sent without dihedral "
                    f"coordinates: ARC's atom_indices for it are missing or unusable."
                )
                logger.warning("TCKDB %s: %s: %s", warning_field,
                               _W_TORSION_WITHOUT_COORDINATES, message)
                if warnings is not None:
                    warnings.append(_self_check(
                        _W_TORSION_WITHOUT_COORDINATES, message, f"{warning_field}.torsions",
                        {"action": "torsion_sent_without_coordinates", "torsion_index": torsion["torsion_index"]}))
        for position in without_coordinates:
            message = (
                f"Torsion #{position} has no usable atom_indices, and the conformer "
                f"upload refuses a torsion without its dihedral coordinates, so it "
                f"is not sent."
            )
            logger.warning("TCKDB %s: %s: %s", warning_field, _W_TORSION_NOT_SENT, message)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_TORSION_NOT_SENT, message, f"{warning_field}.torsions",
                    {"action": "torsion_omitted", "torsion_position": position, "reason": "atom_indices_unusable"}))
        for position, scan_key in dropped_links:
            reason = omitted_scan_keys[scan_key]
            message = (
                f"Torsion #{position} names scan {scan_key!r}, which ARC exported "
                f"but the adapter could not build ({reason}); the torsion is kept "
                f"without source_scan_calculation_key, which would otherwise name "
                f"an undeclared calculation and refuse the whole upload."
            )
            logger.warning("TCKDB %s: %s: %s", warning_field, _W_TORSION_SCAN_NOT_BUILT, message)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_TORSION_SCAN_NOT_BUILT, message, warning_field,
                    {"action": "torsion_scan_link_omitted", "scan_key": scan_key, "reason": reason}))
        if schema_1_3:
            # Output 1.3 states the treatment Arkane applied, read from its own
            # ``output.py``, so neither ARC's rotor list nor the presence of a
            # force-constant matrix needs to stand in for it (the pre-1.3
            # inference below is retired for these documents).
            treatment = _stated_arkane_treatment(
                statmech_input.get("arkane_treatment"),
                torsions_sent=len(slim_torsions),
                warnings=warnings, warning_field=warning_field,
            )
            if treatment is not None:
                block["statmech_treatment"] = treatment
        else:
            treatment = _classify_statmech_treatment(
                torsions_input, emitted_torsions=slim_torsions,
            )
        # ``rrho`` (an empty rotor list) does not depend on the Hessian: Arkane
        # runs plain RRHO with no rotors whether or not it has a force-constant
        # matrix. Only rotor-aware treatments, and each torsion's own
        # ``treatment_kind`` ('hindered_rotor'), claim what Arkane did with the
        # rotors, and it discards them all without a Hessian.
        no_hessian = not freq_hessian_available and not schema_1_3
        withhold_treatment = (
            treatment is not None and treatment != "rrho" and no_hessian
        )
        strip_torsion_kinds = no_hessian and bool(slim_torsions)
        if treatment is not None and not withhold_treatment and not schema_1_3:
            block["statmech_treatment"] = treatment
        if strip_torsion_kinds:
            slim_torsions = [
                {k: v for k, v in t.items() if k != "treatment_kind"}
                for t in slim_torsions
            ]
        if withhold_treatment or strip_torsion_kinds:
            omitted = []
            if withhold_treatment:
                omitted.append("statmech_treatment")
            if strip_torsion_kinds:
                omitted.append("torsions[].treatment_kind")
            message = (
                f"No force-constant matrix (freq Hessian) was found for this "
                f"species, and Arkane ignores every rotor without one, so ARC's "
                f"rotor list does not show what treatment Arkane applied. Omitted: "
                f"{', '.join(omitted)}"
                + (f" (ARC's rotors would give {treatment!r})" if withhold_treatment else "")
                + f". The {len(slim_torsions)} torsion(s) ARC recorded are still "
                f"sent, without a treatment."
            )
            logger.warning("TCKDB %s: %s: %s", warning_field,
                           _W_STATMECH_TREATMENT_NOT_STATED, message)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_STATMECH_TREATMENT_NOT_STATED, message, f"{warning_field}.statmech_treatment",
                    {"action": "statmech_treatment_omitted", "reason": "no_freq_hessian", "inferred_treatment": treatment, "omitted": omitted, "torsion_count": len(slim_torsions)}))

        # After the treatment is classified: a rejected rotor was not treated, so it
        # never counts toward ``statmech_treatment``. tckdb-schemas 0.61 gave the bundle
        # torsion models ``invalidated_reason`` too ("Same field, same meaning and same
        # storage as ``StatmechTorsionIn.invalidated_reason`` on the conformer route"),
        # so every route carries ARC's rejected rotors.
        slim_torsions = [*slim_torsions, *_build_rejected_torsions(
            statmech_input.get("rejected_torsions"),
            first_index=(len(torsions_input) if isinstance(torsions_input, list) else 0) + 1,
            warnings=warnings, warning_field=warning_field,
            # The bundle torsion models have no ``note`` (only ``StatmechTorsionIn`` does).
            with_note=is_conformer_root,
        )]

        if slim_torsions:
            block["torsions"] = slim_torsions

        point_group = statmech_input.get("point_group")
        if isinstance(point_group, str) and point_group.strip():
            block["point_group"] = point_group.strip()

    # Source calculations are provenance for actual statmech metadata,
    # not statmech metadata in themselves — emitting them on an
    # otherwise-empty block produces a "useless container" that the
    # project convention forbids. Only attach them when the block
    # already carries at least one substantive field (FSF or any
    # species-derived field), which preserves the prior behavior of
    # omitting the whole block on FSF-less runs that lack a statmech
    # subdict. The keys are taken straight from ``calc_keys_by_role``;
    # the caller is responsible for passing the correct namespace
    # (unscoped for computed-species, ``r0_*``/``p0_*`` for computed-
    # reaction species blocks) so the server-side ownership check sees
    # only calculations owned by the same species entry.
    if block:
        sources = _build_statmech_source_calculations(
            calc_keys_by_role=calc_keys_by_role,
        )
        if sources:
            block["source_calculations"] = sources
            if energy_declaration is not None and any(
                    link["role"] == energy_declaration[0] for link in sources):
                block["energy_level_of_theory"] = dict(energy_declaration[1])

    if not block:
        return None

    # Belt-and-suspenders (see ``_STATMECH_FIELDS_BY_TARGET``'s
    # docstring): fail loudly here, at build time, rather than shipping
    # a field the target root's extra="forbid" would reject as a 422. A
    # bare ``assert`` would vanish under ``python -O``, precisely when a
    # production guard is wanted most, so this is a real ``raise``.
    disallowed = set(block) - _STATMECH_FIELDS_BY_TARGET[target_model]
    if disallowed:
        raise ValueError(
            f"_build_statmech_block_for_species emitted field(s) not "
            f"accepted by {target_model}: {sorted(disallowed)}"
        )
    return block


_ARKANE_TREATMENTS = frozenset({"rrho", "rrho_1d", "rrho_nd", "rrho_1d_nd"})


def _stated_arkane_treatment(
    stated: Any,
    *,
    torsions_sent: int,
    warnings: list[dict[str, Any]] | None,
    warning_field: str,
) -> str | None:
    """The ``statmech_treatment`` for an output 1.3 record's ``arkane_treatment``.

    ARC derives ``arkane_treatment`` from the rotor modes in the ``output.py``
    Arkane wrote, so it is what Arkane applied (``rrho`` when it dropped every
    rotor), and ``null`` when that output was not parsed or holds a rotor mode of
    unknown kind. ``null`` is never filled in from ARC's own rotor list. A
    rotor-aware treatment is *defined* by the rotors it treats, so TCKDB refuses
    one that lists no torsion: it is withheld, with a warning, when none of the
    record's torsions is sent (for example, the conformer upload drops a torsion
    without usable ``atom_indices``).
    """
    reason: str | None = None
    if stated is None:
        reason = "arkane_treatment_not_recorded"
    elif stated not in _ARKANE_TREATMENTS:
        reason = "arkane_treatment_unrecognized"
    elif stated != "rrho" and not torsions_sent:
        reason = "no_torsion_sent_for_rotor_treatment"
    if reason is None:
        return str(stated)
    explanation = {
        "arkane_treatment_not_recorded": (
            "ARC did not record what treatment Arkane applied (its output was not "
            "parsed, or holds a rotor mode of unknown kind), so none is stated."),
        "arkane_treatment_unrecognized": (
            f"ARC states arkane_treatment={stated!r}, which is not a treatment "
            f"this adapter sends, so none is stated."),
        "no_torsion_sent_for_rotor_treatment": (
            f"ARC states arkane_treatment={stated!r}, but no torsion of the record "
            f"is sent, and TCKDB refuses a rotor-aware treatment that lists no "
            f"rotor, so none is stated."),
    }[reason]
    message = f"statmech_treatment is omitted: {explanation}"
    logger.warning("TCKDB %s: %s: %s", warning_field, _W_STATMECH_TREATMENT_NOT_STATED, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_STATMECH_TREATMENT_NOT_STATED, message, f"{warning_field}.statmech_treatment",
            {"action": "statmech_treatment_omitted", "reason": reason, "arkane_treatment": stated if isinstance(stated, str) else None, "torsion_count": torsions_sent}))
    return None


def _build_slim_torsions(
    torsions: Any,
    *,
    scan_key_renames: Mapping[str, str] | None = None,
    omitted_scan_keys: set[str] | None = None,
    dropped_links: list[tuple[int, str]] | None = None,
    require_coordinates: bool = False,
    dropped_without_coordinates: list[int] | None = None,
    schema_1_3: bool = False,
) -> list[dict[str, Any]]:
    """Build ``BundleStatmechTorsionIn`` entries, with coordinate quartets when available.

    ``schema_1_3`` (an output.yml 1.3 document): a torsion's ``treatment`` is
    nullable there (``null`` where Arkane's own output does not say what it did
    with the rotor, and never defaulted), so a torsion without one is kept and
    sent without ``treatment_kind`` rather than dropped. Before 1.3 a torsion
    without a recognised treatment is omitted.

    ``require_coordinates`` is for the conformer upload's ``StatmechTorsionIn``,
    which unlike the bundle models refuses a torsion whose coordinate count is
    not its dimension (a torsion with none included): a rotor without usable
    ``atom_indices`` is then left out, its position appended to
    ``dropped_without_coordinates``, rather than sent to be refused.

    ARC's per-rotor dict (built by ``_get_torsions`` in ``arc/output.py``)
    carries:

      * ``symmetry_number`` (int)
      * ``treatment`` (``"hindered_rotor"`` / ``"free_rotor"``)
      * ``atom_indices`` (1-based 4-int dihedral defining atom quartet,
        or for ND scans a list of 4-int lists)
      * ``pivot_atoms`` (1-based 2-int axis — bundle has no column)
      * ``barrier_kj_mol`` (fitted barrier — bundle has no column)

    The bundle ``StatmechTorsionInBundle`` schema accepts:

      * ``torsion_index`` (1-based, allocated by emission order)
      * ``symmetry_number``
      * ``treatment_kind``
      * ``dimension`` (default 1)
      * ``coordinates`` (list of ``StatmechTorsionCoordinateIn`` —
        each with ``coordinate_index`` plus ``atom1_index``..``atom4_index``,
        all 1-based; the four atoms must be distinct)
      * ``source_scan_calculation_key`` (optional, must resolve to a
        bundle-local calc of type ``scan`` — deferred until ARC emits
        scan calcs)

    Coordinate emission rules:

      * 1D rotors with a flat 4-int ``atom_indices`` → emit one
        coordinate, ``dimension=1``.
      * ND rotors with a list-of-lists ``atom_indices`` → emit one
        coordinate per inner list, ``dimension=N``. The bundle schema
        requires ``len(coordinates) == dimension`` and contiguous
        ``coordinate_index`` values 1..N.
      * Missing or malformed ``atom_indices`` → log a warning and emit
        the summary fields only (no ``coordinates``, no ``dimension``
        override). Producers must never fabricate atom quartets.

    Rotors whose treatment isn't a recognized TCKDB value (e.g. ARC
    might add new types in the future) are omitted entirely rather than
    emitted with a missing treatment_kind — the latter would produce a
    torsion entry that's effectively meaningless to consumers.

    ``pivot_atoms`` and ``barrier_kj_mol`` are deliberately not emitted:
    the bundle schema rejects ``pivot_atoms`` and has no destination
    column for the fitted barrier. Both stay out of the payload.

    ``torsion_index`` is the entry's 1-based *position in this record's
    own ``torsions`` argument* (i.e. ``statmech.torsions[]`` as it
    appears in ``output.yml``), not a counter of how many torsions this
    function has emitted so far. Positional index vs. emission-order
    counter is the whole difference this function makes: skipping an
    unrecognized-treatment entry leaves a gap in ``torsion_index``
    rather than shifting every later entry's index down by one. TCKDB
    only requires ``torsion_index`` to be unique within the record, not
    contiguous (``BundleStatmechIn.validate_unique_torsion_indices`` /
    ``StatmechTorsionInBundle.validate_unique_torsion_indices``, plus a
    DB ``UniqueConstraint(statmech_id, torsion_index)`` with no
    contiguity requirement), so the gap is valid, not a defect.

    This does **not** restore joinability to ARC's ``rotors_dict``
    ordinal numbering, and should not be described that way:
    ``statmech.torsions[]`` is itself already compacted relative to
    ``rotors_dict`` before it ever reaches the adapter —
    ``_get_torsions`` (``ARC:arc/output.py:1435-1438``) iterates
    ``spc.rotors_dict.items()`` and skips any rotor whose
    ``success is not True``, so a rotor dropped for that reason leaves
    no trace (no gap, no placeholder) in ``output.yml`` at all, and ARC
    exports no raw rotor ordinal alongside it. ``torsion_index`` is
    therefore only ever a stable, unique reference into *this record's
    own* torsions list as ARC wrote it — a real and sufficient property
    for TCKDB's uniqueness rule, but not a restored cross-reference to
    ARC's internal rotor numbering, which the adapter has no way to
    recover.
    """
    if not isinstance(torsions, list):
        return []
    out: list[dict[str, Any]] = []
    for position, entry in enumerate(torsions, start=1):
        if not isinstance(entry, Mapping):
            continue
        treatment = entry.get("treatment")
        if schema_1_3 and treatment is None:
            slim: dict[str, Any] = {"torsion_index": position}
        elif treatment not in _ARC_TO_TCKDB_TORSION_TREATMENTS:
            continue
        else:
            slim = {"torsion_index": position, "treatment_kind": treatment}
        sym = entry.get("symmetry_number")
        if isinstance(sym, int) and sym >= 1:
            slim["symmetry_number"] = sym

        coordinates = _coerce_torsion_coordinates(entry.get("atom_indices"))
        if coordinates is None and require_coordinates:
            if dropped_without_coordinates is not None:
                dropped_without_coordinates.append(position)
            continue
        if coordinates is not None:
            slim["dimension"] = len(coordinates)
            slim["coordinates"] = coordinates
        else:
            atom_indices = entry.get("atom_indices")
            if atom_indices is not None:
                logger.warning(
                    "TCKDB statmech: torsion #%d has unusable atom_indices=%r; "
                    "emitting torsion summary without coordinates.",
                    position, atom_indices,
                )
        # Link the torsion to its underlying scan calc when ARC provided a
        # bundle-local key. Computed-reaction bundles namespace scan calcs
        # per-species (``r0_scan_rotor_0``, etc.) because the server
        # enforces global calc-key uniqueness across the whole bundle —
        # the caller passes a ``scan_key_renames`` map so the torsion's
        # reference matches the namespaced calc key. Computed-species
        # bundles have a single species and pass ``None``: the original
        # un-prefixed key is already unique.
        scan_key = entry.get("source_scan_key") or entry.get("source_scan_calculation_key")
        if isinstance(scan_key, str) and scan_key and scan_key in (omitted_scan_keys or set()):
            if dropped_links is not None:
                dropped_links.append((position, scan_key))
        elif isinstance(scan_key, str) and scan_key:
            if scan_key_renames is not None:
                scan_key = scan_key_renames.get(scan_key, scan_key)
            slim["source_scan_calculation_key"] = scan_key
        out.append(slim)
    return out


_W_TORSION_NOT_SENT = ArcWarning.TORSION_NOT_SENT.value
_W_TORSION_WITHOUT_COORDINATES = ArcWarning.TORSION_WITHOUT_COORDINATES.value
_W_REJECTED_TORSION_NOT_SENT = ArcWarning.REJECTED_TORSION_NOT_SENT.value
_REJECTED_TORSION_NO_REASON = "ARC rejected this rotor and recorded no reason"


def _build_rejected_torsions(
    rejected: Any,
    *,
    first_index: int,
    warnings: list[dict[str, Any]] | None = None,
    warning_field: str = "statmech",
    with_note: bool = True,
) -> list[dict[str, Any]]:
    """Build ``StatmechTorsionIn`` entries for ARC's ``statmech.rejected_torsions``.

    ``with_note`` is false for the bundle torsion models, which have no ``note``
    (ARC's ``rotor_index`` is then not sent).

    ARC's ``_get_rejected_torsions`` (``arc/output.py``) lists each rotor it
    decided against (``success is False``; pending rotors are not rejections)
    with ``rotor_index``, the free-text ``invalidation_reason`` (verbatim,
    possibly concatenated over troubleshooting rounds, and ``''`` when ARC
    recorded none), ``atom_indices``, ``pivot_atoms``, ``dimension`` and, when
    a scan log exists, ``source_log``. TCKDB's only home for a rejected rotor
    is ``torsions[].invalidated_reason``.

    Each entry carries the reason, the dihedral coordinates when ``atom_indices``
    is well-formed, ``dimension`` as their count, and ARC's ``rotor_index`` in
    ``note``. ARC states no ``treatment_kind`` or ``symmetry_number`` for a
    rejected rotor, so none is sent, and no scan calculation is linked (the
    conformer route accepts no scan calculations and ``source_log`` is a file
    path, not a calculation). An empty reason is sent as the plain statement
    that ARC recorded none, so the torsion still reads as invalidated; the
    reason is never invented. TCKDB refuses a torsion whose coordinate count
    differs from its dimension, so a rotor with unusable ``atom_indices`` is not
    sent, with a ``rejected_torsion_not_sent`` warning.

    ``first_index`` is the first ``torsion_index`` free after the accepted
    rotors' (their index is their position in ARC's ``torsions`` list).
    """
    if not isinstance(rejected, list):
        return []
    out: list[dict[str, Any]] = []
    next_index = first_index
    for entry in rejected:
        if not isinstance(entry, Mapping):
            continue
        rotor_index = entry.get("rotor_index")
        coordinates = _coerce_torsion_coordinates(entry.get("atom_indices"))
        if coordinates is None:
            message = (
                f"ARC rejected rotor {rotor_index!r} but recorded no usable "
                f"atom_indices ({entry.get('atom_indices')!r}); a torsion needs its "
                f"dihedral coordinates, so this rejected rotor is not sent."
            )
            logger.warning("TCKDB %s: %s: %s", warning_field, _W_REJECTED_TORSION_NOT_SENT, message)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_REJECTED_TORSION_NOT_SENT, message, f"{warning_field}.torsions",
                    {"action": "rejected_torsion_omitted", "rotor_index": rotor_index, "reason": "atom_indices_unusable"}))
            continue
        reason = entry.get("invalidation_reason")
        slim: dict[str, Any] = {
            "torsion_index": next_index,
            "dimension": len(coordinates),
            "coordinates": coordinates,
            "invalidated_reason": (
                reason if isinstance(reason, str) and reason.strip()
                else _REJECTED_TORSION_NO_REASON
            ),
        }
        if with_note and isinstance(rotor_index, int) and not isinstance(rotor_index, bool):
            slim["note"] = f"ARC rotor_index {rotor_index}"
        out.append(slim)
        next_index += 1
    return out
