"""Path-search calculations carry their whole path: ORCA NEB images and GSM node energies.

Fixtures (``fixtures/path_search``, provenance in its README):

* ``orca_6_0_neb_ts/input.log``: ARC's ORCA 6.0.0 NEB-TS run for i-C3H7 <=> n-C3H7, trimmed.
* ``orca_6_1_neb_ci/orca.out``: an ORCA 6.1.0 NEB-CI run (100 intermediate images), trimmed.
* ``xtb_gsm_ic3h7``: ARC's xtb GSM run for the same reaction (stringfile, per-node xTB
  ``energy``/``gradient`` files) and the GSM parser evidence ARC builds from them.

Expected numbers are read off the files by hand.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from _contract import contract_validate
from test_arc_schema_1_3_levels import _adapter, _project, _reaction_payload, _record, _ts_calcs
from tckdb_arc.adapter import _build_path_search_result_payload
from tckdb_arc.evidence import _validate_gsm
from tckdb_arc.orca_neb import parse_orca_neb_log
from tckdb_core.physical_constants import E_h_kJmol
from tckdb_schemas.fragments.calculation import PathSearchResultPayload

FIXTURES = Path(__file__).parent / "fixtures" / "path_search"
NEB_6_0 = FIXTURES / "orca_6_0_neb_ts" / "input.log"
NEB_6_1 = FIXTURES / "orca_6_1_neb_ci" / "orca.out"
GSM_DIR = FIXTURES / "xtb_gsm_ic3h7"


def _gsm_evidence() -> dict:
    return json.loads((GSM_DIR / "gsm_evidence.json").read_text())["value"]


# ---------------------------------------------------------------------------
# ORCA NEB log parser
# ---------------------------------------------------------------------------

def test_orca_6_0_neb_ts_band():
    band = parse_orca_neb_log(NEB_6_0)
    assert band["program_version"] == "6.0.0"
    assert band["converged"] is True
    images = band["images"]
    # 15 intermediate images plus the two end points; the NEB-TS summary's TS row is not an image.
    assert len(images) == 17
    assert band["climbing_image_index"] == 6
    assert images[0] == {
        "index": 0, "distance_angstrom": 0.0, "energy_hartree": -118.48513,
        "max_force_hartree_per_bohr": 0.00019, "rms_force_hartree_per_bohr": 0.00005,
        "climbing": False, "atoms": None,
    }
    assert images[16]["distance_angstrom"] == 6.066
    assert images[16]["energy_hartree"] == -118.47900
    ci = images[6]
    assert (ci["distance_angstrom"], ci["energy_hartree"]) == (2.216, -118.41268)
    assert (ci["max_force_hartree_per_bohr"], ci["rms_force_hartree_per_bohr"]) == (0.00179, 0.00082)
    assert ci["climbing"] is True
    assert ci["atoms"][0] == "C -0.322156 -0.044561 1.358703"
    assert ci["atoms"][-1] == "H -0.279112 0.647028 -1.925949"
    assert len(ci["atoms"]) == 10
    assert [image["index"] for image in images if image["climbing"]] == [6]
    assert all(image["atoms"] is None for image in images if not image["climbing"])


def test_orca_6_1_neb_ci_band():
    band = parse_orca_neb_log(NEB_6_1)
    assert band["program_version"] == "6.1.0-f.0"
    assert band["converged"] is True
    images = band["images"]
    assert len(images) == 102
    assert band["climbing_image_index"] == 58
    assert (images[0]["energy_hartree"], images[0]["distance_angstrom"]) == (-1967.19636, 0.0)
    assert (images[101]["energy_hartree"], images[101]["distance_angstrom"]) == (-1967.14364, 11.901)
    ci = images[58]
    assert (ci["energy_hartree"], ci["distance_angstrom"]) == (-1967.12166, 6.718)
    assert (ci["max_force_hartree_per_bohr"], ci["rms_force_hartree_per_bohr"]) == (0.00034, 0.00011)
    assert ci["atoms"][0] == "C -1.720784 -0.734672 -1.397304"
    assert len(ci["atoms"]) == 18


def test_neb_convergence_is_null_without_orca_banner(tmp_path):
    text = NEB_6_0.read_text().replace("THE NEB OPTIMIZATION HAS CONVERGED", "")
    log = tmp_path / "input.log"
    log.write_text(text)
    band = parse_orca_neb_log(log)
    assert band["converged"] is None
    assert len(band["images"]) == 17


def test_neb_log_without_band_summary_is_not_parsed(tmp_path):
    lines = NEB_6_0.read_text().splitlines()
    log = tmp_path / "input.log"
    log.write_text("\n".join(line for line in lines if line.strip() != "PATH SUMMARY"))
    assert parse_orca_neb_log(log) is None
    assert parse_orca_neb_log(tmp_path / "missing.log") is None


def test_saddle_geometry_is_not_attached_when_the_climbing_image_line_disagrees(tmp_path):
    text = NEB_6_0.read_text().replace("Climbing image                            ....  6",
                                       "Climbing image                            ....  7")
    log = tmp_path / "input.log"
    log.write_text(text)
    band = parse_orca_neb_log(log)
    assert band["climbing_image_index"] == 6
    assert band["images"][6]["atoms"] is None


# ---------------------------------------------------------------------------
# NEB path_search_result payload
# ---------------------------------------------------------------------------

def test_neb_payload_carries_every_image():
    payload = _build_path_search_result_payload(method="neb", log_path=str(NEB_6_0), fallback_xyz_text=None)
    contract_validate(PathSearchResultPayload, payload)
    assert payload["n_points"] == 17
    assert payload["converged"] is True
    assert payload["climbing_image_index"] == 6
    assert payload["selected_ts_point_index"] == 6
    assert payload["is_double_ended"] is True
    assert payload["zero_energy_reference_hartree"] == -118.48513
    points = {p["point_index"]: p for p in payload["points"]}
    assert points[0]["electronic_energy_hartree"] == -118.48513
    assert points[0]["relative_energy_kj_mol"] == 0.0
    assert points[16]["path_coordinate"] == 6.066
    ci = points[6]
    assert ci["is_climbing_image"] is True and ci["is_ts_guess"] is True
    assert ci["max_force"] == 0.00179 and ci["rms_force"] == 0.00082
    assert ci["relative_energy_kj_mol"] == pytest.approx((-118.41268 + 118.48513) * E_h_kJmol)
    assert ci["geometry"]["xyz_text"].startswith("10\nneb_image_6\nC -0.322156")
    others = [p for i, p in points.items() if i != 6]
    assert not any(p.get("is_climbing_image") or p.get("is_ts_guess") or p.get("geometry") for p in others)


def test_unparseable_neb_log_keeps_the_single_guess_point(tmp_path):
    log = tmp_path / "input.log"
    log.write_text("no NEB here\n")
    payload = _build_path_search_result_payload(
        method="neb", log_path=str(log), fallback_xyz_text="H 0 0 0\nH 0 0 0.74")
    assert payload["n_points"] == 1
    assert "converged" not in payload and "climbing_image_index" not in payload
    assert payload["points"][0]["is_ts_guess"] is True


# ---------------------------------------------------------------------------
# GSM: node energies from ARC's parser evidence
# ---------------------------------------------------------------------------

def test_gsm_fixture_evidence_is_what_arc_builds_from_the_node_files():
    value = _gsm_evidence()
    _validate_gsm(value)
    by_id = {item["invocation_id"]: item for item in value["ograd_invocations"]}
    # 0000.01.energy holds -9.89904684440; 0000.08.energy holds -9.60301569407.
    assert by_id["0000.01"]["electronic_energy_hartree"] == -9.8990468444
    assert by_id["0000.08"]["electronic_energy_hartree"] == -9.60301569407
    # Every stringfile comment line is 0.000000: ARC's xtb_gsm build writes no energies there.
    assert all("stringfile_relative_energy_kcal_mol" not in p for p in value["points"])


def test_gsm_payload_carries_node_energies():
    payload = _build_path_search_result_payload(
        method="gsm", log_path=str(GSM_DIR / "stringfile.xyz0000"), fallback_xyz_text=None,
        gsm_evidence=_gsm_evidence())
    contract_validate(PathSearchResultPayload, payload)
    assert payload["n_points"] == 15
    assert payload["selected_ts_point_index"] == 8
    assert "converged" not in payload
    points = {p["point_index"]: p for p in payload["points"]}
    # The reactant end point was never evaluated by the string's gradient calls: no energy.
    assert "electronic_energy_hartree" not in points[0]
    assert points[1]["electronic_energy_hartree"] == -9.88997851517      # 0000.02.energy
    assert points[7]["electronic_energy_hartree"] == -9.60301569407      # 0000.08.energy
    assert points[8]["electronic_energy_hartree"] == -9.63867284285      # 0000.09.energy
    assert points[14]["electronic_energy_hartree"] == -9.8990468444      # 0000.01.energy
    assert sum("electronic_energy_hartree" in p for p in points.values()) == 14
    assert payload["zero_energy_reference_hartree"] == -9.8990468444
    assert points[14]["relative_energy_kj_mol"] == 0.0
    assert points[8]["is_ts_guess"] is True
    assert points[8]["max_gradient"] > 0 and points[8]["rms_gradient"] > 0
    assert all(not p.get("is_climbing_image") for p in points.values())


# ---------------------------------------------------------------------------
# Whole requests (the conftest hook validates each against tckdb-schemas)
# ---------------------------------------------------------------------------

def test_reaction_upload_files_the_gsm_energies_from_parser_evidence(tmp_path):
    doc = _project(tmp_path)
    result = _ts_calcs(_reaction_payload(_adapter(tmp_path), doc))["ts_guess"]["path_search_result"]
    assert result["n_points"] == 15
    energies = {p["point_index"]: p.get("electronic_energy_hartree") for p in result["points"]}
    # The arc_1_3_levels fixture archives three node outputs; ARC matched each to one frame.
    assert {i: e for i, e in energies.items() if e is not None} == {
        1: -9.88997851517, 2: -9.84219711856, 14: -9.8990468444}


def test_reaction_upload_without_parser_evidence_sends_no_gsm_energies(tmp_path):
    doc = _project(tmp_path, with_sidecar=False)
    doc.pop("parser_evidence", None)
    result = _ts_calcs(_reaction_payload(_adapter(tmp_path), doc))["ts_guess"]["path_search_result"]
    # Geometries come from the stringfile when ARC is importable (else the single guess point);
    # the node files' energies are never attached by index.
    assert not any("electronic_energy_hartree" in p for p in result["points"])


def test_reaction_upload_files_every_neb_image(tmp_path):
    doc = _project(tmp_path)
    neb_dir = tmp_path / "calcs" / "TS_guesses" / "TS0" / "tsg2"
    neb_dir.mkdir(parents=True)
    shutil.copy(NEB_6_0, neb_dir / "input.log")
    ts = _record(doc, "TS0")
    ts["chosen_ts_method"] = "orca_neb"
    ts["ts_guesses"] = [{"chosen": True, "index": 0, "method": "orca_neb", "method_sources": ["orca_neb"]}]
    ts["gsm_log"] = None
    ts["neb_log"] = "calcs/TS_guesses/TS0/tsg2/input.log"
    ts["ess_software"]["neb"] = "orca"
    ts["ess_versions"]["neb"] = "Program Version 6.0.0"
    doc["neb_level"] = {"method": "wb97x-d3", "basis": "def2-tzvp", "software": "orca"}
    guess = _ts_calcs(_reaction_payload(_adapter(tmp_path), doc))["ts_guess"]
    result = guess["path_search_result"]
    assert result["method"] == "neb"
    assert result["n_points"] == 17
    assert result["converged"] is True
    assert result["climbing_image_index"] == result["selected_ts_point_index"] == 6
    assert sum("electronic_energy_hartree" in p for p in result["points"]) == 17
    assert [p["point_index"] for p in result["points"] if "geometry" in p] == [6]
