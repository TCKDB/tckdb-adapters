"""Geometry helpers of the ARC adapter: xyz text, isotope lists and element or formula tables.

Normalises the xyz text ARC records, checks it against the isotope list stated for the species,
and builds the geometry block of a payload. These helpers read no calculation or level.
"""

from collections.abc import Mapping
from typing import Any

from tckdb_arc import arc13
from tckdb_core import composition as _composition, isotopes as _isotopes


_xyz_element_symbols = _composition.xyz_element_symbols
_FORMULA_TERM_RE = _composition.FORMULA_TERM_RE
_formula_element_symbols = _composition.formula_element_symbols
_is_single_atom_geometry = _composition.is_single_atom_geometry
_formula = _composition.formula


def _species_element_symbols(record: Mapping[str, Any]) -> tuple[str, ...] | None:
    """A species record's composition: its xyz, else its ``formula``.

    ARC before c8240195 (output.yml 1.0) writes ``xyz: null`` for monoatomic
    species, which skip opt, while still recording ``formula``.
    """
    symbols = _xyz_element_symbols(record.get("xyz"))
    if symbols is None:
        symbols = _formula_element_symbols(record.get("formula"))
    return symbols


def _normalize_xyz_text(xyz: str | None, label: str | None) -> str | None:
    """Convert an ARC atom-only xyz string into TCKDB's standard XYZ format.

    Input shape (what ``xyz_to_str`` emits):
        ``"C 0.0 0.0 0.0\\nH 1.0 0.0 0.0"``
    Output shape (what TCKDB ``GeometryPayload.xyz_text`` expects):
        ``"<n_atoms>\\n<comment>\\n<atom lines>"``

    If the input already has a valid integer atom-count header, it's
    returned untouched. Returns ``None`` for null/empty input — the
    format-translation boundary between ARC's internal convention and
    the TCKDB schema, with no requirement that input be present.
    """
    if not xyz:
        return None
    text = str(xyz).strip()
    if not text:
        return None
    lines = text.splitlines()
    try:
        int(lines[0].strip())
        return text
    except (ValueError, IndexError):
        pass
    return f"{len(lines)}\n{label or ''}\n{text}"


def _require_xyz_text(record: Mapping[str, Any]) -> str:
    """Pull and normalize the species record's reference xyz, raising if absent.

    Wraps :func:`_normalize_xyz_text` for the conformer-level geometry
    case where missing xyz is a fatal error (the bundle requires a
    ``ConformerInBundle.geometry``). For optional per-calc input
    geometries, call ``_normalize_xyz_text`` directly so a missing xyz
    just yields ``None`` and the caller omits the field.
    """
    xyz = record.get("xyz")
    if not xyz:
        raise ValueError(
            f"output.yml record for label={record.get('label')!r} has no xyz; "
            "cannot build geometry payload."
        )
    text = _normalize_xyz_text(xyz, record.get("label"))
    if text is None:
        raise ValueError(
            f"output.yml record for label={record.get('label')!r} has empty xyz."
        )
    _assert_isotopes_reconcile(record)
    return text


def _isotopic_substitutions(record: Mapping[str, Any]) -> dict[int, int] | None:
    """The record's final geometry's isotope substitutions as ARC states them (1.3), else ``None``."""
    if "xyz_isotopes" not in record:
        return None
    text = _normalize_xyz_text(record.get("xyz"), record.get("label"))
    return arc13.geometry_isotope_substitutions(text, record.get("xyz_isotopes"))


def _assert_isotopes_reconcile(record: Mapping[str, Any]) -> None:
    """Refuse a species whose stated geometry isotopes contradict its SMILES (output.yml 1.3).

    TCKDB's ``species_geometry_isotope_mismatch`` refuses a geometry whose isotope
    substitutions differ from those the species entry's SMILES declares (``[2H]``).
    ARC states the geometry's isotope list (``xyz_isotopes``); the adapter sends it
    as stated, and it never rewrites the SMILES to fit nor drops a stated
    substitution to get past the check. When the two disagree, or the SMILES
    declares a substitution the geometry's isotopes cannot confirm, no request is
    built. The rule is :func:`tckdb_core.isotopes.isotope_reconciliation_error`;
    reading ``xyz_isotopes`` and ``smiles`` from the record is the ARC part.
    """
    if "xyz_isotopes" not in record:
        return
    text = _normalize_xyz_text(record.get("xyz"), record.get("label"))
    error = _isotopes.isotope_reconciliation_error(
        record.get("label"),
        declared=arc13.smiles_isotope_multiset(record.get("smiles")),
        substitutions=arc13.geometry_isotope_substitutions(text, record.get("xyz_isotopes")),
        xyz_text=text,
        producer_name="ARC",
        isotopes_field="xyz_isotopes",
    )
    if error is not None:
        raise error


def _geometry_payload(
    record: Mapping[str, Any], xyz_text: str, isotopes: Any, *, key: str | None = None,
) -> dict[str, Any] | None:
    """``{xyz_text[, key][, isotopes]}`` for a geometry of ``record`` whose ARC isotope list is ``isotopes``.

    TCKDB's ``isotopes`` maps a 1-based atom index to a mass number for the
    substituted atoms only (an unlisted atom is at its most abundant isotope), so
    an all-standard list sends none. Nothing is sent for a record without
    output.yml 1.3's ``xyz_isotopes``. For an isotopically substituted record the
    geometry must state the same substitutions as the record's own geometry (the
    species identity carries them); one that states none, or others, is
    ``None``: the caller leaves that geometry out rather than deposit it as an
    unsubstituted one. The rule is :func:`tckdb_core.isotopes.geometry_payload`.
    """
    record_subs = _isotopic_substitutions(record) if "xyz_isotopes" in record else None
    return _isotopes.geometry_payload(
        xyz_text, isotopes,
        isotopes_stated="xyz_isotopes" in record,
        record_xyz_text=_normalize_xyz_text(record.get("xyz"), record.get("label")),
        record_substitutions=record_subs,
        key=key,
    )


def _conformer_isotopes(record: Mapping[str, Any], index: int) -> Any:
    lists = record.get("conformers_isotopes")
    return lists[index] if isinstance(lists, list) and index < len(lists) else None
