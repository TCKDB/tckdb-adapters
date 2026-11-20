"""Read the converged band of an ORCA NEB run from its output log.

Pure Python, no ARC import. Parsed from the log:

* the band summary (the last block titled exactly ``PATH SUMMARY``): per image
  the index, ``Dist.(Ang.)`` (absent from some layouts), ``E(Eh)``,
  ``max(|Fp|)`` and ``RMS(Fp)`` (Eh/Bohr, as the block states), and the
  ``<= CI`` marker. The two end-point rows carry the total force, the others
  the force perpendicular to the path. The table must end the way ORCA ends it
  (a blank line, then ``Straight line distance between images``) and hold as
  many images as the run states (``Number of images (incl. end points)``, or
  the input's ``NImages`` plus the two end points); otherwise nothing is read;
* ``converged``: ``True`` only when ORCA printed ``THE NEB OPTIMIZATION HAS
  CONVERGED`` before that summary, otherwise ``None``;
* the climbing image's geometry and full-precision energy (``INFORMATION ABOUT
  SADDLE POINT``), only when its ``Climbing image`` line names the image the
  summary marks;
* ``refines_ts``: whether the run is an NEB-TS variant, which optimizes a
  saddle point from the climbing image after the band. That saddle point is
  not an image; the ``PATH SUMMARY FOR NEB-TS`` block that lists it is not
  read.

The other images' geometries are not in the log. ``<basename>_MEP_trj.xyz`` is
not read: ARC does not retrieve it, and in ORCA 6.1 runs its frame energies
differ from the summary's by up to a few mEh near the climbing image.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"Program Version\s+(\S+)")
_CONVERGED_BANNER = "THE NEB OPTIMIZATION HAS CONVERGED"
_BAND_SUMMARY_TITLE = "PATH SUMMARY"
_TABLE_END = "Straight line distance between images"
_SADDLE_TITLE = "SADDLE POINT (ANGSTROEM)"
_CLIMBING_IMAGE_RE = re.compile(r"Climbing image\s*\.+\s*(\d+)\s*$")
_SADDLE_ENERGY_RE = re.compile(r"Energy\s*\.+\s*(-?\d+\.\d+)\s*Eh\s*$")
_IMAGE_COUNT_RE = re.compile(r"Number of images \(incl\. end points\)\s*\.+\s*(\d+)\s*$")
_INPUT_NIMAGES_RE = re.compile(r"^\|\s*\d+>\s*NImages\s+(\d+)\b", re.IGNORECASE)
_INPUT_NEB_TS_RE = re.compile(r"^\|\s*\d+>\s*!.*\bNEB-TS\b", re.IGNORECASE)


def _float(token: str) -> float:
    return float(token.replace("D", "E").replace("d", "e"))


def _expected_image_count(lines: list[str]) -> int | None:
    """The image count the run states, end points included, or ``None``."""
    for line in lines:
        if m := _IMAGE_COUNT_RE.search(line.strip()):
            return int(m.group(1))
    for line in lines:
        if m := _INPUT_NIMAGES_RE.match(line.strip()):
            return int(m.group(1)) + 2
    return None


def _band_summary(lines: list[str]) -> tuple[int, list[dict[str, Any]]] | None:
    """Return ``(title_line_index, images)`` for the last band ``PATH SUMMARY`` block, or ``None``."""
    starts = [i for i, line in enumerate(lines) if line.strip() == _BAND_SUMMARY_TITLE]
    if not starts:
        logger.debug("ORCA NEB log: no PATH SUMMARY block")
        return None
    start = starts[-1]
    has_distance: bool | None = None
    images: list[dict[str, Any]] = []
    terminated = False
    rows_ended = False
    for line in lines[start + 1:]:
        stripped = line.strip()
        if has_distance is None:
            if stripped.startswith("Image"):
                has_distance = "Dist" in stripped
            continue
        if rows_ended:
            if not stripped:
                continue
            terminated = stripped.startswith(_TABLE_END)
            break
        if not stripped:
            rows_ended = bool(images)
            continue
        body, _, marker = stripped.partition("<=")
        tokens = body.split()
        if not tokens or not tokens[0].isdigit():
            logger.debug("ORCA NEB log: PATH SUMMARY row %r is not an image row", stripped)
            return None
        if len(tokens) != (6 if has_distance else 5):
            logger.debug("ORCA NEB log: PATH SUMMARY row %r has %d columns", stripped, len(tokens))
            return None
        try:
            values = [_float(token) for token in tokens[1:]]
        except ValueError:
            logger.debug("ORCA NEB log: PATH SUMMARY row %r is not numeric", stripped)
            return None
        distance = values.pop(0) if has_distance else None
        energy, _relative_kcal, max_force, rms_force = values
        images.append({
            "index": int(tokens[0]),
            "distance_angstrom": distance,
            "energy_hartree": energy,
            "max_force_hartree_per_bohr": max_force,
            "rms_force_hartree_per_bohr": rms_force,
            "climbing": marker.strip() == "CI",
        })
    if not images or [image["index"] for image in images] != list(range(len(images))):
        logger.debug("ORCA NEB log: PATH SUMMARY image indices are not 0..N-1")
        return None
    if not terminated:
        logger.debug("ORCA NEB log: PATH SUMMARY table is not followed by %r (truncated log?)", _TABLE_END)
        return None
    expected = _expected_image_count(lines[:start])
    if expected is not None and expected != len(images):
        logger.debug("ORCA NEB log: PATH SUMMARY has %d images, the run states %d", len(images), expected)
        return None
    return start, images


def _saddle_point(lines: list[str], after: int, climbing_index: int) -> tuple[float | None, list[str] | None]:
    """Return ``(energy_hartree, atom_lines)`` from the saddle-point block for ``climbing_index``.

    Both are ``None`` unless the block's ``Climbing image`` line names ``climbing_index``.
    """
    stated = None
    energy = None
    for i in range(after, len(lines)):
        stripped = lines[i].strip()
        if stated is None:
            if m := _CLIMBING_IMAGE_RE.match(stripped):
                stated = int(m.group(1))
                if stated != climbing_index:
                    return None, None
            continue
        if energy is None and (m := _SADDLE_ENERGY_RE.match(stripped)):
            energy = _float(m.group(1))
            continue
        if stripped != _SADDLE_TITLE:
            continue
        atoms: list[str] = []
        for line in lines[i + 1:]:
            row = line.strip()
            if not row:
                if atoms:
                    break
                continue
            if set(row) == {"-"}:
                continue
            tokens = row.split()
            if len(tokens) != 4 or not tokens[0].isalpha():
                return energy, None
            try:
                coords = [_float(token) for token in tokens[1:]]
            except ValueError:
                return energy, None
            atoms.append(f"{tokens[0]} {coords[0]:.6f} {coords[1]:.6f} {coords[2]:.6f}")
        return energy, atoms or None
    return energy, None


def parse_orca_neb_log(log_path: str | Path) -> dict[str, Any] | None:
    """Parse an ORCA NEB / NEB-CI / NEB-TS output log.

    Returns ``None`` when the file cannot be read or holds no complete band
    ``PATH SUMMARY``. Otherwise a dict with ``program_version`` (or ``None``),
    ``converged`` (``True`` or ``None``), ``refines_ts``,
    ``climbing_image_index`` (or ``None``) and ``images``: per image ``index``,
    ``distance_angstrom``, ``energy_hartree``, ``max_force_hartree_per_bohr``,
    ``rms_force_hartree_per_bohr``, ``climbing`` and ``atoms`` (atom lines in
    Angstrom for the climbing image when the log prints them, else ``None``).
    """
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        logger.debug("ORCA NEB log %s is not readable", log_path)
        return None
    summary = _band_summary(lines)
    if summary is None:
        return None
    start, images = summary
    climbing = [image["index"] for image in images if image["climbing"]]
    climbing_index = climbing[0] if len(climbing) == 1 else None
    for image in images:
        image["climbing"] = image["index"] == climbing_index
        image["atoms"] = None
    if climbing_index is not None:
        energy, atoms = _saddle_point(lines, start, climbing_index)
        ci = images[climbing_index]
        # The summary prints five decimals; the saddle-point block the same energy in full.
        if energy is not None and abs(energy - ci["energy_hartree"]) <= 0.5e-5 + 1e-9:
            ci["energy_hartree"] = energy
        ci["atoms"] = atoms
    version = next((m.group(1) for line in lines[:200] if (m := _VERSION_RE.search(line))), None)
    converged = True if any(_CONVERGED_BANNER in line for line in lines[:start]) else None
    refines_ts = (any(_INPUT_NEB_TS_RE.match(line.strip()) for line in lines[:start])
                  or any("PATH SUMMARY FOR NEB-TS" in line for line in lines[start:]))
    return {
        "program_version": version,
        "converged": converged,
        "refines_ts": refines_ts,
        "climbing_image_index": climbing_index,
        "images": images,
    }
