"""Thermo helpers of the ARC adapter.

The checks that decide whether an enthalpy can be declared a formation enthalpy, the
applied-energy-correction helpers (atom and bond corrections, Arkane keys) and the checks on
composite-method energies. The thermo block itself is built in ``adapter.py``.
"""

import math
import re
from collections.abc import Mapping
from typing import Any

from tckdb_arc import arc13
from tckdb_arc.calculations import _CALC_KEY_OPT, _ROLE_COMPOSITE
from tckdb_arc.geometry import _formula
from tckdb_arc.levels import _describe_level, _level_identity
from tckdb_arc.warning_codes import ArcWarning
from tckdb_core import thermo_numerics as _thermo_numerics
from tckdb_core.level_rules import method_identity_key


_ENERGY_UNITS = frozenset({"hartree", "kj_mol", "kcal_mol"})


def _atom_params_from_reference_atom_energies(
    table: Any,
) -> tuple[list[dict[str, Any]], str | None]:
    """``(atom_params, unit)`` from ARC's ``reference_atom_energies`` block.

    The block is ``{unit, applied_as, values: {element: energy}}``. Returns
    ``([], None)`` (so the scheme is sent without parameters, which the
    contract allows) when the unit is missing or not a TCKDB energy unit
    (never guessed: ``scheme.units`` decides how TCKDB reads every value), or
    when any element or value is unusable (a partial table would misstate
    the scheme).
    """
    if not isinstance(table, Mapping):
        return [], None
    unit = table.get("unit")
    values = table.get("values")
    if unit not in _ENERGY_UNITS or not isinstance(values, Mapping) or not values:
        return [], None
    params: list[dict[str, Any]] = []
    for key, value in sorted(values.items(), key=lambda kv: str(kv[0])):
        element = str(key).strip()
        if (
            not 1 <= len(element) <= 3
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            return [], None
        params.append({"element": element, "value": float(value)})
    if len({p["element"] for p in params}) != len(params):
        return [], None
    return params, str(unit)


def _skipped_bonds_note(skipped: Any) -> str | None:
    """The correction note naming the bonds a Petersson table had no parameter for.

    ``skipped`` is ARC's ``skipped_components`` (``[{bond, count}]``); ``None``
    when nothing was skipped or the list is not usable.
    """
    if not isinstance(skipped, list):
        return None
    parts = []
    for entry in skipped:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("bond"), str):
            return None
        count = entry.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            return None
        parts.append(f"{entry['bond']} x{count}")
    if not parts:
        return None
    return (
        "Arkane's Petersson table has no parameter for " + ", ".join(parts)
        + "; those bonds contributed nothing to this total, and the components "
        "listed are the bonds it did apply."
    )


_GAS_CONSTANT_J_MOL_K = _thermo_numerics.GAS_CONSTANT_J_MOL_K
_T298_K = _thermo_numerics.T298_K
_nasa_h298_kj_mol = _thermo_numerics.nasa_h298_kj_mol

_ARKANE_COMPOSITE_KEY_RE = re.compile(
    r"^\s*CompositeLevelOfTheory\(\s*freq\s*=\s*LevelOfTheory\((?P<freq>[^()]*)\)\s*,"
    r"\s*energy\s*=\s*LevelOfTheory\((?P<energy>[^()]*)\)\s*\)\s*$",
    re.DOTALL,
)
_ARKANE_KEY_FIELD_RE = re.compile(r"\s*(?P<name>[A-Za-z_]+)\s*=\s*'(?P<value>[^']*)'\s*(?:,|$)")


def _arkane_key_half(body: str) -> dict[str, str] | None:
    """Parse the ``name='value',...`` body of one ``LevelOfTheory(...)`` into a dict, or ``None``."""
    fields: dict[str, str] = {}
    pos = 0
    body = body.strip()
    while pos < len(body):
        m = _ARKANE_KEY_FIELD_RE.match(body, pos)
        if m is None or m.group("name") in fields:
            return None
        fields[m.group("name")] = m.group("value")
        pos = m.end()
    return fields or None


def _arkane_composite_key_halves(matched_arkane_key: Any) -> dict[str, dict[str, str]] | None:
    """``{"freq": {...}, "energy": {...}}`` for a ``CompositeLevelOfTheory(freq=..., energy=...)`` key.

    ``None`` for any other key (a single ``LevelOfTheory``, no key, an unparseable string).
    """
    if not isinstance(matched_arkane_key, str):
        return None
    shape = _ARKANE_COMPOSITE_KEY_RE.match(matched_arkane_key)
    if shape is None:
        return None
    freq, energy = _arkane_key_half(shape.group("freq")), _arkane_key_half(shape.group("energy"))
    if not freq or not energy or not freq.get("method"):
        return None
    return {"freq": freq, "energy": energy}


def _norm_level_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


_ARKANE_KEY_RE = re.compile(r"^\s*LevelOfTheory\((?P<body>.*)\)\s*$", re.DOTALL)
_ARKANE_KEY_SOFTWARE_RE = re.compile(r"(?:^|,)\s*software\s*=\s*'(?P<name>[A-Za-z0-9_.+-]+)'\s*(?=,|$)")


def _arkane_key_software(matched_arkane_key: Any) -> str | None:
    """The ``software='<name>'`` in Arkane's matched database key, or ``None``.

    ARC records the key as the entry's repr, e.g.
    ``LevelOfTheory(method='b3lyp2023',basis='def2tzvp',software='gaussian')``
    (arc/output.py ``matched_arkane_key``). Only a key of that shape with
    exactly one clean, single-quoted ``software`` token counts; anything
    else (no key, a key without software, an unparseable string) is
    ``None``, never a guess.
    """
    if not isinstance(matched_arkane_key, str):
        return None
    shape = _ARKANE_KEY_RE.match(matched_arkane_key)
    if shape is None:
        return None
    names = [m.group("name") for m in _ARKANE_KEY_SOFTWARE_RE.finditer(shape.group("body"))]
    if len(names) != 1 or shape.group("body").count("software") != 1:
        return None
    return names[0]


def _bond_corrections_flag(record: Mapping[str, Any]) -> bool | None:
    """``thermo.bond_corrections_applied`` (output.yml 1.2) when a bool, else None."""
    thermo = record.get("thermo")
    flag = thermo.get("bond_corrections_applied") if isinstance(thermo, Mapping) else None
    return flag if isinstance(flag, bool) else None


def _note_omitted_bac_on_thermo(
    thermo_block: dict[str, Any] | None,
    reasons: list[str],
    bond_corrections_applied: Any = None,
) -> None:
    """Record on the thermo ``note`` that an exported BAC is not deposited.

    Arkane skips bond types missing from its Petersson table and applies the
    rest, so the thermo enthalpy can include a partial BAC while the deposit
    holds no applied correction. ``bond_corrections_applied`` is output.yml
    1.2's ``thermo.bond_corrections_applied``: True asserts Arkane applied it;
    False means nothing is missing from the deposit, so no note; absent (None)
    keeps the inference and words the note as what ARC exported. Appended,
    never overwriting.
    """
    if thermo_block is None or not reasons or bond_corrections_applied is False:
        return
    if reasons[0] == "no_bond_had_parameter":
        text = (
            "ARC exported a Petersson bond additivity correction total for this species, "
            "but no bond had a parameter in Arkane's table, so no applied correction is "
            "deposited.")
        existing = thermo_block.get("note")
        thermo_block["note"] = f"{existing}; {text}" if existing else text
        return
    if bond_corrections_applied is True:
        subject = "A Petersson bond additivity correction was applied by Arkane to this species"
    else:
        subject = "ARC exported a Petersson bond additivity correction total for this species"
    text = (
        f"{subject} but it is not deposited as an applied correction because its "
        f"bond decomposition was incomplete ({reasons[0]})."
    )
    existing = thermo_block.get("note")
    thermo_block["note"] = f"{existing}; {text}" if existing else text


def _component_is_usable(component: Any) -> bool:
    return (
        isinstance(component, Mapping)
        and component.get("parameter_value") is not None
        and component.get("contribution_value") is not None
    )


# kcal/mol. ARC computes a component as ``count * parameter`` and the total from
# Arkane, so a real record agrees to float round-off; this admits only the
# rounding of a hand-copied value, and is far below the contribution of any bond
# a decomposition could be missing.
_BAC_COMPONENT_SUM_TOLERANCE = 1e-3


def _petersson_bac_omission_reason(
    rec: Mapping[str, Any],
    components: Any,
    *,
    target_kind: str,
    element_symbols: Any,
) -> tuple[str, str] | None:
    """``(reason, detail)`` when a Petersson ``bac_total`` must not be sent.

    TCKDB's ``assert_bac_total_has_required_components`` refuses a
    ``bac_petersson`` total with no component of kind ``bond`` when it
    targets a transition state or a species with at least one bond; only a
    monatomic species has an honest componentless total. It does not check
    that the components sum to the total, so a partial decomposition (a
    component missing ``parameter_value``/``contribution_value``) would be
    stored silently. ARC itself drops the whole list when any bond lacks a
    parameter. ``None`` means send it (also for every other role or kind:
    ``bac_melius`` is exempt on TCKDB's side).
    """
    scheme = rec.get("scheme")
    if rec.get("application_role") != "bac_total" or not isinstance(scheme, Mapping):
        return None
    if scheme.get("kind") != "bac_petersson":
        return None
    items = components if isinstance(components, list) else []
    applied_bonds = rec.get("components_are_applied_bonds") is True
    if any(not _component_is_usable(c) for c in items):
        return ("component_unusable",
                "a bond component lacks a parameter or contribution value, so the "
                "decomposition would not sum to the total.")
    has_bond = any(c.get("component_kind") == "bond" for c in items)
    if has_bond:
        if applied_bonds:
            # Output 1.3 states that the components are the bonds Arkane applied
            # and that they sum to the total, so a partial BAC (bonds with no
            # parameter listed as ``skipped_components``) is a complete, exact
            # decomposition of what was applied. TCKDB does not check the sum,
            # so verify what ARC promises before sending it.
            try:
                contributions = sum(float(c["contribution_value"]) for c in items)
                total = float(rec["value"])
            except (TypeError, ValueError, KeyError):
                contributions = total = math.nan
            if not math.isclose(contributions, total, rel_tol=1e-6, abs_tol=_BAC_COMPONENT_SUM_TOLERANCE):
                return ("components_do_not_sum",
                        f"the bond components sum to {contributions!r} but the total is "
                        f"{rec.get('value')!r}, although ARC states that they sum to it.")
        return None
    monatomic = (
        target_kind == "species"
        and isinstance(element_symbols, (list, tuple))
        and len(element_symbols) == 1
    )
    if monatomic:
        return None
    if not items:
        if applied_bonds:
            return ("no_bond_had_parameter",
                    "the record lists no bond Arkane applied (every bond of the "
                    "species is in skipped_components, or none is listed).")
        return ("no_components",
                "the record carries no bond components (ARC drops them all when a "
                "bond has no parameter in Arkane's Petersson table).")
    return ("no_bond_component", "the record carries no component of kind 'bond'.")


_W_ENTHALPY_ATOM_CORRECTIONS_LEVEL_MISMATCH = ArcWarning.ENTHALPY_ATOM_CORRECTIONS_LEVEL_MISMATCH.value
_W_ENTHALPY_ATOM_CORRECTIONS_LEVEL_UNVERIFIABLE = ArcWarning.ENTHALPY_ATOM_CORRECTIONS_LEVEL_UNVERIFIABLE.value

# ARC's Arkane key match and data/AEC.yml lookup read only the method
# string and basis, ignoring these level fields, so when either level sets
# one, a matching ``atom_corrections_level`` cannot show that the applied
# atom energies carried it (ARC applies plain B3LYP atom energies to
# B3LYP + GD3BJ, gas-phase ones to SMD).
_LEVEL_FIELDS_ARC_MATCHING_IGNORES = ("dispersion", "solvation_method")

# Magnitude backstop, and the first check for thermo whose flag is null or
# absent (pre-1.2 output, species Arkane loaded from its own YAML). Raw
# absolute energies are about -1e5 kJ/mol per heavy atom, while the largest
# real |ΔHf| are O(10^3-10^4) kJ/mol, so anything beyond this bound cannot
# be a formation enthalpy. It misses every species whose raw total energy
# is below about 7.6 hartree (2e4 kJ/mol): H, H2, He, the Li atom.
_FORMATION_ENTHALPY_MAX_ABS_KJ_MOL = 2.0e4
_W_ENTHALPY_NOT_FORMATION_MAGNITUDE = ArcWarning.ENTHALPY_NOT_FORMATION_MAGNITUDE.value
# Interim checks for a null or absent flag, until every run writes output.yml
# 1.2's ``atom_corrections_applied`` (ARC PR #1059), which supersedes them.
#
# Declaring ``formation_298k`` on output without the 1.2 flags is a
# deliberate, maintainer-approved interpretation, not a
# default filled in for a convention ARC left unstated. Arkane's corrected
# H298 (and the NASA fit and point H/G built from it) is a formation
# enthalpy by construction: Arkane subtracts the atom energies of the level
# of theory and adds the elements' experimental formation enthalpies. What
# pre-1.2 output does not record is whether that correction ran, and at
# which level. The non-finite check is exact; the magnitude, light-species,
# header-level and dispersion/solvation checks are heuristics that make an
# uncorrected (or wrong-level) enthalpy very unlikely to pass, not proof that
# the correction ran at the right level. A block failing any of them loses
# its enthalpy content. When ARC writes the 1.2 flags, they decide instead.
#
# * The magnitude guard's blind spot. A species whose raw (uncorrected)
#   total energy is below the bound looks like a formation enthalpy either
#   way, so its enthalpy cannot be verified without the flag. The raw
#   magnitude is estimated from the composition as a sum of approximate
#   atomic total energies (hartree). Only H, He and Li are listed: any
#   heavier atom alone (Be is about 14.7 hartree) puts the species past
#   the bound, where the magnitude guard decides.
# * The header ``arkane_level_of_theory``, which ARC 1.1 writes from the
#   level it ran Arkane's atom corrections at (the user's setting, else
#   ``composite_method``, else ``sp_level``; null when none is resolved).
#   It must be the energy level; a null header cannot be checked.
# * Dispersion and solvation fields, as for a true flag: ARC's Arkane
#   matching ignores them, so the plain gas-phase method's atom energies
#   were applied.
_LIGHT_ATOM_TOTAL_ENERGY_HARTREE = {"H": 0.50, "He": 2.90, "Li": 7.43}
_W_ENTHALPY_FORMATION_UNVERIFIABLE_LIGHT_SPECIES = ArcWarning.ENTHALPY_FORMATION_UNVERIFIABLE_LIGHT_SPECIES.value
_HEADER_CORRECTIONS_LEVEL_SOURCE = "output_header.arkane_level_of_theory"


def _is_composite_atom_record(record: Mapping[str, Any]) -> bool:
    """Whether the record is a composite-method run (composite log, or a composite level/method stated)."""
    if record.get("composite_log") or record.get("composite_method"):
        return True
    levels = record.get("levels")
    return isinstance(levels, Mapping) and bool(levels.get("composite"))


def _atom_without_sp_energy_error(record: Mapping[str, Any]) -> ValueError:
    if _is_composite_atom_record(record):
        return ValueError(
            f"{record.get('label')!r} is a single atom of a composite run, so its primary "
            "calculation is its single point, but ARC states no sp_energy_hartree for it "
            "(opt_final_energy_hartree is parsed from the composite log and may be an "
            "intermediate SCF energy, so it is not used); the adapter does "
            "not relabel it as an optimisation, so the species is not built.")
    return ValueError(
        f"{record.get('label')!r} is a single atom, so its primary calculation is its "
        "single point (an atom has no geometry to optimise and TCKDB accepts an sp "
        "primary for it), but ARC states neither sp_energy_hartree nor "
        "opt_final_energy_hartree for it; the adapter does "
        "not relabel it as an optimisation, so the species is not built.")


def _light_species_raw_energy_kj_mol(element_symbols: Any) -> float | None:
    """Estimate a light species' raw total energy magnitude (kJ/mol).

    ``None`` when the composition is unknown or holds any atom heavier than
    Li, whose raw energy alone passes ``_FORMATION_ENTHALPY_MAX_ABS_KJ_MOL``.
    """
    from tckdb_arc._vendor import E_h_kJmol

    if not element_symbols:
        return None
    hartree = 0.0
    for symbol in element_symbols:
        atom_hartree = _LIGHT_ATOM_TOTAL_ENERGY_HARTREE.get(symbol)
        if atom_hartree is None:
            return None
        hartree += atom_hartree
    return hartree * E_h_kJmol


def _level_field_unverifiable_error(
    sides: tuple[tuple[str, Any], ...],
    matched: str,
) -> tuple[str, str] | None:
    """Refuse a level that sets a field ARC's atom-energy matching ignores.

    ``sides`` names each level checked; ``matched`` says which level the
    atom energies were matched to, for the message.
    """
    for side, level in sides:
        if not isinstance(level, Mapping):
            continue
        for field in _LEVEL_FIELDS_ARC_MATCHING_IGNORES:
            if level.get(field):
                return (
                    _W_ENTHALPY_ATOM_CORRECTIONS_LEVEL_UNVERIFIABLE,
                    f"The {side} {_describe_level(level)} sets "
                    f"{field}={level[field]!r}, which ARC's atom-energy matching "
                    f"ignores, so {matched} does not show that the "
                    f"subtracted atom energies carried it.",
                )
    return None


def _unflagged_enthalpy_error(
    energy_level: Mapping[str, Any] | None,
    header_corrections_level: Any,
    element_symbols: Any,
) -> tuple[str, str, dict[str, str]] | None:
    """The interim checks for a null or absent ``atom_corrections_applied``.

    See ``_LIGHT_ATOM_TOTAL_ENERGY_HARTREE`` for why each exists.
    """
    header_key = _level_identity(header_corrections_level)
    context = {
        "atom_corrections_applied": "not_recorded",
        "corrections_level_source": (
            _HEADER_CORRECTIONS_LEVEL_SOURCE if header_key is not None else "not_recorded"
        ),
    }
    if header_key is not None and header_key != _level_identity(energy_level):
        return (
            _W_ENTHALPY_ATOM_CORRECTIONS_LEVEL_MISMATCH,
            f"ARC ran Arkane's atom corrections at arkane_level_of_theory "
            f"{_describe_level(header_corrections_level)}, but the energies were "
            f"computed at {_describe_level(energy_level)}, so the enthalpies mix "
            f"two levels and are not formation enthalpies.",
            context,
        )
    unverifiable = _level_field_unverifiable_error(
        (("energy level", energy_level), ("arkane_level_of_theory", header_corrections_level)),
        matched="the recorded level",
    )
    if unverifiable is not None:
        return (*unverifiable, context)
    raw_kj_mol = _light_species_raw_energy_kj_mol(element_symbols)
    if raw_kj_mol is not None and raw_kj_mol < _FORMATION_ENTHALPY_MAX_ABS_KJ_MOL:
        return (
            _W_ENTHALPY_FORMATION_UNVERIFIABLE_LIGHT_SPECIES,
            f"ARC did not record whether Arkane applied atom-energy corrections, "
            f"and this {_formula(element_symbols)} species' raw total energy "
            f"(about {raw_kj_mol:.3g} kJ/mol) is within the "
            f"{_FORMATION_ENTHALPY_MAX_ABS_KJ_MOL:.6g} kJ/mol magnitude bound, so "
            f"an uncorrected enthalpy would pass as a formation enthalpy.",
            context,
        )
    return None


def _has_enthalpy_content(block: Mapping[str, Any]) -> bool:
    return "h298_kj_mol" in block or "nasa" in block or any(
        "h_kj_mol" in p or "g_kj_mol" in p for p in block.get("points", ())
    )


def _strip_enthalpy_content(block: dict[str, Any]) -> None:
    """Remove H298, the NASA fit, and point H/G; keep S298, point S and Cp.

    A point left with only its temperature is dropped (TCKDB requires a
    property on every point).
    """
    block.pop("h298_kj_mol", None)
    block.pop("nasa", None)
    points = [
        {k: v for k, v in p.items() if k not in ("h_kj_mol", "g_kj_mol")}
        for p in block.pop("points", ())
    ]
    points = [p for p in points if set(p) - {"temperature_k"}]
    if points:
        block["points"] = points


def _enthalpy_magnitude_error(block: Mapping[str, Any]) -> tuple[str, str] | None:
    """Refuse a block whose enthalpy is too large to be a formation enthalpy.

    Checks ``h298_kj_mol``, every point ``h_kj_mol``, and the NASA fit's
    H at 298.15 K (the only enthalpy a NASA-only block carries).
    """
    enthalpies = [("h298_kj_mol", block.get("h298_kj_mol"))]
    enthalpies += [
        (f"points[T={p['temperature_k']}].h_kj_mol", p.get("h_kj_mol"))
        for p in block.get("points", ())
    ]
    if "nasa" in block:
        enthalpies.append(("nasa H(298.15 K)", _nasa_h298_kj_mol(block["nasa"])))
    for name, value in enthalpies:
        if value is not None and abs(value) > _FORMATION_ENTHALPY_MAX_ABS_KJ_MOL:
            return (
                _W_ENTHALPY_NOT_FORMATION_MAGNITUDE,
                f"{name}={value:.6g} kJ/mol exceeds the "
                f"{_FORMATION_ENTHALPY_MAX_ABS_KJ_MOL:.6g} kJ/mol bound on any "
                f"formation enthalpy, so it cannot be formation_298k. Arkane "
                f"most likely ran without atom-energy corrections for this "
                f"level of theory, leaving raw absolute energies.",
            )
    return None


#: Marks a record whose energies the adapter withheld (see ``_withhold_unreliable_composite_energies``).
_ENERGY_WITHHELD_KEY = "_tckdb_energy_withheld"

_GAUSSIAN16_A03_BANNER = re.compile(r"gaussian\s*16\b.*\brevision\s*a\.?03\b", re.IGNORECASE | re.DOTALL)
_G4_FAMILY_METHOD_KEYS = frozenset({"g4", "g4mp2"})


def _g4_loader_energy_unreliable(
    output_doc: Mapping[str, Any], record: Mapping[str, Any],
) -> tuple[str, str] | None:
    """``(method, banner)`` when the record's energy is one Arkane's loader mis-reads, else ``None``.

    Stopgap for the G4 / G4MP2 energy of a Gaussian 16 Revision A.03 composite run: ARC reads
    the energy from the number after the ``G4(0 K)`` / ``G4MP2(0 K)`` label (Arkane's
    ``load_energy``, and ARC's parser alike), but on that revision the summary table is shifted
    by one label, so the number there is the 298 K value and ARC's ``sp_energy_hartree`` is
    wrong by 9-11 kJ/mol. The true E0 is the archive ``\\G4=`` / ``\\G4MP2=`` value, which
    ARC does not export (ARC_TCKDB_EXPORT_BRIEF.md, Bug 8).

    The record must be a composite run (``composite_log``) whose composite method (the record's
    ``levels.composite``, else the header ``composite_method``) is G4 or G4MP2 and whose
    composite program banner (``ess_versions.composite``) is Gaussian 16 Revision A.03. Nothing
    is inferred: a missing banner or method is "not this case".
    """
    if not record.get("composite_log"):
        return None
    versions = record.get("ess_versions")
    banner = versions.get("composite") if isinstance(versions, Mapping) else None
    if not isinstance(banner, str) or _GAUSSIAN16_A03_BANNER.search(banner) is None:
        return None
    level = None
    levels = arc13.levels_of(record)
    if levels is not None and isinstance(levels.get("composite"), Mapping):
        level = levels["composite"]
    if level is None or not level.get("method"):
        header = output_doc.get("composite_method")
        level = header if isinstance(header, Mapping) else None
    if level is None or not isinstance(level.get("method"), str):
        return None
    key = method_identity_key(level["method"])
    if key not in _G4_FAMILY_METHOD_KEYS:
        return None
    return key, banner


def _with_composite_role(
    record: Mapping[str, Any], calc_keys_by_role: Mapping[str, str],
) -> dict[str, str]:
    """``calc_keys_by_role`` plus the ``composite`` role for a schema-1.3 composite run.

    On a composite run (``composite_log`` stated) the composite job is what
    produced the record's geometry, so the ``opt`` calculation, filed at the
    composite level with the composite program, is the composite-method
    calculation; thermo and statmech link it under the ``composite`` role as well
    as ``opt``. Unchanged for any other record.
    """
    out = dict(calc_keys_by_role)
    if record.get(_ENERGY_WITHHELD_KEY):
        return out      # the composite job is the energy source, and that energy is withheld
    if (arc13.software_job_key(record, "opt") == "composite"
            and arc13.is_composite_run(record) and out.get(_CALC_KEY_OPT)):
        out[_ROLE_COMPOSITE] = out[_CALC_KEY_OPT]
    return out
