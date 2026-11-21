"""Level-of-theory payload helpers of the ARC adapter.

ARC's level shape as TCKDB's ``LevelOfTheoryRef``, the energy level a calculation set declares, and the
levels a conformer states for its optimisation and single point.
"""

import math
from collections.abc import Mapping
from typing import Any

from tckdb_schemas.fragments.refs import correction_table_method_stem
from tckdb_arc.warning_codes import ArcWarning
from tckdb_core import rules as _rules
from tckdb_arc.levels import _canonical_level_args, _describe_level
from tckdb_arc.calculations import (
    _CALC_KEY_OPT,
    _CALC_KEY_SP,
    _conformer_observed_lists,
    _conformer_observed_provenance,
)
from tckdb_arc._logging import get_logger

logger = get_logger()


def _energy_level_declaration(
    energy_level: Mapping[str, Any] | None,
    *,
    calc_keys_by_role: Mapping[str, str],
    calculations: Mapping[str, Mapping[str, Any]],
) -> tuple[str, dict[str, Any]] | None:
    """Return ``(role, level)`` to declare as ``energy_level_of_theory``, or ``None``.

    The rule is TCKDB's (``assert_role_consistency``) and lives in
    :func:`tckdb_core.rules.energy_level_declaration`; reading ARC's stated energy level
    (``energy_level``, see ``_thermo_energy_level``) into the ``LevelOfTheoryRef`` shape
    is the ARC part. Nothing is declared when ARC's energy level is not stated (adaptive
    runs it cannot attribute).
    """
    if not isinstance(energy_level, Mapping):
        return None
    return _rules.energy_level_declaration(
        _arc_level_to_tckdb_lot(energy_level),
        energy_level_description=_describe_level(energy_level),
        calc_keys_by_role=calc_keys_by_role,
        calculations=calculations,
        producer_name="ARC",
        role_order=(_CALC_KEY_SP, _CALC_KEY_OPT),
        log=logger,
    )


# Field-name translation from ARC's Level (output.yml shape) to TCKDB's
# LevelOfTheoryRef. ARC's Level.as_dict() emits `auxiliary_basis` / `cabs` /
# `solvation_method`; TCKDB's LoT uses `aux_basis` / `cabs_basis` /
# `solvent_model`. `software` / `software_version` belong on
# `software_release`, not the LoT, and are intentionally dropped here.
# `method_type` / `year` / `solvation_scheme_level` / `compatible_ess` have
# no TCKDB LoT counterpart and are also dropped.
_ARC_TO_TCKDB_LOT_FIELDS = {
    "method": "method",
    "basis": "basis",
    "auxiliary_basis": "aux_basis",
    "cabs": "cabs_basis",
    "dispersion": "dispersion",
    "solvent": "solvent",
    "solvation_method": "solvent_model",
}


def _same_arc_job_level(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Whether two ARC levels describe the same JOB level: the TCKDB projection AND the ARC ``args``.

    ARC's ``Level.__eq__`` compares ``str()``, which includes ``args``. The TCKDB
    projection no longer carries ``args`` (as ``keywords``) since tckdb-schemas 0.158, so
    comparing projections alone calls a ``wb97xd/def2-svp`` optimization and a
    ``wb97xd/def2-svp`` single point with ``int=ultrafine scf=tight`` the same level.
    Use this for "is this the same job?" questions (a conformer's energy being its
    optimization's, a re-run at another level); use the projection alone for "is this
    the same TCKDB level of theory?" questions (frequency-scale-factor matching).
    """
    return (
        _arc_level_to_tckdb_lot(a) == _arc_level_to_tckdb_lot(b)
        and _canonical_level_args(a) == _canonical_level_args(b)
    )


_W_LEVEL_METHOD_IS_COMPOUND = ArcWarning.LEVEL_METHOD_IS_COMPOUND.value


def _arc_level_to_tckdb_lot(
    level: Mapping[str, Any] | None, *, split_table: bool = True,
) -> dict[str, Any] | None:
    """Project ARC's per-job level dict (output.yml shape) onto TCKDB's
    ``LevelOfTheoryRef`` shape, applying field-name translation. ARC's ``args``
    are not sent: ``LevelOfTheoryRef`` has no free-text ``keywords`` since
    tckdb-schemas 0.158 (the input deck and log carry the options).

    A method that names a correction table (``cbs-qb3-paraskevas``, which ARC accepts as a
    composite method and runs as CBS-QB3) is sent as its stem (``cbs-qb3``): tckdb-schemas
    0.67 would store the table name as a separate level of theory. ``split_table=False``
    keeps the string, for a scheme level whose caller names the table on the scheme.

    Returns ``None`` if ``level`` is missing or has no ``method`` —
    callers decide whether to error or skip.
    """
    if not isinstance(level, Mapping):
        return None
    if not level.get("method"):
        return None
    if "//" in str(level["method"]):
        # ``energy//geometry`` is two levels of theory, not one method (tckdb-schemas
        # 0.67 refuses it: ``level_of_theory_method_is_compound``). ARC splits the shorthand
        # when it reads it (arc/main.py), so a compound method here is a hand-edited or
        # foreign document; which half belongs to this job is not stated, so the level is
        # refused rather than forwarded or guessed.
        logger.warning(
            "TCKDB %s: level method %r contains '//' (two levels of theory); the level is "
            "not sent, since which half belongs to this calculation is not stated.",
            _W_LEVEL_METHOD_IS_COMPOUND, str(level["method"]),
        )
        return None
    out: dict[str, Any] = {}
    for src, dst in _ARC_TO_TCKDB_LOT_FIELDS.items():
        v = level.get(src)
        if v:
            out[dst] = str(v)
    if split_table:
        stem = correction_table_method_stem(out["method"])
        if stem is not None:
            out["method"] = stem
    return out


def _stated_conformer_level(
    output_doc: Mapping[str, Any], species_record: Mapping[str, Any], index: int,
) -> Mapping[str, Any] | None:
    """The level of conformer ``index`` (output.yml 1.3 ``conformer_levels``) with its program, or ``None``.

    ARC states the level of the optimization job behind each conformer geometry
    but, like every level inside a record, no ``software``. Where ARC also states
    the program of each conformer's own optimization log (``conformer_ess_software``,
    ARC #1059 ebc88ec8) that observed program is the one used and a ``null`` entry
    (no ESS optimization, no log recorded, or none identified) means not filed. Otherwise
    (the earlier 1.3 draft) the program is the header
    ``conformer_opt_level``'s, the level the conformer jobs were requested at
    (software included), accepted only when it names the same level as the
    conformer's own; a conformer re-run at a troubleshooting level, or an adaptive
    ``conf_opt`` (which leaves the header level null), has none and is not filed.
    """
    level = species_record["conformer_levels"][index]
    if not isinstance(level, Mapping):
        return None
    if _conformer_observed_lists(species_record) is not None:
        program = _conformer_observed_provenance(species_record, index)[0]
        return {**level, "software": program} if program else None
    header = output_doc.get("conformer_opt_level")
    if not isinstance(header, Mapping):
        return None
    software = header.get("software")
    if not software:
        return None
    if not _same_arc_job_level(level, header):
        return None
    return {**level, "software": software}


_E_H_KJ_MOL_KIND = "electronic_kj_mol"


def _stated_conformer_opt_result(
    species_record: Mapping[str, Any], index: int, level: Mapping[str, Any],
) -> dict[str, Any] | None:
    """``opt_result`` for a conformer whose energy ARC states as an electronic energy at its own level.

    ``conformer_energies`` is sent only when ``conformer_energy_kind`` says it is an
    electronic energy in kJ/mol and ``conformer_energy_level`` is the conformer's
    optimization level (a conformer single point at another level overwrites the
    optimization energy and would not be this opt's energy). A force-field energy
    (kcal/mol, no level) is never sent.
    """
    if species_record.get("conformer_energy_kind") != _E_H_KJ_MOL_KIND:
        return None
    energy_level = species_record.get("conformer_energy_level")
    if not isinstance(energy_level, Mapping):
        return None
    if not _same_arc_job_level(energy_level, level):
        return None
    energies = species_record.get("conformer_energies")
    if not isinstance(energies, (list, tuple)) or index >= len(energies):
        return None
    energy = energies[index]
    if isinstance(energy, bool) or not isinstance(energy, (int, float)) or not math.isfinite(energy):
        return None
    from tckdb_arc._vendor import E_h_kJmol
    return {"final_energy_hartree": float(energy) / E_h_kJmol}


def _stated_conformer_sp(
    output_doc: Mapping[str, Any], species_record: Mapping[str, Any], index: int,
    conf_level: Mapping[str, Any],
) -> tuple[Mapping[str, Any], float] | None:
    """``(level with program, hartree)`` of a conformer single point ARC states, else ``None``.

    When the screen ran conformer single points (``conformer_energy_level`` is not
    the conformer's own optimization level), the stated electronic energy is the
    single point's. Its program is the header ``conformer_sp_level``'s, accepted
    only when that names the same level as ``conformer_energy_level``.
    """
    if species_record.get("conformer_energy_kind") != _E_H_KJ_MOL_KIND:
        return None
    energy_level = species_record.get("conformer_energy_level")
    header = output_doc.get("conformer_sp_level")
    if not isinstance(energy_level, Mapping) or not isinstance(header, Mapping):
        return None
    if _same_arc_job_level(energy_level, conf_level):
        return None  # the optimization's own energy, see _stated_conformer_opt_result
    software = header.get("software")
    if not software or not _same_arc_job_level(energy_level, header):
        return None
    energies = species_record.get("conformer_energies")
    if not isinstance(energies, (list, tuple)) or index >= len(energies):
        return None
    energy = energies[index]
    if isinstance(energy, bool) or not isinstance(energy, (int, float)) or not math.isfinite(energy):
        return None
    from tckdb_arc._vendor import E_h_kJmol
    return {**energy_level, "software": software}, float(energy) / E_h_kJmol
