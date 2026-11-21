"""``tckdb_arc.adapter`` keeps every name it has exported, as the object its defining module holds.

The adapter's code lives in sibling modules (``HELPER_MODULES`` in ``_adapter_sources``: every module
``adapter.py`` imports from that is not in ``NOT_ADAPTER_HELPERS``); ``adapter.py`` re-imports each name.
Callers, the CLI and the tests keep reaching them as ``tckdb_arc.adapter.<name>``, so this file pins:

* every name the test suite, the CLI and the other package modules use through ``tckdb_arc.adapter``,
  and every ``_W_*`` warning-code constant, as the object its home module defines;
* every top-level name of each helper module, importable from the adapter as the same object;
* the layering: a helper module imports no module above it and never ``tckdb_arc.adapter``;
* the package logger: every module logs through the one ``tckdb_arc._logging`` logger object;
* the names that tests patch on a module of the adapter are looked up only by code in the modules
  the tests patch them on, since a function in another module would look the name up in its own
  globals and miss the patch.
"""

import ast
import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from _adapter_sources import ADAPTER_SOURCE_FILES, HELPER_MODULES, NOT_ADAPTER_HELPERS, PACKAGE_DIR
from tckdb_arc import adapter

#: Home module of each name used through ``tckdb_arc.adapter``; the value is the attribute name there
#: when it differs from the key (``None`` when it is the same).
USED_NAMES = {
    "tckdb_arc._logging": {
        "logger": None,
    },
    "tckdb_arc.adapter": {
        "TCKDBAdapter": None,
    },
    "tckdb_arc.calculation_payloads": {
        "TSReactionCoordinateNotDesignated": None,
        "_TS_TAU_PROTOCOL_NOT_RECORDED_CM1": None,
        "_W_CALCULATION_INPUT_GEOMETRY_NOT_DECLARED": None,
        "_W_GEOMETRY_ISOTOPES_NOT_STATED": None,
        "_W_LEVEL_CONTRADICTED_BY_ROUTE": None,
        "_W_TS_REACTION_COORDINATE_NOT_DESIGNATED": None,
        "_enforce_geometry_anchoring": None,
        "_freq_result_payload": None,
        "_hessian_geometry": None,
        "_route_contradiction": None,
        "_serialize_calc_constraints": None,
        "_sp_result_payload": None,
        "_withhold_unlabelled_point_geometries": None,
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
    "tckdb_arc.core_aliases": {
        "_REACTION_FLAT_RESULT_FIELDS": None,
        "_REACTION_MODE_FIELDS_HANDLED": None,
        "_REACTION_MODE_FIELDS_NOT_CARRIED": None,
        "_REACTION_RESULT_FIELDS_HANDLED_ELSEWHERE": None,
        "_REACTION_RESULT_FIELDS_NOT_CARRIED": None,
        "_build_nasa_block": None,
        "_build_thermo_points": None,
        "arc_to_tckdb_a_units": None,
        "arc_to_tckdb_ea_units": None,
    },
    "tckdb_arc.finding_warnings": {
        "_W_ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE": None,
        "_W_BAC_TYPE_NOT_STATED": None,
        "_W_COMPOSITE_GEOMETRY_LEVEL_NOT_STATED": None,
        "_W_CONFORMER_LEVEL_NOT_STATED": None,
        "_W_CONFORMER_NOT_ESS_OPTIMIZED": None,
        "_W_CONFORMER_PROGRAM_NOT_STATED": None,
        "_W_IRC_LEVEL_ASSUMED_OPT_LEVEL": None,
        "_W_IRC_LEVEL_NOT_STATED": None,
        "_W_PRIMARY_OPT_PLACEHOLDER_NO_OPT_JOB": None,
        "_W_TS_GUESS_LEVEL_NOT_STATED": None,
        "_W_TS_GUESS_SOFTWARE_NOT_STATED": None,
        "_warn_isotopes_not_stated": None,
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
    "tckdb_arc.kinetics_payloads": {
        "_MAX_ARRHENIUS_T0_K": None,
        "_build_kinetics_block": None,
    },
    "tckdb_arc.level_payloads": {
        "_W_LEVEL_METHOD_IS_COMPOUND": None,
        "_arc_level_to_tckdb_lot": None,
        "_energy_level_declaration": None,
        "_same_arc_job_level": None,
    },
    "tckdb_arc.levels": {
        "_ADAPTIVE_LEVELS_KEY": None,
        "_bac_frequency_level": None,
        "_level_identity": None,
        "_level_keys_match": None,
        "_thermo_energy_level": None,
    },
    "tckdb_arc.path_payloads": {
        "_W_IRC_DIRECTION_NOT_STATED": None,
        "_W_IRC_ENDPOINT_SPECIES_SKIPPED": None,
        "_build_irc_result_payload": None,
        "_build_path_search_result_payload": None,
        "_irc_endpoint_skip": None,
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
    "tckdb_arc.reaction_payloads": {
        "_W_ATOM_MAP_TS_ORDER_NOT_STATED": None,
        "_W_REACTION_SPECIES_LABELS_CONTRADICTED": None,
        "_W_REACTION_STOICHIOMETRY_NOT_STATED": None,
        "_W_REACTION_TS_ATOM_MAP_NOT_SENT": None,
        "_warn_atom_map_not_sent": None,
        "_with_stated_participants": None,
    },
    "tckdb_arc.reactions": {
        "_ts_unmapped_smiles_handle": None,
    },
    "tckdb_arc.self_check": {
        "_self_check": None,
    },
    "tckdb_arc.statmech_payloads": {
        "_STATMECH_FIELDS_BY_TARGET": None,
        "_W_FREQ_SCALE_FACTOR_FITTED_FOR_OTHER_LEVEL": None,
        "_W_REJECTED_TORSION_NOT_SENT": None,
        "_W_STATMECH_TREATMENT_NOT_STATED": None,
        "_W_TORSION_NOT_SENT": None,
        "_W_TORSION_SCAN_NOT_BUILT": None,
        "_W_TORSION_WITHOUT_COORDINATES": None,
        "_build_slim_torsions": None,
        "_build_statmech_block_for_species": None,
        "_neutral_scan_result_to_tckdb": None,
        "_scan_entries_from_record": None,
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
    "tckdb_arc.thermo_payloads": {
        "_THERMO_ENTHALPY_REFERENCE_KIND": None,
        "_THERMO_FIELDS_BY_TARGET": None,
        "_W_ATOM_ENERGY_RECORD_NOT_DEPOSITED_AEC_YML": None,
        "_W_BAC_CORRECTION_OMITTED_COMPONENTS_INCOMPLETE": None,
        "_W_BAC_FREQUENCY_LEVEL_CONFLICT": None,
        "_W_CORRECTION_TABLE_METHOD_SPLIT": None,
        "_W_ENERGY_CORRECTION_SCHEME_SOFTWARE_CONFLICT": None,
        "_W_ENTHALPY_ADAPTIVE_LEVELS_UNVERIFIABLE": None,
        "_W_ENTHALPY_ATOM_CORRECTIONS_NOT_APPLIED": None,
        "_W_ENTHALPY_NOT_FINITE": None,
        "_W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03": None,
        "_W_THERMO_REFERENCE_PRESSURE_NOT_STATED": None,
        "_build_applied_energy_corrections": None,
        "_build_thermo_block": None,
        "_correction_records_from_record": None,
        "_one_electron_hf_substitution": None,
        "_withhold_unreliable_composite_energies": None,
    },
    "tckdb_arc.ts_evidence_payloads": {
        "_W_IRC_PARTICIPANT_MAPPING_NOT_SENT": None,
        "_W_TS_ENERGY_ORDERING_NOT_SENT": None,
        "_W_TS_IMAGINARY_MODE_NOT_SENT": None,
        "_W_TS_IRC_EVIDENCE_WITHOUT_IRC_CALCULATION": None,
        "_W_TS_NMD_FORCED_CONTRADICTS_INDEX": None,
        "_irc_participant_mappings": None,
        "_ts_energy_ordering_validation_evidence": None,
        "_ts_imaginary_mode_validation_evidence": None,
        "_warn_ts_evidence_not_sent": None,
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

#: The names that tests patch on a module of the adapter, each with the modules it is patched on
#: (``mock.patch("tckdb_arc.adapter.<name>")``, ``monkeypatch.setattr(<module>, "<name>", ...)``,
#: ``mock.patch.object(<module>, "<name>")``). Only code in those modules may look a name up as a
#: global, so a patch reaches every caller. ``test_the_patched_names_are_the_ones_the_tests_patch``
#: compares this table with the test sources.
PATCHED_NAMES = {
    "TCKDBAdapter": {"adapter"},
    "_THERMO_ENTHALPY_REFERENCE_KIND": {"thermo_payloads"},
    "_calc_prefix_for_actor": {"adapter"},
    "logger": {"adapter"},
    "_preflight_sleep": {"adapter"},
    "_read_gsm_node_outputs": {"adapter"},
    "_thermo_energy_level": {"adapter", "kinetics_payloads"},
    "_ts_unmapped_smiles_handle": {"adapter"},
}

#: Patched names that modules other than the patched ones look up on purpose. ``logger`` is the one
#: package logger object: replacing the name on ``tckdb_arc.adapter`` redirects the class and the
#: upload pipeline (``TCKDBAdapter._log``) only, while the helper modules log through the object
#: itself, so a test that must see every record patches the object's methods
#: (``test_every_module_logs_through_the_one_package_logger`` pins that they all hold that object).
LOOKED_UP_ELSEWHERE_BY_DESIGN = {"logger"}

#: Patched names that no module looks up as a global, with why. Every other patched name must be looked
#: up by each module it is patched on, so a patch is never a no-op.
NOT_LOOKED_UP_AS_A_GLOBAL = {
    "TCKDBAdapter": "the CLI imports it lazily inside ``main`` (``from tckdb_arc.adapter import TCKDBAdapter``), "
                    "so the patch on the module is read at call time through that import",
    "_read_gsm_node_outputs": "no caller; the patches on it assert nothing",
}

REPO_ROOT = PACKAGE_DIR.parents[1]
TEST_DIRS = (REPO_ROOT / "tckdb_arc" / "tests", REPO_ROOT / "tckdb_core" / "tests")
FAMILY = ("adapter", *HELPER_MODULES)  # the modules a patch on the adapter's code can name


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


def _loads_of(module_name):
    """The names the module's code looks up (a ``Name`` in a load context, anywhere in the file)."""
    tree = ast.parse((PACKAGE_DIR / f"{module_name}.py").read_text(encoding="utf-8"))
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}


def _dotted(node):
    """``a.b.c`` for a chain of names and attributes, else ``None``."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return None


def _module_aliases(tree, family):
    """The local names a source file binds to a module of the adapter: ``{alias: module name}``."""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "tckdb_arc" and not node.level:
            aliases.update({a.asname or a.name: a.name for a in node.names if a.name in family})
        elif isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                if a.asname and len(parts) == 2 and parts[0] == "tckdb_arc" and parts[1] in family:
                    aliases[a.asname] = parts[1]
    return aliases


def scan_patched_names(source, family=FAMILY):
    """``{(module, name)}`` that the test source patches on a module of the adapter.

    Sees ``patch("tckdb_arc.<module>.<name>")`` and ``setattr("tckdb_arc.<module>.<name>", ...)`` by string
    (positional or ``target=``), ``patch.object(<module>, "<name>")`` and ``setattr(<module>, "<name>", ...)``,
    ``patch.multiple(<module>, <name>=...)`` and ``patch.multiple("tckdb_arc.<module>", <name>=...)``, and
    ``<module>.<name> = ...``, where ``<module>`` is an import alias or a ``tckdb_arc.<module>`` attribute chain.
    It cannot see a module reached through ``importlib.import_module`` into a variable, or a ``setattr`` or
    ``patch.object`` with a computed name.
    """
    tree = ast.parse(source)
    aliases = _module_aliases(tree, family)

    def module_of(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            dotted = node.value
        else:
            dotted = _dotted(node)
        if dotted in aliases:
            return aliases[dotted]
        parts = (dotted or "").split(".")
        if len(parts) == 2 and parts[0] == "tckdb_arc" and parts[1] in family:
            return parts[1]
        return None

    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called not in {"patch", "object", "setattr", "multiple"}:
                continue
            keywords = {k.arg: k.value for k in node.keywords if k.arg}
            first = node.args[0] if node.args else keywords.get("target")
            if first is None:
                continue
            if called == "multiple":
                module = module_of(first)
                ignored = {"target", "spec", "create", "spec_set", "autospec", "new_callable"}
                if module:
                    found.update((module, name) for name in keywords if name not in ignored)
                continue
            if called in {"patch", "setattr"} and isinstance(first, ast.Constant) and isinstance(first.value, str):
                match = re.fullmatch(r"tckdb_arc\.(\w+)\.(\w+)", first.value)
                if match and match.group(1) in family:
                    found.add((match.group(1), match.group(2)))
                continue
            module = module_of(first)
            if module is None:
                continue
            second = node.args[1] if len(node.args) > 1 else keywords.get("attribute")
            if isinstance(second, ast.Constant) and isinstance(second.value, str):
                found.add((module, second.value))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and module_of(target.value):
                    found.add((module_of(target.value), target.attr))
    return found


SCANNER_SNIPPETS = {
    "patch by string": ('mock.patch("tckdb_arc.adapter.x")', {("adapter", "x")}),
    "patch by string, target keyword": ('patch(target="tckdb_arc.adapter.z")', {("adapter", "z")}),
    "setattr by string": ('monkeypatch.setattr("tckdb_arc.thermo_payloads.y", 1)', {("thermo_payloads", "y")}),
    "patch.object on an alias": (
        "from tckdb_arc import adapter as A\nmock.patch.object(A, 'w')", {("adapter", "w")}),
    "setattr on an alias": (
        "import tckdb_arc.thermo_payloads as T\nmonkeypatch.setattr(T, 'v', 1)", {("thermo_payloads", "v")}),
    "patch.object on an attribute chain": (
        "import tckdb_arc.kinetics_payloads\nmock.patch.object(tckdb_arc.kinetics_payloads, 'u')",
        {("kinetics_payloads", "u")}),
    "patch.object, attribute keyword": (
        "from tckdb_arc import adapter\nmock.patch.object(adapter, attribute='k')", {("adapter", "k")}),
    "patch.multiple on a module string": (
        'mock.patch.multiple("tckdb_arc.adapter", a=1, b=2, spec=None)', {("adapter", "a"), ("adapter", "b")}),
    "patch.multiple on an alias": (
        "from tckdb_arc import adapter as A\nmock.patch.multiple(A, c=1)", {("adapter", "c")}),
    "assignment on an alias": ("from tckdb_arc import adapter as A\nA.d = 1", {("adapter", "d")}),
    "assignment on an attribute chain": ("import tckdb_arc.adapter\ntckdb_arc.adapter.e = 1", {("adapter", "e")}),
    "a class attribute is not a module attribute": (
        "from tckdb_arc import adapter as A\nmock.patch.object(A.TCKDBAdapter, 'm')", set()),
    "another package is not the adapter": ('mock.patch("tckdb_core.uploader.n")', set()),
    "a module outside the adapter's code": ('mock.patch("tckdb_arc.cli.run_upload_sweep")', set()),
    "blind spot: a module in a variable": (
        "import importlib\nm = importlib.import_module('tckdb_arc.adapter')\nmock.patch.object(m, 'q')", set()),
    "blind spot: a computed name": (
        "from tckdb_arc import adapter as A\nname = 'q'\nmonkeypatch.setattr(A, name, 1)", set()),
}


@pytest.mark.parametrize("snippet", sorted(SCANNER_SNIPPETS))
def test_the_patch_scanner_sees_each_form_it_claims_to(snippet):
    source, expected = SCANNER_SNIPPETS[snippet]
    assert scan_patched_names(source) == expected


def _patched_in_the_tests():
    found = set()
    for directory in TEST_DIRS:
        for path in sorted(directory.rglob("*.py")):
            if path.name != Path(__file__).name:
                found |= scan_patched_names(path.read_text(encoding="utf-8"))
    return found


def test_the_patched_names_are_the_ones_the_tests_patch():
    listed = {(module, name) for name, modules in PATCHED_NAMES.items() for module in modules}
    found = _patched_in_the_tests()
    assert found - listed == set(), f"tests patch names PATCHED_NAMES lacks: {sorted(found - listed)}"
    assert listed - found == set(), f"PATCHED_NAMES lists patches no test makes: {sorted(listed - found)}"


@pytest.mark.parametrize("name", sorted(PATCHED_NAMES))
def test_a_patched_name_is_looked_up_by_the_modules_it_is_patched_on_and_by_no_others(name):
    modules = PATCHED_NAMES[name]
    for module_name in modules:
        assert hasattr(importlib.import_module(f"tckdb_arc.{module_name}"), name), (module_name, name)
    lookers = {module_name for module_name in FAMILY if name in _loads_of(module_name)}
    if name not in LOOKED_UP_ELSEWHERE_BY_DESIGN:
        assert lookers <= modules, (
            f"{name} is looked up by {sorted(lookers - modules)}, which a patch on {sorted(modules)} "
            "does not reach")
    if name in NOT_LOOKED_UP_AS_A_GLOBAL:
        assert not lookers & modules, f"{name} is looked up after all; drop it from NOT_LOOKED_UP_AS_A_GLOBAL"
    else:
        assert modules <= lookers, f"nothing in {sorted(modules - lookers)} looks {name} up, so patching it does nothing"


def test_the_exemption_tables_name_only_patched_names():
    assert LOOKED_UP_ELSEWHERE_BY_DESIGN <= set(PATCHED_NAMES)
    assert set(NOT_LOOKED_UP_AS_A_GLOBAL) <= set(PATCHED_NAMES)


def test_every_module_logs_through_the_one_package_logger():
    from tckdb_arc import _logging

    assert _logging.logger.name == "tckdb_arc" and _logging.get_logger() is _logging.logger
    for module_name in FAMILY:
        text = (PACKAGE_DIR / f"{module_name}.py").read_text(encoding="utf-8")
        assert "getLogger" not in text, f"{module_name} makes its own logger"
        if "logger" in _loads_of(module_name):
            module = importlib.import_module(f"tckdb_arc.{module_name}")
            assert module.logger is _logging.logger, module_name


def test_every_sibling_module_is_a_helper_module_or_named_as_not_one():
    siblings = {path.stem for path in PACKAGE_DIR.glob("*.py")}
    assert not set(NOT_ADAPTER_HELPERS) - siblings, "NOT_ADAPTER_HELPERS names a module that is gone"
    assert siblings - set(NOT_ADAPTER_HELPERS) == set(HELPER_MODULES), (
        "a module is neither imported by adapter.py nor listed in NOT_ADAPTER_HELPERS: "
        f"{sorted(siblings - set(NOT_ADAPTER_HELPERS) - set(HELPER_MODULES))}")
    missing = [path.name for path in ADAPTER_SOURCE_FILES if not path.is_file()]
    assert not missing, f"the source scans would skip {missing}: no such file"


def _run_in_a_fresh_interpreter(code):
    # The child does not inherit pytest's path insertion: give it the packages these tests read.
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        [str(PACKAGE_DIR.parent), str(REPO_ROOT / "tckdb_core"), *sys.path])}
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stdout + done.stderr


_CHILD_CHECKS = (
    "import tckdb_core\n"
    f"assert pathlib.Path({str(REPO_ROOT / 'tckdb_core')!r}) in pathlib.Path(tckdb_core.__file__).resolve().parents, "
    "tckdb_core.__file__\n"
    "bad = sorted(n for n in sys.modules if n.split('.')[0] in ('arc', 'rdkit', 'openbabel'))\n"
    "print(repr(bad))\n"
    "sys.exit(1 if bad else 0)\n"
)


@pytest.mark.parametrize("module_name", ["adapter", "cli", "sweep", *HELPER_MODULES])
def test_each_module_imports_first_in_a_fresh_interpreter_without_arc_or_chemistry_toolkits(module_name):
    """The package ``__init__`` imports the adapter, which would hide a cycle or a missing import in any module."""
    _run_in_a_fresh_interpreter(
        "import importlib, pathlib, sys, types\n"
        "stub = types.ModuleType('tckdb_arc')\n"
        f"stub.__path__ = [{str(PACKAGE_DIR)!r}]\n"
        "sys.modules['tckdb_arc'] = stub\n"
        f"module = importlib.import_module('tckdb_arc.{module_name}')\n"
        f"assert pathlib.Path(module.__file__).resolve().parent == pathlib.Path({str(PACKAGE_DIR)!r}), module.__file__\n"
        + _CHILD_CHECKS)


def test_the_package_imports_from_this_checkout_in_a_fresh_interpreter():
    _run_in_a_fresh_interpreter(
        "import pathlib, sys, tckdb_arc\n"
        f"assert pathlib.Path({str(PACKAGE_DIR)!r}) in pathlib.Path(tckdb_arc.__file__).resolve().parents, tckdb_arc.__file__\n"
        + _CHILD_CHECKS)
