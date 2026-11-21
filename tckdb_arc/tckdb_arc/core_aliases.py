"""Names the ARC adapter takes from ``tckdb_core`` and binds under its own spellings.

The unit maps, the reaction-flattening tables, the version-banner splitter and the two small rule
helpers are the core's objects; the unit, NASA and thermo-point wrappers call the core with the
package logger, so their records land under ``tckdb_arc``.
"""

from typing import Any

from tckdb_core import reaction_flatten as _reaction_flatten
from tckdb_core import rmg_units as _rmg_units
from tckdb_core import rules as _rules
from tckdb_core import software_release as _software_release
from tckdb_core import thermo_numerics as _thermo_numerics
from tckdb_core import ts_evidence as _ts_evidence
from tckdb_arc._logging import get_logger

logger = get_logger()


_ARC_TO_TCKDB_A_UNITS = _rmg_units.RMG_TO_TCKDB_A_UNITS
_ARC_TO_TCKDB_EA_UNITS = _rmg_units.RMG_TO_TCKDB_EA_UNITS
_normalize_unit_key = _rmg_units.normalize_unit_key


def arc_to_tckdb_a_units(arc_units: str | None) -> str | None:
    """Map an ARC ``A_units`` string to a TCKDB ``ArrheniusAUnits`` enum value.

    ``tckdb_core.rmg_units.a_units_to_tckdb`` with this module's logger: ``None`` for
    null/empty or unrecognised units (a debug line says which).
    """
    return _rmg_units.a_units_to_tckdb(arc_units, log=logger)


def arc_to_tckdb_ea_units(arc_units: str | None) -> str | None:
    """Map an ARC ``Ea_units`` (or ``dEa_units``) string to a TCKDB ``ActivationEnergyUnits`` value.

    ``tckdb_core.rmg_units.ea_units_to_tckdb`` with this module's logger.
    """
    return _rmg_units.ea_units_to_tckdb(arc_units, log=logger)


def _build_nasa_block(nasa_low: Any, nasa_high: Any) -> dict[str, Any] | None:
    """ARC's two NASA blocks as ``ThermoNASACreate`` (``tckdb_core.thermo_numerics``), or ``None``."""
    return _thermo_numerics.build_nasa_block(nasa_low, nasa_high, log=logger)


def _build_thermo_points(thermo_points: Any) -> list[dict[str, Any]]:
    """ARC's ``thermo_points`` as ``ThermoPointCreate`` dicts (``tckdb_core.thermo_numerics``)."""
    return _thermo_numerics.build_thermo_points(thermo_points, log=logger)


_REACTION_FLAT_RESULT_FIELDS = _reaction_flatten.REACTION_FLAT_RESULT_FIELDS
_REACTION_RESULT_FIELDS_HANDLED_ELSEWHERE = _reaction_flatten.REACTION_RESULT_FIELDS_HANDLED_ELSEWHERE
_REACTION_RESULT_FIELDS_NOT_CARRIED = _reaction_flatten.REACTION_RESULT_FIELDS_NOT_CARRIED
_REACTION_MODE_FIELDS_HANDLED = _reaction_flatten.REACTION_MODE_FIELDS_HANDLED
_REACTION_MODE_FIELDS_NOT_CARRIED = _reaction_flatten.REACTION_MODE_FIELDS_NOT_CARRIED
_flatten_result_fields = _reaction_flatten.flatten_result_fields
_flatten_all_reaction_calcs = _reaction_flatten.flatten_all_reaction_calcs

_XTB_BANNER_RE = _software_release.XTB_BANNER_RE
_split_ess_version_banner = _software_release.split_version_banner

_calculations_by_key = _rules.calculations_by_key
_finite_float = _ts_evidence.finite_float
