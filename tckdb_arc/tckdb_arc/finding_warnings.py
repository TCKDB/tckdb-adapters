"""Findings the ARC adapter reports for a value its source does not state.

Each ``_warn_*`` function logs the finding and returns the warning dict that goes into the payload's
sidecar: a missing bond-correction type, conformer or transition-state guess level or program, an
IRC level, adaptive-level omissions and unstated isotopes.
"""

from collections.abc import Mapping
from dataclasses import field
from typing import Any

from tckdb_arc import arc13
from tckdb_arc.warning_codes import ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE, ArcWarning
from tckdb_arc.self_check import _self_check
from tckdb_arc.geometry import _is_single_atom_geometry
from tckdb_arc.thermo import _bond_corrections_flag
from tckdb_arc._logging import get_logger

from tckdb_arc.calculation_payloads import _W_GEOMETRY_ISOTOPES_NOT_STATED

logger = get_logger()


def _warn_primary_opt_placeholder(
    species_record: Mapping[str, Any],
    warnings: list[dict[str, Any]] | None,
    *,
    field: str,
) -> None:
    """Warn that a molecule's primary opt is a placeholder for a job ARC did not run as an opt.

    TCKDB's computed-species and computed-reaction routes require every
    conformer of two or more atoms to carry an ``opt`` primary calculation
    (``ConformerInBundle.validate_primary_is_opt`` and the reaction ``ConformerIn``
    equivalent). Two shapes of 1.3 record have none: a composite run, whose
    geometry came from a job whose internal optimisation level ARC does not
    export, and a record that exports an sp or freq log but no opt job. Each is
    filed as a marked placeholder opt and reported. The calculation model has no
    ``note`` field to carry the statement.

    A single atom is not a placeholder case: since tckdb-schemas 0.59 (TCKDB#610)
    its primary is its real ``sp`` (``_build_monatomic_primary_sp``), so nothing
    is filed or warned for it.
    """
    if _is_single_atom_geometry(species_record.get("xyz")):
        return
    kind = arc13.primary_opt_placeholder(species_record)
    if kind is None:
        return
    code = (_W_COMPOSITE_GEOMETRY_LEVEL_NOT_STATED if kind == "composite"
            else _W_PRIMARY_OPT_PLACEHOLDER_NO_OPT_JOB)
    label = species_record.get("label") or "<unlabeled>"
    message = (
        f"The primary opt filed for {label!r} is a placeholder (tckdb_origin "
        f"placeholder_primary_opt_{kind}): "
        + ("its geometry came from a composite-method job, whose internal "
           "optimisation level ARC does not export, so the opt carries the composite level ARC states."
           if kind == "composite" else
           "ARC exports no optimisation job for it, so the opt carries the header opt level; "
           "only ARC's stated convergence is sent, with no step count or energy.")
        + " TCKDB requires a primary opt and has no composite calculation type."
    )
    logger.warning("TCKDB %s: %s", code, message)
    if warnings is not None:
        warnings.append(_self_check(
            code, message, field,
            {"action": "placeholder_primary_opt_filed"}))


_W_COMPOSITE_GEOMETRY_LEVEL_NOT_STATED = ArcWarning.COMPOSITE_GEOMETRY_LEVEL_NOT_STATED.value
_W_PRIMARY_OPT_PLACEHOLDER_NO_OPT_JOB = ArcWarning.PRIMARY_OPT_PLACEHOLDER_NO_OPT_JOB.value


_W_BAC_TYPE_NOT_STATED = ArcWarning.BAC_TYPE_NOT_STATED.value


def _warn_bac_type_not_stated(
    output_doc: Mapping[str, Any], record: Mapping[str, Any], warnings: list[dict[str, Any]] | None,
) -> None:
    """Warn when a thermo says bond corrections were applied but the header ``bac_type`` is null.

    Output 1.3 at ARC #1059 ebc88ec8 requires ``bac_type`` to be ``p`` or ``m`` whenever a thermo
    (or statmech) says bond corrections were applied, so ``bond_corrections_applied: true`` beside a
    null ``bac_type`` is a document ARC's own schema refuses. It only warns: the BAC scheme is still built
    from the record's ``energy_corrections``, and ``bac_type`` is never inferred from them.
    """
    if _bond_corrections_flag(record) is not True or output_doc.get("bac_type") in ("p", "m"):
        return
    message = (
        f"{record.get('label')!r}: thermo.bond_corrections_applied is true but the header bac_type is "
        f"{output_doc.get('bac_type')!r}; ARC's output schema requires 'p' or 'm' then. The bond "
        "additivity correction scheme is taken from the record's energy_corrections as before, and bac_type "
        "is not inferred."
    )
    logger.warning("TCKDB %s: %s", _W_BAC_TYPE_NOT_STATED, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_BAC_TYPE_NOT_STATED, message, "thermo",
            {"action": "none_scheme_from_energy_corrections", "bac_type": str(output_doc.get("bac_type"))}))


_W_CONFORMER_NOT_ESS_OPTIMIZED = ArcWarning.CONFORMER_NOT_ESS_OPTIMIZED.value
_W_CONFORMER_PROGRAM_NOT_STATED = ArcWarning.CONFORMER_PROGRAM_NOT_STATED.value


def _warn_conformer_not_esss_optimized(
    warnings: list[dict[str, Any]] | None, *, label: Any, omitted: int, force_field: Any,
) -> None:
    ff = f" (force field {force_field})" if isinstance(force_field, str) and force_field else ""
    message = (
        f"{omitted} screened conformer(s) of {label!r} were not uploaded: ARC states no "
        "ESS optimization level for them (conformer_levels is null: a force-field "
        f"geometry{ff}, a user-supplied conformer, or a restart that predates level "
        "recording), and TCKDB requires an ESS level of theory and program on every "
        "calculation, so a force-field geometry is not a calculation."
    )
    logger.warning("TCKDB %s: %s", _W_CONFORMER_NOT_ESS_OPTIMIZED, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_CONFORMER_NOT_ESS_OPTIMIZED, message, "conformers",
            {"action": "screened_conformers_omitted", "omitted_count": str(omitted)}))


def _warn_conformer_program_not_stated(
    warnings: list[dict[str, Any]] | None, *, label: Any, omitted: int, observed: bool = False,
) -> None:
    if observed:
        message = (
            f"{omitted} screened conformer(s) of {label!r} were not uploaded: ARC states the "
            "level of their optimization but their conformer_ess_software entry is null (the "
            "optimization log was not recorded, is missing, or states no program), and the "
            "program of a calculation is never deduced from a requested level."
        )
    else:
        message = (
            f"{omitted} screened conformer(s) of {label!r} were not uploaded: ARC states the "
            "level of their optimization but not the program (no level inside a record states "
            "one), and the header conformer_opt_level, the only level that names a program, "
            "is null or names another level than theirs (an adaptive conf_opt, or a conformer "
            "re-run at a troubleshooting level)."
        )
    logger.warning("TCKDB %s: %s", _W_CONFORMER_PROGRAM_NOT_STATED, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_CONFORMER_PROGRAM_NOT_STATED, message, "conformers",
            {"action": "screened_conformers_omitted", "omitted_count": str(omitted)}))


_W_CONFORMER_LEVEL_NOT_STATED = ArcWarning.CONFORMER_LEVEL_NOT_STATED.value


def _warn_conformer_level_not_stated(
    warnings: list[dict[str, Any]] | None,
    *,
    label: Any,
    omitted: int,
) -> None:
    message = (
        f"{omitted} screened conformer(s) of {label!r} were not uploaded: ARC "
        "screens conformers at its conformer level "
        "(default wb97xd/def2svp, not the opt level), output.yml does not "
        "export that level, and the project's restart.yml does not give a "
        "usable one (no conformer_opt_level naming its program, conf_opt not "
        "run, or a species-specific adaptive level), so a calculation for "
        "them cannot state one. ARC should export the conformer level."
    )
    logger.warning("TCKDB %s: %s", _W_CONFORMER_LEVEL_NOT_STATED, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_CONFORMER_LEVEL_NOT_STATED, message, "conformers",
            {"action": "screened_conformers_omitted", "omitted_count": str(omitted)}))


_W_TS_GUESS_LEVEL_NOT_STATED = ArcWarning.TS_GUESS_LEVEL_NOT_STATED.value
_W_TS_GUESS_SOFTWARE_NOT_STATED = ArcWarning.TS_GUESS_SOFTWARE_NOT_STATED.value


def _warn_ts_guess_level_not_stated(
    warnings: list[dict[str, Any]] | None,
    *,
    ts_label: Any,
    method: str,
    reason: str | None = None,
    software: bool = False,
    schema_states_level: bool | None = None,
) -> None:
    code = _W_TS_GUESS_SOFTWARE_NOT_STATED if software else _W_TS_GUESS_LEVEL_NOT_STATED
    if reason is not None:
        why = reason if software else (
            "could not build it from ARC's exported "
            f"{'GSM' if method == 'gsm' else 'NEB'} level ({reason})")
    elif method == "neb":
        why = "ARC did not export neb_level for this run"
    elif method == "gsm" and schema_states_level:
        why = (
            "ARC's gsm_level is null: the archived xtb outputs beside the GSM log "
            "do not all show GFN2-xTB with the TS record's charge and spin, or none "
            "were archived"
        )
    else:
        why = (
            f"ARC exports no level for the {method.upper()} path search "
            "(only the ORCA NEB level, neb_level, is exported)"
        )
    message = (
        f"The {method.upper()} path-search calculation of the TS guess for "
        f"{ts_label!r} was not uploaded: {why}. TCKDB requires a level of "
        "theory and a program on every calculation and the adapter does not "
        "file the path search at opt_level or under a deduced program."
        + ("" if schema_states_level else
           " ARC should export the TS-guess level.")
    )
    logger.warning("TCKDB %s: %s", code, message)
    if warnings is not None:
        warnings.append(_self_check(
            code, message, "transition_state.path_search.level_of_theory",
            {"action": "path_search_calculation_omitted", "method": method}))


_W_ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE: Mapping[str, str] = ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE


def _warn_adaptive_omissions(
    warnings: list[dict[str, Any]] | None, marker: Mapping[str, Any],
) -> None:
    """One warning per job kind whose level could not be attributed (and was left out)."""
    sources = ", ".join(marker["sources"])
    for kind, labels in sorted(marker["omitted"].items()):
        code = _W_ADAPTIVE_LEVEL_NOT_ATTRIBUTABLE.get(kind)
        if code is None or not labels:
            continue
        if "output.yml adaptive_levels" in marker["sources"]:
            # Output.yml 1.3 states the per-record level of the opt, freq, sp,
            # composite and IRC jobs (``levels``); the scan and conformer levels of
            # an adaptive run are header levels left null when an entry names them.
            message = (
                f"ARC ran with adaptive_levels (found in {sources}), which assigns the "
                f"{kind} level per species by heavy-atom count, and output.yml 1.3 "
                f"states no {kind} level for {', '.join(sorted(labels))} (the record's "
                f"levels entry is null, or the header {kind}_level is null because an "
                "adaptive entry names it). TCKDB requires a level on every "
                f"calculation, so their {kind} calculation(s) are omitted rather than "
                "labelled with a level they may not have run at."
            )
        else:
            message = (
                f"ARC ran with adaptive_levels (found in {sources}), which assigns the "
                f"{kind} level per species by heavy-atom count, but output.yml records "
                f"one {kind} level per run, and the project's restart.yml does not "
                f"allow the level of {', '.join(sorted(labels))} to be worked out "
                f"(it needs adaptive_levels and a species entry there). TCKDB requires "
                f"a level on every calculation, so their {kind} calculation(s) are "
                "omitted rather than labelled with a level they may not have run at. "
                "ARC should export per-species levels."
            )
        logger.warning("TCKDB %s: %s", code, message)
        if warnings is not None:
            warnings.append(_self_check(
                code, message, f"{kind}.level_of_theory",
                {"action": f"{kind}_calculation_omitted", "adaptive_levels_sources": ",".join(marker["sources"]), "labels": ",".join(sorted(labels))}))


# ARC runs every IRC job at ``irc_level`` (arc/scheduler.py ``run_irc_job``).
# restart.yml records it whenever it differs from the settings default, and
# the adapter then uses it exactly. When it is absent the level is that
# default, ``default_levels_of_theory['irc']`` (arc/settings/settings.py), which
# the adapter cannot read from the project files, and the adapter *assumes* it
# equals ``opt_level`` (true when ``opt_level`` is that same default, the
# maintainer's adapter-0.6.0 decision). That assumption is reported on the IRC
# calculation, until ARC exports the IRC level.
_W_IRC_LEVEL_NOT_STATED = ArcWarning.IRC_LEVEL_NOT_STATED.value


def _warn_irc_level_not_stated(
    warnings: list[dict[str, Any]] | None, *, ts_label: Any, ts_record: Mapping[str, Any],
) -> None:
    """Schema 1.3 states no usable IRC level for this TS, so its IRC calculation is not filed."""
    stated, _ = arc13.irc_recorded_level(ts_record)
    why = (
        "its forward and reverse IRC jobs ran at different levels (levels.irc is null "
        "and irc_log_levels differ), and one calculation cannot carry two levels"
        if stated else
        "ARC recorded no level for its IRC jobs (levels.irc and irc_log_levels are null) "
        "and states no header irc_level"
    )
    message = (
        f"The IRC calculation of {ts_label!r} was not uploaded: {why}. The adapter "
        "does not file an IRC at the opt level."
    )
    logger.warning("TCKDB %s: %s", _W_IRC_LEVEL_NOT_STATED, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_IRC_LEVEL_NOT_STATED, message, "transition_state.irc.level_of_theory",
            {"action": "irc_calculation_omitted"}))


_W_IRC_LEVEL_ASSUMED_OPT_LEVEL = ArcWarning.IRC_LEVEL_ASSUMED_OPT_LEVEL.value


def _warn_irc_level_assumed(
    warnings: list[dict[str, Any]] | None,
    level_of_theory: Mapping[str, Any],
) -> None:
    message = (
        "ARC does not export irc_level and the project's restart.yml does not "
        "record one, so the IRC ran at the run's settings default "
        "(default_levels_of_theory['irc']), which the adapter cannot read. "
        "The IRC calculation is labelled with opt_level "
        f"({level_of_theory.get('method')}"
        f"{'/' + str(level_of_theory['basis']) if level_of_theory.get('basis') else ''}), "
        "assuming the two are equal; that is wrong when opt_level is not the "
        "settings default."
    )
    logger.warning("TCKDB %s: %s", _W_IRC_LEVEL_ASSUMED_OPT_LEVEL, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_IRC_LEVEL_ASSUMED_OPT_LEVEL, message, "transition_state.irc.level_of_theory",
            {"action": "level_of_theory_assumed"}))


def _warn_isotopes_not_stated(
    warnings: list[dict[str, Any]] | None, *, label: Any, what: str,
) -> None:
    message = (
        f"{what} of the isotopically substituted {label!r} were left out: ARC states no "
        "isotope list that matches the species' own geometry for them, and TCKDB would "
        "read an unlabelled geometry as the unsubstituted species."
    )
    logger.warning("TCKDB %s: %s", _W_GEOMETRY_ISOTOPES_NOT_STATED, message)
    if warnings is not None:
        warnings.append(_self_check(
            _W_GEOMETRY_ISOTOPES_NOT_STATED, message, "geometry.isotopes",
            {"action": "geometry_omitted"}))
