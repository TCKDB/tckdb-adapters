"""The ``tckdb.rights`` block is sent as the upload's ``rights`` fragment on every upload kind."""
import json
import os
import shutil
from pathlib import Path
from unittest import mock

import pytest
import yaml

from tckdb_arc.adapter import TCKDBAdapter
from tckdb_arc.config import TCKDBConfig
from test_adapter import _StubClient, _StubResponse

FIXTURES = Path(__file__).parent / "fixtures"
RIGHTS = {"license": "CC-BY-4.0", "depositor_attests_right_to_license": True}


def _config(tmp_path, rights):
    raw = {
        "enabled": True, "base_url": "http://localhost:8000/api/v1", "payload_dir": str(tmp_path),
        "api_key_env": "X_TCKDB_API_KEY", "project_label": "rights", "upload": False, "preflight": False,
    }
    if rights is not None:
        raw["rights"] = rights
    return TCKDBConfig.from_dict(raw)


def _payloads(tmp_path, rights):
    adapter = TCKDBAdapter(
        _config(tmp_path, rights), project_directory=str(tmp_path),
        client_factory=lambda c, k: _StubClient(response=_StubResponse({"id": 1})),
    )
    golden = FIXTURES / "golden"
    doc = yaml.safe_load((golden / "phase3_output.yml").read_text())
    (tmp_path / "output").mkdir()
    shutil.copyfile(golden / "tckdb_evidence.json", tmp_path / "output" / "tckdb_evidence.json")
    species = next(r for r in doc["species"] if r.get("xyz") and r.get("opt_final_energy_hartree") is not None)
    with mock.patch.dict(os.environ, {"X_TCKDB_API_KEY": "tck_x"}):
        outcomes = {
            "conformer": adapter.submit_from_output(output_doc=doc, species_record=species),
            "computed_species": adapter.submit_computed_species_from_output(output_doc=doc, species_record=species),
            "computed_reaction": adapter.submit_computed_reaction_from_output(
                output_doc=doc, reaction_record=doc["reactions"][0]),
            "transition_state": adapter.submit_computed_ts_from_output(
                output_doc=doc, ts_record=doc["transition_states"][0], reaction_record=doc["reactions"][0]),
        }
    return {kind: json.loads(o.payload_path.read_text()) for kind, o in outcomes.items()}


def test_every_upload_kind_carries_the_configured_rights(tmp_path):
    payloads = _payloads(tmp_path, RIGHTS)
    assert {kind: p.get("rights") for kind, p in payloads.items()} == dict.fromkeys(payloads, RIGHTS)


def test_no_rights_block_sends_no_rights_fragment(tmp_path):
    payloads = _payloads(tmp_path, None)
    assert [kind for kind, p in payloads.items() if "rights" in p] == []


@pytest.mark.parametrize("bad", [
    {"license": "CC-BY-4.0"},
    {"license": "CC-BY-4.0", "depositor_attests_right_to_license": False},
    {"license": "", "depositor_attests_right_to_license": True},
])
def test_a_rights_block_the_schema_refuses_is_a_config_error(tmp_path, bad):
    with pytest.raises(ValueError, match="tckdb.rights"):
        _config(tmp_path, bad)
