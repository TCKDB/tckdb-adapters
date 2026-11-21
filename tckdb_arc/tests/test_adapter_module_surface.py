"""``tckdb_arc.adapter`` keeps every name it has exported, as the object its defining module holds.

The adapter's helpers live in sibling modules (``geometry``, ``levels``, ``calculations``,
``path_search``, ``thermo``, ``provenance``, ``statmech``, ``kinetics``, ``reactions``,
``self_check``); ``adapter.py`` re-imports each name. Callers, the CLI and the tests keep reaching
them as ``tckdb_arc.adapter.<name>``, so this file pins:

* every name the test suite, the CLI and the other package modules use through ``tckdb_arc.adapter``,
  and every ``_W_*`` warning-code constant, as the object its home module defines;
* every top-level name of each helper module, importable from the adapter as the same object;
* the layering: a helper module imports no module above it and never ``tckdb_arc.adapter``;
* the names that tests patch on ``tckdb_arc.adapter`` are looked up only by code in ``adapter.py``,
  since a function in another module would look the name up in its own globals and miss the patch.
"""

import ast
import importlib
import subprocess
import sys

import pytest

from _adapter_sources import ADAPTER_SOURCE_FILES, HELPER_MODULES, PACKAGE_DIR
from tckdb_arc import adapter

#: Home module of each name used through ``tckdb_arc.adapter``; the value is the attribute name there
#: when it differs from the key (``None`` when it is the same).
USED_NAMES = {
    "tckdb_arc._logging": {
        "logger": None,
    },
    "tckdb_arc.adapter": {
        "TCKDBAdapter": None,
        "TSReactionCoordinateNotDesignated": None,
        "_MAX_ARRHENIUS_T0_K": None,
        "_REACTION_FLAT_RESULT_FIELDS": None,
        "_REACTION_MODE_FIELDS_HANDLED": None,
        "_REACTION_MODE_FIELDS_NOT_CARRIED": None,
        "_REACTION_RESULT_FIELDS_HANDLED_ELSEWHERE": None,
        "_REACTION_RESULT_FIELDS_NOT_CARRIED": None,
        "_STATMECH_FIELDS_BY_TARGET": None,
        "_THERMO_ENTHALPY_REFERENCE_KIND": None,
        "_THERMO_FIELDS_BY_TARGET": None,
        "_TS_TAU_PROTOCOL_NOT_RECORDED_CM1": None,
        "_W_ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE": None,
        "_W_ATOM_ENERGY_RECORD_NOT_DEPOSITED_AEC_YML": None,
        "_W_ATOM_MAP_TS_ORDER_NOT_STATED": None,
        "_W_BAC_CORRECTION_OMITTED_COMPONENTS_INCOMPLETE": None,
        "_W_BAC_FREQUENCY_LEVEL_CONFLICT": None,
        "_W_BAC_TYPE_NOT_STATED": None,
        "_W_CALCULATION_INPUT_GEOMETRY_NOT_DECLARED": None,
        "_W_COMPOSITE_GEOMETRY_LEVEL_NOT_STATED": None,
        "_W_CONFORMER_LEVEL_NOT_STATED": None,
        "_W_CONFORMER_NOT_ESS_OPTIMIZED": None,
        "_W_CONFORMER_PROGRAM_NOT_STATED": None,
        "_W_CORRECTION_TABLE_METHOD_SPLIT": None,
        "_W_ENERGY_CORRECTION_SCHEME_SOFTWARE_CONFLICT": None,
        "_W_ENTHALPY_ADAPTIVE_LEVELS_UNVERIFIABLE": None,
        "_W_ENTHALPY_ATOM_CORRECTIONS_NOT_APPLIED": None,
        "_W_ENTHALPY_NOT_FINITE": None,
        "_W_FREQ_SCALE_FACTOR_FITTED_FOR_OTHER_LEVEL": None,
        "_W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03": None,
        "_W_GEOMETRY_ISOTOPES_NOT_STATED": None,
        "_W_IRC_DIRECTION_NOT_STATED": None,
        "_W_IRC_ENDPOINT_SPECIES_SKIPPED": None,
        "_W_IRC_LEVEL_ASSUMED_OPT_LEVEL": None,
        "_W_IRC_LEVEL_NOT_STATED": None,
        "_W_IRC_PARTICIPANT_MAPPING_NOT_SENT": None,
        "_W_LEVEL_CONTRADICTED_BY_ROUTE": None,
        "_W_LEVEL_METHOD_IS_COMPOUND": None,
        "_W_PRIMARY_OPT_PLACEHOLDER_NO_OPT_JOB": None,
        "_W_REACTION_SPECIES_LABELS_CONTRADICTED": None,
        "_W_REACTION_STOICHIOMETRY_NOT_STATED": None,
        "_W_REACTION_TS_ATOM_MAP_NOT_SENT": None,
        "_W_REJECTED_TORSION_NOT_SENT": None,
        "_W_STATMECH_TREATMENT_NOT_STATED": None,
        "_W_THERMO_REFERENCE_PRESSURE_NOT_STATED": None,
        "_W_TORSION_NOT_SENT": None,
        "_W_TORSION_SCAN_NOT_BUILT": None,
        "_W_TORSION_WITHOUT_COORDINATES": None,
        "_W_TS_ENERGY_ORDERING_NOT_SENT": None,
        "_W_TS_GUESS_LEVEL_NOT_STATED": None,
        "_W_TS_GUESS_SOFTWARE_NOT_STATED": None,
        "_W_TS_IMAGINARY_MODE_NOT_SENT": None,
        "_W_TS_IRC_EVIDENCE_WITHOUT_IRC_CALCULATION": None,
        "_W_TS_NMD_FORCED_CONTRADICTS_INDEX": None,
        "_W_TS_REACTION_COORDINATE_NOT_DESIGNATED": None,
        "_arc_level_to_tckdb_lot": None,
        "_build_applied_energy_corrections": None,
        "_build_irc_result_payload": None,
        "_build_kinetics_block": None,
        "_build_nasa_block": None,
        "_build_path_search_result_payload": None,
        "_build_slim_torsions": None,
        "_build_statmech_block_for_species": None,
        "_build_thermo_block": None,
        "_build_thermo_points": None,
        "_correction_records_from_record": None,
        "_energy_level_declaration": None,
        "_enforce_geometry_anchoring": None,
        "_freq_result_payload": None,
        "_hessian_geometry": None,
        "_irc_endpoint_skip": None,
        "_irc_participant_mappings": None,
        "_neutral_scan_result_to_tckdb": None,
        "_one_electron_hf_substitution": None,
        "_route_contradiction": None,
        "_same_arc_job_level": None,
        "_scan_entries_from_record": None,
        "_serialize_calc_constraints": None,
        "_sp_result_payload": None,
        "_ts_energy_ordering_validation_evidence": None,
        "_ts_imaginary_mode_validation_evidence": None,
        "_warn_atom_map_not_sent": None,
        "_warn_isotopes_not_stated": None,
        "_warn_ts_evidence_not_sent": None,
        "_with_stated_participants": None,
        "_withhold_unlabelled_point_geometries": None,
        "_withhold_unreliable_composite_energies": None,
        "arc_to_tckdb_a_units": None,
        "arc_to_tckdb_ea_units": None,
    },
    "tckdb_arc.calculations": {
        "_CONTRADICTIONS_KEY": None,
        "_final_settings_for_calc": None,
        "_hessian_parser_block": None,
        "_merge_parameters_json": None,
        "_opt_result_payload": None,
        "_scf_stability_payload": None,
        "_spin_diagnostic_payload": None,
        "_wavefunction_diagnostic_payload": None,
    },
    "tckdb_arc.geometry": {
        "_formula_element_symbols": None,
        "_normalize_xyz_text": None,
        "_species_element_symbols": None,
        "_xyz_element_symbols": None,
    },
    "tckdb_arc.kinetics": {
        "_kinetics_declarations": None,
        "_kinetics_interpretation_sentence": None,
        "_participant_electron_count": None,
        "_source_levels_state_solvation": None,
    },
    "tckdb_arc.levels": {
        "_ADAPTIVE_LEVELS_KEY": None,
        "_bac_frequency_level": None,
        "_level_identity": None,
        "_level_keys_match": None,
        "_thermo_energy_level": None,
    },
    "tckdb_arc.path_search": {
        "_parse_xtb_turbomole_energy_file": None,
        "_parse_xtb_turbomole_gradient_file": None,
        "_parse_xtb_xtbout_energy": None,
        "_read_gsm_node_outputs": None,
        "_resolve_irc_zero_energy_reference": None,
        "_resolve_ts_guess_path_search": None,
    },
    "tckdb_arc.provenance": {
        "_arc_analysis_software_release": None,
        "_arkane_workflow_tool_release": None,
        "_index_species": None,
        "_is_output_schema_1_0": None,
        "_is_output_schema_1_3_or_later": None,
        "_scheme_data_revisions": None,
    },
    "tckdb_arc.reactions": {
        "_ts_unmapped_smiles_handle": None,
    },
    "tckdb_arc.self_check": {
        "_self_check": None,
    },
    "tckdb_arc.thermo": {
        "_W_ENTHALPY_ATOM_CORRECTIONS_LEVEL_MISMATCH": None,
        "_W_ENTHALPY_ATOM_CORRECTIONS_LEVEL_UNVERIFIABLE": None,
        "_W_ENTHALPY_FORMATION_UNVERIFIABLE_LIGHT_SPECIES": None,
        "_W_ENTHALPY_NOT_FORMATION_MAGNITUDE": None,
        "_g4_loader_energy_unreliable": None,
        "_note_omitted_bac_on_thermo": None,
        "_skipped_bonds_note": None,
    },
    "tckdb_core.constants": {
        "ARTIFACTS_ENDPOINT_TEMPLATE": None,
        "COMPUTED_REACTION_ENDPOINT": None,
        "COMPUTED_REACTION_KIND": None,
        "COMPUTED_SPECIES_ENDPOINT": None,
        "COMPUTED_SPECIES_KIND": None,
        "CONFORMER_UPLOAD_ENDPOINT": None,
        "PAYLOAD_KIND": None,
        "PREFLIGHT_MAX_ATTEMPTS": None,
        "TRANSITION_STATE_ENDPOINT": None,
        "TRANSITION_STATE_KIND": None,
        "VALID_TCKDB_ORIGIN_KINDS": None,
    },
    "tckdb_core.keys": {
        "_calc_prefix_for_actor": None,
    },
    "tckdb_core.outcomes": {
        "ArtifactUploadOutcome": None,
        "TCKDBReadinessError": None,
        "UploadOutcome": None,
        "_PreparedArtifactUpload": None,
    },
    "tckdb_core.reaction_flatten": {
        "_flatten_result_fields": "flatten_result_fields",
    },
    "tckdb_core.software_release": {
        "_split_ess_version_banner": "split_version_banner",
    },
    "tckdb_core.uploader": {
        "_W_CALCULATION_REF_NOT_RETURNED": None,
        "_artifact_batch_bodies": None,
        "_build_readiness_error": None,
        "_coerce_artifact_filename": None,
        "_extract_tckdb_public_refs": None,
        "_preflight_sleep": None,
    },
}

#: ``tckdb_arc.adapter.__all__`` as it has always been.
ADAPTER_ALL = (
    "ARTIFACTS_ENDPOINT_TEMPLATE", "ArtifactUploadOutcome", "COMPUTED_REACTION_ENDPOINT",
    "COMPUTED_REACTION_KIND", "COMPUTED_SPECIES_ENDPOINT", "COMPUTED_SPECIES_KIND",
    "CONFORMER_UPLOAD_ENDPOINT", "PAYLOAD_KIND", "TCKDBAdapter", "TRANSITION_STATE_ENDPOINT",
    "TRANSITION_STATE_KIND", "UploadOutcome", "arc_to_tckdb_a_units", "arc_to_tckdb_ea_units",
)

#: Names that tests patch on ``tckdb_arc.adapter`` (``mock.patch("tckdb_arc.adapter.<name>")``,
#: ``monkeypatch.setattr(adapter_module, "<name>", ...)``). Only code in ``adapter.py`` may look
#: them up as a global, so a patch reaches every caller.
PATCHED_ON_THE_ADAPTER_MODULE = (
    "logger", "_preflight_sleep", "_calc_prefix_for_actor", "_thermo_energy_level",
    "_THERMO_ENTHALPY_REFERENCE_KIND", "_ts_unmapped_smiles_handle", "_read_gsm_node_outputs",
    "TCKDBAdapter",
)


def _top_level_names(path):
    names = []
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names.extend(x.id for t in node.targets for x in ast.walk(t) if isinstance(x, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


def test_every_name_used_through_the_adapter_is_the_object_its_home_module_defines():
    wrong = []
    for home, names in USED_NAMES.items():
        module = importlib.import_module(home)
        for name, attribute in names.items():
            if getattr(adapter, name) is not getattr(module, attribute or name):
                wrong.append(f"{name} is not {home}.{attribute or name}")
    assert not wrong, wrong


def test_adapter_all_is_unchanged():
    assert tuple(adapter.__all__) == ADAPTER_ALL


@pytest.mark.parametrize("module_name", HELPER_MODULES)
def test_the_adapter_re_exports_every_top_level_name_of_a_helper_module(module_name):
    module = importlib.import_module(f"tckdb_arc.{module_name}")
    names = _top_level_names(PACKAGE_DIR / f"{module_name}.py")
    assert names
    missing = [n for n in names if getattr(adapter, n, None) is not getattr(module, n)]
    assert not missing, f"tckdb_arc.adapter does not hold {missing} as tckdb_arc.{module_name} defines them"


@pytest.mark.parametrize("module_name", HELPER_MODULES)
def test_a_helper_module_imports_nothing_above_it(module_name):
    above = {"adapter", "cli", "sweep", *HELPER_MODULES[HELPER_MODULES.index(module_name):]}
    imported = set()
    for node in ast.walk(ast.parse((PACKAGE_DIR / f"{module_name}.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            parts = node.module.split(".")
            if parts[0] == "tckdb_arc":
                imported.add(parts[1] if len(parts) > 1 else None)
                if len(parts) == 1:
                    imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name.split(".")[1] for a in node.names if a.name.startswith("tckdb_arc."))
    assert not imported & above, f"{module_name} imports {sorted(imported & above)}"


def test_names_patched_on_the_adapter_are_looked_up_only_by_adapter_code():
    for module_name in HELPER_MODULES:
        path = PACKAGE_DIR / f"{module_name}.py"
        looked_up = {node.id for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                     if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
        assert not looked_up & set(PATCHED_ON_THE_ADAPTER_MODULE), (
            f"{module_name}.py looks up {sorted(looked_up & set(PATCHED_ON_THE_ADAPTER_MODULE))} as a global; "
            "a patch on tckdb_arc.adapter would not reach it")
    assert PACKAGE_DIR / "adapter.py" in ADAPTER_SOURCE_FILES


def test_the_helper_modules_import_in_a_fresh_interpreter():
    code = "import tckdb_arc.self_check, tckdb_arc.reactions, tckdb_arc.adapter, tckdb_arc.cli"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
