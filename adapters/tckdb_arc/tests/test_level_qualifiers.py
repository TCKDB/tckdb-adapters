"""The typed level-of-theory qualifiers of a calculation, read from the deck ARC recorded for its job.

A calculation's ``level_of_theory`` states ``pno_truncation``, ``ri_approximation``, ``relativistic_hamiltonian``
or ``core_treatment`` when, and only when, the input deck ARC recorded for that calculation's job
(output.yml 1.3: ``ess_input_options``, ``irc_log_input_options``, ``conformer_input_options``,
``conformer_energy_input_options``) carries the token. A level no job stands behind states none, and nothing
is copied onto it. ``adapters/tckdb_arc/tests/_input_options.py`` holds the deck strings and where they come from.
"""

import copy

import pytest

from _contract import contract_validate
from _input_options import (
    GAUSSIAN_COMPOSITE_DKH2, GAUSSIAN_FREQ, GAUSSIAN_FREQ_DKH2, GAUSSIAN_IRC_FORWARD, GAUSSIAN_IRC_FORWARD_DKH2,
    GAUSSIAN_IRC_REVERSE, GAUSSIAN_IRC_REVERSE_DKH2, GAUSSIAN_OPT, GAUSSIAN_OPT_DKH2, GAUSSIAN_SP,
    GAUSSIAN_SP_DKH2, ORCA_BLOCK_FC_EWIN, ORCA_SP_DLPNO_TIGHTPNO, ORCA_SP_HF_NO_PNO, ORCA_SP_HF_TIGHTPNO,
    ORCA_SP_NORMALPNO, ORCA_SP_TIGHTPNO, ORCA_SP_TIGHTPNO_RIJCOSX, overlay_decks, without_decks)
from _backend_level_rules import calculations_by_key, energy_level_verdict
from test_arc_schema_1_3_levels import (
    LVL, SP_LVL, _adapter, _calcs, _codes, _doc13, _project, _reaction_payload, _record, _sample,
    _species_payload, _ts_calcs)
from test_kinetics_one_electron_hf_adapter import DLPNO, _doc as _h_atom_doc
from tckdb_arc.adapter import _arc_level_to_tckdb_lot
from tckdb_core.level_rules import level_hash
from tckdb_core.testing.backend_import import backend_level_hash
from tckdb_schemas.fragments.refs import LevelOfTheoryRef
from tckdb_schemas.workflows.computed_reaction_upload import ComputedReactionUploadRequest

QUALIFIERS = ("pno_truncation", "ri_approximation", "relativistic_hamiltonian", "core_treatment")


def _qualifiers(level):
    return {k: v for k, v in level.items() if k in QUALIFIERS}


def _build_species(tmp_path, doc, label):
    """``(payload, warnings)`` of a computed-species build, with the findings recorded during it reported."""
    adapter = _adapter(tmp_path)
    marked = adapter._with_adaptive_levels(doc, [])
    warnings = []
    payload = adapter._build_computed_species_payload(
        output_doc=marked, species_record=_record(marked, label), conformer_key="c0", warnings=warnings)
    adapter._report_adaptive_omissions(marked, warnings)
    return payload, warnings


def _build_reaction(tmp_path, doc):
    adapter = _adapter(tmp_path)
    marked = adapter._with_adaptive_levels(doc, [])
    warnings = []
    payload = adapter._build_computed_reaction_payload(
        output_doc=marked, reaction_record=marked["reactions"][0], warnings=warnings)
    adapter._report_adaptive_omissions(marked, warnings)
    return payload, warnings


def _finding(warnings, code):
    return [w for w in warnings if w["code"] == code]


def _doc_with_decks(**decks):
    doc = _doc13()
    record = doc["species"][0]
    overlay_decks(record, **decks)
    return doc, record["label"]


# ---------------------------------------------------------------- the primary calculations

def test_each_calculation_states_the_qualifiers_its_own_jobs_deck_states(tmp_path):
    doc, label = _doc_with_decks(opt=GAUSSIAN_OPT_DKH2, freq=GAUSSIAN_FREQ, sp=ORCA_SP_TIGHTPNO_RIJCOSX)
    payload, warnings = _build_species(tmp_path, doc, label)
    calcs = _calcs(payload)
    assert calcs["opt"]["level_of_theory"] == {**LVL, "relativistic_hamiltonian": "dkh2"}
    assert calcs["freq"]["level_of_theory"] == LVL
    assert calcs["sp"]["level_of_theory"] == {**SP_LVL, "pno_truncation": "tight", "ri_approximation": "rijcosx"}
    assert not {"level_qualifiers_conflicting", "level_qualifiers_deck_not_read"} & set(_codes(warnings))


def test_a_default_token_ARC_wrote_is_read_like_any_other(tmp_path):
    doc, label = _doc_with_decks(opt=GAUSSIAN_OPT, freq=GAUSSIAN_FREQ, sp=ORCA_SP_NORMALPNO)
    calcs = _calcs(_build_species(tmp_path, doc, label)[0])
    assert _qualifiers(calcs["sp"]["level_of_theory"]) == {"pno_truncation": "normal"}


def test_a_deck_that_states_nothing_adds_nothing(tmp_path):
    doc, label = _doc_with_decks(opt=GAUSSIAN_OPT, freq=GAUSSIAN_FREQ, sp=ORCA_SP_NORMALPNO.replace("normalpno", ""))
    calcs = _calcs(_build_species(tmp_path, doc, label)[0])
    assert calcs["opt"]["level_of_theory"] == LVL and calcs["freq"]["level_of_theory"] == LVL
    assert calcs["sp"]["level_of_theory"] == SP_LVL


def test_a_missing_key_means_the_same_as_null(tmp_path):
    doc = _doc13()
    assert all("ess_input_options" not in r for r in (*doc["species"], *doc["transition_states"]))
    label = doc["species"][0]["label"]
    without_keys, _ = _build_species(tmp_path / "a", doc, label)
    nulls = copy.deepcopy(doc)
    for record in (*nulls["species"], *nulls["transition_states"]):
        overlay_decks(record, irc=[None, None] if record["is_ts"] else None,
                      conformers=None, conformer_energies=None)
    with_nulls, _ = _build_species(tmp_path / "b", nulls, label)
    assert without_keys == with_nulls
    for calc in _calcs(without_keys).values():
        assert not _qualifiers(calc["level_of_theory"])
    # a document whose decks were dropped is the all-null document
    decks, label = _doc_with_decks(opt=GAUSSIAN_OPT_DKH2, freq=GAUSSIAN_FREQ_DKH2, sp=ORCA_SP_TIGHTPNO)
    assert _build_species(tmp_path / "c", without_decks(decks), label)[0] == without_keys


def test_a_record_with_no_deck_of_its_job_states_none_beside_one_that_has(tmp_path):
    doc = _doc13()
    overlay_decks(doc["species"][0], opt=GAUSSIAN_OPT_DKH2, freq=GAUSSIAN_FREQ_DKH2, sp=ORCA_SP_TIGHTPNO)
    payload, _ = _build_reaction(tmp_path, doc)
    species = {s["conformers"][0]["calculation"]["level_of_theory"].get("relativistic_hamiltonian")
               for s in payload["species"]}
    assert species == {"dkh2", None}


def test_a_record_before_1_3_is_never_read(tmp_path):
    from test_monatomic_sp_primary import _atom_record, _fake_output_doc, _submit_species

    record = _atom_record()
    overlay_decks(record, sp=GAUSSIAN_SP_DKH2)
    payload, outcome = _submit_species(tmp_path, _fake_output_doc(), record)
    primary = payload["conformers"][0]["primary_calculation"]
    assert not _qualifiers(primary["level_of_theory"])
    assert not {"level_qualifiers_conflicting", "level_qualifiers_deck_not_read"} & set(_codes_of(outcome))


def _codes_of(outcome):
    return [w["code"] for w in outcome.warnings]


def test_a_monatomic_primary_sp_reads_the_sp_deck(tmp_path):
    from test_monatomic_sp_primary import _atom_record_1_3, _submit_species

    doc, record = _atom_record_1_3()
    overlay_decks(record, sp=GAUSSIAN_SP_DKH2)
    payload, _ = _submit_species(tmp_path, doc, record)
    primary = payload["conformers"][0]["primary_calculation"]
    assert primary["type"] == "sp"
    assert _qualifiers(primary["level_of_theory"]) == {"relativistic_hamiltonian": "dkh2"}


def test_the_coarse_opt_does_not_read_the_fine_opts_deck(tmp_path):
    doc, label = _doc_with_decks(opt=GAUSSIAN_OPT_DKH2, freq=GAUSSIAN_FREQ, sp=ORCA_SP_TIGHTPNO)
    record = doc["species"][0]
    record.update({"coarse_opt_log": "calcs/c.log", "coarse_opt_output_xyz": "C 0.0 0.0 0.0\nH 1.0 0.0 0.1",
                   "coarse_opt_n_steps": 3})
    calcs = _calcs(_build_species(tmp_path, doc, label)[0])
    assert calcs["opt"]["level_of_theory"] == {**LVL, "relativistic_hamiltonian": "dkh2"}
    assert calcs["opt_coarse"]["level_of_theory"] == LVL


def test_an_sp_taken_from_the_opt_log_reads_the_opt_jobs_deck(tmp_path):
    doc = _project(tmp_path)
    record = _record(doc, "iC3H7")
    # ARC records the opt job's deck under ``sp`` too when the sp energy is read from the opt log.
    overlay_decks(record, opt=GAUSSIAN_OPT_DKH2, freq=GAUSSIAN_FREQ, sp=GAUSSIAN_OPT_DKH2)
    calcs = _calcs(_species_payload(_adapter(tmp_path), doc, "iC3H7"))
    assert calcs["sp"]["parameters_json"]["tckdb_origin"]["reused_from"] == {"calculation_type": "opt"}
    assert _qualifiers(calcs["opt"]["level_of_theory"]) == _qualifiers(calcs["sp"]["level_of_theory"]) == {
        "relativistic_hamiltonian": "dkh2"}
    assert not _qualifiers(calcs["freq"]["level_of_theory"])


def test_a_composite_run_reads_the_composite_jobs_deck_for_its_calculations(tmp_path):
    doc = _project(tmp_path)
    record = _record(doc, "SO2OO")
    overlay_decks(record, opt=GAUSSIAN_OPT_DKH2, freq=GAUSSIAN_FREQ_DKH2, sp=GAUSSIAN_SP_DKH2,
                  composite=GAUSSIAN_COMPOSITE_DKH2)
    record["opt_log"] = record["freq_log"] = record["sp_log"] = None
    payload = _species_payload(_adapter(tmp_path), doc, "SO2OO")
    calcs = _calcs(payload)
    assert calcs["opt"]["level_of_theory"] == {"method": "cbs-qb3", "relativistic_hamiltonian": "dkh2"}
    assert calcs["sp"]["level_of_theory"] == {"method": "cbs-qb3", "relativistic_hamiltonian": "dkh2"}
    # the composite log states a route and it says nothing about the Hamiltonian: the deck stands


def test_a_placeholder_opt_for_a_job_ARC_did_not_run_reads_no_deck(tmp_path):
    doc = _sample("species_thermo")               # CH3NH2: only a Molpro sp is exported, so no opt job
    record = _record(doc, "CH3NH2")
    overlay_decks(record, opt=None, freq=None, sp=GAUSSIAN_OPT_DKH2)
    payload, warnings = _build_species(tmp_path, doc, "CH3NH2")
    calcs = _calcs(payload)
    assert calcs["opt"]["parameters_json"]["tckdb_origin"]["origin_detail"].endswith("no_opt_job")
    assert not _qualifiers(calcs["opt"]["level_of_theory"])
    # the sp ran in Molpro, whose decks are not read, and nothing is reported about it
    assert calcs["sp"]["software_release"]["name"] == "molpro" and not _qualifiers(calcs["sp"]["level_of_theory"])
    assert not {"level_qualifiers_conflicting", "level_qualifiers_deck_not_read"} & set(_codes(warnings))


# ---------------------------------------------------------------- scans, path searches and header levels

def test_levels_no_job_stands_behind_state_none(tmp_path):
    assert _arc_level_to_tckdb_lot({"method": "dlpno-ccsd(t)", "basis": "def2-tzvp", "software": "orca",
                                    "args": {"keyword": {"general": "tightpno rijcosx zora"}}}) == {
        "method": "dlpno-ccsd(t)", "basis": "def2-tzvp"}
    doc = _doc13(adaptive=True)
    doc.update(freq_scale_factor=0.97, freq_scale_factor_key=None, freq_scale_factor_source="CCCBDB")
    record = doc["species"][0]
    overlay_decks(record, opt=GAUSSIAN_OPT_DKH2, freq=GAUSSIAN_FREQ_DKH2, sp=ORCA_SP_TIGHTPNO)
    payload, warnings = _build_species(tmp_path, doc, record["label"])
    calcs = _calcs(payload)
    assert calcs["freq"]["level_of_theory"] == {**LVL, "relativistic_hamiltonian": "dkh2"}
    fsf = payload["statmech"]["freq_scale_factor"]
    assert fsf["level_of_theory"] == LVL                               # a fitted level: no job behind it
    assert "freq_scale_factor_fitted_for_other_level" not in _codes(warnings)


def test_a_screened_conformer_is_still_filed_when_decks_state_qualifiers(tmp_path):
    doc = _project(tmp_path)
    record = _record(doc, "nC3H7")
    overlay_decks(record, opt=GAUSSIAN_OPT, freq=GAUSSIAN_FREQ, sp=GAUSSIAN_SP,
                  conformers=[GAUSSIAN_OPT_DKH2, GAUSSIAN_OPT_DKH2, None])
    warnings = []
    payload = _species_payload(_adapter(tmp_path), doc, "nC3H7", warnings)
    assert [c["key"] for c in payload["conformers"]] == ["c0", "alt0"]
    assert "conformer_program_not_stated" not in _codes(warnings)


# ---------------------------------------------------------------- screened conformers

def test_a_screened_conformers_opt_reads_its_own_decks_entry_and_the_primary_its_own(tmp_path):
    doc = _project(tmp_path)
    record = _record(doc, "nC3H7")
    overlay_decks(record, opt=GAUSSIAN_OPT, freq=GAUSSIAN_FREQ, sp=GAUSSIAN_SP,
                  conformers=[GAUSSIAN_OPT_DKH2, GAUSSIAN_OPT_DKH2, None])
    payload = _species_payload(_adapter(tmp_path), doc, "nC3H7")
    primary, alt = payload["conformers"][0], payload["conformers"][1]
    assert not _qualifiers(primary["primary_calculation"]["level_of_theory"])
    assert alt["primary_calculation"]["level_of_theory"] == {
        "method": "wb97xd", "basis": "def2svp", "relativistic_hamiltonian": "dkh2"}


def test_a_screened_conformer_without_a_deck_states_none(tmp_path):
    doc = _project(tmp_path)
    record = _record(doc, "nC3H7")
    overlay_decks(record, opt=GAUSSIAN_OPT_DKH2, freq=GAUSSIAN_FREQ, sp=GAUSSIAN_SP, conformers=[None, None, None])
    payload = _species_payload(_adapter(tmp_path), doc, "nC3H7")
    assert _qualifiers(payload["conformers"][0]["primary_calculation"]["level_of_theory"]) == {
        "relativistic_hamiltonian": "dkh2"}
    assert not _qualifiers(payload["conformers"][1]["primary_calculation"]["level_of_theory"])


def _conformer_sp_doc(tmp_path, deck, program="orca"):
    doc = _project(tmp_path)
    record = _record(doc, "nC3H7")
    sp_level = {"method": "dlpno-ccsd(t)", "basis": "cc-pvtz"}
    record["conformer_energy_level"] = sp_level
    doc["conformer_sp_level"] = {**sp_level, "software": program}
    overlay_decks(record, opt=GAUSSIAN_OPT, freq=GAUSSIAN_FREQ, sp=GAUSSIAN_SP,
                  conformers=[GAUSSIAN_OPT, GAUSSIAN_OPT, None], conformer_energies=[deck, deck, None])
    return doc


def test_a_screened_conformers_single_point_reads_the_energys_deck(tmp_path):
    doc = _conformer_sp_doc(tmp_path, ORCA_SP_DLPNO_TIGHTPNO)
    alt = _species_payload(_adapter(tmp_path), doc, "nC3H7")["conformers"][1]
    assert not _qualifiers(alt["primary_calculation"]["level_of_theory"])
    (sp,) = alt["additional_calculations"]
    assert sp["level_of_theory"] == {"method": "dlpno-ccsd(t)", "basis": "cc-pvtz", "pno_truncation": "tight"}


def test_a_conformer_single_point_deck_of_another_programs_shape_is_not_read_and_is_reported(tmp_path):
    doc = _conformer_sp_doc(tmp_path, GAUSSIAN_SP_DKH2)           # the header says ORCA; the deck is Gaussian's
    payload, warnings = _build_species(tmp_path, doc, "nC3H7")
    alt = payload["conformers"][1]
    (sp,) = alt["additional_calculations"]
    assert not _qualifiers(sp["level_of_theory"])
    (finding,) = _finding(warnings, "level_qualifiers_deck_not_read")
    assert finding["field"] == "sp.level_of_theory" and "shape" in finding["message"]


# ---------------------------------------------------------------- IRC

def _irc_doc(irc):
    doc = _doc13()
    ts = doc["transition_states"][0]
    overlay_decks(ts, opt=GAUSSIAN_OPT, freq=GAUSSIAN_FREQ, sp=ORCA_SP_NORMALPNO, irc=irc)
    return doc


def test_an_irc_calculation_states_a_qualifier_when_every_logs_deck_does(tmp_path):
    payload, warnings = _build_reaction(
        tmp_path, _irc_doc([GAUSSIAN_IRC_FORWARD_DKH2, GAUSSIAN_IRC_REVERSE_DKH2]))
    calcs = _ts_calcs(payload)
    assert calcs["ts_irc"]["level_of_theory"] == {**LVL, "relativistic_hamiltonian": "dkh2"}
    assert not _qualifiers(calcs["ts_opt"]["level_of_theory"])          # the TS's own opt reads its own deck
    assert "level_qualifiers_conflicting" not in _codes(warnings)


def test_decks_of_an_irc_pair_that_differ_state_none_and_are_reported(tmp_path):
    payload, warnings = _build_reaction(tmp_path, _irc_doc([GAUSSIAN_IRC_FORWARD_DKH2, GAUSSIAN_IRC_REVERSE]))
    assert not _qualifiers(_ts_calcs(payload)["ts_irc"]["level_of_theory"])
    (finding,) = _finding(warnings, "level_qualifiers_conflicting")
    assert finding["field"] == "irc.level_of_theory"
    assert "relativistic_hamiltonian" in finding["context"]["detail"]


@pytest.mark.parametrize("irc", [[GAUSSIAN_IRC_FORWARD_DKH2, None], [None, None], None])
def test_an_irc_log_without_a_recorded_deck_states_none_and_nothing_is_reported(tmp_path, irc):
    payload, warnings = _build_reaction(tmp_path, _irc_doc(irc))
    assert not _qualifiers(_ts_calcs(payload)["ts_irc"]["level_of_theory"])
    assert not {"level_qualifiers_conflicting", "level_qualifiers_deck_not_read"} & set(_codes(warnings))


def test_an_irc_pair_without_qualifiers_states_none(tmp_path):
    payload, _ = _build_reaction(tmp_path, _irc_doc([GAUSSIAN_IRC_FORWARD, GAUSSIAN_IRC_REVERSE]))
    assert _ts_calcs(payload)["ts_irc"]["level_of_theory"] == LVL


# ---------------------------------------------------------------- the log echo only vetoes

def test_a_log_echo_that_states_another_value_removes_the_field_and_is_reported(tmp_path):
    doc, label = _doc_with_decks(opt=GAUSSIAN_OPT_DKH2, freq=GAUSSIAN_FREQ, sp=ORCA_SP_TIGHTPNO)
    doc["species"][0]["opt_route"] = "#P opt=(calcfc) wb97xd/def2tzvp integral=(grid=ultrafine, dkh0)"
    payload, warnings = _build_species(tmp_path, doc, label)
    assert _calcs(payload)["opt"]["level_of_theory"] == LVL
    (finding,) = _finding(warnings, "level_qualifiers_conflicting")
    assert finding["field"] == "opt.level_of_theory" and "echo" in finding["context"]["detail"]


def test_a_silent_log_echo_leaves_the_decks_field_and_a_value_only_the_echo_states_is_not_added(tmp_path):
    doc, label = _doc_with_decks(opt=GAUSSIAN_OPT_DKH2, freq=GAUSSIAN_FREQ, sp=ORCA_SP_TIGHTPNO)
    doc["species"][0]["opt_route"] = "#P opt=(calcfc) wb97xd/def2tzvp"
    doc["species"][0]["freq_route"] = "#P wb97xd/def2tzvp freq integral=(grid=ultrafine, dkh2)"
    calcs = _calcs(_build_species(tmp_path, doc, label)[0])
    assert _qualifiers(calcs["opt"]["level_of_theory"]) == {"relativistic_hamiltonian": "dkh2"}
    assert not _qualifiers(calcs["freq"]["level_of_theory"])


def test_an_orca_echo_is_the_keyword_lines_and_the_blocks(tmp_path):
    doc, label = _doc_with_decks(opt=GAUSSIAN_OPT, freq=GAUSSIAN_FREQ,
                                 sp=ORCA_SP_TIGHTPNO + "\n" + ORCA_BLOCK_FC_EWIN)
    record = doc["species"][0]
    record["sp_route"] = "uhf dlpno-ccsd(t)-f12 cc-pvtz-f12 aug-cc-pvtz/c cc-pvtz-f12-cabs loosepno"
    record["sp_blocks"] = "%method\nFrozenCore FC_NONE\nend"
    payload, warnings = _build_species(tmp_path, doc, label)
    assert _calcs(payload)["sp"]["level_of_theory"] == SP_LVL               # both fields vetoed by the echo
    (finding,) = _finding(warnings, "level_qualifiers_conflicting")
    assert "pno_truncation" in finding["context"]["detail"] and "core_treatment" in finding["context"]["detail"]


def test_an_orca_echo_that_agrees_changes_nothing(tmp_path):
    doc, label = _doc_with_decks(opt=GAUSSIAN_OPT, freq=GAUSSIAN_FREQ,
                                 sp=ORCA_SP_TIGHTPNO + "\n" + ORCA_BLOCK_FC_EWIN)
    record = doc["species"][0]
    record["sp_route"] = "uhf dlpno-ccsd(t)-f12 cc-pvtz-f12 aug-cc-pvtz/c cc-pvtz-f12-cabs tightpno"
    record["sp_blocks"] = ORCA_BLOCK_FC_EWIN
    payload, warnings = _build_species(tmp_path, doc, label)
    assert _qualifiers(_calcs(payload)["sp"]["level_of_theory"]) == {
        "pno_truncation": "tight", "core_treatment": "window"}
    assert "level_qualifiers_conflicting" not in _codes(warnings)


# ---------------------------------------------------------------- what cannot be read is reported

def test_a_field_the_deck_states_twice_is_left_out_and_reported(tmp_path):
    deck = ORCA_SP_TIGHTPNO_RIJCOSX.replace("tightpno", "tightpno loosepno")
    doc, label = _doc_with_decks(opt=GAUSSIAN_OPT, freq=GAUSSIAN_FREQ, sp=deck)
    payload, warnings = _build_species(tmp_path, doc, label)
    assert _qualifiers(_calcs(payload)["sp"]["level_of_theory"]) == {"ri_approximation": "rijcosx"}
    (finding,) = _finding(warnings, "level_qualifiers_conflicting")
    assert finding["context"]["label"] == label and "pno_truncation" in finding["context"]["detail"]


def test_a_deck_of_the_wrong_shape_for_its_program_is_not_read_and_is_reported(tmp_path):
    doc, label = _doc_with_decks(opt=ORCA_SP_TIGHTPNO, freq=GAUSSIAN_FREQ, sp=ORCA_SP_TIGHTPNO)
    payload, warnings = _build_species(tmp_path, doc, label)
    assert _calcs(payload)["opt"]["level_of_theory"] == LVL                  # the opt ran in Gaussian
    (finding,) = _finding(warnings, "level_qualifiers_deck_not_read")
    assert finding["field"] == "opt.level_of_theory"


def test_findings_are_reported_once_per_calculation(tmp_path):
    doc, label = _doc_with_decks(opt=ORCA_SP_TIGHTPNO, freq=GAUSSIAN_FREQ, sp=ORCA_SP_TIGHTPNO)
    adapter = _adapter(tmp_path)
    marked = adapter._with_adaptive_levels(doc, [])
    for _ in range(2):
        adapter._build_computed_species_payload(
            output_doc=marked, species_record=_record(marked, label), conformer_key="c0", warnings=[])
    warnings = []
    adapter._report_adaptive_omissions(marked, warnings)
    assert len(_finding(warnings, "level_qualifiers_deck_not_read")) == 1


# ---------------------------------------------------------------- declared energy levels

def _thermo_doc():
    doc, label = _doc_with_decks(opt=GAUSSIAN_OPT_DKH2, freq=GAUSSIAN_FREQ_DKH2, sp=ORCA_SP_TIGHTPNO)
    record = doc["species"][0]
    record["thermo"] = {"h298_kj_mol": -100.0, "s298_j_mol_k": 200.0, "standard_state_pressure_pa": 101325.0,
                        "atom_corrections_applied": True, "bond_corrections_applied": True,
                        "atom_corrections_level": SP_LVL}
    return doc, label


def test_the_declared_energy_level_is_the_sp_calculations_level_with_its_qualifiers(tmp_path):
    doc, label = _thermo_doc()
    payload, _ = _build_species(tmp_path, doc, label)
    calcs = _calcs(payload)
    expected = calcs["sp"]["level_of_theory"]
    assert _qualifiers(expected) == {"pno_truncation": "tight"}
    assert payload["thermo"]["energy_level_of_theory"] == expected
    by_key = calculations_by_key(*calcs.values())
    assert energy_level_verdict(payload["thermo"], by_key) is None
    assert payload["thermo"]["h298_kj_mol"] == -100.0                      # the atom-corrections level still matched


def test_the_energy_level_is_still_declared_when_the_sp_deck_states_no_qualifier(tmp_path):
    doc, label = _thermo_doc()
    overlay_decks(doc["species"][0], opt=GAUSSIAN_OPT, freq=GAUSSIAN_FREQ,
                  sp=ORCA_SP_NORMALPNO.replace("normalpno", ""))
    payload, _ = _build_species(tmp_path, doc, label)
    calcs = _calcs(payload)
    assert not _qualifiers(calcs["sp"]["level_of_theory"])
    assert payload["thermo"]["energy_level_of_theory"] == calcs["sp"]["level_of_theory"]
    assert energy_level_verdict(payload["thermo"], calculations_by_key(*calcs.values())) is None


# ---------------------------------------------------------------- kinetics

def _h_doc(h_deck):
    doc = _h_atom_doc()
    for record in (*doc["species"], *doc["transition_states"]):
        deck = h_deck if record["label"] == "H" else ORCA_SP_TIGHTPNO
        overlay_decks(record, opt=GAUSSIAN_OPT, freq=GAUSSIAN_FREQ, sp=deck)
    return doc


def test_the_h_atom_without_the_pno_the_others_state_declares_nothing_and_says_why(tmp_path):
    payload, warnings = _build_reaction(tmp_path, _h_doc(ORCA_SP_HF_NO_PNO))
    (block,) = payload["kinetics"]
    assert "energy_level_of_theory" not in block
    (finding,) = _finding(warnings, "kinetics_energy_level_qualifiers_differ")
    assert finding["field"] == "kinetics[0].energy_level_of_theory"
    assert len(finding["context"]["calculation_keys"]) == 1
    assert not _finding(warnings, "one_electron_hf_substitution")
    contract_validate(ComputedReactionUploadRequest, payload)


def test_the_h_atom_with_the_same_pno_declares_the_level_with_it(tmp_path):
    payload, warnings = _build_reaction(tmp_path, _h_doc(ORCA_SP_HF_TIGHTPNO))
    (block,) = payload["kinetics"]
    assert block["energy_level_of_theory"] == {**DLPNO, "spin_treatment": "unrestricted", "pno_truncation": "tight"}
    assert len(_finding(warnings, "one_electron_hf_substitution")) == 1
    assert not _finding(warnings, "kinetics_energy_level_qualifiers_differ")


def test_a_kinetics_level_is_declared_when_no_calculation_states_a_qualifier(tmp_path):
    doc = _h_atom_doc()
    payload, warnings = _build_reaction(tmp_path, doc)
    (block,) = payload["kinetics"]
    assert block["energy_level_of_theory"] == {**DLPNO, "spin_treatment": "unrestricted"}
    assert not _finding(warnings, "kinetics_energy_level_qualifiers_differ")


# ---------------------------------------------------------------- hashes and the contract

_LEVELS_BY_TOKEN = [
    (GAUSSIAN_OPT_DKH2, {"relativistic_hamiltonian": "dkh2"}),
    (ORCA_SP_TIGHTPNO_RIJCOSX, {"pno_truncation": "tight", "ri_approximation": "rijcosx"}),
    (ORCA_SP_TIGHTPNO + "\n" + ORCA_BLOCK_FC_EWIN, {"pno_truncation": "tight", "core_treatment": "window"}),
]


@pytest.mark.parametrize("deck, expected", _LEVELS_BY_TOKEN)
def test_a_level_with_qualifiers_is_the_contract_shape_and_hashes_as_the_backend_does(tmp_path, deck, expected):
    program_key = "opt" if deck.startswith("#") else "sp"
    doc, label = _doc_with_decks(**{program_key: deck})
    calc = _calcs(_build_species(tmp_path, doc, label)[0])[program_key]
    level = calc["level_of_theory"]
    assert _qualifiers(level) == expected
    contract_validate(LevelOfTheoryRef, level)
    assert level_hash(level) != level_hash({k: v for k, v in level.items() if k not in QUALIFIERS})
    assert level_hash(level) == backend_level_hash(level)


def test_the_four_deck_keys_are_the_only_source(tmp_path):
    """Levels' ``args`` and the route are not read for a qualifier: a record with args and no decks states none."""
    doc = _doc13()
    record = doc["species"][0]
    record["levels"]["sp"] = {**SP_LVL, "args": {"keyword": {"general": "tightpno rijcosx"}}}
    record["sp_route"] = "uhf dlpno-ccsd(t)-f12 cc-pvtz-f12 aug-cc-pvtz/c cc-pvtz-f12-cabs tightpno"
    calcs = _calcs(_build_species(tmp_path, doc, record["label"])[0])
    assert calcs["sp"]["level_of_theory"] == SP_LVL
