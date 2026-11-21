"""Adapter that turns an ARC ``output.yml`` record into a TCKDB conformer upload.

The adapter is the only ARC module that knows the shape of a TCKDB
upload. It builds a JSON payload matching ``ConformerUploadRequest``,
writes it to disk, and (optionally) hands it to ``tckdb-client``.

Source of truth for upload data is ``<project>/output/output.yml`` —
``arc/output.py`` was designed for this consumer (see its module
docstring). The adapter therefore takes two dicts: the full output
document (for top-level levels-of-theory, ARC version, etc.) and one
species record from ``output_doc['species']`` or
``output_doc['transition_states']``. This keeps the adapter decoupled
from ARC's live object model and makes a separate replay path (read
output.yml later, post payloads, no ARC needed) trivial.

Three guarantees:

1. If the adapter is disabled or no config is provided, it is a no-op.
2. The payload is on disk *before* any network call. Replay tooling
   only needs ``payload_file + endpoint + idempotency_key``.
3. By default, an upload failure is logged + recorded in the sidecar
   but does not raise. ``strict=True`` flips that.
"""

import base64
import hashlib
import json
import math
import os
import re
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from tckdb_client import TCKDBClient
from tckdb_client.errors import TCKDBError
from tckdb_schemas.enthalpy_reference import enthalpy_reference_error
from tckdb_schemas.fragments.refs import (
    correction_table_method_stem,
    W_SOFTWARE_RELEASE_VERSION_IS_COMPOSITE,
    SoftwareReleaseRef,
)
from tckdb_schemas.utils import normalize_tunneling_model

from tckdb_arc import arc13
from tckdb_arc._logging import LOGGER_NAME as _LOGGER_NAME
from tckdb_arc._logging import get_logger
from tckdb_arc.adaptive import (
    RUN_LEVEL,
    UNDETERMINABLE,
    AdaptiveLevels,
    RestartInfo,
    detect_adaptive_levels,
    read_restart_info,
)
from tckdb_arc.warning_codes import ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE, ArcWarning
from tckdb_arc.config import (
    IMPLEMENTED_ARTIFACT_KINDS,
    TCKDBConfig,
    UPLOAD_MODE_COMPUTED_REACTION,
    UPLOAD_MODE_COMPUTED_SPECIES,
)

from tckdb_arc.evidence import EvidenceStore
from tckdb_core.constraints import serialize_constraints
from tckdb_core.level_rules import method_identity_key
from tckdb_core.method_families import ONE_ELECTRON_EXEMPT_FAMILIES, method_family
from tckdb_core.payload_writer import (
    ArtifactSidecarMetadata,
    PayloadWriter,
    SidecarMetadata,
    WrittenArtifact,
    WrittenPayload,
    _utcnow_iso,
)
# The producer-agnostic upload pipeline, outcome types, endpoint constants and
# key helpers moved to ``tckdb_core``. Every name that lived here is
# re-exported so ``tckdb_arc.adapter.<name>`` keeps resolving for callers and
# tests; the leading-underscore names are part of that surface.
from tckdb_core import composition as _composition
from tckdb_core import isotopes as _isotopes
from tckdb_core import reaction_flatten as _reaction_flatten
from tckdb_core import rmg_units as _rmg_units
from tckdb_core import rules as _rules
from tckdb_core import software_release as _software_release
from tckdb_core import thermo_numerics as _thermo_numerics
from tckdb_core import ts_evidence as _ts_evidence
from tckdb_core.adapter_warnings import AdapterWarning, WarningSink
from tckdb_core.constants import (  # noqa: F401
    ARTIFACTS_ENDPOINT_TEMPLATE,
    COMPUTED_REACTION_ENDPOINT,
    COMPUTED_REACTION_KIND,
    COMPUTED_SPECIES_ENDPOINT,
    COMPUTED_SPECIES_KIND,
    CONFORMER_UPLOAD_ENDPOINT,
    PAYLOAD_KIND,
    PREFLIGHT_BASE_DELAY_SECONDS,
    PREFLIGHT_MAX_ATTEMPTS,
    PREFLIGHT_MAX_DELAY_SECONDS,
    TRANSITION_STATE_ENDPOINT,
    TRANSITION_STATE_KIND,
    VALID_TCKDB_ORIGIN_KINDS,
    _OUTPUT_LOG_ALLOWED_EXTS,
)
from tckdb_core.keys import (  # noqa: F401
    _KEY_PART_RE,
    _calc_prefix_for_actor,
    _local_key_for_actor,
    _safe_key_part,
)
from tckdb_core.outcomes import (  # noqa: F401
    ArtifactUploadOutcome,
    TCKDBReadinessError,
    UploadOutcome,
    _ArtifactBatchResult,
    _PreparedArtifactUpload,
)
from tckdb_core.uploader import (  # noqa: F401
    _BUNDLE_MODES_WITH_INLINE_ARTIFACTS,
    _PUBLIC_REF_ECHO_KEYS,
    _W_CALCULATION_REF_NOT_RETURNED,
    TCKDBUploaderBase,
    _append_request_id,
    _artifact_batch_bodies,
    _artifact_batch_digest,
    _attach_preflight,
    _build_readiness_error,
    _close_quietly,
    _coerce_artifact_filename,
    _extract_calc_refs,
    _extract_submission_refs,
    _extract_tckdb_public_refs,
    _format_readiness_message,
    _headers_from,
    _preflight_sleep,
    _readyz_body_is_ready,
    _request_id_from,
    _server_warnings,
    _skip,
    _summarize_artifact_batch_results,
    _summarize_response_body,
)
# Helpers that moved to sibling modules; every name stays importable from here.
from tckdb_arc.self_check import (  # noqa: F401
    _PRODUCER_TAG,
    _self_check,
)
from tckdb_arc.geometry import (  # noqa: F401
    _assert_isotopes_reconcile,
    _conformer_isotopes,
    _formula,
    _formula_element_symbols,
    _FORMULA_TERM_RE,
    _geometry_payload,
    _is_single_atom_geometry,
    _isotopic_substitutions,
    _normalize_xyz_text,
    _require_xyz_text,
    _species_element_symbols,
    _xyz_element_symbols,
)
from tckdb_arc.levels import (  # noqa: F401
    _adaptive_kind_named,
    _adaptive_level_status,
    _ADAPTIVE_LEVELS_KEY,
    _adaptive_marker,
    _bac_frequency_level,
    _canonical_dispersion,
    _canonical_level_args,
    _conformer_screen_level,
    _describe_level,
    _DISPERSION_SUFFIX_RE,
    _energy_level_unattributable,
    _header_adaptive_job_types,
    _JOB_TYPES_BY_KIND,
    _level_identity,
    _level_keys_match,
    _METHOD_REFIT_YEAR_RE,
    _n_heavy_from_record,
    _normalize_level_name,
    _note_adaptive_omission,
    _opt_level_software,
    _resolve_level,
    _RESTART_KEY,
    _restart_levels,
    _TCKDB_LOT_REF_FIELDS,
    _thermo_energy_level,
)
from tckdb_arc.calculations import (  # noqa: F401
    _ARC_STABILITY_INSTABILITY_TYPE,
    _ARC_STABILITY_VERDICT_TO_STATUS,
    _CALC_KEY_FREQ,
    _CALC_KEY_IRC,
    _CALC_KEY_OPT,
    _CALC_KEY_OPT_COARSE,
    _CALC_KEY_SCAN,
    _CALC_KEY_SP,
    _CALC_KEY_TS_GUESS,
    _coarse_opt_result_payload,
    _conformer_observed_lists,
    _conformer_observed_provenance,
    _CONTRADICTIONS_KEY,
    _designate_reaction_coordinate_index,
    _FINAL_SETTINGS_FIELD_BY_CALC_ROLE,
    _final_settings_for_calc,
    _hessian_parser_block,
    _merge_parameters_json,
    _note_route_contradiction,
    _opt_result_payload,
    _placeholder_origin,
    _reused_origin,
    _ROLE_COMPOSITE,
    _route_observation,
    _route_parameters,
    _scan_job_type,
    _SCAN_ROTOR_KEY_RE,
    _scf_stability_payload,
    _screened_conformer_origin,
    _sp_is_reused_from_opt,
    _sp_reuse_origin,
    _spin_diagnostic_payload,
    _stated_integer,
    _TS_MAJOR_MODE_MAX_CM1,
    _TS_MAJOR_MODE_MIN_CM1,
    _wavefunction_diagnostic_payload,
)
from tckdb_arc.path_search import (  # noqa: F401
    _detect_irc_direction,
    _IRC_DIRECTION_FORWARD,
    _IRC_DIRECTION_REVERSE,
    _irc_level,
    _IRC_NOT_GAUSSIAN_METHODS,
    _LOG_FIELD_BY_CALC_KEY,
    _parse_xtb_turbomole_energy_file,
    _parse_xtb_turbomole_gradient_file,
    _parse_xtb_xtbout_energy,
    _read_gsm_node_outputs,
    _resolve_irc_zero_energy_reference,
    _resolve_log_field,
    _resolve_ts_guess_path_search,
    _resolve_ts_guess_path_search_for_record,
    _TS_GUESS_LOG_FIELD_BY_METHOD,
    _TS_GUESS_PATH_SEARCH_METHODS,
    _TS_GUESS_UPLOAD_LOG_AS_ARTIFACT,
    _with_irc_program,
    _with_observed_irc_program,
    _XTBOUT_TOTAL_ENERGY_RE,
)
from tckdb_arc.thermo import (  # noqa: F401
    _arkane_composite_key_halves,
    _ARKANE_COMPOSITE_KEY_RE,
    _ARKANE_KEY_FIELD_RE,
    _arkane_key_half,
    _ARKANE_KEY_RE,
    _arkane_key_software,
    _ARKANE_KEY_SOFTWARE_RE,
    _atom_params_from_reference_atom_energies,
    _atom_without_sp_energy_error,
    _BAC_COMPONENT_SUM_TOLERANCE,
    _bond_corrections_flag,
    _component_is_usable,
    _ENERGY_UNITS,
    _ENERGY_WITHHELD_KEY,
    _enthalpy_magnitude_error,
    _FORMATION_ENTHALPY_MAX_ABS_KJ_MOL,
    _G4_FAMILY_METHOD_KEYS,
    _g4_loader_energy_unreliable,
    _GAS_CONSTANT_J_MOL_K,
    _GAUSSIAN16_A03_BANNER,
    _has_enthalpy_content,
    _HEADER_CORRECTIONS_LEVEL_SOURCE,
    _is_composite_atom_record,
    _level_field_unverifiable_error,
    _LEVEL_FIELDS_ARC_MATCHING_IGNORES,
    _LIGHT_ATOM_TOTAL_ENERGY_HARTREE,
    _light_species_raw_energy_kj_mol,
    _nasa_h298_kj_mol,
    _norm_level_token,
    _note_omitted_bac_on_thermo,
    _petersson_bac_omission_reason,
    _skipped_bonds_note,
    _strip_enthalpy_content,
    _T298_K,
    _unflagged_enthalpy_error,
    _W_ENTHALPY_ATOM_CORRECTIONS_LEVEL_MISMATCH,
    _W_ENTHALPY_ATOM_CORRECTIONS_LEVEL_UNVERIFIABLE,
    _W_ENTHALPY_FORMATION_UNVERIFIABLE_LIGHT_SPECIES,
    _W_ENTHALPY_NOT_FORMATION_MAGNITUDE,
    _with_composite_role,
)
from tckdb_arc.provenance import (  # noqa: F401
    _arc_aec_yml_digest,
    _arc_analysis_software_release,
    _arc_workflow_tool_release,
    _arkane_workflow_tool_release,
    _DATA_REVISION_MAX_CHARS,
    _index_species,
    _index_transition_states,
    _is_output_schema_1_0,
    _is_output_schema_1_3_or_later,
    _rmg_database_data_revision,
    _RMG_DATABASE_SCHEME_KINDS,
    _scheme_data_revisions,
    _sha256_or_none,
)
from tckdb_arc.statmech import (  # noqa: F401
    _build_statmech_source_calculations,
    _classify_statmech_treatment,
    _coerce_torsion_coordinates,
    _STATMECH_CALC_ROLES,
)
from tckdb_arc.kinetics import (  # noqa: F401
    _any_level_states_solvation,
    _build_kinetics_source_calculations,
    _calculation_electron_counts,
    _calculation_multiplicities,
    _coerce_optional_float,
    _header_levels,
    _kinetics_declarations,
    _KINETICS_DETERMINATION_KEY,
    _kinetics_determination_key,
    _KINETICS_FIT_KEYS,
    _kinetics_interpretation_sentence,
    _KINETICS_ROLE_PRODUCT_ENERGY,
    _KINETICS_ROLE_REACTANT_ENERGY,
    _KINETICS_ROLE_TS_ENERGY,
    _KINETICS_ROLE_TS_FREQ,
    _KINETICS_ROLE_TS_IRC,
    _level_states_solvation,
    _note_kinetics_declarations_interpreted,
    _participant_electron_count,
    _participant_levels_state_solvent,
    _PRESSURE_DEPENDENCE_WORDS,
    _SOLVATION_ARGS_TOKENS,
    _source_levels_state_solvation,
    _stringify_tunneling_model,
    _TUNNELING_MODEL_ATTRS,
    _TUNNELING_MODEL_KEYS,
)
from tckdb_arc.reactions import (  # noqa: F401
    _is_plain_int,
    _participant_slots,
    _ts_route_atom_map_slots,
    _ts_unmapped_smiles_handle,
)
from tckdb_arc.core_aliases import (  # noqa: F401
    _ARC_TO_TCKDB_A_UNITS,
    arc_to_tckdb_a_units,
    _ARC_TO_TCKDB_EA_UNITS,
    arc_to_tckdb_ea_units,
    _build_nasa_block,
    _build_thermo_points,
    _calculations_by_key,
    _finite_float,
    _flatten_all_reaction_calcs,
    _flatten_result_fields,
    _normalize_unit_key,
    _REACTION_FLAT_RESULT_FIELDS,
    _REACTION_MODE_FIELDS_HANDLED,
    _REACTION_MODE_FIELDS_NOT_CARRIED,
    _REACTION_RESULT_FIELDS_HANDLED_ELSEWHERE,
    _REACTION_RESULT_FIELDS_NOT_CARRIED,
    _split_ess_version_banner,
    _XTB_BANNER_RE,
)
from tckdb_arc.level_payloads import (  # noqa: F401
    _arc_level_to_tckdb_lot,
    _ARC_TO_TCKDB_LOT_FIELDS,
    _E_H_KJ_MOL_KIND,
    _energy_level_declaration,
    _same_arc_job_level,
    _stated_conformer_level,
    _stated_conformer_opt_result,
    _stated_conformer_sp,
    _W_LEVEL_METHOD_IS_COMPOUND,
)
from tckdb_arc.calculation_payloads import (  # noqa: F401
    _enforce_geometry_anchoring,
    _FREQ_FIELD_SPECS,
    _freq_result_payload,
    _hessian_geometry,
    _monatomic_sp_result_payload,
    _route_contradiction,
    _serialize_calc_constraints,
    _sp_result_payload,
    _TS_TAU_PROTOCOL_NOT_RECORDED_CM1,
    TSReactionCoordinateNotDesignated,
    _W_CALCULATION_INPUT_GEOMETRY_NOT_DECLARED,
    _W_GEOMETRY_ISOTOPES_NOT_STATED,
    _W_LEVEL_CONTRADICTED_BY_ROUTE,
    _W_TS_REACTION_COORDINATE_NOT_DESIGNATED,
    _withhold_unlabelled_point_geometries,
)
from tckdb_arc.thermo_payloads import (  # noqa: F401
    _APPLIED_CORRECTION_COMPONENT_FIELDS,
    _ATOM_PARAM_APPLICATIONS,
    _bac_key_frequency_level,
    _build_applied_energy_corrections,
    _build_thermo_block,
    _correction_records_from_record,
    _enthalpy_validity_error,
    _FREQ_KEYED_SCHEME_KINDS,
    _one_electron_hf_substitution,
    _scheme_level_of_theory,
    _THERMO_ENTHALPY_REFERENCE_KIND,
    _THERMO_ENTHALPY_WITHHELD_KEY,
    _THERMO_FIELDS_BY_TARGET,
    _thermo_reference_pressure_bar,
    _THERMO_REFERENCE_PRESSURE_WINDOW_BAR,
    _W_ATOM_ENERGY_RECORD_NOT_DEPOSITED_AEC_YML,
    _W_BAC_CORRECTION_OMITTED_COMPONENTS_INCOMPLETE,
    _W_BAC_FREQUENCY_LEVEL_CONFLICT,
    _W_CORRECTION_TABLE_METHOD_SPLIT,
    _W_ENERGY_CORRECTION_SCHEME_SOFTWARE_CONFLICT,
    _W_ENTHALPY_ADAPTIVE_LEVELS_UNVERIFIABLE,
    _W_ENTHALPY_ATOM_CORRECTIONS_NOT_APPLIED,
    _W_ENTHALPY_NOT_FINITE,
    _W_G4_LOADER_SHIFTED_LABEL_GAUSSIAN16_A03,
    _W_THERMO_REFERENCE_PRESSURE_NOT_STATED,
    _withhold_reaction_kinetics,
    _withhold_unreliable_composite_energies,
    _withhold_unreliable_energies_in_doc,
)
from tckdb_arc.statmech_payloads import (  # noqa: F401
    _ARC_TO_TCKDB_TORSION_TREATMENTS,
    _ARKANE_TREATMENTS,
    _build_freq_scale_factor_ref,
    _build_rejected_torsions,
    _build_slim_torsions,
    _build_statmech_block_for_species,
    _neutral_scan_result_to_tckdb,
    _REJECTED_TORSION_NO_REASON,
    _scan_entries_from_record,
    _stated_arkane_treatment,
    _STATMECH_FIELDS_BY_TARGET,
    _TCKDB_RIGID_ROTOR_KINDS,
    _W_FREQ_SCALE_FACTOR_FITTED_FOR_OTHER_LEVEL,
    _W_REJECTED_TORSION_NOT_SENT,
    _W_STATMECH_TREATMENT_NOT_STATED,
    _W_TORSION_NOT_SENT,
    _W_TORSION_SCAN_NOT_BUILT,
    _W_TORSION_WITHOUT_COORDINATES,
)
from tckdb_arc.kinetics_payloads import (  # noqa: F401
    _build_kinetics_block,
    _kinetics_energy_level,
    _MAX_ARRHENIUS_T0_K,
)
from tckdb_arc.path_payloads import (  # noqa: F401
    _build_irc_result_payload,
    _build_path_search_result_payload,
    _GSM_STRINGFILE_ENERGY_EPS,
    _IRC_DIRECTION_BOTH,
    _irc_endpoint_skip,
    _KCAL_MOL_TO_KJ_MOL,
    _NEB_GUESS_NOTE,
    _NEB_TS_GUESS_NOTE,
    _PATH_SEARCH_METHOD_PROPERTIES,
    _W_IRC_DIRECTION_NOT_STATED,
    _W_IRC_ENDPOINT_SPECIES_SKIPPED,
)
from tckdb_arc.ts_evidence_payloads import (  # noqa: F401
    _ENERGY_ORDERING_MARGIN_KJ_MOL,
    _irc_participant_mappings,
    _ts_energy_ordering_validation_evidence,
    _ts_imaginary_mode_validation_evidence,
    _ts_irc_validation_evidence,
    _W_IRC_PARTICIPANT_MAPPING_NOT_SENT,
    _W_TS_ENERGY_ORDERING_NOT_SENT,
    _W_TS_IMAGINARY_MODE_NOT_SENT,
    _W_TS_IRC_EVIDENCE_WITHOUT_IRC_CALCULATION,
    _W_TS_NMD_FORCED_CONTRADICTS_INDEX,
    _warn_ts_evidence_not_sent,
)
from tckdb_arc.reaction_payloads import (  # noqa: F401
    _tckdb_reaction_atom_map,
    _TS_ATOM_MAP_METHOD,
    _TS_ATOM_MAP_NOTE,
    _W_ATOM_MAP_TS_ORDER_NOT_STATED,
    _W_REACTION_SPECIES_LABELS_CONTRADICTED,
    _W_REACTION_STOICHIOMETRY_NOT_STATED,
    _W_REACTION_TS_ATOM_MAP_NOT_SENT,
    _warn_atom_map_not_sent,
    _warn_ts_atom_map_not_sent,
    _with_stated_participants,
)
from tckdb_arc.finding_warnings import (  # noqa: F401
    _W_ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE,
    _W_BAC_TYPE_NOT_STATED,
    _W_COMPOSITE_GEOMETRY_LEVEL_NOT_STATED,
    _W_CONFORMER_LEVEL_NOT_STATED,
    _W_CONFORMER_NOT_ESS_OPTIMIZED,
    _W_CONFORMER_PROGRAM_NOT_STATED,
    _W_IRC_LEVEL_ASSUMED_OPT_LEVEL,
    _W_IRC_LEVEL_NOT_STATED,
    _W_PRIMARY_OPT_PLACEHOLDER_NO_OPT_JOB,
    _W_TS_GUESS_LEVEL_NOT_STATED,
    _W_TS_GUESS_SOFTWARE_NOT_STATED,
    _warn_adaptive_omissions,
    _warn_bac_type_not_stated,
    _warn_conformer_level_not_stated,
    _warn_conformer_not_esss_optimized,
    _warn_conformer_program_not_stated,
    _warn_irc_level_assumed,
    _warn_irc_level_not_stated,
    _warn_isotopes_not_stated,
    _warn_primary_opt_placeholder,
    _warn_ts_guess_level_not_stated,
)


logger = get_logger()


# Bundle-only calculation keys that ``_build_ts_block`` layers onto each
# calc dict for the computed-reaction wire shape (local cross-reference
# identity + dependency edges). The standalone TS endpoint consumes a
# plain ``CalculationWithResultsPayload`` (``extra="forbid"``) for
# primary_opt / additional_calculations, so these must be stripped before
# upload. Dependency wiring (e.g. path_search -> primary_opt) is
# re-derived server-side for the standalone endpoint.
_TS_STANDALONE_STRIP_CALC_KEYS = ("key", "depends_on", "geometry_key", "artifacts")


# Version tag stamped onto every parsed Hessian payload. Bump when the
# Cartesian-Hessian parsing in ``arc/parser/adapters/{gaussian,orca}.py``
# changes in a way that would alter the emitted lower-triangle values.
_HESSIAN_PARSER_VERSION = "arc-hessian-1"


# ESS name (from ``arc.parser.parser.determine_ess``) → TCKDB
# ``HessianSource`` enum value. Gaussian prints the Hessian inside the log
# (``parsed_log``); Orca writes it to a sibling ``.hess`` file
# (``parsed_hess``). Other ESS engines have no Cartesian-Hessian parser
# wired up, so they are absent and the Hessian is simply skipped.
_HESSIAN_SOURCE_BY_ESS = {
    "gaussian": "parsed_log",
    "orca": "parsed_hess",
}

# Same shape, for input-deck paths. `arc/output.py` emits these per-job
# (with per-job software → per-job filename), only when the deck file
# actually exists on disk; null when the deck wasn't kept (archived runs).
# (No coarse_opt_input field today — coarse jobs share the engine's
# input filename with the fine opt; the deck is keyed off the log dir.)
_INPUT_FIELD_BY_CALC_KEY = {
    _CALC_KEY_OPT: "opt_input",
    _CALC_KEY_FREQ: "freq_input",
    _CALC_KEY_SP: "sp_input",
}

# Held-fixed coordinate constraints emitted onto the species record by
# ``arc/output.py::_spc_to_dict`` (one list per ESS-job calc). Scan calcs
# carry their constraints inline on the ``additional_calculations`` entry
# rather than via species_record, so they're not in this map.
_CONSTRAINTS_FIELD_BY_CALC_KEY = {
    _CALC_KEY_OPT: "opt_constraints",
    _CALC_KEY_FREQ: "freq_constraints",
    _CALC_KEY_SP: "sp_constraints",
}

# (artifact_kind, record-field-map) pairs that the inline-artifact
# helper iterates per calc. Keeping the mapping data-driven so adding
# checkpoints (or any future kind) is one tuple, not a code branch.
_INLINE_ARTIFACT_SOURCES: tuple[tuple[str, dict[str, str]], ...] = (
    ("output_log", _LOG_FIELD_BY_CALC_KEY),
    ("input", _INPUT_FIELD_BY_CALC_KEY),
)

class TCKDBAdapter(TCKDBUploaderBase):
    """Build, write, and optionally upload one conformer/calculation payload.

    The adapter holds the config and a payload writer. ``client_factory``
    is overridable so tests can inject a mocked ``TCKDBClient`` without
    touching the network.

    Reading ARC's output and building the payloads is ARC-specific and lives
    here; the upload, sidecar, readiness and artifact-batch pipeline is the
    producer-agnostic :class:`tckdb_core.uploader.TCKDBUploaderBase`.
    """

    PRODUCER_TAG = _PRODUCER_TAG
    LOGGER_NAME = _LOGGER_NAME
    PRODUCER_NAME = "ARC"
    IDEMPOTENCY_NAMESPACE = "arc"

    @property
    def _log(self):
        # Resolved at call time so ``tckdb_arc.adapter.logger`` (patched by tests
        # and by anyone redirecting ARC's TCKDB logging) sees the shared pipeline's
        # records exactly as it saw them before the pipeline moved to tckdb_core.
        return logger

    def _sleep_between_probes(self, seconds: float) -> None:
        # Resolved at call time through this module's global so
        # ``mock.patch("tckdb_arc.adapter._preflight_sleep")`` still skips the wait.
        _preflight_sleep(seconds)

    def _project_label_for(self, output_doc: Mapping[str, Any]) -> Any:
        return output_doc.get("project")

    def __init__(
        self,
        config: TCKDBConfig,
        *,
        project_directory: str | Path | None = None,
        client_factory=None,
        input_dict: Mapping[str, Any] | None = None,
    ):
        # ARC's parsed ``input.yml`` when the caller has it (the CLI), used with
        # the project's ``restart.yml`` / ``input.yml`` to detect
        # ``adaptive_levels`` runs (see ``tckdb_arc.adaptive``).
        self._input_dict = input_dict
        self._adaptive_levels_checked = False
        self._adaptive_levels: AdaptiveLevels | None = None
        self._restart_info: RestartInfo | None = None
        # Payloads land under the active ARC project when ``payload_dir`` is relative.
        super().__init__(
            config, project_directory=project_directory, client_factory=client_factory,
        )
        self._evidence = EvidenceStore(self._project_directory)
        # Emit the "artifacts unsupported for standalone TS uploads"
        # warning at most once per adapter instance rather than once per
        # TS (or per calc) — a sweep of many TSs would otherwise spam it.
        self._warned_ts_artifacts_unsupported = False

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def _with_adaptive_levels(
        self,
        output_doc: Mapping[str, Any],
        warnings: list[dict[str, Any]] | None = None,
    ) -> Mapping[str, Any]:
        """Attach what the project's ``restart.yml`` / ``input.yml`` say about levels.

        Returns ``output_doc`` unchanged when neither file says anything;
        otherwise a shallow copy carrying ``_RESTART_KEY`` (``restart.yml``'s
        adaptive ranges, IRC level and conformer level, see
        ``tckdb_arc.adaptive``) and, for an ``adaptive_levels`` run,
        ``_ADAPTIVE_LEVELS_KEY``. ``_resolve_level`` then attributes each
        adaptively-chosen level exactly where ``restart.yml`` allows and
        otherwise states nothing (``_report_adaptive_omissions`` lists what
        was left out after the build).
        """
        if not self._adaptive_levels_checked:
            self._adaptive_levels = detect_adaptive_levels(
                self._project_directory, self._input_dict)
            self._restart_info = read_restart_info(self._project_directory)
            self._adaptive_levels_checked = True
        detection, restart = self._adaptive_levels, self._restart_info
        contradictions = output_doc.get("schema_version") == "1.3"
        # Schema 1.3 states ``adaptive_levels`` in the header itself: the job types
        # its entries name join those found in the project files, so a job whose
        # level the record does not state is not filled from a run-level header.
        header_types = _header_adaptive_job_types(output_doc)
        if header_types:
            detection = AdaptiveLevels(
                job_types=frozenset(header_types) | (
                    detection.job_types if detection is not None else frozenset()),
                sources=("output.yml adaptive_levels",) + (
                    detection.sources if detection is not None else ()),
            )
        if detection is None and restart is None and not contradictions:
            return output_doc
        marked = dict(output_doc)
        if contradictions:
            marked[_CONTRADICTIONS_KEY] = []
        if restart is not None:
            marked[_RESTART_KEY] = restart
        if detection is not None:
            marked[_ADAPTIVE_LEVELS_KEY] = {
                "named": detection.job_types,
                "sources": detection.sources,
                # kind -> labels whose level could not be attributed
                "omitted": {},
            }
        return marked

    @staticmethod
    def _report_adaptive_omissions(
        output_doc: Mapping[str, Any],
        warnings: list[dict[str, Any]] | None,
    ) -> None:
        """One warning per level kind that ``adaptive_levels`` left unattributed."""
        marker = _adaptive_marker(output_doc)
        if marker is not None:
            _warn_adaptive_omissions(warnings, marker)
        noted = output_doc.get(_CONTRADICTIONS_KEY)
        for label, kind, detail in (noted if isinstance(noted, list) else ()):
            message = (
                f"The {kind} calculation of {label!r} was not uploaded: its recorded level "
                f"(requested) contradicts the keyword line the job ran with ({detail}). "
                "Observed outranks requested, and the adapter never sends both as consistent.")
            logger.warning("TCKDB %s: %s", _W_LEVEL_CONTRADICTED_BY_ROUTE, message)
            if warnings is not None:
                warnings.append(_self_check(
                    _W_LEVEL_CONTRADICTED_BY_ROUTE, message, f"{kind}.level_of_theory",
                    {"action": f"{kind}_calculation_omitted", "label": label}))

    def submit_from_output(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        conformer_index: int = 0,
        extra_label: str | None = None,
    ) -> UploadOutcome | None:
        """Build, write, and (if configured) upload one conformer payload.

        ``output_doc`` is the full parsed ``output.yml``. ``species_record``
        is one entry from ``output_doc['species']`` or
        ``output_doc['transition_states']``.

        Returns ``None`` if the adapter is disabled (so callers can write
        ``adapter.submit_from_output(...)`` without an enabled-check).
        """
        if not self._config.enabled:
            return None

        build_warnings: list[dict[str, Any]] = WarningSink(_PRODUCER_TAG)
        output_doc = self._with_adaptive_levels(output_doc, build_warnings)
        species_record = _withhold_unreliable_composite_energies(output_doc, species_record, build_warnings)
        skipped = _irc_endpoint_skip(output_doc, species_record)
        if skipped is not None:
            return skipped
        payload = self._build_payload(
            output_doc=output_doc,
            species_record=species_record,
            warnings=build_warnings,
        )
        self._report_adaptive_omissions(output_doc, build_warnings)

        species_label = species_record.get("label") or "unlabeled"
        conformer_label = extra_label or f"conf{conformer_index}"
        return self._write_and_upload(
            payload=payload,
            kind=PAYLOAD_KIND,
            endpoint=CONFORMER_UPLOAD_ENDPOINT,
            label=f"{species_label}.{conformer_label}",
            project_label=self._config.project_label or output_doc.get("project"),
            species_label=species_label,
            conformer_label=conformer_label,
            warnings=build_warnings,
        )

    def submit_artifacts_for_calculation(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        calculation_id: int | None = None,
        calculation_ref: str | None = None,
        calculation_type: str,
        file_path: str | Path,
        kind: str = "output_log",
    ) -> ArtifactUploadOutcome | None:
        """Upload one local file as an artifact attached to a TCKDB calculation.

        ``output_doc`` and ``species_record`` mirror :meth:`submit_from_output`.
        ``file_path`` may be absolute or relative — relative paths are
        resolved against ``project_directory`` (passed at construction).
        ``kind`` selects the TCKDB ArtifactKind; defaults to
        ``"output_log"`` for back-compat with v1 callers.

        Returns ``None`` only if the adapter itself is disabled. Otherwise
        returns an :class:`ArtifactUploadOutcome` whose ``status`` is one
        of ``uploaded`` / ``failed`` / ``skipped``. Skip reasons:
        artifact upload disabled, kind not in config.kinds, kind not yet
        implemented in ARC, file path missing, or file exceeds
        ``max_size_mb``.

        ``calculation_ref`` is the calculation's ``calc_`` ref from the upload
        response (``calculation_ref``); it names the calculation in the URL and
        in the artifact idempotency key. ``calculation_id`` is the integer
        fallback for a response that carries no ref (an older server): it is used
        only then, with a warning, and never preferred over a ref.

        ``calculation_type`` is accepted for the callers that pass it and is not
        used: the endpoint takes the calculation handle directly.
        """
        outcomes = self.submit_artifact_batch_for_calculation(
            output_doc=output_doc,
            species_record=species_record,
            calculation_id=calculation_id,
            calculation_ref=calculation_ref,
            calculation_type=calculation_type,
            artifacts=[(kind, file_path)],
        )
        if outcomes is None:
            return None
        return outcomes[0] if outcomes else None

    def submit_artifact_batch_for_calculation(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        calculation_id: int | None = None,
        calculation_ref: str | None = None,
        calculation_type: str,
        artifacts: list[tuple[str, str | Path]],
    ) -> list[ArtifactUploadOutcome] | None:
        """Upload artifacts for one calculation with client-side batch grouping.

        The calculation is named by ``calculation_ref`` (``calc_...``); the
        integer ``calculation_id`` is a fallback, warned about, for a response that
        returned no ref. The upload itself is
        :meth:`tckdb_core.uploader.TCKDBUploaderBase.submit_artifact_batch_for_calculation`;
        this method supplies the species label from ``species_record`` and hands
        ``output_doc`` to :meth:`_project_label_for`. ``calculation_type`` is
        accepted for the callers that pass it and is not used.
        """
        if not self._config.enabled:
            return None
        # Without a ref the core returns skipped outcomes before it reads the label, so the
        # record is not touched then (a caller may pass ``species_record=None`` with an id only).
        species_label = (
            (species_record.get("label") or "unlabeled") if calculation_ref else "unlabeled"
        )
        return super().submit_artifact_batch_for_calculation(
            species_label=species_label,
            calculation_id=calculation_id,
            calculation_ref=calculation_ref,
            artifacts=artifacts,
            output_doc=output_doc,
        )

    # ------------------------------------------------------------------
    # Computed-species bundle path (POST /uploads/computed-species)
    # ------------------------------------------------------------------

    def submit_computed_species_from_output(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        conformer_index: int = 0,
        extra_label: str | None = None,
    ) -> UploadOutcome | None:
        """Build, write, and (if configured) upload one computed-species bundle.

        Bundles species_entry + conformer geometry + opt/freq/sp + thermo
        + (optionally) inline artifacts into a single
        ``ComputedSpeciesUploadRequest`` and POSTs to
        ``/uploads/computed-species``. Returns ``None`` if the adapter is
        disabled, mirroring :meth:`submit_from_output`.

        Build failures (e.g. missing opt level, missing xyz) raise; the
        caller in scheduler/processor is responsible for wrapping the
        per-species call in a try/except so one bad species doesn't take
        down the rest of the run — same shape as the conformer path.
        """
        if not self._config.enabled:
            return None

        species_label = species_record.get("label") or "unlabeled"
        conformer_label = extra_label or f"conf{conformer_index}"
        project_label = self._config.project_label or output_doc.get("project")

        build_warnings: list[dict[str, Any]] = WarningSink(_PRODUCER_TAG)
        output_doc = self._with_adaptive_levels(output_doc, build_warnings)
        species_record = _withhold_unreliable_composite_energies(output_doc, species_record, build_warnings)
        skipped = _irc_endpoint_skip(output_doc, species_record)
        if skipped is not None:
            return skipped
        payload = self._build_computed_species_payload(
            output_doc=output_doc,
            species_record=species_record,
            conformer_key=conformer_label,
            warnings=build_warnings,
        )
        _enforce_geometry_anchoring(payload, "computed_species", build_warnings)
        _withhold_unlabelled_point_geometries(payload, "computed_species", build_warnings)
        self._report_adaptive_omissions(output_doc, build_warnings)

        return self._write_and_upload(
            payload=payload,
            kind=COMPUTED_SPECIES_KIND,
            endpoint=COMPUTED_SPECIES_ENDPOINT,
            label=f"{species_label}.{conformer_label}",
            project_label=project_label,
            species_label=species_label,
            conformer_label=conformer_label,
            log_name="computed-species",
            subdir=PayloadWriter.COMPUTED_SPECIES_SUBDIR,
            warnings=build_warnings,
        )

    def _build_computed_species_payload(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        conformer_key: str,
        warnings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compose one ComputedSpeciesUploadRequest dict.

        Reuses the existing per-calc and species-entry shapers; the only
        bundle-specific surface is the conformer wrapper, dependency
        edges (declared by local calc keys), inline artifacts, and the
        optional thermo block. Producer-side omissions (a thermo block the
        producer self-check refuses) are appended to ``warnings``.
        """
        _warn_primary_opt_placeholder(species_record, warnings, field="conformers[0].primary_calculation")
        # Scans ARC exported that could not be built, key -> reason; the
        # statmech builder drops torsion links to them (``torsion_scan_not_built``).
        unbuilt_scans: dict[str, str] = {}
        included_keys, conformer_block = self._build_conformer_block(
            output_doc=output_doc,
            species_record=species_record,
            conformer_key=conformer_key,
            unbuilt_scans=unbuilt_scans,
            warnings=warnings,
        )

        # Additional screened conformers, if ARC surfaced them in the
        # output.yml record. Only the selected conformer carries full
        # opt/freq/sp/thermo provenance — the rest land as honest
        # "geometry + bare opt" observations so the conformer screen
        # isn't lost to the database. Selected stays first for stable
        # ordering and so consumers reading conformers[0] still get the
        # canonical conformer.
        selected_xyz = conformer_block["geometry"]["xyz_text"]
        alt_blocks = self._build_alt_conformer_blocks(
            output_doc=output_doc,
            species_record=species_record,
            selected_xyz_text=selected_xyz,
            selected_key=conformer_key,
            warnings=warnings,
        )

        bundle: dict[str, Any] = {
            "species_entry": self._species_entry_payload(species_record),
            "conformers": [conformer_block, *alt_blocks],
        }

        omitted_bacs: list[str] = []
        applied_corrections = _build_applied_energy_corrections(
            _correction_records_from_record(
                species_record, legacy_1_0=_is_output_schema_1_0(output_doc),
                schema_1_3=_is_output_schema_1_3_or_later(output_doc)),
            source_calculation_key=(
                _CALC_KEY_SP if _CALC_KEY_SP in included_keys else None
            ),
            warnings=warnings,
            target_kind="species",
            element_symbols=_species_element_symbols(species_record),
            target_label=str(species_record.get("label") or "") or None,
            arkane_release=_arkane_workflow_tool_release(output_doc),
            scheme_data_revisions=_scheme_data_revisions(output_doc),
            aec_yml_digest=_arc_aec_yml_digest(output_doc),
            bac_frequency_level=_bac_frequency_level(output_doc, species_record),
            omitted_bac_reasons=omitted_bacs,
        )
        if applied_corrections:
            bundle["applied_energy_corrections"] = applied_corrections

        energy_level = (
            _thermo_energy_level(output_doc, species_record)
            if isinstance(species_record.get("thermo"), Mapping) else None
        )
        # The level TCKDB checks the thermo and statmech energy declarations
        # against is that of the sp calculation they link (A11), so it is read
        # off the calculations just built, not from ARC's bare level.
        energy_declaration = _energy_level_declaration(
            _thermo_energy_level(output_doc, species_record),
            calc_keys_by_role={key: key for key in included_keys},
            calculations=_calculations_by_key(
                conformer_block["primary_calculation"],
                conformer_block["additional_calculations"]),
        )
        thermo_block = _build_thermo_block(
            species_record.get("thermo"),
            # Computed-species has a single, unscoped calc namespace, so
            # each included role's own literal ("opt"/"freq"/"sp") is
            # also its bundle-local key — an identity map.
            calc_keys_by_role=_with_composite_role(
                species_record, {key: key for key in included_keys}),
            # This bundle's root is ComputedSpeciesUploadRequest, whose
            # thermo field is ``ThermoInBundle`` — the shape that accepts
            # ``source_calculations``.
            target_model="ThermoInBundle",
            warnings=warnings,
            energy_level=energy_level,
            header_corrections_level=output_doc.get("arkane_level_of_theory"),
            element_symbols=_species_element_symbols(species_record),
            energy_level_unattributable=_energy_level_unattributable(output_doc, energy_level),
            energy_declaration=energy_declaration,
            legacy_cp_data=_is_output_schema_1_0(output_doc),
            electron_count=_participant_electron_count(species_record),
        )
        # This route has no bundle-level analysis_software_release (the
        # reaction bundle does, and its thermo/statmech inherit it), so the
        # Arkane release goes on the thermo and statmech blocks themselves.
        arkane_release = _arc_analysis_software_release(output_doc)
        _note_omitted_bac_on_thermo(
            thermo_block, omitted_bacs, _bond_corrections_flag(species_record))
        _warn_bac_type_not_stated(output_doc, species_record, warnings)
        if thermo_block is not None:
            if arkane_release is not None:
                thermo_block["software_release"] = dict(arkane_release)
            bundle["thermo"] = thermo_block

        # Workflow-tool release at bundle level (in addition to per-calc):
        # mirrors what the conformer adapter records and lets the server
        # tag the species_entry with the producer.
        arc_wt = _arc_workflow_tool_release(output_doc)
        if arc_wt is not None:
            bundle["workflow_tool_release"] = arc_wt

        # Statmech block: carries frequency-scale-factor provenance,
        # plus richer base statmech metadata (external_symmetry,
        # is_linear, rigid_rotor_kind, statmech_treatment, point_group)
        # and slim torsion summaries when ARC populated them in the
        # species record's ``statmech`` subdict (written by
        # ``arc/output.py::_statmech_to_dict``). The container is omitted
        # entirely when nothing useful resolves — no empty containers.
        # Computed-species emits unscoped role keys (``opt`` / ``freq``
        # / ``sp``) directly as the bundle-local calc keys.
        species_calc_keys_by_role: dict[str, str] = {
            role: role for role in (_CALC_KEY_OPT, _CALC_KEY_FREQ, _CALC_KEY_SP)
            if role in included_keys
        }
        statmech_block = _build_statmech_block_for_species(
            output_doc=output_doc,
            species_record=species_record,
            calc_keys_by_role=_with_composite_role(
                species_record, species_calc_keys_by_role),
            workflow_tool_release=arc_wt,
            # This bundle's root is ComputedSpeciesUploadRequest, whose
            # statmech field is ``StatmechInBundle``.
            target_model="StatmechInBundle",
            unbuilt_scans=unbuilt_scans,
            freq_hessian_available=self._freq_hessian_available(
                output_doc=output_doc, species_record=species_record,
            ),
            warnings=warnings,
            warning_field="statmech",
            energy_declaration=energy_declaration,
        )
        if statmech_block is not None:
            if arkane_release is not None:
                statmech_block["software_release"] = dict(arkane_release)
            bundle["statmech"] = statmech_block

        return bundle

    def _build_conformer_block(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        conformer_key: str,
        unbuilt_scans: dict[str, str] | None = None,
        warnings: list[dict[str, Any]] | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        """Build one ConformerInBundle and return (included_calc_keys, block).

        A rotor scan that cannot be built is skipped with a warning and, when
        ``unbuilt_scans`` is given, recorded there as ``key -> reason``.

        The keys list is what's actually present in the bundle's calc
        namespace, used to drive thermo's source_calculations links.

        The conformer's reference xyz (the optimized geometry) is
        normalized once and threaded through to ``_build_calc_in_bundle``
        so freq + sp can declare it as their explicit input geometry —
        ARC's invariant guarantees they ran on this geometry. Server
        auto-fill would also reach the same answer for freq/sp, but
        being explicit makes the bundle self-describing.
        """
        # Required: the bundle's ConformerInBundle.geometry, also reused
        # as the explicit input_geometries entry on freq + sp.
        conformer_xyz_text = _require_xyz_text(species_record)

        if _is_single_atom_geometry(conformer_xyz_text):
            # A single atom has no geometry to optimise: its primary calculation
            # is its own single point (tckdb-schemas 0.59, TCKDB#610), and it has
            # no opt, coarse opt, freq or rotor scan to carry.
            primary_calc = self._build_monatomic_primary_sp(
                output_doc=output_doc, species_record=species_record,
                calc_key=_CALC_KEY_SP, conformer_xyz_text=conformer_xyz_text)
            block: dict[str, Any] = {
                "key": conformer_key,
                "geometry": _geometry_payload(
                    species_record, conformer_xyz_text, species_record.get("xyz_isotopes")),
                "primary_calculation": primary_calc,
                "additional_calculations": [],
            }
            if species_record.get("label"):
                block["label"] = str(species_record["label"])[:64]
            return [_CALC_KEY_SP], block

        # Coarse-opt provenance. Emitted only when ``coarse_opt_log``
        # exists AND ``coarse_opt_output_xyz`` is populated — the
        # output xyz is what chains the fine opt to the coarse opt
        # (it's both opt_coarse's output and the fine opt's declared
        # input). When the coarse log existed but its geometry didn't
        # parse cleanly, ``arc/output.py`` deliberately leaves
        # ``coarse_opt_output_xyz`` null so we fall back to single-stage
        # bundle shape rather than emit a half-described opt_coarse.
        opt_coarse_calc = self._build_opt_coarse_calc(
            output_doc=output_doc, species_record=species_record,
        )

        # Fine opt's depends_on: gain an ``optimized_from → opt_coarse``
        # edge when coarse was emitted. Otherwise no upstream calc.
        fine_opt_depends_on: list[Mapping[str, Any]] | None = None
        if opt_coarse_calc is not None:
            fine_opt_depends_on = [
                {"parent_calculation_key": _CALC_KEY_OPT_COARSE,
                 "role": "optimized_from"}
            ]

        primary_calc = self._build_calc_in_bundle(
            output_doc=output_doc,
            species_record=species_record,
            calc_key=_CALC_KEY_OPT,
            calc_type="opt",
            level_kind="opt",
            ess_job_key="opt",
            result_field="opt_result",
            result_payload=_opt_result_payload(species_record),
            depends_on=fine_opt_depends_on,
            tckdb_origin=None,
            conformer_xyz_text=conformer_xyz_text,
        )

        included: list[str] = [_CALC_KEY_OPT]
        additional: list[dict[str, Any]] = []
        # Coarse opt is an *additional* calc, not primary — fine opt is
        # the geometry of record. Order matters for thermo-source-link
        # determinism: coarse goes first so the bundle reads
        # opt → opt_coarse → freq → sp in additional_calculations.
        if opt_coarse_calc is not None:
            additional.append(opt_coarse_calc)
            included.append(_CALC_KEY_OPT_COARSE)

        freq_result = _freq_result_payload(species_record, schema_1_3=_is_output_schema_1_3_or_later(output_doc))
        if freq_result is not None:
            try:
                additional.append(self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=species_record,
                    calc_key=_CALC_KEY_FREQ,
                    calc_type="freq",
                    level_kind="freq",
                    ess_job_key="freq",
                    result_field="freq_result",
                    result_payload=freq_result,
                    depends_on=[{"parent_calculation_key": _CALC_KEY_OPT, "role": "freq_on"}],
                    tckdb_origin=None,
                    conformer_xyz_text=conformer_xyz_text,
                    warnings=warnings,
                ))
                included.append(_CALC_KEY_FREQ)
            except ValueError as exc:
                logger.warning(
                    "TCKDB computed-species: freq calculation skipped for label=%s: %s",
                    species_record.get("label"), exc,
                )

        sp_result = _sp_result_payload(species_record)
        if sp_result is not None:
            try:
                additional.append(self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=species_record,
                    calc_key=_CALC_KEY_SP,
                    calc_type="sp",
                    level_kind="sp",
                    ess_job_key="sp",
                    result_field="sp_result",
                    result_payload=sp_result,
                    depends_on=[{"parent_calculation_key": _CALC_KEY_OPT, "role": "single_point_on"}],
                    tckdb_origin=(
                        _sp_reuse_origin(output_doc, species_record)
                    ),
                    conformer_xyz_text=conformer_xyz_text,
                ))
                included.append(_CALC_KEY_SP)
            except ValueError as exc:
                logger.warning(
                    "TCKDB computed-species: sp calculation skipped for label=%s: %s",
                    species_record.get("label"), exc,
                )

        # Rotor scans. ``arc/output.py`` populates the species record's
        # ``additional_calculations`` list with one entry per successful
        # 1D rotor whose log was parseable. Each entry already carries a
        # bundle-local ``key`` (``scan_rotor_<i>``) and a TCKDB-shaped
        # ``scan_result`` dict; the adapter only needs to layer level /
        # software / workflow_tool_release on top. An explicit scan level
        # is required; the optimization level does not identify the scan. The
        # ``depends_on`` edge points back to opt — the scan is a series
        # of constrained reoptimizations from that geometry.
        for scan_entry in _scan_entries_from_record(species_record):
            if not isinstance(scan_entry, Mapping):
                continue
            if scan_entry.get("type") != _CALC_KEY_SCAN:
                continue
            scan_key = scan_entry.get("key")
            scan_result = scan_entry.get("scan_result")
            if not isinstance(scan_key, str) or not scan_key:
                continue
            if not isinstance(scan_result, Mapping):
                continue
            try:
                additional.append(self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=species_record,
                    calc_key=scan_key,
                    calc_type=_CALC_KEY_SCAN,
                    level_kind="scan",
                    ess_job_key="scan",
                    result_field="scan_result",
                    result_payload=scan_result,
                    depends_on=[{"parent_calculation_key": _CALC_KEY_OPT,
                                 "role": "scan_parent"}],
                    tckdb_origin=None,
                    conformer_xyz_text=conformer_xyz_text,
                    calc_role=_CALC_KEY_SCAN,
                    source_constraints=scan_entry.get("constraints"),
                    output_log_path=scan_entry.get("source_log"),
                    observed_software=scan_entry.get("ess_software"),
                    observed_version=scan_entry.get("ess_version"),
                    level_job_type=_scan_job_type(output_doc, species_record, scan_key),
                ))
                included.append(scan_key)
            except ValueError as exc:
                logger.warning(
                    "TCKDB computed-species: scan calculation %s skipped for label=%s: %s",
                    scan_key, species_record.get("label"), exc,
                )
                if unbuilt_scans is not None:
                    unbuilt_scans[scan_key] = str(exc)

        block: dict[str, Any] = {
            "key": conformer_key,
            "geometry": _geometry_payload(
                species_record, conformer_xyz_text, species_record.get("xyz_isotopes")),
            "primary_calculation": primary_calc,
            "additional_calculations": additional,
        }
        label = species_record.get("label")
        if label:
            block["label"] = str(label)[:64]
        return included, block

    def _build_monatomic_primary_sp(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        calc_key: str,
        conformer_xyz_text: str,
    ) -> dict[str, Any]:
        """The ``sp`` primary calculation of a single atom (tckdb-schemas 0.59, TCKDB#610).

        Contract (``ConformerInBundle.validate_primary_is_opt``): "A monatomic species
        has no geometry to optimise ... send that single point, once, as
        ``primary_calculation`` with ``type: "sp"``, its ``sp_result``, and the atom's
        one-atom XYZ as the conformer ``geometry``. Do not relabel it as an ``opt``."
        It is the atom's own job: its log, level, program, energy and settings as ARC
        states them for the sp (an output.yml 1.3 monoatomic's sp log is also its
        ``opt_log``, and ``levels.sp`` is its level). It carries no ``depends_on``
        (there is no opt to depend on) and no reused-result marker. ``SPResultPayload``
        has no convergence field, so ARC's ``converged`` for the atom is not sent. The
        energy is ``sp_energy_hartree``, else (non-composite atoms only) the
        ``opt_final_energy_hartree`` ARC parsed from the same one log
        (``_monatomic_sp_result_payload``).
        """
        sp_result = _monatomic_sp_result_payload(species_record)
        if sp_result is None:
            raise _atom_without_sp_energy_error(species_record)
        return self._build_calc_in_bundle(
            output_doc=output_doc,
            species_record=species_record,
            calc_key=calc_key,
            calc_role=_CALC_KEY_SP,
            calc_type="sp",
            level_kind="sp",
            ess_job_key="sp",
            result_field="sp_result",
            result_payload=sp_result,
            depends_on=None,
            tckdb_origin=None,
            conformer_xyz_text=conformer_xyz_text,
        )

    def _build_alt_conformer_blocks(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        selected_xyz_text: str,
        selected_key: str,
        warnings: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Additional ``ConformerInBundle`` dicts for ARC's screened conformers.

        ARC's ``output.yml`` species record lists the conformers its screen
        optimized alongside the selected one (``conformers``, xyz strings).
        Each needs a primary ``opt`` calculation, and TCKDB requires a
        ``level_of_theory`` on every calculation
        (``ConformerInBundle.primary_calculation``), so there is no honest
        partial form.

        Output.yml 1.3 states the level of each conformer geometry's optimization
        (``conformer_levels``); each conformer is then filed at its own level, with
        the program of the header ``conformer_opt_level`` when that names the same
        level (``_stated_conformer_level``), and its electronic energy only with a
        stated kind and level (``_stated_conformer_opt_result``,
        ``_stated_conformer_sp``). A ``null`` entry (force field, user-supplied) is
        not an ESS calculation and is omitted (``conformer_geometry_not_esss_optimized``).
        Everything below describes output.yml 1.2 and older.

        ARC screens conformers at its *conformer* level (``conformer_opt_level``;
        default ``wb97xd/def2svp``, lower than the ``opt_level`` default), and
        ``output.yml`` exports no conformer level. The project's ``restart.yml``
        does record ``conformer_opt_level`` and ``job_types`` (``arc/main.py``
        ``as_dict``), so the screened conformers are filed at that level, with
        the program the level names, when all of these hold
        (``_conformer_screen_level``):

        * ``restart.yml`` records the level and ``job_types['conf_opt']`` is true
          (otherwise the geometries are force-field ones, never optimized by an
          ESS);
        * the adaptive levels do not name ``conf_opt`` for the species' range
          (a species-specific screening level);
        * the conformer's own ``conformer_energies`` entry is not null (ARC
          fills it, and replaces the force-field geometry with the optimized
          one, only when that conformer's ``conf_opt`` finished; a missing or
          misaligned energies list leaves every conformer unverified).

        Gap: when no conformer converged ARC re-runs all of them at a
        troubleshooting level (``arc/scheduler.py`` 5050-5078,
        ``ess_trsh_methods`` ``'conf_opt: <level>'``), but nothing durable
        records that (it lives in the job objects, kept in ``restart.yml`` only
        while running), so ``conformer_opt_level`` may then be wrong.

        Otherwise, and whenever the level lacks a program, they are omitted and
        reported once per species as ``conformer_level_not_stated`` (ARC should
        export the conformer level). Each conformer is a bare
        ``opt`` marked as a screened-conformer anchor (``tckdb_origin``), with
        no ``opt_result`` and no relative energy: ``conformer_energies`` are
        workflow-local values (the reference is the lowest of ARC's screening
        set), which TCKDB, being workflow-tool agnostic, does not receive.

        Skips: an empty ``conformers`` list, xyz strings that fail to normalize,
        and exact duplicates of the selected geometry or of an earlier
        candidate. Failures log and continue; they never fail the bundle.
        """
        raw_conformers = species_record.get("conformers")
        if not isinstance(raw_conformers, (list, tuple)) or not raw_conformers:
            return []
        if _is_single_atom_geometry(selected_xyz_text):
            # An atom has one geometry; an alternative conformer would need the
            # bare opt that TCKDB does not take for an atom.
            return []

        label = species_record.get("label")
        candidates: list[str] = []
        unoptimized: list[str] = []
        candidate_index: dict[str, int] = {}
        seen_xyz: set[str] = {selected_xyz_text}
        energies = species_record.get("conformer_energies")
        lockstep = isinstance(energies, (list, tuple)) and len(energies) == len(raw_conformers)
        conformer_levels = species_record.get("conformer_levels")
        # Schema 1.3 states the level of the optimization behind each conformer
        # geometry (``conformer_levels``, in lockstep with ``conformers``).
        stated_levels = (
            isinstance(conformer_levels, list) and len(conformer_levels) == len(raw_conformers))
        # Schema 1.3 (ARC #1059 ebc88ec8) also states the program and banner of each conformer's own
        # optimization log. Where those lists are present the program is theirs, never the header's.
        observed_programs = _conformer_observed_lists(species_record) is not None
        for index, raw_xyz in enumerate(raw_conformers):
            normalized = _normalize_xyz_text(raw_xyz, label)
            if normalized is None or normalized in seen_xyz:
                continue
            seen_xyz.add(normalized)
            candidates.append(normalized)
            candidate_index[normalized] = index
            if stated_levels:
                if not isinstance(conformer_levels[index], Mapping):
                    unoptimized.append(normalized)
                continue
            # ARC replaces conformers[i] with the optimized geometry only when
            # its conf_opt finished (arc/scheduler.py:3133-3134, else it just
            # warns), and fills conformer_energies[i] at the same time; a null
            # energy means the geometry is still the force-field one.
            if not lockstep or energies[index] is None:
                unoptimized.append(normalized)
        if not candidates:
            return []

        level = None if stated_levels else _conformer_screen_level(output_doc, species_record)
        blocks: list[dict[str, Any]] = []
        no_program: list[str] = []
        no_isotopes: list[str] = []
        if level is not None or stated_levels:
            for normalized in candidates:
                if normalized in unoptimized:
                    continue
                result_payload = None
                observed_version = None
                if stated_levels:
                    index = candidate_index[normalized]
                    level = _stated_conformer_level(output_doc, species_record, index)
                    if level is None:
                        no_program.append(normalized)
                        continue
                    if observed_programs:
                        observed_version = _conformer_observed_provenance(species_record, index)[1]
                    result_payload = _stated_conformer_opt_result(
                        species_record, index, conformer_levels[index])
                alt_key = f"alt{len(blocks)}"
                if alt_key == selected_key:
                    # Caller picked a selected key in our alt-key namespace;
                    # skip rather than silently shadow the selected entry.
                    logger.warning(
                        "TCKDB computed-species: alt conformer for label=%s "
                        "would collide with selected key %r; skipping.",
                        label, selected_key,
                    )
                    continue
                try:
                    opt_calc = self._calculation_payload(
                        output_doc, species_record,
                        calc_type="opt",
                        level=level,
                        ess_job_key="conf_opt",
                        result_field="opt_result",
                        result_payload=result_payload,
                        # ``tckdb_origin`` tags the row as a screened-conformer
                        # anchor, NOT a parsed opt job of the selected conformer.
                        tckdb_origin=_screened_conformer_origin(),
                        # The program of the conformer's own optimization log and its banner
                        # (``conformer_ess_software`` / ``conformer_ess_version``), when ARC states them.
                        observed_software=level["software"] if observed_programs else None,
                        observed_version=observed_version,
                    )
                except ValueError as exc:
                    logger.warning(
                        "TCKDB computed-species: alt conformers of label=%s "
                        "skipped (opt calc not buildable): %s", label, exc,
                    )
                    break
                opt_calc["key"] = f"{alt_key}_opt"
                # Anchor opt's output_geometries to the alt xyz explicitly: the
                # backend's auto-fill would pin every conformer's opt output to
                # the selected conformer's geometry of record.
                alt_geometry = _geometry_payload(
                    species_record, normalized, _conformer_isotopes(species_record, candidate_index[normalized]))
                if alt_geometry is None:
                    no_isotopes.append(normalized)
                    continue
                opt_calc["output_geometries"] = [
                    {"geometry": dict(alt_geometry), "role": "final"},
                ]
                alt_additional: list[dict[str, Any]] = []
                conformer_sp = (
                    _stated_conformer_sp(
                        output_doc, species_record, candidate_index[normalized],
                        conformer_levels[candidate_index[normalized]])
                    if stated_levels else None)
                if conformer_sp is not None:
                    sp_level, sp_energy = conformer_sp
                    try:
                        sp_calc = self._calculation_payload(
                            output_doc, species_record,
                            calc_type="sp", level=sp_level, ess_job_key="conf_sp",
                            result_field="sp_result",
                            result_payload={"electronic_energy_hartree": sp_energy},
                            tckdb_origin=_screened_conformer_origin("sp"),
                        )
                    except ValueError as exc:
                        logger.warning(
                            "TCKDB computed-species: conformer sp of label=%s skipped: %s",
                            label, exc)
                    else:
                        sp_calc["key"] = f"{alt_key}_sp"
                        sp_calc["depends_on"] = [
                            {"parent_calculation_key": f"{alt_key}_opt", "role": "single_point_on"}]
                        sp_calc["input_geometries"] = [dict(alt_geometry)]
                        alt_additional.append(sp_calc)
                block: dict[str, Any] = {
                    "key": alt_key,
                    "geometry": alt_geometry,
                    "primary_calculation": opt_calc,
                    "additional_calculations": alt_additional,
                }
                if label:
                    block["label"] = str(label)[:64]
                blocks.append(block)
        omitted = len(candidates) - len(blocks)
        if no_isotopes:
            _warn_isotopes_not_stated(
                warnings, label=label, what=f"{len(no_isotopes)} screened conformer geometr(ies)")
            omitted -= len(no_isotopes)
        if stated_levels:
            if unoptimized:
                _warn_conformer_not_esss_optimized(
                    warnings, label=label, omitted=len(unoptimized),
                    force_field=species_record.get("conformer_force_field"))
            if no_program:
                _warn_conformer_program_not_stated(
                    warnings, label=label, omitted=len(no_program), observed=observed_programs)
        elif omitted:
            _warn_conformer_level_not_stated(warnings, label=label, omitted=omitted)
        return blocks

    def _build_calc_in_bundle(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        calc_key: str,
        calc_type: str,
        level_kind: str,
        ess_job_key: str,
        result_field: str,
        result_payload: Mapping[str, Any] | None,
        depends_on: list[Mapping[str, Any]] | None,
        tckdb_origin: Mapping[str, Any] | None,
        conformer_xyz_text: str | None = None,
        calc_role: str | None = None,
        source_constraints: list | None = None,
        include_artifacts: bool = True,
        level_job_type: str | None = None,
        level_override: Mapping[str, Any] | None = None,
        output_log_path: str | None = None,
        observed_software: str | None = None,
        observed_version: str | None = None,
        warnings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build one CalculationInBundle dict.

        ``warnings``, when given, receives the sidecar findings of the optional pieces left out
        while building (an isotope-unlabellable Hessian).

        ``observed_software`` / ``observed_version`` are the program and banner a
        rotor scan states for its own log (output.yml 1.3 ``rotor_scans[]``).

        ``output_log_path`` names the calculation's own output log when it is
        not a field of the species record (a rotor scan's
        ``rotor_scans[].source_log``); it is used for the ``output_log``
        artifact of a ``scan`` calculation and ignored for other roles.

        ``calc_key`` is the bundle-local identity (e.g. ``"opt"`` for a
        single-species bundle, ``"r0_opt"`` for a reaction bundle).
        ``calc_role`` is the chemistry role used for policy lookups
        (input/output geometry rules, log-field map). When ``calc_role``
        is ``None`` it defaults to ``calc_key`` — that's the
        single-species case where the two are identical and the existing
        callers don't have to change.

        Reuses :meth:`_calculation_payload` for the level/software/result
        plumbing, then layers on the bundle-specific fields: ``key``,
        ``depends_on``, ``input_geometries``, and inline ``artifacts``.

        Input-geometry policy (matches TCKDB v0 backend):
        - ``opt``: emit ``input_geometries`` only when ARC has the
          actual pre-opt xyz on the record (``opt_input_xyz``). Never
          fall back to the conformer's optimized geometry — that's
          opt's *output*, not its input. If absent, the field is
          omitted; backend has no auto-fill for opt.
        - ``freq`` / ``sp``: emit ``input_geometries`` set to the
          conformer's optimized geometry (passed in via
          ``conformer_xyz_text``). ARC's invariant guarantees these
          ran on that geometry. Backend would auto-fill the same value
          if we omitted, but explicit is better — keeps the bundle
          self-describing and removes ambiguity for any consumer that
          doesn't replicate the auto-fill rule.
        """
        level = (
            level_override if level_override is not None
            else _resolve_level(output_doc, level_kind, species_record,
                                job_type=level_job_type)
        )
        role = calc_role if calc_role is not None else calc_key
        final_settings = _final_settings_for_calc(
            species_record=species_record,
            calc_role=role,
        )
        calc = self._calculation_payload(
            output_doc, species_record,
            calc_type=calc_type,
            level=level,
            ess_job_key=ess_job_key,
            result_field=result_field,
            result_payload=result_payload,
            tckdb_origin=tckdb_origin,
            final_settings=final_settings,
            scf_stability_target=(role == _CALC_KEY_OPT),
            observed_software=observed_software,
            observed_version=observed_version,
        )
        calc["key"] = calc_key
        if depends_on:
            calc["depends_on"] = [dict(d) for d in depends_on]
        input_geometries = self._input_geometries_for_calc(
            calc_role=role,
            species_record=species_record,
            conformer_xyz_text=conformer_xyz_text,
        )
        if input_geometries:
            calc["input_geometries"] = input_geometries
        output_geometries = self._output_geometries_for_calc(
            calc_role=role,
            species_record=species_record,
            conformer_xyz_text=conformer_xyz_text,
        )
        if output_geometries:
            calc["output_geometries"] = output_geometries
        # Optional inline Cartesian Hessian for freq calcs (DR-0030). Parsed
        # from the freq job's ESS output; skipped silently when unavailable so
        # it can never break payload construction.
        if role == _CALC_KEY_FREQ:
            hessian = self._build_freq_hessian_payload(
                output_doc=output_doc,
                species_record=species_record,
                geometry_xyz_text=conformer_xyz_text,
                warnings=warnings,
            )
            if hessian is not None:
                calc["hessian"] = hessian
        # ``include_artifacts=False`` short-circuits the read+base64 of
        # potentially multi-MB logs. The standalone transition-state
        # endpoint has no artifact slot, so building them here would waste
        # I/O and then be stripped downstream anyway (see
        # ``_ts_calc_to_standalone``). We skip the work entirely rather
        # than build-then-drop.
        if include_artifacts:
            artifacts = self._inline_artifacts_for_calc(
                species_record, calc_role=role, output_log_path=output_log_path)
            # Schema defaults `artifacts: []`. Emit explicitly only when we
            # have bytes to send (or when artifact upload is enabled and we
            # want to signal "no log available" with an empty list); omit
            # otherwise.
            if artifacts:
                calc["artifacts"] = artifacts

        # Held-fixed coordinate constraints. ``source_constraints`` wins
        # when caller supplies it explicitly (scan calcs pull from the
        # ``additional_calculations`` entry). Otherwise we pick up the
        # per-job list ``arc/output.py`` populated on the species record.
        constraints_source = source_constraints
        if constraints_source is None:
            field = _CONSTRAINTS_FIELD_BY_CALC_KEY.get(role)
            if field:
                constraints_source = species_record.get(field)
        constraint_payload = _serialize_calc_constraints(constraints_source)
        if constraint_payload:
            calc["constraints"] = constraint_payload
        return calc

    @staticmethod
    def _output_geometries_for_calc(
        *,
        calc_role: str,
        species_record: Mapping[str, Any],
        conformer_xyz_text: str | None,
    ) -> list[dict[str, Any]]:
        """Compute the ``output_geometries`` list for one calc, per-kind policy.

        Each entry is shaped ``{"geometry": {"xyz_text": ...}, "role":
        "final"}`` per TCKDB's ``OutputGeometryEntry``. Returns ``[]``
        when nothing should be emitted; the caller drops empty lists.

        Policy mirrors what the calc actually produced:

        - ``opt`` (fine): produced the conformer's geometry of record.
          Emit ``conformer_xyz_text`` with ``role=final``. If absent
          (e.g., bundle-build path that skipped requiring xyz), the
          backend still has its single-stage fallback that links opt
          to the conformer geometry — but that fallback is server-
          side and shouldn't be relied on once we're declaring outputs
          explicitly elsewhere.
        - ``opt_coarse``: produced ``coarse_opt_output_xyz``. Emit it
          with ``role=final``.
        - ``freq`` / ``sp``: don't move atoms; we don't surface a
          standalone "freq output geometry" or "sp output geometry"
          today. Backend's freq/sp fallback now creates zero output
          rows for these (per the new contract), so omitting is correct.
        """
        if calc_role == _CALC_KEY_OPT:
            if not conformer_xyz_text:
                return []
            geometry = _geometry_payload(
                species_record, conformer_xyz_text, species_record.get("xyz_isotopes"))
            return [{"geometry": geometry, "role": "final"}] if geometry else []
        if calc_role == _CALC_KEY_OPT_COARSE:
            coarse_out = species_record.get("coarse_opt_output_xyz")
            if not coarse_out:
                return []
            normalized = _normalize_xyz_text(coarse_out, species_record.get("label"))
            if not normalized:
                return []
            geometry = _geometry_payload(
                species_record, normalized, species_record.get("coarse_opt_output_xyz_isotopes"))
            return [{"geometry": geometry, "role": "final"}] if geometry else []
        # freq / sp / irc / others: no output_geometries today.
        return []

    @staticmethod
    def _input_geometries_for_calc(
        *,
        calc_role: str,
        species_record: Mapping[str, Any],
        conformer_xyz_text: str | None,
    ) -> list[dict[str, Any]]:
        """Compute the ``input_geometries`` list for one calc, per-kind policy.

        Returns ``[]`` when nothing should be emitted. The caller checks
        truthiness — empty lists are dropped from the payload so we don't
        send a zero-length array where None is more accurate.
        """
        if calc_role == _CALC_KEY_OPT:
            opt_input_xyz = species_record.get("opt_input_xyz")
            if not opt_input_xyz:
                return []
            normalized = _normalize_xyz_text(opt_input_xyz, species_record.get("label"))
            if not normalized:
                return []
            geometry = _geometry_payload(
                species_record, normalized, species_record.get("opt_input_xyz_isotopes"))
            return [geometry] if geometry else []
        if calc_role == _CALC_KEY_OPT_COARSE:
            # Coarse opt's input is the species' truly-initial xyz —
            # ``coarse_opt_input_xyz`` from arc/output.py. The caller
            # only invokes this when the coarse stage actually ran, so
            # an absent value here is a real bug; skip rather than
            # fabricate (the parent ``_build_opt_coarse_calc`` short-
            # circuits before reaching us if either coarse field is
            # missing).
            coarse_in = species_record.get("coarse_opt_input_xyz")
            if not coarse_in:
                return []
            normalized = _normalize_xyz_text(coarse_in, species_record.get("label"))
            if not normalized:
                return []
            geometry = _geometry_payload(
                species_record, normalized, species_record.get("coarse_opt_input_xyz_isotopes"))
            return [geometry] if geometry else []
        if calc_role in (_CALC_KEY_FREQ, _CALC_KEY_SP, _CALC_KEY_IRC):
            # ARC invariant: freq, sp, and (TS) irc all run on the
            # conformer's optimized xyz. Surface it explicitly rather
            # than relying on backend auto-fill — keeps the bundle
            # self-describing.
            if not conformer_xyz_text:
                return []
            return [_geometry_payload(
                species_record, conformer_xyz_text, species_record.get("xyz_isotopes"))]
        return []

    def _build_opt_coarse_calc(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        calc_key: str = _CALC_KEY_OPT_COARSE,
    ) -> dict[str, Any] | None:
        """Build the coarse opt's CalculationInBundle dict, or return None.

        ``calc_key`` is the bundle-local identity. Computed-species uses
        the bare role (``"opt_coarse"``); computed-reaction passes a
        species-namespaced variant (``"r0_opt_coarse"``, ``"p1_opt_coarse"``)
        so it stays globally unique across the bundle. The chemistry
        role passed to ``_build_calc_in_bundle`` stays
        ``_CALC_KEY_OPT_COARSE`` either way — it's what drives the
        ``coarse_opt_log`` / ``coarse_opt_input_xyz`` lookups on the
        species record.

        Returns ``None`` when:
        - ``coarse_opt_log`` is absent (no coarse stage ran)
        - ``coarse_opt_output_xyz`` is null (coarse log existed but its
          final geometry couldn't be parsed; modeling the calc without
          its output geometry would create a half-described provenance
          row, so we drop the whole opt_coarse rather than mislead)

        Note on output geometry: TCKDB's bundle workflow auto-anchors
        every calc's ``CalculationOutputGeometry`` to the conformer
        geometry (the FINE opt's output). Until the bundle schema
        gains an ``output_geometries`` field on ``CalculationInBundle``,
        opt_coarse's output-geometry row will incorrectly point at
        the fine geometry server-side. The input chain
        (``calculation_input_geometry``) is correct — that's what was
        empty before this change and is what the producer can fix.
        """
        if not species_record.get("coarse_opt_log"):
            return None
        if not species_record.get("coarse_opt_output_xyz"):
            logger.debug(
                "TCKDB: coarse_opt_log present but coarse_opt_output_xyz "
                "is null for label=%s — falling back to single-stage opt "
                "bundle.",
                species_record.get("label"),
            )
            return None
        result_payload = _coarse_opt_result_payload(species_record)
        # Reuse the standard calc-in-bundle builder. opt_coarse takes
        # the same level/software as the fine opt (ARC runs both
        # stages at the configured opt level — the difference is
        # convergence criterion, not method/basis). No depends_on
        # (it's the chain head). No tckdb_origin (it's a real ESS run,
        # not a reuse of another calc's result).
        try:
            coarse = self._build_calc_in_bundle(
                output_doc=output_doc,
                species_record=species_record,
                calc_key=calc_key,
                calc_role=_CALC_KEY_OPT_COARSE,
                calc_type="opt",
                level_kind="opt",
                ess_job_key="opt",
                result_field="opt_result",
                result_payload=result_payload,
                depends_on=None,
                tckdb_origin=None,
                conformer_xyz_text=None,  # opt_coarse's input is its own xyz, not the conformer
            )
            # ``opt_route`` is the keyword line of the fine opt, not of the coarse stage.
            coarse.pop("parameters", None)
            return coarse
        except ValueError as exc:
            logger.warning(
                "TCKDB: opt_coarse calculation skipped for label=%s "
                "(calc_key=%s): %s",
                species_record.get("label"), calc_key, exc,
            )
            return None

    def _inline_artifacts_for_calc(
        self,
        species_record: Mapping[str, Any],
        *,
        calc_role: str,
        output_log_path: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the inline artifact list for one calc within a bundle.

        ``calc_role`` is the chemistry role (``opt``/``freq``/``sp``/
        ``opt_coarse``/``irc``) used to look up the matching record
        field name (``opt_log`` etc.). The bundle-local key — e.g.
        ``r0_opt`` for a reaction — is irrelevant here; ARC stores the
        log paths on the species record under role-keyed names.

        Iterates ``_INLINE_ARTIFACT_SOURCES`` (currently ``output_log``
        and ``input``) and emits one ArtifactIn dict per kind whose
        record path resolves to a real file on disk. Each kind is
        independently gated on ``config.artifacts.kinds``, so a user
        can opt into logs but not decks (or vice versa).

        Skip rules per (calc_role, kind):
            - artifacts globally disabled              → entire list = []
            - kind not in config.artifacts.kinds       → that kind only
            - record-field path missing / null         → that kind only
            - resolved file not on disk                → that kind only
            - file > artifacts.max_size_mb             → that kind only (warn)
        """
        artifact_cfg = self._config.artifacts
        if not artifact_cfg.upload:
            return []
        artifacts: list[dict[str, Any]] = []
        for kind, field_map in _INLINE_ARTIFACT_SOURCES:
            if kind not in artifact_cfg.kinds:
                continue
            # ``output_log`` for ``ts_guess`` is method-dispatched
            # (NEB → ``neb_log``, GSM → ``gsm_log``); other (kind, role)
            # combinations look up in the static map.
            path_value = None
            if kind == "output_log":
                record_field = _resolve_log_field(calc_role, species_record)
                if calc_role == _CALC_KEY_SCAN:
                    # A scan's log is per rotor, so it is carried on the scan
                    # entry rather than in a species-record field.
                    path_value = output_log_path
            else:
                record_field = field_map.get(calc_role)
            if (calc_role == _CALC_KEY_OPT and arc13.is_composite_run(species_record)
                    and arc13.software_job_key(species_record, _CALC_KEY_OPT) == "composite"
                    and not species_record.get(record_field or "")):
                # A composite run's geometry job is its composite job: its log and
                # input deck are what the record's ``opt`` calculation ran.
                record_field = {"output_log": "composite_log", "input": "composite_input"}[kind]
            artifact = self._read_inline_artifact(
                species_record,
                calc_role=calc_role,
                kind=kind,
                record_field=record_field,
                path_value=path_value,
            )
            if artifact is not None:
                artifacts.append(artifact)
        return artifacts

    def _read_inline_artifact(
        self,
        species_record: Mapping[str, Any],
        *,
        calc_role: str,
        kind: str,
        record_field: str | None,
        path_value: str | None = None,
    ) -> dict[str, Any] | None:
        """Resolve, read, hash, and base64-encode one artifact for the bundle.

        ``path_value`` supplies the path directly (a rotor scan's log);
        otherwise it is read from ``record_field`` on the species record.

        Returns ``None`` (with a debug or warning log) on any of:
        unknown calc_key, missing/null record path, file not on disk, or
        file exceeding ``max_size_mb``. Otherwise returns the
        ``ArtifactIn``-shaped dict ready to drop into ``calc.artifacts``.
        """
        if path_value is None:
            if record_field is None:
                return None
            path_value = species_record.get(record_field)
        if not path_value:
            return None
        return self._read_artifact_file(
            path_value,
            calc_role=calc_role,
            kind=kind,
            label=species_record.get("label"),
        )

    def _freq_hessian_available(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
    ) -> bool:
        """Whether parser evidence (or the log fallback) yields a freq Hessian.

        This is the evidence that Arkane had a force-constant matrix for the
        species, which ``statmech_treatment`` depends on. It reuses
        ``_build_freq_hessian_payload``, so it is True exactly when a
        Hessian payload can be built for the freq calculation.
        """
        return self._build_freq_hessian_payload(
            output_doc=output_doc, species_record=species_record,
            geometry_xyz_text=None,
        ) is not None

    def _build_freq_hessian_payload(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        geometry_xyz_text: str | None,
        warnings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Build the optional ``hessian`` payload for a freq calculation.

        Parses the Cartesian Hessian from the freq job's ESS output (Gaussian
        log block or Orca sibling ``.hess`` file) via the parser-layer adapters,
        and packs it into the TCKDB ``HessianPayload`` shape:
        ``{"geometry": {"xyz_text": ...}, "lower_triangle_hartree_bohr2":
        [...], "source": "parsed_log"|"parsed_hess", "parser_version": ...}``.

        The Hessian is native atomic units (hartree/bohr²) straight from the
        parser — no unit conversion is applied here. Portable evidence carries
        the geometry in the Hessian's own Cartesian frame, which must be used
        unchanged even when the conformer uses a different orientation.
        Raw parser fallback also requires the parser's frame-matched geometry;
        the conformer's geometry cannot establish the Hessian coordinate frame.

        Returns ``None`` — never raises — when the Hessian is unavailable for
        any reason (no geometry, no freq log on disk, unknown/unsupported ESS,
        missing FC block / ``.hess``, monatomic species, or any parse error).
        The Hessian is strictly optional and must never break payload
        construction.
        """
        record_kind = "transition_state" if species_record.get("is_ts") else "species"
        lookup = self._evidence.lookup(
            output_doc, record_kind, str(species_record.get("label") or ""), "freq_hessian"
        )
        if lookup.state == "available":
            value = lookup.value or {}
            geometry = _hessian_geometry(species_record, value["geometry_xyz_text"], warnings)
            if geometry is None:
                return None
            return {
                "geometry": geometry,
                "lower_triangle_hartree_bohr2": list(value["lower_triangle"]),
                "source": value["source"],
                **_hessian_parser_block(value["parser_version"]),
            }
        if lookup.state == "unavailable":
            return None
        log_path = species_record.get(_LOG_FIELD_BY_CALC_KEY[_CALC_KEY_FREQ])
        if not log_path:
            return None
        resolved = self._resolve_local_path(log_path)
        if resolved is None or not resolved.is_file():
            return None
        try:
            # Late import via the optional-ARC boundary: keeps the ESS parser
            # adapters (heavyweight, ARC-only) off the import path unless a
            # Hessian is actually being built. When ARC isn't installed these
            # raise OptionalArcUnavailable (an ImportError), caught by the
            # surrounding ``except Exception`` → the Hessian is omitted.
            from tckdb_arc._arc_optional import determine_ess, ess_factory

            ess_name = determine_ess(str(resolved), raise_error=False)
            source = _HESSIAN_SOURCE_BY_ESS.get(ess_name) if ess_name else None
            if source is None:
                return None
            ess_adapter = ess_factory(str(resolved), ess_name)
            parse = getattr(ess_adapter, "parse_cartesian_hessian_lower_triangle", None)
            if parse is None:
                return None
            triangle = parse()
            if not triangle:
                return None
            parse_frame = getattr(ess_adapter, "parse_cartesian_hessian_geometry", None)
            frame_xyz, frame = parse_frame() if parse_frame else (None, None)
            expected_frame = {"gaussian": "gaussian_input_orientation", "orca": "orca_hess_atoms"}
            if frame_xyz is None or frame != expected_frame.get(ess_name):
                logger.warning(
                    "TCKDB bundle: Hessian omitted for label=%s: matching Cartesian frame geometry unavailable",
                    species_record.get("label"),
                )
                return None
            from tckdb_arc._vendor import xyz_to_str

            geometry_xyz_text = _normalize_xyz_text(xyz_to_str(frame_xyz), species_record.get("label"))
            if geometry_xyz_text is None:
                return None
            dimension = 3 * int(geometry_xyz_text.splitlines()[0])
            if len(triangle) != dimension * (dimension + 1) // 2:
                logger.warning("TCKDB bundle: Hessian omitted: matrix dimension disagrees with frame geometry")
                return None
        except Exception as exc:  # noqa: BLE001 — Hessian is best-effort only.
            logger.debug(
                "TCKDB bundle: Hessian parse skipped for label=%s (%s)",
                species_record.get("label"), exc,
            )
            return None
        if not triangle:
            return None
        geometry = _hessian_geometry(species_record, geometry_xyz_text, warnings)
        if geometry is None:
            return None
        return {
            "geometry": geometry,
            "lower_triangle_hartree_bohr2": triangle,
            "source": source,
            **_hessian_parser_block(_HESSIAN_PARSER_VERSION),
        }

    def _parse_irc_trajectories(
        self,
        output_doc: Mapping[str, Any],
        ts_record: Mapping[str, Any],
    ) -> list[dict[str, Any]] | None:
        """Parse each IRC log into a trajectory dict for payload assembly.

        Returns one dict per parsed log:
            ``{
                "direction": "forward"|"reverse"|None,
                "rich_points": [<rich-point dict>, ...] | None,
                "geom_points": [<xyz_dict>, ...] | None,
            }``

        Each log is first attempted with the rich parser
        (:func:`arc.parser.parser.parse_irc_path`) which carries energies,
        gradients, reaction coordinates, and per-point direction labels.
        On rich-parser failure (or for ESS backends that haven't
        implemented it), the geometry-only :func:`parse_irc_traj` runs
        as a fallback so the upload retains the geometry-only IRC payload
        behavior described in the task spec.

        Direction-resolution order (used when a per-point direction
        isn't present in the rich data):
        1. Rich parser's per-point ``direction`` (Gaussian's
           ``FORWARD/REVERSE path direction.`` announcement).
        2. ``ts_record['irc_log_directions'][i]`` — the authoritative
           value the scheduler captured from ``job.irc_direction`` and
           ``arc/output.py`` paired with ``irc_logs``. Production ARC
           IRC log filenames are just ``output.log`` inside an
           ``irc_<server_id>`` folder, so filename detection alone
           always returns None on real runs.
        3. ``_detect_irc_direction(filename)`` — back-compat fallback
           for output.yml files written before the paired-list
           tracking, and for test fixtures that hand-craft directional
           filenames.
        4. ``None`` — last resort. The trajectory is still emitted;
           per-point direction is omitted (the schema allows nullable
           ``IRCDirection`` on each point).

        Returns ``None`` when no logs resolve, no logs exist on disk, or
        every parse attempt failed. The caller uses ``None`` as the
        signal to omit ``irc_result`` (partial-data fallback per spec).
        """
        lookup = self._evidence.lookup(
            output_doc, "transition_state", str(ts_record.get("label") or ""), "irc"
        )
        if lookup.state == "available":
            return [
                {
                    "direction": trajectory.get("declared_direction"),
                    "rich_points": [
                        {
                            "point_number": point["source_point_index"],
                            "direction": point.get("direction"),
                            "geometry_xyz_text": point["geometry_xyz_text"],
                            "electronic_energy_hartree": point.get("electronic_energy_hartree"),
                            "reaction_coordinate": point.get("reaction_coordinate_sqrt_amu_bohr"),
                            "max_gradient": point.get("max_gradient_hartree_per_bohr"),
                            "rms_gradient": point.get("rms_gradient_hartree_per_bohr"),
                        }
                        for point in trajectory["points"]
                    ],
                    "geom_points": None,
                }
                for trajectory in (lookup.value or {})["trajectories"]
            ]
        if lookup.state == "unavailable":
            return None
        log_paths = ts_record.get("irc_logs") or []
        if not log_paths:
            return None
        # Lazy import via the optional-ARC boundary: parser imports drag in
        # heavyweight ESS adapters (ARC-only). These wrappers import cleanly and
        # only raise OptionalArcUnavailable at *call* time, where the existing
        # per-log ``try/except`` degrades to the geometry-only / omitted path.
        from tckdb_arc._arc_optional import parse_irc_path, parse_irc_traj

        log_directions = list(ts_record.get("irc_log_directions") or [])
        trajectories: list[dict[str, Any]] = []
        for i, log_path in enumerate(log_paths):
            resolved = self._resolve_local_path(log_path)
            if resolved is None or not resolved.is_file():
                logger.debug(
                    "TCKDB computed-reaction: IRC log path not found on disk: %r",
                    log_path,
                )
                continue
            # Resolve the trajectory-level direction once: scheduler-
            # tracked first, filename heuristic second, None last. This
            # is used as a per-point fallback when the rich parser
            # doesn't carry FORWARD/REVERSE labels (currently only
            # Gaussian does).
            direction = log_directions[i] if i < len(log_directions) else None
            if direction not in (_IRC_DIRECTION_FORWARD, _IRC_DIRECTION_REVERSE):
                direction = _detect_irc_direction(str(log_path))
            rich_points: list[dict[str, Any]] | None = None
            try:
                rich_points = parse_irc_path(log_file_path=str(resolved))
            except Exception as exc:
                logger.debug(
                    "TCKDB computed-reaction: parse_irc_path failed for %s: %s",
                    resolved, exc,
                )
            geom_points: list[dict[str, Any]] | None = None
            if not rich_points:
                # Geometry-only fallback so non-Gaussian logs (and
                # malformed Gaussian logs) keep producing a usable IRC
                # payload, matching the pre-rich-parser behavior.
                try:
                    geom_points = parse_irc_traj(log_file_path=str(resolved))
                except Exception as exc:
                    logger.debug(
                        "TCKDB computed-reaction: parse_irc_traj failed for %s: %s",
                        resolved, exc,
                    )
                    continue
                if not geom_points:
                    continue
            trajectories.append({
                "direction": direction,
                "rich_points": rich_points,
                "geom_points": geom_points,
            })
        return trajectories or None

    # ------------------------------------------------------------------
    # Computed-reaction bundle path (POST /uploads/computed-reaction)
    # ------------------------------------------------------------------

    def submit_computed_reaction_from_output(
        self,
        *,
        output_doc: Mapping[str, Any],
        reaction_record: Mapping[str, Any],
        is_partial: bool = False,
    ) -> UploadOutcome | None:
        """Build, write, and (if configured) upload one computed-reaction bundle.

        Walks the reaction's ``reactant_labels``/``product_labels`` against
        ``output_doc['species']`` (and ``output_doc['transition_states']``
        for ``ts_label``) and assembles a self-contained
        ``ComputedReactionUploadRequest`` covering species, TS, and
        modified-Arrhenius kinetics — all cross-referenced by local
        string keys, no DB ids.

        ``is_partial=True`` marks the bundle as a deliberately
        incomplete record (phase-1 partial sidecar for a reaction whose
        TS search failed). The caller is responsible for stripping
        ``ts_label`` and ``kinetics`` from ``reaction_record`` before
        calling — the adapter does not edit the input. Partial bundles
        are sidecar-only in phase-1: the network POST is skipped
        regardless of ``config.upload``, the payload + sidecar land on
        disk with a ``.partial`` filename infix and ``is_partial=true``
        metadata, and the returned :class:`UploadOutcome` has
        ``status="skipped"``.

        Returns ``None`` if the adapter is disabled. Build failures (e.g.
        missing reactant species, missing opt level) raise; the caller is
        responsible for wrapping the per-reaction call in a try/except so
        one bad reaction doesn't take down the rest of the run.
        """
        if not self._config.enabled:
            return None

        reaction_label = reaction_record.get("label") or "unlabeled"
        project_label = self._config.project_label or output_doc.get("project")

        build_warnings: list[dict[str, Any]] = WarningSink(_PRODUCER_TAG)
        output_doc = self._with_adaptive_levels(output_doc, build_warnings)
        output_doc = _withhold_unreliable_energies_in_doc(output_doc, build_warnings)
        reaction_record = _withhold_reaction_kinetics(output_doc, reaction_record, build_warnings)
        payload = self._build_computed_reaction_payload(
            output_doc=output_doc,
            reaction_record=reaction_record,
            warnings=build_warnings,
        )
        _enforce_geometry_anchoring(payload, "computed_reaction", build_warnings)
        _withhold_unlabelled_point_geometries(payload, "computed_reaction", build_warnings)
        self._report_adaptive_omissions(output_doc, build_warnings)

        return self._write_and_upload(
            payload=payload,
            kind=COMPUTED_REACTION_KIND,
            endpoint=COMPUTED_REACTION_ENDPOINT,
            label=reaction_label,
            project_label=project_label,
            species_label=reaction_label,
            # The reaction has no conformer concept at the bundle level —
            # just one fit per upload — so the conformer slot in the key
            # carries the TS label (or "noTS"). This keeps the key shape
            # consistent with the species path while still uniquely
            # identifying the reaction within a project.
            conformer_label=str(reaction_record.get("ts_label") or "noTS"),
            log_name="computed-reaction",
            subdir=PayloadWriter.COMPUTED_REACTION_SUBDIR,
            warnings=build_warnings,
            # A partial reaction is written (payload and sidecar) and never posted.
            is_partial=is_partial,
        )

    def _build_computed_reaction_payload(
        self,
        *,
        output_doc: Mapping[str, Any],
        reaction_record: Mapping[str, Any],
        warnings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compose one ``ComputedReactionUploadRequest`` dict.

        Resolves reactant/product/TS records from ``output_doc``,
        delegates per-actor block construction (species + TS) to the
        shared per-actor helpers, and stitches in a single
        modified-Arrhenius kinetics fit when ARC produced one.
        Producer-side omissions are appended to ``warnings``.
        """
        species_index = _index_species(output_doc)
        ts_index = _index_transition_states(output_doc)
        reaction_record = _with_stated_participants(
            reaction_record,
            ts_record=ts_index.get(reaction_record.get("ts_label")),
            species_index=species_index,
            warnings=warnings,
        )

        reactant_labels = list(reaction_record.get("reactant_labels") or [])
        product_labels = list(reaction_record.get("product_labels") or [])
        if not reactant_labels:
            raise ValueError(
                f"reaction label={reaction_record.get('label')!r} has no reactant_labels."
            )
        if not product_labels:
            raise ValueError(
                f"reaction label={reaction_record.get('label')!r} has no product_labels."
            )

        # Build reactant/product species blocks under namespaced keys.
        species_blocks: list[dict[str, Any]] = []
        reactant_keys: list[str] = []
        product_keys: list[str] = []
        # Per-actor calc-role → bundle-key map; consumed by the kinetics
        # builder so source_calculations references resolve to the same
        # local keys the species blocks declared.
        actor_calc_keys: dict[str, dict[str, str]] = {}

        # One species block per distinct ARC species label. A species that
        # appears on both sides (degenerate H2 + H <=> H + H2) or twice on
        # one side (H + H <=> H2) is declared once and *referenced* from
        # every slot: ComputedReactionUploadRequest allows the same key in
        # reactant_keys / product_keys (the schema's own example is
        # ``reactant_keys: ["h", "h"]``) and the server resolves
        # participants by position. Re-declaring it would deposit the
        # species' calculations, conformer observation and thermo twice.
        # The first slot to see a label names the block (r0_H2), so
        # non-degenerate reactions keep their existing keys.
        key_by_label: dict[str, str] = {}
        for side, prefix, labels, keys_out in (
            ("reactant", "r", reactant_labels, reactant_keys),
            ("product", "p", product_labels, product_keys),
        ):
            for i, label in enumerate(labels):
                existing_key = key_by_label.get(label)
                if existing_key is not None:
                    keys_out.append(existing_key)
                    continue
                actor_key = _local_key_for_actor(prefix, i, label)
                calc_prefix = _calc_prefix_for_actor(prefix, i)
                record = species_index.get(label)
                if record is None:
                    raise ValueError(
                        f"reaction {reaction_record.get('label')!r}: {side} "
                        f"label {label!r} not found in output_doc.species."
                    )
                block, calc_keys = self._build_reaction_species_block(
                    output_doc=output_doc,
                    species_record=record,
                    actor_key=actor_key,
                    calc_prefix=calc_prefix,
                    warnings=warnings,
                )
                species_blocks.append(block)
                keys_out.append(actor_key)
                key_by_label[label] = actor_key
                actor_calc_keys[actor_key] = calc_keys

        # TS block (inline). Optional — a reaction with no TS still
        # carries kinetics but server-side it's a thinner record.
        ts_label = reaction_record.get("ts_label")
        ts_block: dict[str, Any] | None = None
        ts_calc_keys: dict[str, str] = {}
        atom_map_block: dict[str, Any] | None = None
        if ts_label:
            ts_record = ts_index.get(ts_label)
            if ts_record is None:
                raise ValueError(
                    f"reaction {reaction_record.get('label')!r}: ts_label "
                    f"{ts_label!r} not found in output_doc.transition_states."
                )
            ts_block, ts_calc_keys = self._build_ts_block(
                output_doc=output_doc,
                ts_record=ts_record,
                ts_label=ts_label,
                reaction_multiplicity=reaction_record.get("multiplicity"),
                unmapped_smiles=_ts_unmapped_smiles_handle(
                    ts_record=ts_record,
                    reaction_record=reaction_record,
                    species_index=species_index,
                ),
                warnings=warnings,
                reaction_record=reaction_record,
                species_index=species_index,
            )
            ts_geometry = ts_block.get("geometry")
            if isinstance(ts_geometry, Mapping) and ts_geometry.get("key"):
                slots = []
                for side, keys, labels in (("reactant", reactant_keys, reactant_labels),
                                           ("product", product_keys, product_labels)):
                    for position, (species_key, label) in enumerate(zip(keys, labels), start=1):
                        block = next(b for b in species_blocks if b["key"] == species_key)
                        slots.append({"side": side, "index": position, "species_key": species_key,
                                      "label": label, "geometry": block["conformers"][0].get("geometry")})
                atom_map_block = _tckdb_reaction_atom_map(
                    reaction_record, ts_label=ts_label, ts_xyz_text=ts_geometry.get("xyz_text"),
                    ts_geometry_key=ts_geometry["key"], participants=slots,
                    irc_evidence=ts_block.get("validation_evidence") or (), warnings=warnings)
            else:
                _warn_atom_map_not_sent(reaction_record, ts_label=ts_label, warnings=warnings)
            # ARC's electronic-energy ordering verdict (``ts_checks['e_elect']``) needs every
            # participant's own sp calculation, which only the bundle carries: the standalone
            # TS upload refuses ``energy_ordering``, so it is built here and not in the TS block.
            ordering = _ts_energy_ordering_validation_evidence(
                ts_record,
                ts_sp_key=ts_calc_keys.get(_CALC_KEY_SP),
                reactant_labels=reactant_labels,
                product_labels=product_labels,
                reactant_keys=reactant_keys,
                product_keys=product_keys,
                actor_calc_keys=actor_calc_keys,
                species_index=species_index,
                ts_label=ts_label,
                warnings=warnings,
            )
            if ordering:
                ts_block["validation_evidence"] = [*ts_block.get("validation_evidence", []), *ordering]

        # Kinetics. ARC produces at most one fit per reaction today.
        kinetics_payload = reaction_record.get("kinetics")
        kinetics_blocks: list[dict[str, Any]] = []
        if isinstance(kinetics_payload, Mapping):
            kinetics_block = _build_kinetics_block(
                kinetics_record=kinetics_payload,
                reactant_keys=reactant_keys,
                product_keys=product_keys,
                actor_calc_keys=actor_calc_keys,
                ts_calc_keys=ts_calc_keys,
                long_kinetic_description=reaction_record.get(
                    "long_kinetic_description"
                ),
                schema_1_3=_is_output_schema_1_3_or_later(output_doc),
            )
            if kinetics_block is not None:
                # tckdb-schemas 0.99: the level the fit's energies came from, declared only when
                # ARC states it for every participant and they all agree (see
                # ``_kinetics_energy_level``); otherwise TCKDB warns
                # ``missing_level_of_theory_provenance`` and nothing is claimed.
                declared = _kinetics_energy_level(
                    output_doc=output_doc,
                    kinetics_block=kinetics_block,
                    reactant_keys=reactant_keys,
                    product_keys=product_keys,
                    key_by_label=key_by_label,
                    species_index=species_index,
                    ts_record=ts_index.get(ts_label) if ts_label else None,
                    calculations=_calculations_by_key(
                        *(c for b in species_blocks
                          for c in [b["conformers"][0]["calculation"], *b.get("calculations", ())]),
                        (ts_block or {}).get("calculations") or ()),
                    multiplicities=_calculation_multiplicities(species_blocks, ts_block),
                    electron_counts=_calculation_electron_counts(
                        species_blocks, ts_block, key_by_label=key_by_label,
                        species_index=species_index,
                        ts_record=ts_index.get(ts_label) if ts_label else None),
                    warnings=warnings,
                )
                if declared is not None:
                    kinetics_block["energy_level_of_theory"] = declared
                declarations = _kinetics_declarations(
                    kinetics_payload, project=output_doc.get("project"),
                    has_workflow_tool=_arc_workflow_tool_release(output_doc) is not None,
                    ts_label=ts_label, kinetics_block=kinetics_block,
                    output_doc=output_doc,
                    source_records=[
                        (label, species_index.get(label))
                        for label in (*reactant_labels, *product_labels)
                    ] + ([(ts_label, ts_index.get(ts_label))] if ts_label else []),
                    calculations=_calculations_by_key(
                        *(c for b in species_blocks
                          for c in [b["conformers"][0]["calculation"], *b.get("calculations", ())]),
                        (ts_block or {}).get("calculations") or (),
                        *((ts_block["calculation"],) if ts_block and ts_block.get("calculation") else ())))
                kinetics_block.update(declarations)
                _note_kinetics_declarations_interpreted(
                    kinetics_block, declarations, warnings=warnings,
                    field=f"kinetics[{len(kinetics_blocks)}]")
                kinetics_blocks.append(kinetics_block)

        bundle: dict[str, Any] = {
            "species": species_blocks,
            "reactant_keys": reactant_keys,
            "product_keys": product_keys,
        }
        # Output 1.3 states ``reversible`` per reaction (arc/output.py
        # ``_get_reversible``: True for ``<=>``, False for ``=>``, null for any
        # other arrow), and a stated bool is sent as stated. Otherwise (before
        # 1.3, or null) it is omitted, so the schema's default (True) applies.
        if (_is_output_schema_1_3_or_later(output_doc)
                and isinstance(reaction_record.get("reversible"), bool)):
            bundle["reversible"] = reaction_record["reversible"]
        if ts_block is not None:
            bundle["transition_state"] = ts_block
        if atom_map_block is not None:
            # Participants are the slots of reactant_keys / product_keys; each counts into the
            # conformer geometry of its species block, and the map names the TS geometry.
            bundle["atom_map"] = atom_map_block
        if kinetics_blocks:
            bundle["kinetics"] = kinetics_blocks

        # Final pass: flatten every calc's wrapped result into the
        # network_pdep flat fields the computed-reaction endpoint
        # expects. Keeping this as a single end-of-build walker means
        # any new calc-emit site automatically gets the right shape.
        _flatten_all_reaction_calcs(bundle)

        family = reaction_record.get("family")
        if family:
            bundle["reaction_family"] = str(family)
            # The server validates against a canonical-family list and
            # demands a source_note when the supplied name is unknown.
            # We don't have that list at the producer, so always tag
            # the source — a no-op for canonical names, a safety net
            # for non-canonical ones.
            bundle["reaction_family_source_note"] = "ARC-reported family"

        workflow_tool = _arc_workflow_tool_release(output_doc)
        if workflow_tool is not None:
            bundle["workflow_tool_release"] = workflow_tool

        analysis_release = _arc_analysis_software_release(output_doc)
        if analysis_release is not None:
            bundle["analysis_software_release"] = analysis_release

        return bundle

    def _build_reaction_species_block(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        actor_key: str,
        calc_prefix: str,
        warnings: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Build one ``BundleSpeciesIn`` dict + a calc-role → bundle-key map.

        ``actor_key`` (e.g. ``"r0_CHO"``) becomes the species block's
        ``key`` and the geometry/conformer key tails. ``calc_prefix``
        (e.g. ``"r0"``) is the namespace for calculation keys —
        deliberately shorter than ``actor_key`` so source_calculations
        references stay compact.

        The returned map (e.g. ``{"opt": "r0_opt", "freq": "r0_freq",
        "sp": "r0_sp"}``) is what the kinetics builder uses to wire
        ``source_calculations`` back to the freshly-minted local keys.
        It only contains the roles whose calculation actually made it
        into the bundle.
        """
        _warn_primary_opt_placeholder(
            species_record, warnings,
            field=f"species[{actor_key}].conformers[0].calculation")
        conformer_xyz_text = _require_xyz_text(species_record)
        opt_key = f"{calc_prefix}_{_CALC_KEY_OPT}"
        opt_coarse_key = f"{calc_prefix}_{_CALC_KEY_OPT_COARSE}"
        freq_key = f"{calc_prefix}_{_CALC_KEY_FREQ}"
        sp_key = f"{calc_prefix}_{_CALC_KEY_SP}"
        geom_key = f"{actor_key}_geom"
        conf_key = f"{actor_key}_conf0"
        monatomic = _is_single_atom_geometry(conformer_xyz_text)

        # Coarse-opt provenance, parallel to the computed-species path.
        # Same gate (both ``coarse_opt_log`` and ``coarse_opt_output_xyz``
        # must be present) — when only one resolves, we fall back to
        # single-stage rather than emit a half-described opt_coarse.
        # Namespacing the calc key with ``calc_prefix`` keeps it globally
        # unique across reactant/product/TS, which the bundle's
        # ``validate_unique_keys`` requires.
        opt_coarse_calc = self._build_opt_coarse_calc(
            output_doc=output_doc,
            species_record=species_record,
            calc_key=opt_coarse_key,
        )
        fine_opt_depends_on: list[Mapping[str, Any]] | None = None
        if opt_coarse_calc is not None:
            fine_opt_depends_on = [
                {"parent_calculation_key": opt_coarse_key,
                 "role": "optimized_from"}
            ]

        try:
            if monatomic:
                # An atom's primary is its own single point (tckdb-schemas 0.59,
                # TCKDB#610): no opt, coarse opt, freq or rotor scan exists to carry.
                primary_calc = self._build_monatomic_primary_sp(
                    output_doc=output_doc, species_record=species_record,
                    calc_key=sp_key, conformer_xyz_text=conformer_xyz_text)
                opt_coarse_calc = None
                fine_opt_depends_on = None
                calc_keys: dict[str, str] = {_CALC_KEY_SP: sp_key}
            else:
                primary_calc = self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=species_record,
                    calc_key=opt_key,
                    calc_role=_CALC_KEY_OPT,
                    calc_type="opt",
                    level_kind="opt",
                    ess_job_key="opt",
                    result_field="opt_result",
                    result_payload=_opt_result_payload(species_record),
                    depends_on=fine_opt_depends_on,
                    tckdb_origin=None,
                    conformer_xyz_text=conformer_xyz_text,
                )
                calc_keys = {_CALC_KEY_OPT: opt_key}
        except ValueError as exc:
            # The primary calculation is the one a participant cannot do without (an
            # atom's sp, a molecule's opt), so unlike freq/sp/scan below it cannot be
            # skipped: the whole reaction is refused, naming the participant.
            raise ValueError(
                f"reaction participant {actor_key!r} ({species_record.get('label')!r}): its "
                f"primary calculation cannot be built, so the reaction is not uploaded: {exc}"
            ) from exc
        additional: list[dict[str, Any]] = []
        # opt_coarse is type=opt, so the schema validator at
        # computed_reaction_upload.py:446 exempts it from needing
        # geometry_key — leave it bare.
        if opt_coarse_calc is not None:
            # Anchor to the conformer observation without claiming the coarse
            # geometry is the final conformer geometry.
            opt_coarse_calc["conformer_key"] = conf_key
            additional.append(opt_coarse_calc)
            calc_keys[_CALC_KEY_OPT_COARSE] = opt_coarse_key

        freq_result = _freq_result_payload(species_record, schema_1_3=_is_output_schema_1_3_or_later(output_doc))
        if freq_result is not None and not monatomic:
            try:
                freq_calc = self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=species_record,
                    calc_key=freq_key,
                    calc_role=_CALC_KEY_FREQ,
                    calc_type="freq",
                    level_kind="freq",
                    ess_job_key="freq",
                    result_field="freq_result",
                    result_payload=freq_result,
                    depends_on=[{"parent_calculation_key": opt_key, "role": "freq_on"}],
                    tckdb_origin=None,
                    conformer_xyz_text=conformer_xyz_text,
                    warnings=warnings,
                )
                # Server requires non-opt species calcs to reference a
                # conformer geometry by key (BundleSpeciesIn validator
                # validate_calc_geometry_keys).
                freq_calc["geometry_key"] = geom_key
                additional.append(freq_calc)
                calc_keys[_CALC_KEY_FREQ] = freq_key
            except ValueError as exc:
                logger.warning(
                    "TCKDB computed-reaction: %s freq calculation skipped: %s",
                    actor_key, exc,
                )

        sp_result = _sp_result_payload(species_record)
        if sp_result is not None and not monatomic:
            try:
                sp_calc = self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=species_record,
                    calc_key=sp_key,
                    calc_role=_CALC_KEY_SP,
                    calc_type="sp",
                    level_kind="sp",
                    ess_job_key="sp",
                    result_field="sp_result",
                    result_payload=sp_result,
                    depends_on=[{"parent_calculation_key": opt_key, "role": "single_point_on"}],
                    tckdb_origin=(
                        _sp_reuse_origin(output_doc, species_record)
                    ),
                    conformer_xyz_text=conformer_xyz_text,
                )
                sp_calc["geometry_key"] = geom_key
                additional.append(sp_calc)
                calc_keys[_CALC_KEY_SP] = sp_key
            except ValueError as exc:
                logger.warning(
                    "TCKDB computed-reaction: %s sp calculation skipped: %s",
                    actor_key, exc,
                )

        # Rotor scans. Mirrors ``_build_conformer_block``'s scan loop,
        # with two reaction-path-specific deltas:
        #
        #  - Calc keys are namespaced (``r0_scan_rotor_0``, etc.). The
        #    bundle schema's ``validate_unique_keys`` requires *globally*
        #    unique calc keys across all species + the TS, so two
        #    reactants both reporting ``scan_rotor_0`` would collide.
        #    The torsion's ``source_scan_calculation_key`` gets rewritten
        #    in lockstep via ``scan_key_renames`` so the reference still
        #    resolves.
        #  - ``geometry_key`` is set: non-opt species calcs in reaction
        #    bundles must point at the conformer geometry (same reason
        #    freq/sp set it above).
        scan_key_renames: dict[str, str] = {}
        unbuilt_scans: dict[str, str] = {}
        for scan_entry in ([] if monatomic else _scan_entries_from_record(species_record)):
            if not isinstance(scan_entry, Mapping):
                continue
            if scan_entry.get("type") != _CALC_KEY_SCAN:
                continue
            original_scan_key = scan_entry.get("key")
            scan_result = scan_entry.get("scan_result")
            if not isinstance(original_scan_key, str) or not original_scan_key:
                continue
            if not isinstance(scan_result, Mapping):
                continue
            namespaced_scan_key = f"{calc_prefix}_{original_scan_key}"
            try:
                scan_calc = self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=species_record,
                    calc_key=namespaced_scan_key,
                    calc_role=_CALC_KEY_SCAN,
                    calc_type=_CALC_KEY_SCAN,
                    level_kind="scan",
                    ess_job_key="scan",
                    result_field="scan_result",
                    result_payload=scan_result,
                    depends_on=[{"parent_calculation_key": opt_key,
                                 "role": "scan_parent"}],
                    tckdb_origin=None,
                    conformer_xyz_text=conformer_xyz_text,
                    source_constraints=scan_entry.get("constraints"),
                    output_log_path=scan_entry.get("source_log"),
                    observed_software=scan_entry.get("ess_software"),
                    observed_version=scan_entry.get("ess_version"),
                    level_job_type=_scan_job_type(
                        output_doc, species_record, original_scan_key),
                )
                scan_calc["geometry_key"] = geom_key
                additional.append(scan_calc)
                scan_key_renames[original_scan_key] = namespaced_scan_key
            except ValueError as exc:
                logger.warning(
                    "TCKDB computed-reaction: %s scan calculation %s skipped: %s",
                    actor_key, original_scan_key, exc,
                )
                unbuilt_scans[original_scan_key] = str(exc)

        species_block: dict[str, Any] = {
            "key": actor_key,
            "species_entry": self._species_entry_payload(species_record),
            "conformers": [
                {
                    "key": conf_key,
                    "geometry": _geometry_payload(
                        species_record, conformer_xyz_text, species_record.get("xyz_isotopes"),
                        key=geom_key),
                    "calculation": primary_calc,
                }
            ],
            "calculations": additional,
        }
        # ``ConformerIn.label`` (computed-reaction) is the same optional
        # conformer label the computed-species route already sends.
        conformer_label = species_record.get("label")
        if conformer_label:
            species_block["conformers"][0]["label"] = str(conformer_label)[:64]
        energy_level = (
            _thermo_energy_level(output_doc, species_record)
            if isinstance(species_record.get("thermo"), Mapping) else None
        )
        # See the computed-species route: declared from the linked sp's own level.
        energy_declaration = _energy_level_declaration(
            _thermo_energy_level(output_doc, species_record),
            calc_keys_by_role=calc_keys,
            calculations=_calculations_by_key(primary_calc, additional),
        )
        thermo_block = _build_thermo_block(
            species_record.get("thermo"),
            # The current reaction root accepts species-scoped thermo provenance.
            calc_keys_by_role=_with_composite_role(species_record, calc_keys),
            target_model="BundleThermoIn",
            warnings=warnings,
            warning_field=f"species[{actor_key}].thermo",
            energy_level=energy_level,
            header_corrections_level=output_doc.get("arkane_level_of_theory"),
            element_symbols=_species_element_symbols(species_record),
            energy_level_unattributable=_energy_level_unattributable(output_doc, energy_level),
            energy_declaration=energy_declaration,
            legacy_cp_data=_is_output_schema_1_0(output_doc),
            electron_count=_participant_electron_count(species_record),
        )
        if thermo_block is not None:
            species_block["thermo"] = thermo_block

        # Per-species AEC/BAC corrections target this species's resolved
        # species_entry. Anchor each correction to this species's own SP
        # calc (e.g. r0_sp / p1_sp); never to ts_sp or to a sibling
        # species's SP key — the server enforces ownership in
        # ``_persist_species_applied_corrections`` and would 422 on a
        # cross-species reference.
        omitted_bacs: list[str] = []
        applied_corrections = _build_applied_energy_corrections(
            _correction_records_from_record(
                species_record, legacy_1_0=_is_output_schema_1_0(output_doc),
                schema_1_3=_is_output_schema_1_3_or_later(output_doc)),
            source_calculation_key=calc_keys.get(_CALC_KEY_SP),
            warnings=warnings,
            warning_field=f"species[{actor_key}].applied_energy_corrections",
            target_kind="species",
            element_symbols=_species_element_symbols(species_record),
            target_label=str(species_record.get("label") or "") or None,
            arkane_release=_arkane_workflow_tool_release(output_doc),
            scheme_data_revisions=_scheme_data_revisions(output_doc),
            aec_yml_digest=_arc_aec_yml_digest(output_doc),
            bac_frequency_level=_bac_frequency_level(output_doc, species_record),
            omitted_bac_reasons=omitted_bacs,
        )
        _note_omitted_bac_on_thermo(
            species_block.get("thermo"), omitted_bacs,
            _bond_corrections_flag(species_record))
        _warn_bac_type_not_stated(output_doc, species_record, warnings)
        if applied_corrections:
            species_block["applied_energy_corrections"] = applied_corrections

        # Statmech block: carries frequency-scale-factor provenance plus
        # the schema-supported base statmech fields for this reactant/
        # product (external_symmetry, is_linear, rigid_rotor_kind,
        # statmech_treatment, point_group, slim torsions, and
        # source_calculations referencing this species's own opt/freq/
        # sp calcs). This bundle's root is ComputedReactionUploadRequest,
        # whose per-species statmech field is ``BundleStatmechIn`` — it
        # currently accepts the same field set this builder can produce
        # as ``StatmechInBundle`` (the computed-species root) does (see
        # ``_STATMECH_FIELDS_BY_TARGET``), but that parity is incidental
        # rather than guaranteed, so ``target_model`` is passed
        # explicitly below rather than assumed. The only mode-specific
        # input is the calc-key namespace — we pass the *species-scoped*
        # ``calc_keys`` (e.g. ``{"opt": "r0_opt", ...}``) so the
        # server-side ownership check sees only this species's own
        # calculations. Sibling species and the TS use disjoint
        # namespaces (``r1_*``/``p0_*``/``ts_*``) and therefore can't
        # leak in here. The helper returns ``None`` (and we skip the
        # whole statmech block) when nothing useful resolves, keeping
        # payloads backward-compatible with FSF-less runs.
        species_statmech = _build_statmech_block_for_species(
            output_doc=output_doc,
            species_record=species_record,
            calc_keys_by_role=_with_composite_role(species_record, calc_keys),
            workflow_tool_release=_arc_workflow_tool_release(output_doc),
            target_model="BundleStatmechIn",
            scan_key_renames=scan_key_renames or None,
            freq_hessian_available=self._freq_hessian_available(
                output_doc=output_doc, species_record=species_record,
            ),
            unbuilt_scans=unbuilt_scans,
            warnings=warnings,
            warning_field=f"species[{actor_key}].statmech",
            energy_declaration=energy_declaration,
        )
        if species_statmech is not None:
            species_block["statmech"] = species_statmech

        return species_block, calc_keys

    def _build_ts_block(
        self,
        *,
        output_doc: Mapping[str, Any],
        ts_record: Mapping[str, Any],
        ts_label: str,
        reaction_multiplicity: int | None,
        unmapped_smiles: str | None = None,
        include_artifacts: bool = True,
        warnings: list[dict[str, Any]] | None = None,
        reaction_record: Mapping[str, Any] | None = None,
        species_index: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Build one ``BundleTransitionStateIn`` dict + a calc-role → key map.

        ARC stores TS records in ``output_doc['transition_states']`` with
        the same shape as species records (the TS is just a stationary
        point with ``is_ts: true``); the multiplicity falls back to the
        reaction's multiplicity when the TS record doesn't carry one.

        ``unmapped_smiles`` is a deterministic textual handle for the
        TS, computed by :func:`_ts_unmapped_smiles_handle` at the
        caller. ``None`` is the common case (TS has no Lewis structure
        and no upstream textual identifier was derivable); the field is
        omitted from the payload, leaving the server to store NULL.
        The producer never fabricates a normal-molecule SMILES from
        the TS geometry — the ``mol`` field stays absent regardless.

        IRC provenance: when ARC has ``irc_logs`` populated, emit a
        ``ts_irc`` calc with ``depends_on(role=irc_start)`` pointing at
        ``ts_opt`` — IRC is seeded from the optimized TS saddle, so
        ``ts_opt`` is its primary geometry-producing parent (the TS freq
        validates the saddle but isn't the geometry source). When the
        IRC log files parse cleanly, attach a structured ``irc_result``
        with forward/reverse points and producer-declared
        ``output_geometries`` (``irc_forward``/``irc_reverse`` endpoints).
        Forward/reverse are ESS path-direction labels — the producer
        does NOT infer reactant/product side from them.
        """
        conformer_xyz_text = _require_xyz_text(ts_record)
        if arc13.primary_opt_placeholder(ts_record) is not None:
            _warn_primary_opt_placeholder(
                ts_record, warnings, field="transition_state.calculation")
        ts_opt_key = f"ts_{_CALC_KEY_OPT}"
        # ``_CALC_KEY_TS_GUESS`` already starts with ``ts_``; using it
        # bare keeps the bundle key as ``ts_guess`` (don't double-prefix).
        ts_guess_key = _CALC_KEY_TS_GUESS
        ts_freq_key = f"ts_{_CALC_KEY_FREQ}"
        ts_sp_key = f"ts_{_CALC_KEY_SP}"
        ts_irc_key = f"ts_{_CALC_KEY_IRC}"
        ts_geom_key = "ts_geom"

        # Path-search ts_guess provenance. Emit a parent calculation
        # only when the chosen TS guess is itself a real path-search
        # calculation (orca_neb → method=neb, xtb_gsm → method=gsm) AND
        # the producer recorded the corresponding log path
        # (``neb_log`` / ``gsm_log``). Heuristics / AutoTST / KinBot /
        # GCN / user-supplied guesses stay geometry-only on the
        # ts_opt: ``calculation_dependency`` requires a real parent
        # calculation, never a geometry. The artifact path picks up
        # the log via ``_resolve_log_field`` when artifact upload is
        # enabled.
        ts_guess_calc: dict[str, Any] | None = None
        ts_guess_method = _resolve_ts_guess_path_search_for_record(ts_record)
        ts_guess_log_field = (
            _TS_GUESS_LOG_FIELD_BY_METHOD.get(ts_guess_method)
            if ts_guess_method else None
        )
        # The path-search calculation's level. ARC exports ``neb_level`` (the
        # ORCA NEB level, arc/output.py) and ``ess_software.neb`` /
        # ``ess_versions.neb`` for NEB, and nothing for GSM (xtb-gsm has no
        # level export). TCKDB requires a level on every calculation, so a
        # guess whose level ARC does not state is not filed at all: never at
        # ``opt_level``, which is not what the path search ran at.
        ts_guess_level: Mapping[str, Any] | None = None
        ts_guess_level_kind: str | None = None
        if (
            ts_guess_method
            and ts_guess_log_field
            and ts_record.get(ts_guess_log_field)
        ):
            # ``neb_level`` is ORCA's (orca_neb_settings) but ARC's Level
            # deduces a software from the method alone (wb97xd/def2tzvp gives
            # gaussian), so ``neb_level.software`` says nothing about what
            # ran. The program is the one observed on the NEB log
            # (``ess_software.neb``), or none: then nothing is filed. Output.yml
            # 1.3 states the GSM level too (``gsm_level``: GFN2-xTB, only when
            # every archived xtb node output shows it) with the program observed
            # on those outputs (``ess_software.gsm``); older output exports none
            # (``output_doc`` has no ``gsm_level``) and the GSM is not filed.
            path_search_level = (
                _resolve_level(output_doc, ts_guess_method)
                if ts_guess_method in ("neb", "gsm") else None)
            observed = ts_record.get("ess_software")
            if path_search_level is not None and not (
                    isinstance(observed, Mapping) and observed.get(ts_guess_method)):
                _warn_ts_guess_level_not_stated(
                    warnings, ts_label=ts_label, method=ts_guess_method,
                    reason=f"ARC recorded no ess_software.{ts_guess_method} for the "
                           f"{ts_guess_method.upper()} log",
                    software=True)
            elif path_search_level is None:
                _warn_ts_guess_level_not_stated(
                    warnings, ts_label=ts_label, method=ts_guess_method,
                    schema_states_level=("gsm_level" in output_doc if ts_guess_method == "gsm" else None))
            else:
                ts_guess_level = path_search_level
                ts_guess_level_kind = ts_guess_method
        if ts_guess_level is not None and ts_guess_level_kind is not None:
            # Resolve the on-disk log path (neb_log / gsm_log are stored
            # run-relative on the record). GSM energies come only from ARC's
            # parser-evidence sidecar, which attaches a node's xTB energy to
            # a stringfile frame by geometry.
            gsm_lookup = self._evidence.lookup(
                output_doc, "transition_state", str(ts_record.get("label") or ""), "gsm"
            ) if ts_guess_method == "gsm" else None
            log_local = self._resolve_local_path(
                ts_record.get(ts_guess_log_field),
            )
            path_search_payload = _build_path_search_result_payload(
                method=ts_guess_method,
                log_path=str(log_local) if log_local is not None else None,
                fallback_xyz_text=ts_record.get("opt_input_xyz"),
                gsm_evidence=(gsm_lookup.value if gsm_lookup is not None and gsm_lookup.state == "available"
                              else None),
                evidence_unavailable=bool(gsm_lookup is not None and gsm_lookup.state == "unavailable"),
            )
            if path_search_payload is None:
                logger.warning(
                    "TCKDB computed-reaction: ts_guess (%s) calc skipped — "
                    "could not build path_search_result.points (no parseable "
                    "log and no fallback xyz).",
                    ts_guess_method,
                )
            else:
                try:
                    ts_guess_calc = self._build_calc_in_bundle(
                        output_doc=output_doc,
                        species_record=ts_record,
                        calc_key=ts_guess_key,
                        calc_role=_CALC_KEY_TS_GUESS,
                        calc_type="path_search",
                        level_kind=ts_guess_level_kind,
                        ess_job_key=ts_guess_level_kind,
                        result_field="path_search_result",
                        result_payload=path_search_payload,
                        depends_on=None,
                        tckdb_origin=None,
                        conformer_xyz_text=None,
                        include_artifacts=include_artifacts,
                    )
                    # A parsed NEB band marks no image as the guess (see
                    # ``_build_path_search_result_payload``); the geometry ARC
                    # fed to ts_opt is the path search's final output.
                    guess_xyz = _normalize_xyz_text(ts_record.get("opt_input_xyz"), ts_record.get("label"))
                    if (ts_guess_method == "neb" and guess_xyz
                            and not any(p.get("is_ts_guess") for p in path_search_payload["points"])):
                        guess_geometry = _geometry_payload(
                            ts_record, guess_xyz, ts_record.get("opt_input_xyz_isotopes"))
                        if guess_geometry:
                            ts_guess_calc["output_geometries"] = [{"geometry": guess_geometry, "role": "final"}]
                except ValueError as exc:
                    logger.warning(
                        "TCKDB computed-reaction: ts_guess (%s) calc skipped: %s",
                        ts_guess_method, exc,
                    )
                    _warn_ts_guess_level_not_stated(
                        warnings, ts_label=ts_label, method=ts_guess_method,
                        reason=str(exc))

        ts_opt_depends_on: list[Mapping[str, Any]] | None = None
        if ts_guess_calc is not None:
            ts_opt_depends_on = [
                {"parent_calculation_key": ts_guess_key,
                 "role": "optimized_from"}
            ]

        # ARC's selected TS-guess method (heuristics, AutoTST, KinBot,
        # GCN, user, …) is workflow narrative — TCKDB has no
        # schema-reviewed slot for "what generated the guess that fed
        # ts_opt". The optimized TS itself (geometry, freq imag mode,
        # IRC connectivity to reactant/product) is the scientific
        # evidence the database stores; the upstream guess source is
        # ARC-internal context that lives in ARC's artifacts. Do not
        # re-introduce ``selected_ts_guess`` as a ``tckdb_origin``
        # marker on ts_opt unless TCKDB grows a dedicated field.
        # NEB/GSM is a separate case: ``path_search_result`` ships
        # actual scientific path-search data (per-image energies,
        # geometries) on the ts_guess calc, accepted by the schema.
        try:
            primary_calc = self._build_calc_in_bundle(
                output_doc=output_doc,
                species_record=ts_record,
                calc_key=ts_opt_key,
                calc_role=_CALC_KEY_OPT,
                calc_type="opt",
                level_kind="opt",
                ess_job_key="opt",
                result_field="opt_result",
                result_payload=_opt_result_payload(ts_record),
                depends_on=ts_opt_depends_on,
                tckdb_origin=None,
                conformer_xyz_text=conformer_xyz_text,
                include_artifacts=include_artifacts,
            )
        except ValueError as exc:
            raise ValueError(
                f"transition state {ts_label!r}: its primary calculation cannot be built, so the "
                f"reaction is not uploaded: {exc}"
            ) from exc

        calc_keys: dict[str, str] = {_CALC_KEY_OPT: ts_opt_key}
        additional: list[dict[str, Any]] = []
        # Order: ts_guess (parent) first, then freq/sp/irc descend
        # from ts_opt. ``BundleTransitionStateIn`` doesn't enforce a
        # geometry_key on additional calcs (unlike the species-side
        # validator), so type=path_search stays bare.
        if ts_guess_calc is not None:
            additional.append(ts_guess_calc)
            calc_keys[_CALC_KEY_TS_GUESS] = ts_guess_key

        freq_result = _freq_result_payload(ts_record, schema_1_3=_is_output_schema_1_3_or_later(output_doc))
        if freq_result is not None:
            try:
                additional.append(self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=ts_record,
                    calc_key=ts_freq_key,
                    calc_role=_CALC_KEY_FREQ,
                    calc_type="freq",
                    level_kind="freq",
                    ess_job_key="freq",
                    result_field="freq_result",
                    result_payload=freq_result,
                    depends_on=[{"parent_calculation_key": ts_opt_key, "role": "freq_on"}],
                    tckdb_origin=None,
                    conformer_xyz_text=conformer_xyz_text,
                    include_artifacts=include_artifacts,
                    warnings=warnings,
                ))
                calc_keys[_CALC_KEY_FREQ] = ts_freq_key
            except ValueError as exc:
                logger.warning("TCKDB computed-reaction: ts freq calc skipped: %s", exc)

        sp_result = _sp_result_payload(ts_record)
        if sp_result is not None:
            try:
                additional.append(self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=ts_record,
                    calc_key=ts_sp_key,
                    calc_role=_CALC_KEY_SP,
                    calc_type="sp",
                    level_kind="sp",
                    ess_job_key="sp",
                    result_field="sp_result",
                    result_payload=sp_result,
                    depends_on=[{"parent_calculation_key": ts_opt_key, "role": "single_point_on"}],
                    tckdb_origin=(
                        _sp_reuse_origin(output_doc, ts_record)
                    ),
                    conformer_xyz_text=conformer_xyz_text,
                    include_artifacts=include_artifacts,
                ))
                calc_keys[_CALC_KEY_SP] = ts_sp_key
            except ValueError as exc:
                logger.warning("TCKDB computed-reaction: ts sp calc skipped: %s", exc)

        # IRC: emit a calc when ARC has irc_logs populated. Always carry
        # depends_on(role=irc_start) → ts_opt, because IRC is seeded from
        # the optimized TS saddle. When the logs parse cleanly, attach a
        # structured irc_result with forward/reverse points and producer-
        # declared output_geometries for the irc_forward/irc_reverse
        # endpoints. When parsing fails we still emit the type=irc calc
        # (so kinetics.source_calculations(role=irc) and the dependency
        # edge can stand on their own) but omit irc_result rather than
        # fabricating incomplete structured data.
        # The IRC level: the adaptive level ``restart.yml`` gives this TS when the
        # adaptive levels name ``irc``, else the ``irc_level`` restart.yml
        # records (ARC records it whenever it differs from the settings
        # default), else assumed (below). A named ``irc`` that cannot be
        # attributed states no level, so no IRC calculation.
        irc_level, irc_level_unattributable = _irc_level(output_doc, ts_record)
        if (irc_level is None and irc_level_unattributable and ts_record.get("irc_logs")
                and arc13.has_levels(ts_record)):
            _warn_irc_level_not_stated(warnings, ts_label=ts_label, ts_record=ts_record)
        if (irc_level is not None and not irc_level.get("software")
                and arc13.has_levels(ts_record)):
            # Schema 1.3 states the program of the IRC logs, or it is not stated.
            logger.warning(
                "TCKDB irc_software_not_stated: TS %r IRC calculation not filed: "
                "ARC recorded no ess_software.irc.", ts_label)
            if warnings is not None:
                warnings.append(_self_check(
                    ArcWarning.IRC_SOFTWARE_NOT_STATED.value,
                    f"The IRC calculation of {ts_label!r} was not uploaded: ARC "
                        "states the program of the IRC logs in ess_software.irc, "
                        "only when every IRC log was identified as the same program, "
                        "and it did not here.",
                    "transition_state.irc.software_release",
                    {"action": "irc_calculation_omitted"}))
            irc_level_unattributable = True
        elif irc_level is not None and not irc_level.get("software"):
            # ARC's IRC program rule gives no ESS this adapter can name.
            logger.warning(
                "TCKDB irc_software_not_stated: TS %r IRC calculation not filed: "
                "its level's method (%s) is not run in Gaussian, and ARC records no "
                "IRC program.", ts_label, irc_level.get("method"))
            if warnings is not None:
                warnings.append(_self_check(
                    ArcWarning.IRC_SOFTWARE_NOT_STATED.value,
                    f"The IRC calculation of {ts_label!r} was not uploaded: ARC runs "
                        "an IRC in Gaussian (arc/level.py deduce_software) except for "
                        f"UMA/torchani/xtb-type methods ({irc_level.get('method')!r}), "
                        "and records no program for it.",
                    "transition_state.irc.software_release",
                    {"action": "irc_calculation_omitted"}))
            irc_level_unattributable = True
        if ts_record.get("irc_logs") and not irc_level_unattributable:
            try:
                irc_calc = self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=ts_record,
                    calc_key=ts_irc_key,
                    calc_role=_CALC_KEY_IRC,
                    calc_type="irc",
                    # ARC runs IRC at ``irc_level`` (arc/scheduler.py
                    # ``run_irc_job``), which output.yml does not export.
                    # When restart.yml records it (or the adaptive levels
                    # give it), that is the level, and the program is the one
                    # the level names (there is no ``ess_software.irc``). If
                    # not, the level is the run's settings default, which is
                    # unreadable here; it is assumed to be ``opt_level`` and
                    # reported (``irc_level_assumed_opt_level``, below).
                    level_kind="irc" if irc_level is not None else "opt",
                    ess_job_key="irc" if irc_level is not None else "opt",
                    level_override=irc_level,
                    result_field=None,
                    result_payload=None,
                    depends_on=[{"parent_calculation_key": ts_opt_key, "role": "irc_start"}],
                    tckdb_origin=None,
                    conformer_xyz_text=conformer_xyz_text,
                    include_artifacts=include_artifacts,
                )
                # IRC output geometries are derived by the server from
                # ``irc_result.points``: ``_persist_irc_result`` writes a
                # ``calculation_output_geometry`` row (role=irc_forward /
                # irc_reverse) for every forward/reverse point. Emitting
                # explicit ``output_geometries`` here would double-claim
                # those geometries and the
                # ``attach_calculation_output_geometries`` uniqueness check
                # would 422. We therefore attach only ``irc_result`` and
                # let the server own the output-geometry links.
                irc_parsed = self._parse_irc_trajectories(output_doc, ts_record)
                if irc_parsed is not None:
                    zero_ref = _resolve_irc_zero_energy_reference(
                        output_doc=output_doc,
                        ts_record=ts_record,
                        irc_level=irc_level,
                    )
                    # Build the optional TS marker from the seed-saddle
                    # record. ``conformer_xyz_text`` is the same TS xyz
                    # the TS block already uses for ``ts_opt``'s output
                    # geometry, so the marker points at the canonical
                    # saddle — no separate xyz_to_str pass needed.
                    ts_marker: dict[str, Any] | None = None
                    if conformer_xyz_text:
                        ts_marker = {
                            "xyz_text": conformer_xyz_text,
                            # Pair the marker's energy with the same
                            # zero_ref the per-point relatives are
                            # computed against — keeps relative_energy
                            # internally consistent (TS marker → 0).
                            "electronic_energy_hartree": zero_ref,
                        }
                    irc_result = _build_irc_result_payload(
                        irc_parsed,
                        zero_energy_reference_hartree=zero_ref,
                        ts_marker=ts_marker,
                        warnings=warnings,
                    )
                    if irc_result is not None:
                        irc_calc["irc_result"] = irc_result
                additional.append(irc_calc)
                calc_keys[_CALC_KEY_IRC] = ts_irc_key
                if irc_level is None:
                    _warn_irc_level_assumed(warnings, irc_calc["level_of_theory"])
            except ValueError as exc:
                logger.debug("TCKDB computed-reaction: ts irc calc skipped: %s", exc)

        # ARC exports successful TS rotor scans through the same record
        # contract as stable species. Preserve the calculation and its parent
        # even though the TS upload root has no statmech/torsion slot.
        for scan_entry in _scan_entries_from_record(ts_record):
            scan_key = scan_entry.get("key")
            scan_result = scan_entry.get("scan_result")
            if (scan_entry.get("type") != _CALC_KEY_SCAN
                    or not isinstance(scan_key, str) or not scan_key
                    or not isinstance(scan_result, Mapping)):
                continue
            try:
                scan_calc = self._build_calc_in_bundle(
                    output_doc=output_doc,
                    species_record=ts_record,
                    calc_key=f"ts_{scan_key}",
                    calc_role=_CALC_KEY_SCAN,
                    calc_type=_CALC_KEY_SCAN,
                    level_kind="scan",
                    ess_job_key="scan",
                    result_field="scan_result",
                    result_payload=scan_result,
                    depends_on=[{"parent_calculation_key": ts_opt_key,
                                 "role": "scan_parent"}],
                    tckdb_origin=None,
                    conformer_xyz_text=conformer_xyz_text,
                    source_constraints=scan_entry.get("constraints"),
                    output_log_path=scan_entry.get("source_log"),
                    observed_software=scan_entry.get("ess_software"),
                    observed_version=scan_entry.get("ess_version"),
                    include_artifacts=include_artifacts,
                    level_job_type=_scan_job_type(output_doc, ts_record, scan_key),
                )
                additional.append(scan_calc)
            except ValueError as exc:
                logger.warning("TCKDB transition-state: scan %s skipped: %s", scan_key, exc)

        # TS multiplicity: the TS record's own field is authoritative;
        # fall back to the reaction's multiplicity when missing (some
        # ARC runs only carry it on the reaction).
        # ARC creates the TS species with the reaction's multiplicity
        # (arc/scheduler.py ``ARCSpecies(is_ts=True, multiplicity=
        # rxn.multiplicity, ...)``), so the reaction's value is ARC's own
        # statement, not a default.
        ts_mult = ts_record.get("multiplicity")
        if ts_mult is None:
            ts_mult = reaction_multiplicity
        if ts_mult is None:
            raise ValueError(
                f"transition state {ts_label!r} has no multiplicity and the "
                "reaction record has none either; cannot build TS block."
            )
        ts_mult_record = {"multiplicity": ts_mult}

        ts_block: dict[str, Any] = {
            "charge": _stated_integer(ts_record, "charge", f"transition state {ts_label!r}"),
            "multiplicity": _stated_integer(
                ts_mult_record, "multiplicity", f"transition state {ts_label!r}", minimum=1),
            "geometry": _geometry_payload(
                ts_record, conformer_xyz_text, ts_record.get("xyz_isotopes"), key=ts_geom_key),
            "calculation": primary_calc,
            "calculations": additional,
            "label": str(ts_label)[:64],
        }
        if unmapped_smiles:
            ts_block["unmapped_smiles"] = str(unmapped_smiles)

        # ARC's verdict on whether the IRC connects the declared reactants and
        # products, from ``ts_checks['IRC']`` and nothing else (never
        # ``irc_converged``, which only says the IRC jobs finished).
        validation_evidence = _ts_irc_validation_evidence(
            ts_record,
            irc_calc_key=calc_keys.get(_CALC_KEY_IRC),
            ts_label=ts_label,
            warnings=warnings,
            reaction_record=reaction_record,
            species_index=species_index,
            ts_xyz_text=conformer_xyz_text,
        )
        # ARC's verdict on the TS frequency calculation (``ts_checks['freq']``), read from the
        # ``freq_result`` sent for it, so the record cannot contradict that result.
        validation_evidence += _ts_imaginary_mode_validation_evidence(
            ts_record,
            freq_calc_key=calc_keys.get(_CALC_KEY_FREQ),
            freq_result=freq_result,
            ts_label=ts_label,
            warnings=warnings,
        )
        if validation_evidence:
            ts_block["validation_evidence"] = validation_evidence

        # TS-side AEC/BAC corrections target the TS entry directly via
        # ``target_transition_state_entry_id`` server-side — never via the
        # reaction entry. Anchor to ts_sp specifically; species-side SP
        # keys (r0_sp / p1_sp) belong to other species and a cross-owner
        # reference would 422.
        applied_corrections = _build_applied_energy_corrections(
            _correction_records_from_record(
                ts_record, legacy_1_0=_is_output_schema_1_0(output_doc),
                schema_1_3=_is_output_schema_1_3_or_later(output_doc)),
            source_calculation_key=calc_keys.get(_CALC_KEY_SP),
            warnings=warnings,
            warning_field="transition_state.applied_energy_corrections",
            target_kind="transition_state",
            target_label=str(ts_label) or None,
            arkane_release=_arkane_workflow_tool_release(output_doc),
            scheme_data_revisions=_scheme_data_revisions(output_doc),
            aec_yml_digest=_arc_aec_yml_digest(output_doc),
            bac_frequency_level=_bac_frequency_level(output_doc, ts_record),
        )
        if applied_corrections:
            ts_block["applied_energy_corrections"] = applied_corrections

        return ts_block, calc_keys

    # ------------------------------------------------------------------
    # Standalone transition-state path (POST /uploads/transition-states)
    # ------------------------------------------------------------------

    def submit_computed_ts_from_output(
        self,
        *,
        output_doc: Mapping[str, Any],
        ts_record: Mapping[str, Any],
        reaction_record: Mapping[str, Any],
    ) -> UploadOutcome | None:
        """Build, write, and (if configured) upload one standalone TS payload.

        Composes a ``TransitionStateUploadRequest`` for a single converged
        transition state and POSTs it to ``/uploads/transition-states``.
        The embedded reaction (reactants/products by identity, family,
        reversibility) is drawn from ``reaction_record`` — the TS's
        reaction entry in ``output_doc['reactions']`` — while the saddle
        geometry and opt/freq/sp/irc/path_search calculations come from
        ``ts_record`` (one entry of ``output_doc['transition_states']``).

        Returns ``None`` if the adapter is disabled, mirroring the other
        ``submit_*`` entry points. Build failures (missing xyz, missing
        opt level, a reactant/product absent from ``output_doc.species``)
        raise; the caller (``_run_ts_sweep``) wraps the per-TS call in a
        try/except so one bad TS doesn't take down the rest of the sweep.
        """
        if not self._config.enabled:
            return None

        ts_label = (
            ts_record.get("label") or ts_record.get("original_label") or "unlabeled-ts"
        )
        project_label = self._config.project_label or output_doc.get("project")

        build_warnings: list[dict[str, Any]] = WarningSink(_PRODUCER_TAG)
        output_doc = self._with_adaptive_levels(output_doc, build_warnings)
        output_doc = _withhold_unreliable_energies_in_doc(output_doc, build_warnings)
        ts_record = _withhold_unreliable_composite_energies(output_doc, ts_record, build_warnings)
        reaction_record = _withhold_reaction_kinetics(output_doc, reaction_record, build_warnings)
        payload = self._compose_transition_state_request(
            output_doc=output_doc,
            ts_record=ts_record,
            reaction_record=reaction_record,
            warnings=build_warnings,
        )
        _enforce_geometry_anchoring(payload, "transition_state", build_warnings)
        _withhold_unlabelled_point_geometries(payload, "transition_state", build_warnings)
        self._report_adaptive_omissions(output_doc, build_warnings)

        return self._write_and_upload(
            payload=payload,
            kind=TRANSITION_STATE_KIND,
            endpoint=TRANSITION_STATE_ENDPOINT,
            label=str(ts_label),
            project_label=project_label,
            species_label=str(ts_label),
            # A TS has no conformer concept at this endpoint — one saddle
            # per upload. The conformer slot carries the reaction label so
            # the same TS geometry reused across two reaction channels
            # keeps distinct keys; the payload hash tail still makes any
            # content change replay as a new upload.
            conformer_label=str(reaction_record.get("label") or "ts"),
            log_name="transition-state",
            subdir=PayloadWriter.TRANSITION_STATE_SUBDIR,
            warnings=build_warnings,
        )

    def _compose_transition_state_request(
        self,
        *,
        output_doc: Mapping[str, Any],
        ts_record: Mapping[str, Any],
        reaction_record: Mapping[str, Any],
        warnings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compose one ``TransitionStateUploadRequest`` dict.

        Producer-side omissions (an IRC result whose direction ARC does not
        state) are appended to ``warnings``.

        Reuses :meth:`_build_ts_block` — the same TS composer the
        computed-reaction bundle uses — for the saddle geometry and the
        opt/freq/sp/irc/path_search calculations (with their wrapped
        ``opt_result``/``freq_result``/… shape, which is exactly what the
        standalone endpoint's ``CalculationWithResultsPayload`` fragment
        expects). The bundle-local calc keys (``key``, ``depends_on``,
        ``geometry_key``, ``artifacts``) are stripped via
        :meth:`_ts_calc_to_standalone` because the standalone fragment
        forbids extra fields; the backend re-derives the
        path_search -> primary_opt dependency for this endpoint.

        The embedded ``reaction`` is built from ``reaction_record``:
        each reactant/product label is resolved through
        ``output_doc['species']`` into a ``SpeciesEntryIdentityPayload``
        (the same ``_species_entry_payload`` shape the species path uses).

        One piece of computed-reaction provenance is intentionally NOT carried
        here because the ``/uploads/transition-states`` schema has no slot for it:

        * **Inline artifacts** (ESS logs / input decks). The bundle path
          base64-inlines these under each calc when
          ``config.artifacts.upload`` is set; the standalone endpoint has
          no artifact field, so ``_build_ts_block`` is called with
          ``include_artifacts=False`` to skip the read+encode entirely
          (rather than build-then-drop), and a one-time WARNING tells the
          user their request can't be honored on this path.

        Since tckdb-schemas 0.64 the request also takes rotor-``scan``
        calculations (with ``scan_result``) and ``applied_energy_corrections``
        (AEC/BAC; with no source keys, since the payload has no key
        namespace), both carried here, and an ``atom_map`` (participants with
        their own ``key`` and ``geometry``, the saddle point named by
        ``geometry_key``). The map is built from ARC's ``ts_atom_map`` (output
        1.3 at ARC #1059 ebc88ec8) by the same function the bundle uses
        (:func:`_tckdb_reaction_atom_map`); without it the slot stays unset and the
        reason is reported (``reaction_ts_atom_map_not_sent``, or
        ``reaction_atom_map_ts_order_not_stated`` for a document that predates the key).
        """
        species_index = _index_species(output_doc)
        reaction_record = _with_stated_participants(
            reaction_record, ts_record=ts_record, species_index=species_index,
            warnings=warnings)

        ts_label = (
            ts_record.get("label") or ts_record.get("original_label") or "unlabeled-ts"
        )

        if self._config.artifacts.upload and not self._warned_ts_artifacts_unsupported:
            logger.warning(
                "TCKDB: artifact upload (config.artifacts.upload=true) is not "
                "supported for standalone transition-state uploads "
                "(/uploads/transition-states has no artifact slot); ESS logs / "
                "input decks will NOT be uploaded for TS records in this run. "
                "Use computed_species/conformer mode for artifact uploads."
            )
            self._warned_ts_artifacts_unsupported = True

        ts_warnings: list[dict[str, Any]] = WarningSink(_PRODUCER_TAG)
        ts_block, _ = self._build_ts_block(
            output_doc=output_doc,
            ts_record=ts_record,
            ts_label=str(ts_label),
            reaction_multiplicity=reaction_record.get("multiplicity"),
            unmapped_smiles=_ts_unmapped_smiles_handle(
                ts_record=ts_record,
                reaction_record=reaction_record,
                species_index=species_index,
            ),
            # The standalone TS endpoint has no artifact slot — skip the
            # (potentially multi-MB) read+base64 rather than build+strip.
            include_artifacts=False,
            warnings=ts_warnings,
            reaction_record=reaction_record,
            species_index=species_index,
        )
        # ARC's ts_atom_map (ebc88ec8) becomes TCKDB's atom_map here too, from the same data as on
        # the bundle: each participant carries its own ``key`` and ``geometry`` (the geometries the
        # map counts into) and the request names the saddle-point geometry by ``geometry_key``.
        ts_geometry = ts_block.get("geometry")
        slots = _ts_route_atom_map_slots(reaction_record, species_index)
        atom_map_block = None
        if isinstance(ts_geometry, Mapping):
            atom_map_block = _tckdb_reaction_atom_map(
                reaction_record, ts_label=str(ts_label), ts_xyz_text=ts_geometry.get("xyz_text"),
                ts_geometry_key=_TS_ROUTE_TS_GEOMETRY_KEY, participants=slots,
                irc_evidence=ts_block.get("validation_evidence") or (), warnings=ts_warnings)
        else:
            _warn_atom_map_not_sent(
                reaction_record, ts_label=str(ts_label), warnings=ts_warnings)
        if warnings is not None:
            warnings.extend(ts_warnings)

        # primary_opt is required and must be type=opt; _build_ts_block
        # always emits ts_block["calculation"] as the type=opt primary.
        primary_opt = self._ts_calc_to_standalone(ts_block["calculation"])
        additional_calculations = []
        # tckdb-schemas 0.64: "``additional_calculations`` now accepts ``scan``, and
        # ``CalculationWithResultsPayload`` gains ``scan_result``", so a rotor scan travels
        # with its points on this route too.
        for calc in ts_block.get("calculations", []):
            additional_calculations.append(self._ts_calc_to_standalone(calc))

        request: dict[str, Any] = {
            "reaction": self._build_ts_reaction_upload(
                reaction_record=reaction_record,
                species_index=species_index,
                schema_1_3=_is_output_schema_1_3_or_later(output_doc),
            ),
            "charge": ts_block["charge"],
            "multiplicity": ts_block["multiplicity"],
            "geometry": {k: v for k, v in ts_block["geometry"].items() if k != "key"},
            "primary_opt": primary_opt,
        }
        if atom_map_block is not None:
            members = {"reactant": request["reaction"]["reactants"], "product": request["reaction"]["products"]}
            for slot in slots:
                member = members[slot["side"]][slot["index"] - 1]
                member["key"] = slot["species_key"]
                member["geometry"] = dict(slot["geometry"])
            request["geometry_key"] = _TS_ROUTE_TS_GEOMETRY_KEY
            request["atom_map"] = atom_map_block
        if additional_calculations:
            request["additional_calculations"] = additional_calculations
        # 0.64: ``applied_energy_corrections`` takes no ``source_calculation_key`` or
        # ``source_conformer_key`` ("the payload has no key namespace") and no frequency
        # scale factor, which "is defined by the frequency calculation it was applied to".
        corrections = [
            {k: v for k, v in correction.items()
             if k not in ("source_calculation_key", "source_conformer_key")}
            for correction in ts_block.get("applied_energy_corrections") or []
            if correction.get("frequency_scale_factor") is None
        ]
        if corrections:
            request["applied_energy_corrections"] = corrections
        # The standalone route has no calculation-key namespace: evidence binds
        # to the upload's single irc additional calculation and must omit
        # ``source_calculation_key``.
        # ``energy_ordering`` is refused here outright (no calculations for the wells), and
        # ``_build_ts_block`` never builds it; ``imaginary_mode`` binds to the one freq calculation.
        if ts_block.get("validation_evidence"):
            request["validation_evidence"] = [
                {k: v for k, v in evidence.items() if k != "source_calculation_key"}
                for evidence in ts_block["validation_evidence"]
                if evidence["kind"] != "energy_ordering"
            ]
        unmapped_smiles = ts_block.get("unmapped_smiles")
        if unmapped_smiles:
            request["unmapped_smiles"] = str(unmapped_smiles)
        label = ts_block.get("label")
        if label:
            request["label"] = str(label)[:64]
        return request

    @staticmethod
    def _ts_calc_to_standalone(calc: Mapping[str, Any]) -> dict[str, Any]:
        """Strip bundle-only keys, yielding a plain CalculationWithResultsPayload.

        ``_build_ts_block`` produces calc dicts carrying the computed-
        reaction bundle's cross-reference fields (``key``, ``depends_on``)
        and, for freq, an inline ``artifacts`` list. The standalone TS
        endpoint validates each calc against ``CalculationWithResultsPayload``
        (``extra="forbid"``), which accepts none of those. Everything else
        — ``type``, ``software_release``, ``level_of_theory``, the wrapped
        result block, ``parameters_json`` (origin metadata), input/output
        geometries, ``hessian``, and ``constraints`` — is preserved.
        """
        return {
            key: value
            for key, value in calc.items()
            if key not in _TS_STANDALONE_STRIP_CALC_KEYS
        }

    def _build_ts_reaction_upload(
        self,
        *,
        reaction_record: Mapping[str, Any],
        species_index: Mapping[str, Mapping[str, Any]],
        schema_1_3: bool = False,
    ) -> dict[str, Any]:
        """Build the embedded ``TSReactionUpload`` for a standalone TS upload.

        Ordered reactant/product participants resolve each label through
        ``species_index`` into a ``SpeciesEntryIdentityPayload`` via
        :meth:`_species_entry_payload`. A missing label raises — the same
        loud failure the computed-reaction path uses — so a TS is never
        uploaded with an incomplete reaction description.

        ``reversible`` is required by the schema (no default). Output 1.3
        states it per reaction (``True`` for the ``<=>`` arrow, ``False`` for
        ``=>``, ``null`` when the arrow is unknown), and a stated bool is sent as
        stated. Otherwise (a document before 1.3, or a ``null``) this sends True
        (the same default the computed-reaction schema applies). Kept by
        maintainer decision (adapter 0.6.0): refusing would block every
        standalone TS upload whose reaction does not state it. TCKDB issue
        https://github.com/TCKDB/TCKDB/issues/583 asks the transition-state
        route to default it like the computed-reaction route does.
        """
        reactant_labels = list(reaction_record.get("reactant_labels") or [])
        product_labels = list(reaction_record.get("product_labels") or [])
        if not reactant_labels:
            raise ValueError(
                f"reaction label={reaction_record.get('label')!r} has no "
                "reactant_labels; cannot build the embedded TS reaction."
            )
        if not product_labels:
            raise ValueError(
                f"reaction label={reaction_record.get('label')!r} has no "
                "product_labels; cannot build the embedded TS reaction."
            )

        def _participants(labels: list[str], side: str) -> list[dict[str, Any]]:
            participants: list[dict[str, Any]] = []
            for label in labels:
                record = species_index.get(label)
                if record is None:
                    raise ValueError(
                        f"reaction {reaction_record.get('label')!r}: {side} "
                        f"label {label!r} not found in output_doc.species."
                    )
                participants.append(
                    {"species_entry": self._species_entry_payload(record)}
                )
            return participants

        stated_reversible = reaction_record.get("reversible") if schema_1_3 else None
        reaction: dict[str, Any] = {
            "reversible": stated_reversible if isinstance(stated_reversible, bool) else True,
            "reactants": _participants(reactant_labels, "reactant"),
            "products": _participants(product_labels, "product"),
        }
        family = reaction_record.get("family")
        # Mirror the backend's ``normalize_optional_text``: a
        # whitespace-only family would pass a plain truthy gate, ship with
        # the source note, then normalize to None server-side and 422
        # (source_note without a family). Gate on the stripped value so a
        # blank family is omitted entirely, like the backend treats it.
        if family is not None and str(family).strip():
            reaction["reaction_family"] = str(family).strip()
            # The server validates the family against its canonical list
            # and demands a source_note for non-canonical names. We don't
            # have that list at the producer, so always tag the source —
            # a no-op for canonical names, a safety net otherwise. Mirrors
            # the computed-reaction path.
            reaction["reaction_family_source_note"] = "ARC-reported family"
        return reaction

    # ------------------------------------------------------------------
    # Payload construction
    # ------------------------------------------------------------------

    @staticmethod
    def _species_entry_payload(record: Mapping[str, Any]) -> dict[str, Any]:
        smiles = record.get("smiles")
        if not smiles:
            raise ValueError(
                f"output.yml record for label={record.get('label')!r} has no smiles; "
                "TCKDB upload requires a SMILES on the species_entry."
            )
        smiles_text = str(smiles)
        is_ts = bool(record.get("is_ts"))
        if is_ts:
            # Every production call site passes a record sourced from
            # ``output_doc['species']`` (never ``output_doc
            # ['transition_states']``), and arc/output.py's
            # ``build_output_dict`` routes an entry into ``species`` or
            # ``transition_states`` by ``spc.is_ts`` at construction time
            # — so no path inside ARC's own pipeline is known to reach
            # here with ``is_ts=True``. This is the same situation as the
            # freq_n_imag contradictions in ``_freq_result_payload``: the
            # live threat is a hand-written/older/third-party output.yml
            # that carries ``is_ts=True`` on a ``species[]`` entry —
            # evidence.py's schema check only verifies ``schema_version``,
            # not the full output.yml JSON schema, so nothing upstream
            # would stop it reaching here. TCKDB 0.22.0's
            # ``StationaryPointKind`` has no ``transition_state`` member
            # at all (only ``minimum``/``vdw_complex``), and neither is a
            # defensible stand-in for a genuine TS — a transition state
            # belongs in ``output_doc['transition_states']`` and its own
            # upload path, not folded into a species_entry. Applying the
            # same standard as ``_freq_result_payload``'s raises: refuse
            # the record rather than emit an invalid enum value or
            # silently reclassify it as a minimum.
            raise ValueError(
                f"output.yml record for label={record.get('label')!r} has "
                f"is_ts=True in a species-entry context (species/reactant/"
                f"product), but TCKDB 0.22.0's StationaryPointKind has no "
                f"'transition_state' member. Refusing to deposit this "
                f"record rather than emit an invalid enum value or "
                f"silently reclassify it as a minimum."
            )
        # ``electronic_state_kind`` is not sent (adapter 0.6.0, maintainer
        # decision). ARC does not state it: it computes the multiplicity it
        # was given, which need not be the ground state (singlet O2 or
        # CH2). The field is optional, so TCKDB applies its own default
        # rather than the adapter asserting one. The other identity-
        # disambiguation fields (stereo_label, electronic_state_label,
        # term_symbol[_raw], isotopologue_label) are likewise left null:
        # ARC has no reliable source for them, the TCKDB backend derives stereo_label
        # from the uploaded 3D geometry when applicable, and the dedupe
        # uniqueness key uses ``nulls_not_distinct`` so paired nulls collapse
        # to the same row rather than fragmenting it.
        entry: dict[str, Any] = {
            "molecule_kind": "molecule",
            "smiles": smiles_text,
            "charge": _stated_integer(record, "charge", f"species {record.get('label')!r}"),
            "multiplicity": _stated_integer(
                record, "multiplicity", f"species {record.get('label')!r}", minimum=1),
            # is_ts is always False here now — the raise above refuses
            # any is_ts=True record before this point.
            "species_entry_kind": "minimum",
        }
        # ``unmapped_smiles`` is not sent for a species: ARC's output.yml
        # writes no such key, and the adapter never derives one by string
        # manipulation of the SMILES (atom-map stripping is not attempted).
        return entry

    def _build_payload(
        self,
        *,
        output_doc: Mapping[str, Any],
        species_record: Mapping[str, Any],
        warnings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compose one ConformerUploadRequest dict.

        Besides the opt (primary) and freq/sp (additional) calculations, it
        carries the statmech block and the applied energy corrections, built by
        the same builders as the computed-species route (Hessian-gated
        treatments, BAC omission, ``atom_params``, the Arkane release, the
        declared energy level). The conformer route accepts only freq and sp as
        additional calculations, so no rotor scan is sent and each torsion drops
        its scan link (``torsion_scan_not_built``); ARC's rejected rotors go to
        ``statmech.torsions[].invalidated_reason``. There is no thermo slot.
        Producer-side omissions are appended to ``warnings``.
        """
        _warn_primary_opt_placeholder(species_record, warnings, field="calculation")
        species_entry = self._species_entry_payload(species_record)
        geometry_payload = _geometry_payload(
            species_record, _require_xyz_text(species_record), species_record.get("xyz_isotopes"))
        primary, additional = self._build_calculations(output_doc, species_record)

        # Local keys, so the statmech links and the corrections' source name
        # the calculations of this request (as computed-species' role keys do).
        primary["key"] = _CALC_KEY_SP if primary["type"] == "sp" else _CALC_KEY_OPT
        for calc in additional:
            calc["key"] = calc["type"]
        calc_keys_by_role = {
            calc["key"]: calc["key"] for calc in (primary, *additional)}

        payload: dict[str, Any] = {
            "species_entry": species_entry,
            "geometry": geometry_payload,
            "calculation": primary,
            "scientific_origin": "computed",
        }
        if additional:
            payload["additional_calculations"] = additional

        omitted_bacs: list[str] = []
        applied_corrections = _build_applied_energy_corrections(
            _correction_records_from_record(
                species_record, schema_1_3=_is_output_schema_1_3_or_later(output_doc)),
            source_calculation_key=(
                _CALC_KEY_SP if _CALC_KEY_SP in calc_keys_by_role else None
            ),
            warnings=warnings,
            target_kind="species",
            element_symbols=_species_element_symbols(species_record),
            target_label=str(species_record.get("label") or "") or None,
            arkane_release=_arkane_workflow_tool_release(output_doc),
            scheme_data_revisions=_scheme_data_revisions(output_doc),
            aec_yml_digest=_arc_aec_yml_digest(output_doc),
            bac_frequency_level=_bac_frequency_level(output_doc, species_record),
            omitted_bac_reasons=omitted_bacs,
        )
        if applied_corrections:
            payload["applied_energy_corrections"] = applied_corrections

        arc_wt = _arc_workflow_tool_release(output_doc)
        # The route accepts no scan calculations: every scan ARC exported is
        # one this request cannot carry.
        unbuilt_scans = {
            entry["key"]: "the conformer upload accepts only freq and sp as "
                          "additional calculations"
            for entry in _scan_entries_from_record(species_record)
            if entry.get("type") == _CALC_KEY_SCAN
            and isinstance(entry.get("key"), str) and entry["key"]
            and isinstance(entry.get("scan_result"), Mapping)
        }
        statmech_block = _build_statmech_block_for_species(
            output_doc=output_doc,
            species_record=species_record,
            calc_keys_by_role=_with_composite_role(species_record, calc_keys_by_role),
            workflow_tool_release=arc_wt,
            target_model="ConformerUploadStatmechPayload",
            unbuilt_scans=unbuilt_scans,
            freq_hessian_available=self._freq_hessian_available(
                output_doc=output_doc, species_record=species_record,
            ),
            warnings=warnings,
            warning_field="statmech",
            energy_declaration=_energy_level_declaration(
                _thermo_energy_level(output_doc, species_record),
                calc_keys_by_role=calc_keys_by_role,
                calculations=_calculations_by_key(primary, additional),
            ),
        )
        if statmech_block is not None:
            arkane_release = _arc_analysis_software_release(output_doc)
            if arkane_release is not None:
                statmech_block["software_release"] = dict(arkane_release)
            # The computed-species route names ARC once, at bundle level, and
            # its statmech inherits it; this request has no such slot.
            if arc_wt is not None:
                statmech_block["workflow_tool_release"] = dict(arc_wt)
            payload["statmech"] = statmech_block

        label = species_record.get("label")
        if label:
            payload["label"] = str(label)[:64]
        return payload

    @classmethod
    def _build_calculations(
        cls,
        output_doc: Mapping[str, Any],
        record: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Return (primary opt calculation, [freq, sp] additional calculations).

        Additional calculations are skipped (with a warning) when their
        result fields are absent or malformed, or when no level of theory
        is available. Skipping an optional calc never fails the upload.

        A single atom (its own XYZ has one atom) returns its ``sp`` as the primary
        calculation and no additional calculations (tckdb-schemas 0.59,
        TCKDB#610; ``ConformerUploadRequest`` gives a one-atom ``sp`` primary
        the conformer geometry link), see ``_build_monatomic_primary_sp``.
        """
        if _is_single_atom_geometry(_require_xyz_text(record)):
            sp_result = _monatomic_sp_result_payload(record)
            if sp_result is None:
                raise _atom_without_sp_energy_error(record)
            return cls._calculation_payload(
                output_doc,
                record,
                calc_type="sp",
                level=_resolve_level(output_doc, "sp", record),
                ess_job_key="sp",
                result_field="sp_result",
                result_payload=sp_result,
                tckdb_origin=None,
                final_settings=_final_settings_for_calc(species_record=record, calc_role=_CALC_KEY_SP),
            ), []
        primary = cls._calculation_payload(
            output_doc,
            record,
            calc_type="opt",
            level=_resolve_level(output_doc, "opt", record),
            ess_job_key="opt",
            result_field="opt_result",
            result_payload=_opt_result_payload(record),
            scf_stability_target=True,
        )

        additional: list[dict[str, Any]] = []
        freq_result = _freq_result_payload(record, schema_1_3=_is_output_schema_1_3_or_later(output_doc))
        if freq_result is not None:
            freq_level = _resolve_level(output_doc, "freq", record)
            try:
                additional.append(
                    cls._calculation_payload(
                        output_doc,
                        record,
                        calc_type="freq",
                        level=freq_level,
                        ess_job_key="freq",
                        result_field="freq_result",
                        result_payload=freq_result,
                    )
                )
            except ValueError as exc:
                logger.warning(
                    "TCKDB freq additional calculation skipped for label=%s: %s",
                    record.get("label"), exc,
                )

        sp_result = _sp_result_payload(record)
        if sp_result is not None:
            sp_level = _resolve_level(output_doc, "sp", record)
            sp_origin = _sp_reuse_origin(output_doc, record)
            try:
                additional.append(
                    cls._calculation_payload(
                        output_doc,
                        record,
                        calc_type="sp",
                        level=sp_level,
                        ess_job_key="sp",
                        result_field="sp_result",
                        result_payload=sp_result,
                        tckdb_origin=sp_origin,
                    )
                )
            except ValueError as exc:
                logger.warning(
                    "TCKDB sp additional calculation skipped for label=%s: %s",
                    record.get("label"), exc,
                )

        return primary, additional

    @staticmethod
    def _calculation_payload(
        output_doc: Mapping[str, Any],
        record: Mapping[str, Any],
        *,
        calc_type: str,
        level: Mapping[str, Any] | None,
        ess_job_key: str,
        result_field: str | None = None,
        result_payload: Mapping[str, Any] | None = None,
        tckdb_origin: Mapping[str, Any] | None = None,
        final_settings: Mapping[str, Any] | None = None,
        scf_stability_target: bool = False,
        observed_software: str | None = None,
        observed_version: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(level, Mapping):
            adaptive = (
                f" ARC ran with adaptive_levels naming {calc_type}, and the level "
                f"this species ran at cannot be attributed "
                f"({_W_ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE.get(calc_type, calc_type + '_level_adaptive_not_attributable')})."
                if _adaptive_kind_named(output_doc, calc_type) else ""
            )
            if arc13.has_levels(record) and not adaptive:
                adaptive = (
                    f" ARC output.yml 1.3 states no level for the {calc_type} job of this "
                    "record (its levels entry is null and no log of that job is exported, "
                    "or the level was not recorded); a header level is not borrowed for it.")
            raise ValueError(
                f"no level of theory available for {calc_type} calculation;"
                f"{adaptive} cannot build TCKDB calculation payload."
            )
        method = level.get("method")
        if not method:
            raise ValueError(
                f"level of theory for {calc_type} is missing method; "
                "cannot build TCKDB calculation payload."
            )
        if "//" in str(method):
            raise ValueError(
                f"{_W_LEVEL_METHOD_IS_COMPOUND}: the {calc_type} level method {str(method)!r} "
                "contains '//' (two levels of theory, which TCKDB refuses in a method); which "
                "half belongs to this calculation is not stated, so the calculation is not built."
            )
        if not arc13.has_levels(record):
            # Before output.yml 1.3 the scan's own program was not relied on.
            observed_software = observed_version = None
        contradiction = _route_contradiction(record, calc_type, ess_job_key, level)
        if contradiction is not None:
            _note_route_contradiction(output_doc, record, calc_type, contradiction)
            raise ValueError(
                f"level_contradicted_by_route: the {calc_type} calculation's recorded level "
                f"contradicts the keyword line the job ran with ({contradiction}); observed "
                "outranks requested, so the calculation is not built.")
        placeholder = (
            arc13.primary_opt_placeholder(record)
            if calc_type == "opt" and ess_job_key == "opt" and tckdb_origin is None else None)
        if placeholder is not None:
            tckdb_origin = _placeholder_origin(placeholder)
        if calc_type == _CALC_KEY_SCAN and arc13.has_levels(record) and not observed_software:
            # Output.yml 1.3 states each rotor scan's own program (identified from
            # its log); the header scan_level's software is only a deduction.
            raise ValueError(
                "ARC states no program for this rotor scan (rotor_scans[].ess_software "
                "is null: its log is missing or was not identified); the scan_level's "
                "software is only a deduction, so the scan calculation is not built."
            )
        # ARC's per-job banner identification names the program that actually
        # ran; the requested level may name a different troubleshooting ESS.
        # On a schema-1.3 record the program of a calculation whose level came from
        # another job (a composite run's geometry and energy, a monoatomic's sp log)
        # is that job's (``arc13.software_job_key``). ``observed_software`` /
        # ``observed_version`` are the provenance a caller read from the job's own
        # record (a rotor scan states its own program and banner).
        ess_software = record.get("ess_software")
        software_key = arc13.software_job_key(record, ess_job_key)
        if observed_software is None and isinstance(ess_software, Mapping):
            observed_software = ess_software.get(software_key)
        software_name = observed_software or level.get("software")
        if not software_name:
            raise ValueError(
                f"level of theory for {calc_type} is missing software; "
                "cannot identify the ESS for TCKDB."
            )

        level_of_theory = _arc_level_to_tckdb_lot(level)
        if level_of_theory is not None and level_of_theory["method"] != str(level["method"]):
            message = (
                f"ARC's {calc_type} level method {str(level['method'])!r} names a correction "
                f"table, not a method; the {calc_type} calculation is sent with method "
                f"{level_of_theory['method']!r} (the calculation that ran)."
            )
            logger.warning("TCKDB %s: %s: %s", f"calculation.{calc_type}",
                           _W_CORRECTION_TABLE_METHOD_SPLIT, message)
        if level_of_theory is None:
            # method was already validated above; this is defensive against
            # a future change to _arc_level_to_tckdb_lot that drops the row.
            raise ValueError(
                f"could not project level of theory for {calc_type} onto "
                "TCKDB LevelOfTheoryRef shape."
            )

        # These are the references recorded from the actual freq/SP inputs.
        # ARC's stability verdict describes the opt job's wavefunction, so it
        # goes on the primary opt only (``scf_stability_target``), never on
        # freq/sp (their own SCF may land on another solution), scans or IRC.
        scf_reference = record.get("scf_reference")
        scf_stability: dict[str, Any] | None = None
        if calc_type in {_CALC_KEY_FREQ, _CALC_KEY_SP} and isinstance(scf_reference, Mapping):
            reference = scf_reference.get(f"{calc_type}_reference")
            if reference in {"restricted", "unrestricted", "restricted_open"}:
                level_of_theory["spin_treatment"] = reference
        if scf_stability_target:
            scf_stability = _scf_stability_payload(record)

        software_release: dict[str, Any] = {"name": str(software_name)}
        ess_versions = record.get("ess_versions")
        if observed_version:
            software_release.update(
                _split_ess_version_banner(software_release["name"], str(observed_version)))
        elif isinstance(ess_versions, Mapping):
            # ess_versions is keyed by job type ('opt', 'freq', 'sp', 'neb'),
            # not by software name. Fall back to opt's version if the
            # job-specific entry is missing (often the case for combined
            # opt+freq runs or shared sp/freq logs).
            ess_version = ess_versions.get(software_key)
            if not ess_version and ess_job_key not in (_CALC_KEY_SCAN, "conf_opt", "irc"):
                # Never borrow optimization provenance for a scan or a
                # conformer screen, or pair
                # another program's banner with this program.
                opt_software = (
                    ess_software.get("opt") if isinstance(ess_software, Mapping)
                    else (_resolve_level(output_doc, "opt", record) or {}).get("software")
                )
                if (opt_software and str(opt_software).lower() == str(software_name).lower()):
                    ess_version = ess_versions.get("opt")
            if ess_version:
                software_release.update(
                    _split_ess_version_banner(software_release["name"], str(ess_version)))

        calc: dict[str, Any] = {
            "type": calc_type,
            "quality": "raw",
            "software_release": software_release,
            "level_of_theory": level_of_theory,
        }

        arc_version = output_doc.get("arc_version")
        arc_git_commit = output_doc.get("arc_git_commit")
        if arc_version or arc_git_commit:
            wt: dict[str, Any] = {"name": "ARC"}
            if arc_version:
                wt["version"] = str(arc_version)
            if arc_git_commit:
                wt["git_commit"] = str(arc_git_commit)
            calc["workflow_tool_release"] = wt

        if result_field and result_payload:
            calc[result_field] = dict(result_payload)

        parameters: list[dict[str, Any]] = []
        hessian_method = record.get("freq_hessian_method")
        if calc_type == _CALC_KEY_FREQ and hessian_method in {
            "analytic", "finite_difference_gradient", "finite_difference_energy",
        }:
            parameters.append({
                "raw_key": "freq_hessian_method",
                "raw_value": hessian_method,
                "canonical_key": "freq.hessian_method",
                "canonical_value": hessian_method,
                "section": "freq",
                "value_type": "string",
            })
        # The observed ESS keyword line (schema 1.3 ``*_route``): the one execution
        # control ARC states whole. TCKDB's ``CalculationParameterObservation`` is its
        # home (``raw_key`` is software-specific, ``section`` the job); it is sent as
        # stated, never split into options or rebuilt from the level.
        parameters.extend(_route_parameters(record, calc_type, ess_job_key))
        if parameters:
            calc["parameters"] = parameters

        # Optional S**2 spin-contamination diagnostic for the sp calc. Every
        # sp-calc construction site funnels through here, so attaching it once
        # covers computed-species / computed-reaction / TS / species-entry
        # paths uniformly. Emitted only for open-shell/unrestricted single
        # points where ARC parsed ``<S**2>`` (``sp_spin_diagnostic`` on the
        # record); restricted / closed-shell records yield None and the block
        # is omitted entirely (never an all-null / fabricated block).
        if calc_type == _CALC_KEY_SP:
            spin_diagnostic = _spin_diagnostic_payload(record)
            if spin_diagnostic is not None:
                calc["spin_diagnostic"] = spin_diagnostic
            # Output 1.3's ``sp_t1_diagnostic`` is the T1 diagnostic ARC parsed
            # from the sp log (stated only for a coupled-cluster or QCISD sp
            # level); TCKDB's home for it is the sp calculation's
            # ``wavefunction_diagnostic.t1_diagnostic``.
            if _is_output_schema_1_3_or_later(output_doc):
                wavefunction_diagnostic = _wavefunction_diagnostic_payload(record)
                if wavefunction_diagnostic is not None:
                    calc["wavefunction_diagnostic"] = wavefunction_diagnostic

        if scf_stability is not None:
            calc["scf_stability"] = scf_stability

        # ``parameters_json`` is the single per-calc slot for free-form
        # qualifier metadata. Two writers feed it: ``tckdb_origin``
        # (provenance qualifier — reused-result / screened-conformer)
        # and ``final_settings`` (the calc's effective scientific
        # settings, e.g. ``{"fine": True}``, distinct from the level of
        # theory's identity fields). The merge helper drops the field entirely when
        # both sources are empty.
        parameters_json = _merge_parameters_json(
            tckdb_origin=tckdb_origin,
            final_settings=final_settings,
        )
        if parameters_json is not None:
            calc["parameters_json"] = parameters_json

        return calc

    # ------------------------------------------------------------------
    # Upload + sidecar finalization
    # ------------------------------------------------------------------


    def _log_readiness_recovery(self) -> None:
        """Tell the user the payloads are on disk and how to re-run manually.

        A readiness failure means the science is done and the payload was
        written — only the network POST didn't happen. The upload is fully
        replayable via the standalone CLI once the server is back, so emit
        the exact command on its own line for copy-paste. The input.yml
        path isn't in scope at this layer, so we give the invariant form
        (``<project_dir>/input.yml``, the CLI's default location) with the
        real project directory and upload mode we do have.
        """
        payload_dir = self._writer.root
        project_dir = self._project_directory
        mode = self._config.upload_mode
        if project_dir is not None:
            input_ref = f"{project_dir}/input.yml"
            recovery_cmd = (
                f"tckdb-arc-upload {input_ref} "
                f"-p {project_dir} --upload-mode {mode}"
            )
        else:
            # No project directory in scope — give the invariant shape with
            # a clear placeholder rather than a wrong absolute path.
            recovery_cmd = (
                f"tckdb-arc-upload <input.yml> --upload-mode {mode}"
            )
        logger.warning(
            "TCKDB server was not ready after %d attempts; payloads were "
            "written to %s. Re-run the upload once it is up:\n%s",
            PREFLIGHT_MAX_ATTEMPTS, payload_dir, recovery_cmd,
        )


_TS_ROUTE_TS_GEOMETRY_KEY = "ts_geom"


__all__ = [
    "ARTIFACTS_ENDPOINT_TEMPLATE",
    "ArtifactUploadOutcome",
    "COMPUTED_REACTION_ENDPOINT",
    "COMPUTED_REACTION_KIND",
    "COMPUTED_SPECIES_ENDPOINT",
    "COMPUTED_SPECIES_KIND",
    "CONFORMER_UPLOAD_ENDPOINT",
    "PAYLOAD_KIND",
    "TCKDBAdapter",
    "TRANSITION_STATE_ENDPOINT",
    "TRANSITION_STATE_KIND",
    "UploadOutcome",
    "arc_to_tckdb_a_units",
    "arc_to_tckdb_ea_units",
]
