"""The H atom at HF under a correlated atom-corrections level keeps its enthalpy.

ORCA cannot run a correlated method on one electron, so ARC runs a free H atom at HF in the same
basis, auxiliary basis and CABS basis as the DLPNO-CCSD(T)-F12 level its atom energies were matched
to. The same exemption the kinetics energy-level rule has (``tckdb_core.rules``) accepts the
mismatch in ``enthalpy_atom_corrections_level_mismatch``: exactly one electron, an energy level that
is Hartree-Fock, a wavefunction or multireference corrections method, and every other identity
field equal. The enthalpy is kept and ``one_electron_hf_substitution`` says so. Every other
mismatch is refused as before.
"""

import copy
from pathlib import Path

import pytest
import yaml

from tckdb_arc.adapter import _build_thermo_block, _one_electron_hf_substitution
from tckdb_schemas.workflows.computed_reaction_upload import BundleThermoIn
from tckdb_schemas.workflows.computed_species_upload import ThermoInBundle

from _contract import contract_validate

FIXTURE = Path(__file__).parent / "fixtures" / "arc_1_2" / "output.yml"
CODE = "one_electron_hf_substitution"
MISMATCH = "enthalpy_atom_corrections_level_mismatch"
F12 = {"method": "dlpno-ccsd(t)-f12", "basis": "cc-pvtz-f12", "auxiliary_basis": "aug-cc-pvtz/c",
       "cabs": "cc-pvtz-f12-cabs", "method_type": "wavefunction"}
HF = {**F12, "method": "hf"}


def _thermo(corrections_level):
    doc = yaml.safe_load(FIXTURE.read_text())
    thermo = copy.deepcopy(next(s for s in doc["species"] if s["label"] == "CH4")["thermo"])
    thermo["atom_corrections_applied"] = True
    thermo["atom_corrections_level"] = corrections_level
    return thermo


def _build(energy_level, corrections_level, electron_count, target="BundleThermoIn"):
    warnings = []
    block = _build_thermo_block(
        _thermo(corrections_level), calc_keys_by_role={}, target_model=target, warnings=warnings,
        energy_level=energy_level, electron_count=electron_count)
    return block, warnings


def _kept(block):
    return block is not None and block["h298_kj_mol"] == -74.6 and \
        block["enthalpy_reference_kind"] == "formation_298k" and "nasa" in block


def _codes(warnings):
    return [w["code"] for w in warnings]


def test_the_h_atom_at_hf_under_dlpno_f12_corrections_keeps_its_enthalpy():
    block, warnings = _build(HF, F12, 1)
    assert _kept(block)
    assert [p["h_kj_mol"] for p in block["points"]] == [-74.5, -36.1]
    for model in (BundleThermoIn, ThermoInBundle):
        contract_validate(model, block)
    (warning,) = warnings
    assert warning["code"] == CODE
    assert warning["field"] == "thermo"
    assert warning["context"] == {
        "source": "tckdb_arc_self_check", "action": "one_electron_hf_enthalpy_accepted",
        "energy_level": "hf/cc-pvtz-f12", "atom_corrections_level": "dlpno-ccsd(t)-f12/cc-pvtz-f12",
        "cabs_singles_correction": "absent"}
    assert "CABS singles" in warning["message"]


def test_a_non_f12_correlated_level_is_accepted_without_the_cabs_caveat():
    level = {"method": "ccsd(t)", "basis": "cc-pvtz"}
    block, warnings = _build({**level, "method": "hf"}, level, 1)
    assert _kept(block)
    (warning,) = warnings
    assert warning["code"] == CODE and "cabs_singles_correction" not in warning["context"]


def test_the_warning_names_the_species_field_on_a_reaction_participant():
    warnings = []
    _build_thermo_block(
        _thermo(F12), calc_keys_by_role={}, target_model="BundleThermoIn", warnings=warnings,
        warning_field="species[r1].thermo", energy_level=HF, electron_count=1)
    assert [w["field"] for w in warnings] == ["species[r1].thermo"]


@pytest.mark.parametrize("electrons", [2, 0, 3, 10, None])
def test_any_other_electron_count_is_refused_as_before(electrons):
    """H- (two electrons), H+ (none), He+ , an unknown count: no exemption."""
    block, warnings = _build(HF, F12, electrons)
    assert block is not None and "h298_kj_mol" not in block and "nasa" not in block
    assert _codes(warnings) == [MISMATCH]


def test_h2_at_hf_is_refused():
    block, warnings = _build(HF, F12, 2)
    assert "h298_kj_mol" not in block
    assert _codes(warnings) == [MISMATCH]


@pytest.mark.parametrize("changed", [
    {"basis": "cc-pvdz-f12"},
    {"auxiliary_basis": "aug-cc-pvqz/c"},
    {"cabs": "cc-pvdz-f12-cabs"},
    {"auxiliary_basis": None},
    {"dispersion": "d3bj"},
    {"solvent": "water", "solvation_method": "cpcm"},
], ids=["basis", "aux", "cabs", "aux_absent", "dispersion", "solvent"])
def test_hf_in_another_identity_field_is_refused(changed):
    energy = {**HF, **changed}
    block, warnings = _build(energy, F12, 1)
    assert "h298_kj_mol" not in block
    assert MISMATCH in _codes(warnings) or "enthalpy_atom_corrections_level_unverifiable" in _codes(warnings)
    assert CODE not in _codes(warnings)


def test_hf_under_a_dft_corrections_level_is_refused():
    dft = {"method": "b3lyp", "basis": "cc-pvtz-f12", "auxiliary_basis": "aug-cc-pvtz/c",
           "cabs": "cc-pvtz-f12-cabs", "method_type": "dft"}
    block, warnings = _build(HF, dft, 1)
    assert "h298_kj_mol" not in block
    assert _codes(warnings) == [MISMATCH]


def test_a_non_hf_energy_level_is_not_exempt():
    block, warnings = _build({**F12, "method": "mp2"}, F12, 1)
    assert "h298_kj_mol" not in block
    assert _codes(warnings) == [MISMATCH]


def test_uhf_is_not_the_hf_identity_key():
    block, warnings = _build({**HF, "method": "uhf"}, F12, 1)
    assert "h298_kj_mol" not in block
    assert _codes(warnings) == [MISMATCH]


def test_an_energy_level_equal_to_the_corrections_level_needs_no_exemption():
    block, warnings = _build(F12, F12, 1)
    assert _kept(block)
    assert warnings == []


def test_the_predicate_requires_exactly_one_electron():
    assert _one_electron_hf_substitution(HF, F12, 1) is True
    for count in (0, 2, None, True):
        assert _one_electron_hf_substitution(HF, F12, count) is False


def _h_submission(tmp_path, *, charge=0, corrections=F12):
    """The computed-species route on the fixture's H atom, its energy level HF and its corrections F12."""
    from test_thermo_atom_corrections import _doc, _species, _submit

    doc = _doc(sp_level={**HF, "software": "orca"})
    record = _species(doc, "H")
    record["charge"] = charge
    record["thermo"]["atom_corrections_applied"] = True
    record["thermo"]["atom_corrections_level"] = corrections
    # The fixture's H atom carries no recorded route; its energy is the one the doc's sp_level states.
    record.pop("levels", None)
    return _submit(tmp_path, doc, record)


def test_the_species_route_keeps_the_h_atoms_enthalpy_and_warns(tmp_path):
    thermo, warnings = _h_submission(tmp_path)
    assert thermo["enthalpy_reference_kind"] == "formation_298k" and "h298_kj_mol" in thermo
    assert [w["code"] for w in warnings if w["code"] == CODE] == [CODE]
    assert MISMATCH not in _codes(warnings)


def test_the_species_route_still_refuses_a_hydride(tmp_path):
    thermo, warnings = _h_submission(tmp_path, charge=-1)
    assert "h298_kj_mol" not in thermo
    assert MISMATCH in _codes(warnings) and CODE not in _codes(warnings)
