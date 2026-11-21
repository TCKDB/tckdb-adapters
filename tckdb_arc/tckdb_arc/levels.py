"""Level-of-theory helpers of the ARC adapter.

Resolves the level a calculation ran at (and the levels ARC's adaptive and restart records
change), compares levels by identity, and holds the field tables that translate ARC's level
shape into TCKDB's level-of-theory reference.
"""

import json
import re
from collections.abc import Mapping
from typing import Any

from tckdb_arc import arc13
from tckdb_arc.adaptive import RestartInfo, RUN_LEVEL, UNDETERMINABLE
from tckdb_arc.geometry import _species_element_symbols
from tckdb_core import rules as _rules


_describe_level = _rules.describe_level


def _resolve_level(
    output_doc: Mapping[str, Any],
    job_kind: str,
    record: Mapping[str, Any] | None = None,
    *,
    job_type: str | None = None,
) -> Mapping[str, Any] | None:
    """Return the level-of-theory dict for ``job_kind`` ('opt'/'freq'/'sp').

    The opt level is always required by the primary calculation. For
    freq/sp, ARC users very often run all three at the same level and
    only declare ``opt_level=`` — ``output.yml`` then writes ``freq_level:
    null`` / ``sp_level: null``. To avoid silently dropping the freq/sp
    additional calculations in the common case, fall back to ``opt_level``
    when the job-specific level is absent. Scans require their own level;
    ARC can run them at a different method from optimization. ``neb`` is the
    ORCA NEB path-search level (``neb_level``, exported only when the
    ``orca_neb`` TS adapter was configured) and never falls back to the opt
    level either: the NEB runs at ``orca_neb_settings['level']``, not at any of
    the run's opt/freq/sp levels. A present-but-distinct
    ``freq_level`` / ``sp_level`` is treated as authoritative.

    The fallback follows ARC's own semantics (kept in adapter 0.6.0 by
    maintainer decision): ARC itself sets ``freq_level = opt_level`` when a
    frequency job is requested without one (arc/main.py
    ``set_levels_of_theory``), and a null ``sp_level`` means no sp job was
    requested, so ARC parses the species' electronic energy from the
    optimization log (arc/scheduler.py: ``parse_opt_e_elect`` is called
    when ``not self.job_types['sp']``); an ``sp_level`` equal to
    ``opt_level`` likewise reuses the optimization output (``run_sp_job``).
    output.yml's ``sp_energy_hartree`` is that ``e_elect``.

    Under ARC ``adaptive_levels`` (``output_doc`` marked by
    ``TCKDBAdapter._with_adaptive_levels``) a job type the adaptive levels name
    has a per-species level that output.yml does not record. With ``record``
    (the species or TS) and a ``restart.yml`` holding the adaptive spec and
    the species, ARC's own choice is replayed exactly
    (``tckdb_arc.adaptive.RestartInfo.attribute_level``); a job type the
    species' range does not name runs at the run's regular level, as
    everywhere else. Otherwise ``None`` is returned (and noted, for the
    warning) and the caller states no level. ``job_type`` names the ARC job
    type when a kind has several (``scan`` / ``directed_scan``). A null
    ``sp_level`` reuses the optimization's energy, so it follows the
    (attributed) opt level; a null ``freq_level`` beside an adaptive opt has
    no stated source and yields ``None``.
    """
    # Schema 1.3 states the level of each job whose log the record exports
    # (``levels``), also under ``adaptive_levels``: it is authoritative and
    # replaces the restart.yml replay. It states no ``software`` (the program is
    # ``ess_software``). What it leaves unstated falls through to the pre-1.3 rules.
    if record is not None and arc13.has_levels(record):
        recorded = arc13.recorded_level(record, job_kind)
        if recorded is not None:
            if (recorded.level is None and job_kind == "opt"
                    and arc13.primary_opt_placeholder(record) == "no_opt_job"):
                # ARC exports an sp/freq log but no opt job; the primary opt TCKDB
                # requires is filed as a marked placeholder at the header opt level.
                header = output_doc.get("opt_level")
                return header if isinstance(header, Mapping) else None
            return recorded.level
        if job_kind == "scan":
            # ARC exports no per-species scan level: the header ``scan_level`` is the
            # requested level of the run, ``null`` when rotor scans were not requested
            # or when an adaptive entry names ``scan`` (the level then depends on the
            # species).
            level = output_doc.get("scan_level")
            if not isinstance(level, Mapping) and _adaptive_kind_named(output_doc, "scan"):
                _note_adaptive_omission(output_doc, "scan", record)
            return level if isinstance(level, Mapping) else None
    if _adaptive_kind_named(output_doc, job_kind) if job_type is None else (
            job_type in (_adaptive_marker(output_doc) or {}).get("named", ())):
        types = (job_type,) if job_type else _JOB_TYPES_BY_KIND.get(job_kind, (job_kind,))
        status = (
            _adaptive_level_status(output_doc, record, types[0])
            if len(types) == 1 else UNDETERMINABLE
        )
        if isinstance(status, Mapping):
            return status
        if status == UNDETERMINABLE:
            _note_adaptive_omission(output_doc, job_kind, record)
            return None
        # RUN_LEVEL: the species' range does not name it; the run level applies.
    if job_kind in {"opt", "scan", "neb", "gsm"}:
        level = output_doc.get(f"{job_kind}_level")
        return level if isinstance(level, Mapping) else None
    job_level = output_doc.get(f"{job_kind}_level")
    if isinstance(job_level, Mapping):
        return job_level
    if job_kind == "freq" and _adaptive_kind_named(output_doc, "opt"):
        _note_adaptive_omission(output_doc, job_kind, record)
        return None
    return _resolve_level(output_doc, "opt", record)


# TCKDB's LevelOfTheoryRef primary-key set — used by _level_keys_match
# for conservative LoT equality. Note the names here are TCKDB's
# (post-translation), not ARC's (output.yml writes `auxiliary_basis` /
# `cabs`); _level_keys_match compares projected dicts.
_TCKDB_LOT_REF_FIELDS = ("method", "basis", "aux_basis", "cabs_basis")


def _canonical_level_args(level: Mapping[str, Any]) -> str | None:
    """ARC ``args`` of a level as a canonical string (``None`` when it states none).

    Order-independent (``sort_keys``); a missing, ``None`` or empty ``args`` all read
    as ``None``. Not sent to TCKDB (``LevelOfTheoryRef`` has no ``keywords``): it is
    only used to tell two jobs apart.
    """
    args = level.get("args")
    if not args:
        return None
    try:
        return json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(args)


def _bac_frequency_level(
    output_doc: Mapping[str, Any], record: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """ARC's stated frequency level behind a record's BAC (``scheme.frequency_level_of_theory``).

    Arkane keys a Petersson or Melius BAC on ``energy//freq`` and ARC's correction
    record names the energy half only (``level_of_theory``), so the frequency half
    is the level of the record's frequency job, and only when ARC states it:

    * an output 1.3 record: ``levels.freq``. A record whose frequencies are the
      composite job's own (``levels.freq`` null by design, or a ``freq_log`` that is
      the composite log) names none, so none is sent;
    * an earlier record: the header ``freq_level``, only when the adapter attributes
      it to this record's frequency calculation that way (``_resolve_level``: not
      under ``adaptive_levels``, where the level of a job type the adaptive levels
      name is per species and not exported). A null header ``freq_level`` (ARC wrote
      it null because the frequencies share the opt level) states nothing about the
      BAC, so none is sent: the opt level is not substituted.

    ``None`` means no frequency level is stated; the scheme is then sent without one,
    as before 0.10.
    """
    if record is None:
        return None
    if arc13.has_levels(record):
        recorded = arc13.recorded_level(record, "freq")
        if recorded is None or recorded.level is None or recorded.job_key == "composite":
            return None
        return recorded.level
    header = output_doc.get("freq_level")
    if not isinstance(header, Mapping) or not header.get("method"):
        return None
    if _adaptive_kind_named(output_doc, "freq") or _adaptive_kind_named(output_doc, "opt"):
        return None
    return header


# Levels compare by effective method (the method with its dispersion
# folded in) and basis, normalized exactly as ARC matches levels against
# Arkane's database: case, hyphens and spaces and a trailing refit year are
# ignored, and ``b3lyp`` + ``gd3bj``, ``b3lyp-d3bj`` and ``b3lyp-d3(bj)`` are
# one method, while ``wb97xd``/``wb97xd3`` and ``ccsd(t)``/``ccsdt`` stay
# two. Software, year, method_type and args never matter. Ported, since the
# adapter must not import ARC, from ARC 1977e53b: arc/statmech/arkane.py
# ``_normalize_name`` and ``_split_method_year``; arc/main.py
# ``DISPERSION_SUFFIX_REGEX``, ``_canonical_dispersion`` and
# ``_normalized_method_and_basis``. A parity test runs against ARC when it
# has them.
_METHOD_REFIT_YEAR_RE = re.compile(r"^(.*?)(\d{4})$")
_DISPERSION_SUFFIX_RE = re.compile(r"g?d[234](\(?bj\)?)?$")


def _thermo_energy_level(
    output_doc: Mapping[str, Any], record: Mapping[str, Any] | None = None,
) -> Mapping[str, Any] | None:
    """Return the level a species' energies were computed at.

    ``composite_method`` when set, else the SP level (``opt_level`` when
    ``sp_level`` is null, as ARC then reuses the opt energy). output.yml
    records one level per run, so under ARC ``adaptive_levels`` the level is
    the one ``restart.yml`` attributes to ``record`` (see ``_resolve_level``),
    or ``None`` when that cannot be worked out.
    """
    # Schema 1.3: the record states the level of the job its energy was read from
    # (the composite job on a composite run, else the sp job, or the opt job when no
    # sp ran), also under ``adaptive_levels``.
    if record is not None and arc13.has_levels(record):
        recorded = arc13.recorded_level(
            record, "composite" if record.get("composite_log") else "sp")
        if recorded is not None and recorded.level is not None:
            return recorded.level
    composite = output_doc.get("composite_method")
    if isinstance(composite, Mapping):
        if _adaptive_kind_named(output_doc, "composite"):
            status = _adaptive_level_status(output_doc, record, "composite")
            if isinstance(status, Mapping):
                return status
            if status == UNDETERMINABLE:
                return None
        return composite
    return _resolve_level(output_doc, "sp", record)


def _normalize_level_name(name: Any) -> str | None:
    """ARC's ``arkane.py::_normalize_name``: lowercase, hyphens and spaces removed."""
    if name is None:
        return None
    return str(name).replace("-", "").replace(" ", "").lower()


def _canonical_dispersion(dispersion: Any) -> str:
    """ARC's ``main.py::_canonical_dispersion``: ``gd3bj``, ``D3(BJ)`` and
    ``EmpiricalDispersion=GD3BJ`` all give ``d3bj``; ``''`` for none."""
    if not dispersion:
        return ""
    dispersion = str(dispersion).lower()
    for character in ("-", " ", "(", ")"):
        dispersion = dispersion.replace(character, "")
    dispersion = dispersion.removeprefix("empiricaldispersion=")
    if dispersion in ("gd2", "gd3", "gd3bj"):
        dispersion = dispersion[1:]  # Gaussian's spelling.
    return dispersion


def _level_identity(level: Any) -> tuple[str, str | None] | None:
    """ARC's ``main.py::_normalized_method_and_basis`` on an output.yml level dict.

    Returns the normalized ``(effective method, basis)``, or ``None`` for a
    level without a method.
    """
    if not isinstance(level, Mapping) or not level.get("method"):
        return None
    method = _normalize_level_name(level["method"])
    year_split = _METHOD_REFIT_YEAR_RE.match(method)  # arkane.py::_split_method_year
    if year_split is not None:
        method = year_split.group(1)
    suffix = _DISPERSION_SUFFIX_RE.search(method)
    if suffix is not None:
        method = method[:suffix.start()] + _canonical_dispersion(suffix.group())
    method += _canonical_dispersion(level.get("dispersion"))
    return method, _normalize_level_name(level.get("basis"))


def _opt_level_software(output_doc: Mapping[str, Any]) -> str | None:
    opt_level = output_doc.get("opt_level")
    if isinstance(opt_level, Mapping):
        sw = opt_level.get("software")
        if sw:
            return str(sw)
    return None


# ARC ``adaptive_levels`` and the other levels output.yml does not record (see
# ``tckdb_arc.adaptive``). ``TCKDBAdapter._with_adaptive_levels`` marks a copy of
# the output document with the project's ``restart.yml`` state
# (``_RESTART_KEY``) and, for an adaptive run, the job types the adaptive levels
# name (``_ADAPTIVE_LEVELS_KEY``). output.yml records one level per run, so for
# a named job type it states a level no particular species need have run at:
# ``_resolve_level`` replays ARC's own per-species choice from ``restart.yml``
# where that is possible, and otherwise states nothing.
_ADAPTIVE_LEVELS_KEY = "_tckdb_adaptive_levels"
_RESTART_KEY = "_tckdb_restart_levels"

# The ARC job types (``arc/scheduler.py`` ``run_job``) behind each level kind the
# adapter states; adaptive levels key on these exact strings.
_JOB_TYPES_BY_KIND: Mapping[str, tuple[str, ...]] = {
    "opt": ("opt",),
    "freq": ("freq",),
    "sp": ("sp",),
    "composite": ("composite",),
    "irc": ("irc",),
    "scan": ("scan", "directed_scan"),
    "conf_opt": ("conf_opt",),
}


def _header_adaptive_job_types(output_doc: Mapping[str, Any]) -> set[str]:
    """The job types the schema-1.3 header ``adaptive_levels`` entries name (``"opt freq"`` is two)."""
    entries = output_doc.get("adaptive_levels")
    types: set[str] = set()
    if not isinstance(entries, list):
        return types
    for entry in entries:
        levels = entry.get("levels") if isinstance(entry, Mapping) else None
        if isinstance(levels, Mapping):
            for key in levels:
                types.update(str(key).split())
    return types


def _adaptive_marker(output_doc: Mapping[str, Any]) -> Mapping[str, Any] | None:
    marker = output_doc.get(_ADAPTIVE_LEVELS_KEY)
    return marker if isinstance(marker, Mapping) else None


def _restart_levels(output_doc: Mapping[str, Any]) -> RestartInfo | None:
    info = output_doc.get(_RESTART_KEY)
    return info if isinstance(info, RestartInfo) else None


def _adaptive_kind_named(output_doc: Mapping[str, Any], kind: str) -> bool:
    """Whether the run's adaptive levels name any job type behind ``kind``."""
    marker = _adaptive_marker(output_doc)
    return marker is not None and any(
        t in marker["named"] for t in _JOB_TYPES_BY_KIND.get(kind, (kind,)))


def _n_heavy_from_record(record: Mapping[str, Any] | None) -> int | None:
    """ARC's fallback heavy-atom count: the non-``H`` atoms of the geometry."""
    symbols = _species_element_symbols(record) if isinstance(record, Mapping) else None
    return None if symbols is None else sum(1 for symbol in symbols if symbol != "H")


def _adaptive_level_status(
    output_doc: Mapping[str, Any], record: Mapping[str, Any] | None, job_type: str,
) -> Mapping[str, Any] | str:
    """The level ARC ran ``job_type`` at for ``record``, from ``restart.yml``.

    A level dict (same shape as output.yml levels), ``RUN_LEVEL`` (the
    species' adaptive range does not name the job type), or ``UNDETERMINABLE``
    (no ``restart.yml`` adaptive spec or species entry, or no heavy-atom
    count).
    """
    restart = _restart_levels(output_doc)
    if restart is None or record is None:
        return UNDETERMINABLE
    return restart.attribute_level(
        record.get("label"), job_type, _n_heavy_from_record(record))


def _note_adaptive_omission(
    output_doc: Mapping[str, Any], kind: str, record: Mapping[str, Any] | None,
) -> None:
    marker = _adaptive_marker(output_doc)
    label = record.get("label") if isinstance(record, Mapping) else None
    if marker is not None and isinstance(label, str):
        marker["omitted"].setdefault(kind, set()).add(label)


def _energy_level_unattributable(
    output_doc: Mapping[str, Any], energy_level: Mapping[str, Any] | None,
) -> bool:
    """Whether an adaptive run left the species' energy level (sp, or composite) unknown."""
    return _adaptive_marker(output_doc) is not None and energy_level is None


def _conformer_screen_level(
    output_doc: Mapping[str, Any], species_record: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """The level ARC's conformer screen ran at for a species, or ``None``.

    ``conformer_opt_level`` from ``restart.yml``, only when ``conf_opt`` ran
    (``job_types``) and the level names its program; not when the adaptive
    levels name ``conf_opt`` for the species' range or the range cannot be
    worked out.
    """
    restart = _restart_levels(output_doc)
    if (restart is None or restart.conformer_opt_level is None
            or restart.job_types.get("conf_opt") is not True
            or not restart.conformer_opt_level.get("software")):
        return None
    if _adaptive_kind_named(output_doc, "conf_opt"):
        status = _adaptive_level_status(output_doc, species_record, "conf_opt")
        if status != RUN_LEVEL:
            return None
    return restart.conformer_opt_level


def _level_keys_match(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> bool:
    """Conservative level-of-theory equality check.

    Compares the fields TCKDB treats as primary keys for
    ``LevelOfTheoryRef`` (method/basis/aux_basis/cabs_basis). A difference
    in software is intentionally not enough to declare inequality,
    since ARC frequently leaves it null on one side
    (the opt level) and populated on the other (the sp level) without
    that meaning the *energies* are at different levels.

    The one exception is ``dispersion`` and ``solvation_method``: a
    difference there (present on one side only, or different) *is* a
    mismatch, so an SMD single point is never taken as the reference of a
    gas-phase IRC (its only use is the IRC reference energy).
    """
    if a is None or b is None:
        return False
    for field in ("dispersion", "solvation_method"):
        av, bv = a.get(field), b.get(field)
        if (av is None) != (bv is None):
            return False
        if av is not None and str(av).strip().lower() != str(bv).strip().lower():
            return False
    for field in _TCKDB_LOT_REF_FIELDS:
        av = a.get(field)
        bv = b.get(field)
        if av is None and bv is None:
            continue
        if av is None or bv is None:
            return False
        if str(av).strip().lower() != str(bv).strip().lower():
            return False
    return True
