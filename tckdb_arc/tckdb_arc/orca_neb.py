"""Read the converged band of an ORCA NEB run from its output log.

Pure Python, no ARC import. Parsed from the log:

* the band summary (the last block titled exactly ``PATH SUMMARY``): per image
  the index, ``Dist.(Ang.)`` (absent from some layouts), ``E(Eh)``,
  ``max(|Fp|)`` and ``RMS(Fp)`` (Eh/Bohr, as the block states), and the
  ``<= CI`` marker;
* ``converged``: ``True`` only when ORCA printed ``THE NEB OPTIMIZATION HAS
  CONVERGED`` before that summary, otherwise ``None``;
* the climbing image's geometry (``SADDLE POINT (ANGSTROEM)``), only when the
  ``Climbing image`` line above it names the image the summary marks.

The ``PATH SUMMARY FOR NEB-TS`` block repeats the band and adds the optimized
TS row; the TS is not an image, so that block is not read. The other images'
geometries are not in the log. ``<basename>_MEP_trj.xyz`` is not read: ARC does
not retrieve it, and in ORCA 6.1 runs its frame energies differ from the
summary's by up to a few mEh near the climbing image.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_VERSION_RE = re.compile(r"Program Version\s+(\S+)")
_CONVERGED_BANNER = "THE NEB OPTIMIZATION HAS CONVERGED"
_BAND_SUMMARY_TITLE = "PATH SUMMARY"
_SADDLE_TITLE = "SADDLE POINT (ANGSTROEM)"
_CLIMBING_IMAGE_RE = re.compile(r"Climbing image\s*\.+\s*(\d+)\s*$")


def _float(token: str) -> float:
    return float(token.replace("D", "E").replace("d", "e"))


def _band_summary(lines: list[str]) -> tuple[int, list[dict[str, Any]]] | None:
    """Return ``(title_line_index, images)`` for the last band ``PATH SUMMARY`` block."""
    starts = [i for i, line in enumerate(lines) if line.strip() == _BAND_SUMMARY_TITLE]
    if not starts:
        return None
    start = starts[-1]
    has_distance: bool | None = None
    images: list[dict[str, Any]] = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if has_distance is None:
            if stripped.startswith("Image"):
                has_distance = "Dist" in stripped
            continue
        if not stripped:
            if images:
                break
            continue
        body, _, marker = stripped.partition("<=")
        tokens = body.split()
        if not tokens or not tokens[0].isdigit():
            break
        if len(tokens) != (6 if has_distance else 5):
            return None
        try:
            values = [_float(token) for token in tokens[1:]]
        except ValueError:
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
        return None
    return start, images


def _saddle_point_atoms(lines: list[str], after: int) -> list[str] | None:
    """Return the atom lines of the first ``SADDLE POINT (ANGSTROEM)`` block after ``after``."""
    for i in range(after, len(lines)):
        if lines[i].strip() != _SADDLE_TITLE:
            continue
        atoms: list[str] = []
        for line in lines[i + 1:]:
            stripped = line.strip()
            if not stripped:
                if atoms:
                    break
                continue
            if set(stripped) == {"-"}:
                continue
            tokens = stripped.split()
            if len(tokens) != 4 or not tokens[0].isalpha():
                return None
            try:
                coords = [_float(token) for token in tokens[1:]]
            except ValueError:
                return None
            atoms.append(f"{tokens[0]} {coords[0]:.6f} {coords[1]:.6f} {coords[2]:.6f}")
        return atoms or None
    return None


def parse_orca_neb_log(log_path: str | Path) -> dict[str, Any] | None:
    """Parse an ORCA NEB / NEB-CI / NEB-TS output log.

    Returns ``None`` when the file cannot be read or holds no well-formed band
    ``PATH SUMMARY``. Otherwise a dict with ``program_version`` (or ``None``),
    ``converged`` (``True`` or ``None``), ``climbing_image_index`` (or
    ``None``) and ``images``: per image ``index``, ``distance_angstrom``,
    ``energy_hartree``, ``max_force_hartree_per_bohr``,
    ``rms_force_hartree_per_bohr``, ``climbing`` and ``atoms`` (atom lines in
    Angstrom for the climbing image when the log prints them, else ``None``).
    """
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
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
    stated = next((m.group(1) for line in lines[start:] if (m := _CLIMBING_IMAGE_RE.match(line.strip()))), None)
    if climbing_index is not None and stated is not None and int(stated) == climbing_index:
        images[climbing_index]["atoms"] = _saddle_point_atoms(lines, start)
    version = next((m.group(1) for line in lines[:200] if (m := _VERSION_RE.search(line))), None)
    converged = True if any(_CONVERGED_BANNER in line for line in lines[:start]) else None
    return {
        "program_version": version,
        "converged": converged,
        "climbing_image_index": climbing_index,
        "images": images,
    }
