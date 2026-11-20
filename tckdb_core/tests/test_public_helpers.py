"""Helpers a producer imports have public names, and the sets TCKDB owns are read from tckdb-schemas."""

import pytest
from tckdb_schemas.enums import ArtifactKind
from tckdb_schemas.fragments.artifact import KIND_ALLOWED_EXTENSIONS
from tckdb_schemas.fragments.calculation_origin import CalculationOriginKind

from tckdb_core import config, constants, isotopes, keys, outcomes, payload_writer, uploader

ALIASES = [
    (keys, "safe_key_part", "_safe_key_part"),
    (keys, "local_key_for_actor", "_local_key_for_actor"),
    (keys, "calc_prefix_for_actor", "_calc_prefix_for_actor"),
    (keys, "KEY_PART_RE", "_KEY_PART_RE"),
    (isotopes, "xyz_symbols", "_xyz_symbols"),
    (isotopes, "SMILES_ISOTOPE_RE", "_SMILES_ISOTOPE_RE"),
    (payload_writer, "utcnow_iso", "_utcnow_iso"),
    (outcomes, "PreparedArtifactUpload", "_PreparedArtifactUpload"),
    (outcomes, "ArtifactBatchResult", "_ArtifactBatchResult"),
    (constants, "OUTPUT_LOG_ALLOWED_EXTS", "_OUTPUT_LOG_ALLOWED_EXTS"),
    (uploader, "coerce_artifact_filename", "_coerce_artifact_filename"),
    (uploader, "artifact_batch_digest", "_artifact_batch_digest"),
    (uploader, "artifact_batch_idempotency_prefix", "_artifact_batch_idempotency_prefix"),
    (uploader, "preflight_sleep", "_preflight_sleep"),
]


@pytest.mark.parametrize("module, public, earlier", ALIASES, ids=[f"{m.__name__}.{p}" for m, p, _ in ALIASES])
def test_the_earlier_underscore_name_is_the_public_object(module, public, earlier):
    assert getattr(module, earlier) is getattr(module, public)


def test_key_helpers_keep_their_output():
    assert keys.safe_key_part("C-H.O 3") == "CHO3"
    assert keys.safe_key_part("***") == "x" and keys.safe_key_part(None) == "x"
    assert keys.local_key_for_actor("r", 0, "CH3.") == "r0_CH3"
    assert keys.calc_prefix_for_actor("p", 1) == "p1"


def test_xyz_symbols_reads_bare_and_headed_xyz():
    assert isotopes.xyz_symbols("O 0 0 0\nH 0 0 1\nH 0 1 0") == ["O", "H", "H"]
    assert isotopes.xyz_symbols("2\ncomment\nH 0 0 0\nH 0 0 0.74") == ["H", "H"]
    assert isotopes.xyz_symbols(None) is None


def test_the_base_class_sleeps_through_the_public_name(monkeypatch):
    slept = []
    monkeypatch.setattr(uploader, "preflight_sleep", slept.append)
    uploader.TCKDBUploaderBase._sleep_between_probes(object(), 0.25)
    assert slept == [0.25]


def test_artifact_kinds_are_the_shipped_enum():
    assert config.VALID_ARTIFACT_KINDS == frozenset(kind.value for kind in ArtifactKind)
    # ``hessian`` was missing while the set was a hand-kept copy.
    assert "hessian" in config.VALID_ARTIFACT_KINDS
    assert config.IMPLEMENTED_ARTIFACT_KINDS <= config.VALID_ARTIFACT_KINDS
    assert set(config.DEFAULT_ARTIFACT_KINDS) <= config.IMPLEMENTED_ARTIFACT_KINDS


def test_origin_kinds_and_log_extensions_are_the_shipped_ones():
    assert constants.VALID_TCKDB_ORIGIN_KINDS == frozenset(kind.value for kind in CalculationOriginKind)
    assert constants.VALID_TCKDB_ORIGIN_KINDS == {"executed", "reused_result", "imported", "derived"}
    assert constants.OUTPUT_LOG_ALLOWED_EXTS == frozenset(KIND_ALLOWED_EXTENSIONS[ArtifactKind.output_log])
    assert constants.OUTPUT_LOG_ALLOWED_EXTS == {".log", ".out", ".orca"}
    assert uploader.coerce_artifact_filename("stringfile.xyz0000", "output_log") == "stringfile.xyz0000.log"
    assert uploader.coerce_artifact_filename("run.out", "output_log") == "run.out"
    assert uploader.coerce_artifact_filename("deck.gjf", "input") == "deck.gjf"
