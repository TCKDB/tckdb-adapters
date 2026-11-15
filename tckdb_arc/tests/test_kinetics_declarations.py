"""What a fit ARC's Arkane kinetics run produced declares: direction, determination, applicability.

output.yml states a ``tunneling`` on a ``reactions[].kinetics`` block only for parameters the Arkane
kinetics run fitted (null for user-supplied, restored or non-Arkane kinetics). That stamp is the
adapter's gate: ARC's kinetics run is the high-pressure-limit rate-coefficient job on the reaction as
written, so the block declares

* ``direction: forward``;
* ``determination``: the whole reaction, key scoped to the bundle's ``workflow_tool_release``;
* ``applicability``: a rate coefficient, an elementary one, in the high-pressure limit, in the gas phase
  unless a participant level states a solvent, claimed by the depositor's reading of ARC's contract.

None of these is an output.yml field; the block's ``note`` and ``kinetics_declarations_interpreted`` say
so. Nothing else is claimed (no reaction order or ``protocol``). The selection assessment of TCKDB
is replicated for the four findings these fields clear; see ``_selection_findings``.
"""

import copy

import pytest
from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest

from _contract import contract_validate
from test_arc_schema_1_3_levels import _adapter, _doc13, _reaction_payload
from tckdb_arc import adapter as adapter_module
from tckdb_arc.adapter import _kinetics_declarations

APPLICABILITY = {
    "version": 1,
    "observable": "rate_coefficient",
    "coefficient_basis": "elementary_coefficient",
    "pressure_dependence": "high_pressure_limit",
    "phase": "gas",
    "claim_origin": "depositor_interpretation",
}
KEY_PREFIX = "arkane_kinetics:demo_reaction:"

DEMO_KINETICS = {
    "A": 0.204432, "A_units": "cm^3/(mol*s)", "n": 4.37947, "Ea": 78.903, "Ea_units": "kJ/mol",
    "T0_k": 1.0, "Tmin_k": 300.0, "Tmax_k": 3000.0, "dA": 1.48469, "dn": 0.0514763, "dEa": 0.294379,
    "dEa_units": "kJ/mol", "n_data_points": 50, "tunneling": "Eckart", "atom_corrections_applied": True,
    "comment": "Fitted to 50 data points; dA = *|/ 1.48469, dn = +|- 0.0514763, dEa = +|- 0.294379 kJ/mol",
    "ts_validation": None,
}


def _doc(**kinetics):
    doc = copy.deepcopy(_doc13())
    doc["project"] = "demo_reaction"
    doc["reactions"][0]["kinetics"] = {**copy.deepcopy(DEMO_KINETICS), **kinetics}
    return doc


def _build(tmp_path, doc):
    payload = _reaction_payload(_adapter(tmp_path), doc)
    contract_validate(ComputedReactionUploadRequest, payload)
    (block,) = payload["kinetics"]
    return payload, block


def _selection_findings(block, payload, *, pressure_kind="high_pressure_limit", target_kind="whole_reaction"):
    """The four ``unresolved``/``incompatible`` findings of TCKDB's ``kinetics_selection.assessment``.

    Mirrors only what ``_check_identity`` and ``_check_pressure`` read: a determination (with its
    target), a direction, an applicability declaration, and the claimed pressure dependence against
    the request's pressure kind. A bundle determination needs the bundle's source.
    """
    found = []
    det = block.get("determination")
    if det is None or not (payload.get("literature") or payload.get("workflow_tool_release")):
        found.append("determination_not_declared")
    elif det["target_kind"] != target_kind:
        found.append("target_mismatch")
    direction = block.get("direction")
    if direction is None:
        found.append("direction_not_recorded")
    elif direction != "forward":
        found.append("direction_mismatch")
    declared = block.get("applicability")
    if declared is None:
        found.append("applicability_not_declared")
    claimed = (declared or {}).get("pressure_dependence")
    if pressure_kind == "independent":
        ok = {"independent"}
    else:
        ok = {"high_pressure_limit", "independent"}
    if claimed is None:
        found.append("pressure_dependence_not_declared")
    elif claimed not in ok:
        found.append("pressure_dependence_mismatch")
    return found


def test_the_demo_fit_declares_exactly_these_dicts(tmp_path):
    payload, block = _build(tmp_path, _doc())
    assert block["direction"] == "forward"
    det = block["determination"]
    assert det.pop("key").startswith(KEY_PREFIX)
    assert det == {"target_kind": "whole_reaction", "representation_role": "complete"}
    assert block["applicability"] == APPLICABILITY
    assert "protocol" not in block
    assert payload["workflow_tool_release"]["name"] == "ARC"


def test_the_demo_fit_clears_the_four_selection_findings(tmp_path):
    payload, block = _build(tmp_path, _doc())
    assert _selection_findings(block, payload) == []


def test_an_independent_pressure_request_is_not_answered_by_a_high_pressure_limit_claim(tmp_path):
    payload, block = _build(tmp_path, _doc())
    assert _selection_findings(block, payload, pressure_kind="independent") == ["pressure_dependence_mismatch"]


def test_the_findings_replica_reports_a_fit_without_declarations(tmp_path):
    payload, block = _build(tmp_path, _doc(tunneling=None))
    assert _selection_findings(block, payload) == [
        "determination_not_declared", "direction_not_recorded", "applicability_not_declared",
        "pressure_dependence_not_declared"]


@pytest.mark.parametrize("stamp", [None, "", "  "])
def test_a_fit_without_the_arkane_stamp_declares_nothing(tmp_path, stamp):
    _, block = _build(tmp_path, _doc(tunneling=stamp))
    assert not {"direction", "determination", "applicability", "protocol"} & set(block)


def test_a_fit_without_a_tunneling_key_declares_nothing(tmp_path):
    doc = _doc()
    del doc["reactions"][0]["kinetics"]["tunneling"]
    _, block = _build(tmp_path, doc)
    assert not {"direction", "determination", "applicability"} & set(block)


def test_without_a_workflow_tool_release_the_determination_is_not_sent(tmp_path):
    doc = _doc()
    for key in ("arc_version", "arc_git_commit"):
        doc.pop(key, None)
    payload, block = _build(tmp_path, doc)
    assert "workflow_tool_release" not in payload
    assert "determination" not in block
    assert block["direction"] == "forward" and block["applicability"] == APPLICABILITY


def test_the_key_names_the_project_only_when_output_states_one(tmp_path):
    doc = _doc()
    doc.pop("project")
    _, block = _build(tmp_path, doc)
    ts_label = doc["reactions"][0]["ts_label"]
    assert block["determination"]["key"].startswith(f"arkane_kinetics:{ts_label}:")


def test_a_fit_without_tmin_tmax_is_declared_the_same_and_sends_no_bounds(tmp_path):
    doc = _doc()
    for key in ("Tmin_k", "Tmax_k"):
        del doc["reactions"][0]["kinetics"][key]
    _, block = _build(tmp_path, doc)
    assert "tmin_k" not in block and "tmax_k" not in block
    assert block["direction"] == "forward"
    assert block["determination"]["key"].startswith(KEY_PREFIX) and block["applicability"] == APPLICABILITY


def test_the_stated_temperature_range_goes_out_as_the_fits_bounds(tmp_path):
    _, block = _build(tmp_path, _doc())
    assert (block["tmin_k"], block["tmax_k"]) == (300.0, 3000.0)
    assert "tmin_k" not in block["applicability"]


def test_what_arc_does_not_state_is_not_claimed(tmp_path):
    _, block = _build(tmp_path, _doc())
    assert set(block["applicability"]) == set(APPLICABILITY)  # no scope, reaction_order, collider
    assert "protocol" not in block


def test_an_overlong_project_name_keeps_a_bounded_key():
    out = _kinetics_declarations(
        {"tunneling": "Eckart"}, project="p" * 200, has_workflow_tool=True, ts_label="TS0")
    key = out["determination"]["key"]
    assert len(key) <= 128 and key.startswith("arkane_kinetics:")


def _key(tmp_path, mutate=None):
    doc = _doc()
    payload = _reaction_payload(_adapter(tmp_path), doc)
    if mutate is not None:
        mutate(doc)
        payload = _reaction_payload(_adapter(tmp_path), doc)
    return payload["kinetics"][0]["determination"]["key"]


def test_the_key_is_deterministic_for_one_bundle(tmp_path):
    assert _key(tmp_path) == _key(tmp_path)


def test_a_rerun_with_other_sp_energies_gets_another_key(tmp_path):
    def mutate(doc):
        for record in (*doc["species"], *doc["transition_states"]):
            if record.get("sp_energy_hartree") is not None:
                record["sp_energy_hartree"] += 1e-3
    assert _key(tmp_path) != _key(tmp_path, mutate)


def test_renaming_the_bundle_local_calculation_keys_leaves_the_key_unchanged(tmp_path, monkeypatch):
    before = _key_of(tmp_path, _doc())
    original = adapter_module._calc_prefix_for_actor
    monkeypatch.setattr(adapter_module, "_calc_prefix_for_actor",
                        lambda *a, **k: "renamed_" + original(*a, **k))
    payload, block = _build(tmp_path, _doc())
    assert any(l["calculation_key"].startswith("renamed_") for l in block["source_calculations"])
    assert block["determination"]["key"] == before


@pytest.mark.parametrize("change", [
    {"tunneling": "Wigner"}, {"A": 0.3}, {"n": 4.4}, {"Ea": 80.0}, {"T0_k": 300.0},
    {"Tmin_k": 400.0}, {"Tmax_k": 2500.0}, {"n_data_points": 40}, {"A_units": "m^3/(mol*s)"},
    {"Ea_units": "kcal/mol"}])
def test_a_different_fit_of_the_same_energies_gets_another_key(tmp_path, change):
    assert _key_of(tmp_path, _doc()) != _key_of(tmp_path, _doc(**change))


def test_an_identical_doc_gets_the_same_key(tmp_path):
    assert _key_of(tmp_path, _doc()) == _key_of(tmp_path, _doc())


def test_a_changed_participant_level_gets_another_key(tmp_path):
    doc = _doc()
    for record in (*doc["species"], *doc["transition_states"]):
        sp = (record.get("levels") or {}).get("sp")
        if sp:
            sp["basis"] = "other-basis"
    assert _key_of(tmp_path, _doc()) != _key_of(tmp_path, doc)


def _key_of(tmp_path, doc):
    return _build(tmp_path, doc)[1]["determination"]["key"]


def _solvate(doc, *, only_ts=False, skip_sp_software=False, key="solvation_method", value="smd"):
    records = doc["transition_states"] if only_ts else (*doc["species"], *doc["transition_states"])
    for record in records:
        sp = (record.get("levels") or {}).get("sp")
        if sp:
            sp.update({"solvent": "water", key: value})
        if skip_sp_software:
            (record.get("ess_software") or {})["sp"] = None


def test_a_solvated_participant_level_sends_no_phase(tmp_path):
    doc = _doc()
    for record in (*doc["species"], *doc["transition_states"]):
        sp = (record.get("levels") or {}).get("sp")
        if sp:
            sp.update({"solvent": "water", "solvation_method": "cpcm"})
    payload, block = _build(tmp_path, doc)
    solvated = any(
        c["level_of_theory"].get("solvent") for s in payload["species"]
        for c in [s["conformers"][0]["calculation"], *s.get("calculations", ())])
    assert solvated
    assert "phase" not in block["applicability"]
    assert block["applicability"]["pressure_dependence"] == "high_pressure_limit"


def _interpreted(payload_warnings):
    return [w for w in payload_warnings if w["code"] == "kinetics_declarations_interpreted"]


def _build_with_warnings(tmp_path, doc):
    warnings = []
    payload = _reaction_payload(_adapter(tmp_path), doc, warnings=warnings)
    contract_validate(ComputedReactionUploadRequest, payload)
    return payload, warnings


def test_the_interpretation_is_reported_once_with_the_declared_facts(tmp_path):
    payload, warnings = _build_with_warnings(tmp_path, _doc())
    (found,) = _interpreted(warnings)
    block = payload["kinetics"][0]
    assert found["field"] == "kinetics[0]"
    ctx = found["context"]
    assert ctx["direction"] == "forward" and ctx["pressure_dependence"] == "high_pressure_limit"
    assert ctx["phase"] == "gas" and ctx["determination_key"] == block["determination"]["key"]
    assert found["message"] == NOTE_SENTENCE == adapter_module._kinetics_interpretation_sentence(block)


def test_an_unstamped_fit_reports_no_interpretation(tmp_path):
    payload, warnings = _build_with_warnings(tmp_path, _doc(tunneling=None))
    assert _interpreted(warnings) == []
    assert "output.yml states none" not in (payload["kinetics"][0].get("note") or "")


def test_without_a_workflow_tool_the_warning_names_no_determination_key(tmp_path):
    doc = _doc()
    for key in ("arc_version", "arc_git_commit"):
        doc.pop(key, None)
    _, warnings = _build_with_warnings(tmp_path, doc)
    (found,) = _interpreted(warnings)
    assert "determination_key" not in found["context"]


NOTE_SENTENCE = (
    "Direction (forward), pressure dependence (high-pressure limit), phase (gas) and the determination "
    "(one complete determination of the whole reaction) are the adapter's interpretation of ARC's Arkane "
    "high-pressure-limit kinetics run; output.yml states none of them.")


def test_the_note_carries_the_sentence_after_arkanes_own_comment(tmp_path):
    _, block = _build(tmp_path, _doc())
    assert block["note"].endswith(NOTE_SENTENCE)
    assert DEMO_KINETICS["comment"] in block["note"]


def test_a_solvated_participant_level_leaves_phase_out_of_note_and_warning(tmp_path):
    doc = _doc()
    for record in (*doc["species"], *doc["transition_states"]):
        sp = (record.get("levels") or {}).get("sp")
        if sp:
            sp.update({"solvent": "water", "solvation_method": "cpcm"})
    payload, warnings = _build_with_warnings(tmp_path, doc)
    note = payload["kinetics"][0]["note"]
    assert "phase" not in note
    assert note.endswith(
        "Direction (forward), pressure dependence (high-pressure limit) and the determination (one "
        "complete determination of the whole reaction) are the adapter's interpretation of ARC's Arkane "
        "high-pressure-limit kinetics run; output.yml states none of them.")
    (found,) = _interpreted(warnings)
    assert "phase" not in found["context"] and "phase" not in found["message"]


def test_the_note_is_the_sentence_alone_when_the_run_states_no_comment(tmp_path):
    doc = copy.deepcopy(_doc13())
    doc["project"] = "demo_reaction"
    doc["reactions"][0]["kinetics"] = {
        **copy.deepcopy(DEMO_KINETICS), "comment": None, "atom_corrections_applied": None}
    doc["reactions"][0].pop("long_kinetic_description", None)
    _, block = _build(tmp_path, doc)
    assert block["note"] == NOTE_SENTENCE


def test_without_a_workflow_tool_the_sentence_omits_the_determination(tmp_path):
    doc = _doc()
    for key in ("arc_version", "arc_git_commit"):
        doc.pop(key, None)
    payload, _ = _build_with_warnings(tmp_path, doc)
    note = payload["kinetics"][0]["note"]
    assert "determination" not in note.splitlines()[-1]
    assert note.splitlines()[-1].startswith("Direction (forward), pressure dependence (high-pressure limit) and phase (gas) are")


@pytest.mark.parametrize("case", [
    {},
    {"key": "solvation_scheme_level", "value": "x"},
])
def test_a_solvated_level_whose_sp_calculation_was_skipped_still_withholds_phase(tmp_path, case):
    doc = _doc()
    _solvate(doc, skip_sp_software=True, **case)
    payload, block = _build(tmp_path, doc)
    assert "phase" not in block["applicability"]
    assert block["applicability"]["pressure_dependence"] == "high_pressure_limit"


def test_a_solvated_ts_level_alone_withholds_phase(tmp_path):
    doc = _doc()
    _solvate(doc, only_ts=True)
    _, block = _build(tmp_path, doc)
    assert "phase" not in block["applicability"]


def test_a_solvated_header_level_withholds_phase(tmp_path):
    doc = _doc()
    doc["sp_level"] = {**(doc.get("sp_level") or {"method": "wb97xd", "basis": "def2-tzvp"}),
                       "solvent": "water", "solvation_method": "smd"}
    _, block = _build(tmp_path, doc)
    assert "phase" not in block["applicability"]


def test_a_level_asking_for_solvation_only_through_args_withholds_phase(tmp_path):
    doc = _doc()
    for record in (*doc["species"], *doc["transition_states"]):
        sp = (record.get("levels") or {}).get("sp")
        if sp:
            sp["args"] = {"keyword": {"sp": "SCRF=(SMD,Solvent=Water)"}}
    _, block = _build(tmp_path, doc)
    assert "phase" not in block["applicability"]


@pytest.mark.parametrize("args", ["cpcm(water)", {"block": {"sp": ["%CPCM"]}}, "COSMO", "pcm", "solvation"])
def test_each_solvation_args_token_withholds_phase(tmp_path, args):
    doc = _doc()
    doc["sp_level"] = {**(doc.get("sp_level") or {"method": "wb97xd", "basis": "def2-tzvp"}), "args": args}
    _, block = _build(tmp_path, doc)
    assert "phase" not in block["applicability"]


def test_args_without_solvation_tokens_still_send_phase(tmp_path):
    doc = _doc()
    for record in (*doc["species"], *doc["transition_states"]):
        for level in (record.get("levels") or {}).values():
            if isinstance(level, dict):
                level["args"] = {"keyword": {"opt": "tight"}}
    _, block = _build(tmp_path, doc)
    assert block["applicability"]["phase"] == "gas"


def test_a_solvated_adaptive_level_withholds_phase(tmp_path):
    doc = _doc()
    doc["adaptive_levels"] = [
        {"atom_range": [1, "inf"],
         "levels": {"sp": {"method": "wb97xd", "basis": "def2-tzvp", "solvation_method": "smd", "solvent": "water"}}}]
    assert adapter_module._source_levels_state_solvation(doc, ())
    doc["adaptive_levels"][0]["levels"]["sp"].pop("solvent")
    doc["adaptive_levels"][0]["levels"]["sp"].pop("solvation_method")
    assert not adapter_module._source_levels_state_solvation(doc, ())


def test_a_solver_keyword_in_args_is_not_solvation(tmp_path):
    doc = _doc()
    for record in (*doc["species"], *doc["transition_states"]):
        for level in (record.get("levels") or {}).values():
            if isinstance(level, dict):
                level["args"] = {"keyword": {"general": "solver_maxiter 100"}}
    _, block = _build(tmp_path, doc)
    assert block["applicability"]["phase"] == "gas"
