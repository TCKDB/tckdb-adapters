"""Calculation helpers of the ARC adapter.

The calculation-key and role vocabulary, the small result-payload builders (optimisation, spin,
wavefunction and SCF-stability diagnostics, the Hessian parser block), and the readers of a
calculation's provenance: the route observed in its log, the final settings, what an SP or a
conformer was reused from.
"""

import math
import re
from collections.abc import Mapping
from typing import Any

from tckdb_arc import arc13
from tckdb_arc.levels import _restart_levels
from tckdb_core import rules as _rules


# Local calculation-key namespace within a computed-species bundle.
# These keys are referenced from `depends_on.parent_calculation_key` and
# `thermo.source_calculations[].calculation_key`. They have no relation
# to TCKDB-assigned calculation_ids — the bundle endpoint mints those
# server-side and returns them in the response.
_CALC_KEY_OPT = "opt"
_CALC_KEY_OPT_COARSE = "opt_coarse"
_CALC_KEY_FREQ = "freq"
_CALC_KEY_SP = "sp"

# ``ThermoCalculationRole`` / ``StatmechCalculationRole`` ``composite``: the
# calculation is a composite-method job (CBS-QB3, G4, ...). TCKDB has no
# composite *calculation type*; the role "describes a scientific origin rather
# than a specific job type" and accepts any type, so a composite run's job (filed
# as the record's ``opt``, see ``_with_composite_role``) is linked under it.
_ROLE_COMPOSITE = "composite"


def _hessian_parser_block(stamp: str) -> dict[str, Any]:
    """The Hessian's ``parser`` block (tckdb-schemas 0.130 ``ParserStamp``) from ARC's stamp.

    ARC spells the parser as one token, ``arc-hessian-1`` (``HESSIAN_PARSER_VERSION``);
    TCKDB wants the name beside the version and refuses a name that embeds one. The split
    is ARC's own ``<name>-<version>`` shape: the trailing ``-<digits>`` is the version
    (kept as the text ARC wrote) and what precedes it the name. A stamp that does not
    have that shape is not guessed at: the block is left out (``parser`` is optional)
    rather than invent a name.
    """
    name, sep, version = str(stamp).rpartition("-")
    if not sep or not name or not version.isdigit():
        return {}
    return {"parser": {"name": name, "version": version}}


# TS-side calc role; reaction bundles can also carry an irc on the TS,
# but ARC may not always have one parsed cleanly, so it's optional.
_CALC_KEY_IRC = "irc"

# TS guess parent calculation (only emitted when the chosen guess was
# itself a real path-search calculation — orca_neb or xtb_gsm).
# Geometry-only guesses (heuristics, AutoTST/KinBot wrappers, GCN,
# user-supplied XYZ) stay as ``calculation_input_geometry`` and never
# produce a calc node here, per the calculation_dependency contract:
# parent must be a real calc.
_CALC_KEY_TS_GUESS = "ts_guess"

# Bundle-local calc role for rotor scans. Per-rotor keys are minted as
# ``f"{_CALC_KEY_SCAN}_rotor_{i}"`` by ``arc/output.py``; the adapter
# accepts whatever key the species record provides without re-minting,
# so the source-of-truth for naming stays on the producer side.
_CALC_KEY_SCAN = "scan"

_CONTRADICTIONS_KEY = "_tckdb_route_contradictions"


def _note_route_contradiction(
    output_doc: Mapping[str, Any], record: Mapping[str, Any], calc_type: str, detail: str,
) -> None:
    noted = output_doc.get(_CONTRADICTIONS_KEY)
    if isinstance(noted, list):
        entry = (str(record.get("label")), calc_type, detail)
        if entry not in noted:
            noted.append(entry)


def _placeholder_origin(kind: str) -> dict[str, Any]:
    """``tckdb_origin`` marking a primary ``opt`` that stands in for a job ARC did not run as an opt."""
    reason = (
        "placeholder: the geometry came from a composite-method job whose internal "
        "optimisation level ARC does not export; filed at the composite level ARC states"
        if kind == "composite" else
        "placeholder: ARC exports no optimisation job for this record; TCKDB requires a "
        "primary opt, filed at the run's header opt level"
    )
    return {
        "origin_kind": "derived",
        "origin_detail": f"placeholder_primary_opt_{kind}",
        "reason": reason,
        "independent_ess_job": False,
        "producer": "ARC",
    }


def _route_parameters(
    record: Mapping[str, Any], calc_type: str, ess_job_key: str,
) -> list[dict[str, Any]]:
    """``parameters`` observations for the ESS keyword line(s) of a 1.3 record's job."""
    if calc_type == _CALC_KEY_IRC:
        routes = record.get("irc_log_routes")
        if not arc13.has_levels(record) or not isinstance(routes, list):
            return []
        stated = [r.strip() for r in routes if isinstance(r, str) and r.strip()]
        if not stated:
            return []
        if len(set(stated)) == 1 and len(stated) == len(routes):
            return [_route_observation(stated[0], "irc")]
        return [
            _route_observation(r.strip(), "irc", index=i)
            for i, r in enumerate(routes) if isinstance(r, str) and r.strip()
        ]
    if calc_type != ess_job_key or calc_type not in arc13.ROUTE_FIELDS:
        return []
    route = arc13.route_for_job(record, calc_type)
    return [_route_observation(route, calc_type)] if route else []


def _route_observation(route: str, section: str, *, index: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "raw_key": "route", "raw_value": route, "section": section, "value_type": "string",
    }
    if index is not None:
        out["parameter_index"] = index
    return out


def _sp_reuse_origin(
    output_doc: Mapping[str, Any], record: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The ``tckdb_origin`` of a record's sp calculation when its energy is reused, else ``None``.

    A schema-1.3 record states it: with no ``sp_log`` the energy is read from the
    optimization log, or on a composite run from the composite log
    (``arc13.sp_energy_source``). Older output has only the header levels
    (``_sp_is_reused_from_opt``).
    """
    if arc13.has_levels(record):
        source = arc13.sp_energy_source(record)
        return _reused_origin(source) if source else None
    return _reused_origin("opt") if _sp_is_reused_from_opt(output_doc) else None


def _sp_is_reused_from_opt(output_doc: Mapping[str, Any]) -> bool:
    """Whether ARC's SP energy is reused from the opt calculation.

    The signal is structural equality between ``sp_level`` and
    ``opt_level``. Two flavors both count:

    - ``sp_level`` absent / null in output.yml — the common ARC case
      where the user only declares ``opt_level=`` and ARC reuses the
      opt energy as the SP energy.
    - ``sp_level`` explicitly set to a dict structurally equal to
      ``opt_level`` — same outcome, no separate ESS job.

    A ``sp_level`` that differs from ``opt_level`` (different method,
    basis, software, etc.) means a real SP job was executed, so we
    return False and the calculation gets no reused-result marker.
    """
    opt_level = output_doc.get("opt_level")
    if not isinstance(opt_level, Mapping):
        return False
    sp_level = output_doc.get("sp_level")
    if sp_level is None:
        return True
    if not isinstance(sp_level, Mapping):
        return False
    return dict(sp_level) == dict(opt_level)


def _reused_origin(reused_from_calc_type: str) -> dict[str, Any]:
    """Build the ``tckdb_origin`` payload for a reused-result calculation.

    Lives under ``parameters_json.tckdb_origin`` on the calculation row.
    The DAG edge between calculations carries the relational link
    (e.g. ``opt -> sp`` with role ``single_point_on``); this dict
    carries the qualifier — *this* row's energy is reused, not freshly
    computed — so downstream consumers can tell aggregate-from-opt SP
    rows apart from independently executed SP jobs.
    """
    # TCKDB has no composite calculation type: a composite job is filed as the
    # record's ``opt`` calculation (it produced the geometry), so the energy is
    # reused from that ``opt`` row and ``source_job`` names the ARC job.
    reused_from = (
        {"calculation_type": "opt", "source_job": "composite"}
        if reused_from_calc_type == "composite"
        else {"calculation_type": reused_from_calc_type}
    )
    return {
        "origin_kind": "reused_result",
        "reused_from": reused_from,
        "reason": (
            f"sp_level equals {reused_from_calc_type}_level; "
            f"{reused_from_calc_type} electronic energy reused as SP energy"
            if reused_from_calc_type == "opt" else
            f"the {reused_from_calc_type} job's electronic energy is the SP energy "
            "(ARC read it from that job's log)"
        ),
        "independent_ess_job": False,
        "producer": "ARC",
    }


# Per-calc-role mapping to the ``<role>_final_settings`` field name on the
# species record. Roles not in the map have no current producer-side
# source of final-settings data (ARC's ``output.yml`` writes only the opt,
# coarse-opt, freq and sp fields; there is no ``irc_final_settings``); the
# helper returns ``None`` for them and the adapter omits
# ``parameters_json.final_settings`` from the calc.
_FINAL_SETTINGS_FIELD_BY_CALC_ROLE: Mapping[str, str] = {
    "opt": "opt_final_settings",
    "opt_coarse": "coarse_opt_final_settings",
    "freq": "freq_final_settings",
    "sp": "sp_final_settings",
}


def _final_settings_for_calc(
    *,
    species_record: Mapping[str, Any],
    calc_role: str,
) -> dict[str, Any] | None:
    """Return the calc's ``final_settings`` dict from the species record.

    ``final_settings`` carries the final effective scientific/numerical
    knobs that defined this calculation — distinct from the
    level of theory (whose identity is its method, basis and typed
    qualifiers; ESS keywords are calculation parameters, not identity)
    and from operational/scheduler metadata (server, queue, runtime,
    job ids — explicitly out of scope).

    Today the producer (``arc/output.py``) populates these honestly
    from observable run state (e.g. ``opt_final_settings.fine = True``
    when a coarse stage ran, since that proves the fine opt ran with
    ``fine=True``). Calls return ``None`` when:
    * the calc role has no producer-side source today,
    * the field is missing from the species record (older output.yml),
    * the field is present but empty / not a mapping.
    """
    field = _FINAL_SETTINGS_FIELD_BY_CALC_ROLE.get(calc_role)
    if field is None:
        return None
    raw = species_record.get(field)
    if not isinstance(raw, Mapping) or not raw:
        return None
    return {k: v for k, v in raw.items() if v is not None}


def _merge_parameters_json(
    *,
    existing: Mapping[str, Any] | None = None,
    tckdb_origin: Mapping[str, Any] | None = None,
    final_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Merge optional calculation metadata into ``parameters_json``.

    ``parameters_json`` is the single calc-level slot TCKDB exposes for
    free-form qualifier metadata (per-row, opaque to the schema). Today
    two writers populate it:

    * ``tckdb_origin`` — origin/qualifier markers (see :func:`_reused_origin`,
      :func:`_screened_conformer_origin`). Identifies non-standard rows
      so consumers don't mistake them for independently executed jobs.
    * ``final_settings`` — the final effective scientific/numerical
      settings of the calculation (e.g. ``{"fine": True}``). Distinct
      from the level of theory, which since tckdb-schemas 0.158 has no
      free-text keywords: ESS options are calculation parameters.

    ``existing`` lets callers fold both writers on top of a caller-
    provided base. Keys collide explicitly: the merge intentionally
    does NOT silently overwrite a pre-existing ``tckdb_origin`` /
    ``final_settings`` — that would mask bugs. Callers should pass
    only one source for each key.

    Returns ``None`` when the merged dict is empty, so the caller can
    omit the field entirely rather than emit an empty container.
    """
    merged: dict[str, Any] = dict(existing) if existing else {}
    if tckdb_origin:
        merged["tckdb_origin"] = dict(tckdb_origin)
    if final_settings:
        merged["final_settings"] = dict(final_settings)
    return merged or None


def _screened_conformer_origin(job: str = "opt") -> dict[str, Any]:
    """Build the ``tckdb_origin`` payload for an alt-conformer's opt row.

    TCKDB's ``ConformerInBundle`` requires every conformer to carry a
    ``primary_calculation`` of type ``opt``. For screened conformers
    that ARC didn't select, no parsed ESS opt log lands on the species
    record — we only have a converged geometry plus a relative kJ/mol
    energy from the conformer screen. This marker makes that explicit
    on the wire so downstream consumers can tell screened-conformer
    anchor rows apart from independently executed opt jobs.

    ``origin_kind`` maps to ``derived``: the backend hoists this value
    into the validated ``CalculationWithResultsPayload.origin_kind`` enum
    (``{executed, reused_result, imported, derived}``), and a screened
    conformer's geometry is *derived* from ARC's conformer screen rather
    than produced by an independently executed ESS opt job. The
    ARC-specific ``screened_conformer`` distinction is preserved verbatim
    under ``origin_detail`` (opaque to the backend enum) so downstream
    consumers can still single these rows out. Emitting
    ``origin_kind="screened_conformer"`` here previously caused a 422 —
    it is not an enum member.
    """
    return {
        "origin_kind": "derived",
        "origin_detail": "screened_conformer",
        "reason": (
            "alt conformer geometry anchored to the bundle; ARC did "
            "not parse an independent opt job for this conformer"
            if job == "opt" else
            "alt conformer single-point energy from ARC's conformer screen; "
            "ARC did not parse an independent sp job for this conformer"
        ),
        "independent_ess_job": False,
        "producer": "ARC",
    }


def _stated_integer(
    record: Mapping[str, Any],
    field: str,
    subject: str,
    *,
    minimum: int | None = None,
) -> int:
    """Return ARC's own integer ``record[field]``, or refuse the upload.

    Charge and multiplicity are required identity fields with no TCKDB
    default, and ARC writes both for every species and TS
    (arc/output.py ``_spc_to_dict``). A record without a usable value is
    malformed; before adapter 0.6.0 it silently became charge 0 or a
    singlet, which is a different species identity (a radical deposited as
    a singlet). Raise ``ValueError`` instead: the per-record build fails
    and the sweep reports it, like every other unbuildable record.
    """
    value = record.get(field)
    usable = (
        not isinstance(value, bool)
        and (isinstance(value, int) or (isinstance(value, float) and value.is_integer()))
        and (minimum is None or value >= minimum)
    )
    if not usable:
        raise ValueError(
            f"{subject}: ARC's record states no usable {field} ({value!r}). "
            f"TCKDB requires {field} and the adapter does not default it; "
            f"refusing to build this upload."
        )
    return int(value)


def _opt_result_payload(record: Mapping[str, Any]) -> dict[str, Any] | None:
    out: dict[str, Any] = {}
    # opt_converged is emitted by arc/output.py::_spc_to_dict (line 541) and
    # accepted by TCKDB's OptResultPayload as ``converged: bool | None``.
    # Without this mapping the calc_opt_result.converged column lands NULL
    # even for known-converged species. Coerce to bool defensively — ARC
    # writes True/False but the schema is strict about the type.
    if record.get("opt_converged") is not None:
        out["converged"] = bool(record["opt_converged"])
    if record.get("opt_n_steps") is not None:
        out["n_steps"] = record["opt_n_steps"]
    if record.get("opt_final_energy_hartree") is not None:
        out["final_energy_hartree"] = record["opt_final_energy_hartree"]
    return out or None


def _coarse_opt_result_payload(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Like :func:`_opt_result_payload` but reads the ``coarse_opt_*`` fields.

    The coarse opt is treated as ``converged=True`` whenever it ran to
    completion — output.yml only records ``coarse_opt_log`` after a
    successful coarse run (see ``_spc_to_dict`` line 555: ``if converged
    and coarse_path``). A separate ``coarse_opt_converged`` field would
    be slightly more honest, but ARC doesn't currently emit one and the
    convention "the coarse log only exists if it converged" holds.
    """
    out: dict[str, Any] = {}
    # The presence of the coarse log indicates a successful coarse stage.
    out["converged"] = True
    if record.get("coarse_opt_n_steps") is not None:
        out["n_steps"] = record["coarse_opt_n_steps"]
    if record.get("coarse_opt_final_energy_hartree") is not None:
        out["final_energy_hartree"] = record["coarse_opt_final_energy_hartree"]
    return out


# ARC's own criterion for a transition state's *major* mode (the
# reaction coordinate): the unique imaginary mode whose magnitude falls
# strictly inside this window. Anything outside it is either numerical
# noise (too soft — a grid/optimisation artifact) or implausibly stiff
# (an SCF/parse artifact), per ``arc/checks/ts.py::
# check_imaginary_frequencies`` and ``arc/settings/settings.py``'s
# ``LOWEST_MAJOR_TS_FREQ`` / ``HIGHEST_MAJOR_TS_FREQ``.
_TS_MAJOR_MODE_MIN_CM1 = 75.0
_TS_MAJOR_MODE_MAX_CM1 = 10000.0


def _designate_reaction_coordinate_index(
    imaginary_values: list[float],
) -> int | None:
    """1-based position in ``imaginary_values`` of the unique major TS mode.

    The rule (largest-magnitude candidate inside the window, ``None`` on a tie or no
    candidate) is :func:`tckdb_core.rules.designate_reaction_coordinate_index`; the
    window is ARC's own, (75, 10000) cm-1, mirroring ``arc/checks/ts.py::
    check_imaginary_frequencies`` (``_TS_MAJOR_MODE_MIN_CM1`` / ``_TS_MAJOR_MODE_MAX_CM1``).
    """
    return _rules.designate_reaction_coordinate_index(
        imaginary_values, window_cm1=(_TS_MAJOR_MODE_MIN_CM1, _TS_MAJOR_MODE_MAX_CM1))


def _spin_diagnostic_payload(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Build the TCKDB ``SpinDiagnosticPayload`` dict for a species/TS sp calc.

    Reads the ``sp_spin_diagnostic`` block that ``arc/output.py`` writes into
    ``output.yml`` for open-shell/unrestricted single points (parsed ``<S**2>``).
    Emits the block ONLY when a finite ``s_squared`` is present; restricted /
    closed-shell records carry ``sp_spin_diagnostic=None`` (or omit the key),
    for which this returns ``None`` and the caller leaves ``spin_diagnostic``
    unset — never an all-null or fabricated block.

    The ``s_squared`` field is required by the backend schema; the two
    companion fields (``s_squared_expected`` / ``s_squared_annihilated``) are
    optional and are only included when present and finite. All values are
    clamped to ``ge=0`` by construction of the source parser (S(S+1) and
    ``<S**2>`` are non-negative); a defensively-negative value is dropped.
    """
    block = record.get("sp_spin_diagnostic")
    if not isinstance(block, Mapping):
        return None
    s_squared = block.get("s_squared")
    if s_squared is None:
        return None
    try:
        s_squared = float(s_squared)
    except (TypeError, ValueError):
        return None
    if s_squared < 0:
        return None
    payload: dict[str, Any] = {"s_squared": s_squared}
    for optional in ("s_squared_expected", "s_squared_annihilated"):
        value = block.get(optional)
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if value < 0:
            continue
        payload[optional] = value
    return payload


def _wavefunction_diagnostic_payload(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The ``WavefunctionDiagnosticPayload`` for an sp calc from ``sp_t1_diagnostic``, or ``None``.

    Only a finite, non-negative number is sent (TCKDB: ``t1_diagnostic >= 0``);
    ARC writes ``null`` for every level that is not coupled-cluster or QCISD and
    when the log prints none, and an all-null block is refused by TCKDB, so none
    is built then.
    """
    t1 = record.get("sp_t1_diagnostic")
    if isinstance(t1, bool) or not isinstance(t1, (int, float)):
        return None
    t1 = float(t1)
    if not math.isfinite(t1) or t1 < 0:
        return None
    return {"t1_diagnostic": t1}


# ARC's ``wavefunction_stability.verdict`` (arc/parser/adapters gaussian.py and
# orca.py, documented at arc/output.py ``_parse_wavefunction_stability``) onto
# the contract's ``scf_stability.status``. ``'unknown'`` means an analysis ran
# whose verdict ARC could not read, which is the contract's ``inconclusive``
# ("clearly attempted but its result could not be parsed"). Any verdict not
# listed here is not sent: the adapter never guesses a status.
_ARC_STABILITY_VERDICT_TO_STATUS = {
    "stable": "stable",
    "internal_instability": "unstable",
    "external_instability": "unstable",
    "unattributed_instability": "unstable",
    "unknown": "inconclusive",
}
_ARC_STABILITY_INSTABILITY_TYPE = {
    "internal_instability": "internal",
    "external_instability": "external",
}


def _scf_stability_payload(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the primary opt's ``scf_stability``, or ``None``.

    ARC runs its wavefunction stability analysis once per species, from the
    optimization job: at the optimization level, on the geometry that
    optimization converged to, with the orbitals it wrote
    (``arc/scheduler.py::run_stability_job``), so that its SCF reproduces the
    wavefunction under test. ``wavefunction_stability`` describes that
    wavefunction, the opt's, and the block goes on the opt calculation only.
    A freq or sp job runs its own SCF, which may land on another solution, so
    the analysis is not an observation of theirs.

    Nothing is sent when ARC's own record shows the opt sent is not the one
    tested: ``scf_reference.source == 'derived'`` (ARC adopted the verdict and
    re-optimized at another reference, ``arc/output.py::_scf_reference_block``)
    or ``measured_on_ts_guess`` (the verdict was measured on an abandoned TS
    guess).

    ``stable`` is sent only for ARC's ``'stable'`` verdict, and not when the ORCA
    reader reports it followed an instability to a stable solution
    (``followed_to_stable``), which contradicts a stable verdict.
    ``lowest_eigenvalue`` is ARC's own (the tested wavefunction's). ARC exports
    no instability count, and ``followed_to_stable`` is not mapped to
    ``stabilized``: the tested wavefunction stayed unstable and no
    re-optimization on its solution is recorded.
    """
    stability = record.get("wavefunction_stability")
    scf_reference = record.get("scf_reference")
    if not isinstance(stability, Mapping) or not isinstance(scf_reference, Mapping):
        return None
    status = _ARC_STABILITY_VERDICT_TO_STATUS.get(stability.get("verdict"))
    if status is None:
        return None
    if scf_reference.get("measured_on_ts_guess") is not None:
        return None
    if scf_reference.get("source") == "derived":
        return None
    if status == "stable" and stability.get("followed_to_stable") is True:
        return None
    out: dict[str, Any] = {"status": status}
    lowest = stability.get("lowest_eigenvalue")
    if isinstance(lowest, (int, float)) and not isinstance(lowest, bool) and math.isfinite(lowest):
        out["lowest_eigenvalue"] = float(lowest)
    instability_type = _ARC_STABILITY_INSTABILITY_TYPE.get(stability.get("verdict"))
    if instability_type is not None:
        out["instability_type"] = instability_type
    return out


def _conformer_observed_lists(
    species_record: Mapping[str, Any],
) -> tuple[list[Any], list[Any]] | None:
    """``(conformer_ess_software, conformer_ess_version)`` when ARC states both, index-aligned with ``conformers``.

    Output.yml 1.3 at ARC #1059 ebc88ec8 emits them with ``conformers``; the earlier 1.3 draft had
    neither (the fallback is the header ``conformer_opt_level``'s program). A pair that is not aligned
    with ``conformers`` is not used.
    """
    conformers = species_record.get("conformers")
    software = species_record.get("conformer_ess_software")
    version = species_record.get("conformer_ess_version")
    if (isinstance(conformers, (list, tuple)) and isinstance(software, list)
            and isinstance(version, list)
            and len(software) == len(version) == len(conformers)):
        return software, version
    return None


def _conformer_observed_provenance(
    species_record: Mapping[str, Any], index: int,
) -> tuple[str | None, str | None]:
    """``(program, banner)`` of conformer ``index``'s optimization log, each ``None`` when not stated.

    The banner is only meaningful with its program: it is dropped when the program is ``None``.
    """
    lists = _conformer_observed_lists(species_record)
    if lists is None:
        return None, None
    program, banner = lists[0][index], lists[1][index]
    program = program.strip() if isinstance(program, str) and program.strip() else None
    banner = banner.strip() if program and isinstance(banner, str) and banner.strip() else None
    return program, banner


_SCAN_ROTOR_KEY_RE = re.compile(r"^scan_rotor_(\d+)$")


def _scan_job_type(
    output_doc: Mapping[str, Any], record: Mapping[str, Any], scan_key: Any,
) -> str | None:
    """``'scan'`` (ESS) or ``'directed_scan'`` for an exported rotor scan.

    output.yml does not say which kind ran; ``restart.yml`` records each
    rotor's ``directed_scan_type`` (``arc/species/species.py``), and the export
    key is ``scan_rotor_<rotor index>`` (``arc/output.py``). ``None`` when that
    cannot be looked up: the level then cannot tell the two apart.
    """
    restart = _restart_levels(output_doc)
    match = _SCAN_ROTOR_KEY_RE.match(scan_key) if isinstance(scan_key, str) else None
    if restart is None or match is None or not isinstance(record, Mapping):
        return None
    return restart.scan_job_type(record.get("label"), int(match.group(1)))
