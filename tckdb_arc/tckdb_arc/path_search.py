"""IRC and path-search helpers of the ARC adapter.

Resolves the log field and method of a transition-state guess, reads the xTB and GSM node
outputs of a path search, and holds the IRC helpers that do not build a payload: direction,
level and the zero-energy reference.
"""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tckdb_arc import arc13
from tckdb_arc.adaptive import UNDETERMINABLE
from tckdb_arc.calculations import (
    _CALC_KEY_FREQ,
    _CALC_KEY_OPT,
    _CALC_KEY_OPT_COARSE,
    _CALC_KEY_SP,
    _CALC_KEY_TS_GUESS,
)
from tckdb_arc.levels import (
    _adaptive_kind_named,
    _adaptive_level_status,
    _level_keys_match,
    _note_adaptive_omission,
    _resolve_level,
    _restart_levels,
)


# Per-calc record-field map for output_log artifacts. Same convention as
# `_LOG_FIELD_BY_CALC_KEY` in the existing artifact path: the species
# record carries `<job>_log` paths that come straight from
# `arc/output.py::_spc_to_dict`.
_LOG_FIELD_BY_CALC_KEY = {
    _CALC_KEY_OPT: "opt_log",
    _CALC_KEY_OPT_COARSE: "coarse_opt_log",
    _CALC_KEY_FREQ: "freq_log",
    _CALC_KEY_SP: "sp_log",
    # ts_guess role is method-dispatched at lookup time (see
    # ``_resolve_log_field``) because the producing TS-guess adapter
    # determines which record field carries the log path
    # (``neb_log`` for orca_neb, ``gsm_log`` for xtb_gsm). The gate in
    # ``_build_ts_block`` is what decides whether the calc is emitted
    # at all.
}

# ARC's TSGuess.method strings → TCKDB ``path_search_result.method``
# enum value. Lookup is case- and whitespace-insensitive at the call
# site (producer strings are stable identifiers but historically have
# casing variations like ``orca_neb`` vs ``ORCA_NEB`` and module-name
# vs class-name forms ``xtb_gsm`` vs ``xTB-GSM``). Geometry-only
# methods (heuristics, AutoTST, KinBot, GCN, user XYZ) are deliberately
# absent — they have no path-search log to anchor a parent calc.
_TS_GUESS_PATH_SEARCH_METHODS: dict[str, str] = {
    "orca_neb": "neb",
    "xtb_gsm": "gsm",
    "xtb-gsm": "gsm",
}

# Per-method record-field name carrying the path-search log path.
# ``neb_log`` is populated by ``arc/output.py`` from ``paths['neb']``
# (set by the scheduler from ``tsg.log_path`` when orca_neb wins) and
# ``gsm_log`` from ``paths['gsm']`` (xtb_gsm wins). Both fields are
# read by ``_resolve_ts_guess_path_search`` to gate emission of the
# ``ts_guess`` parent calc.
_TS_GUESS_LOG_FIELD_BY_METHOD: dict[str, str] = {
    "neb": "neb_log",
    "gsm": "gsm_log",
}

# Per-method gate for emitting the path-search log as an inline
# ``output_log`` artifact. Gated by what the backend's ESS-signature
# validator accepts: NEB logs are real ORCA output files (pass); GSM
# stringfiles are multi-frame XYZ trajectories (fail signature check —
# backend supports cfour/gaussian/molpro/nwchem/orca/psi4/qchem/turbomole
# only). For GSM, the per-frame geometries already ride inside
# ``path_search_result.points``, so the raw stringfile would be
# redundant provenance even if the backend accepted it.
_TS_GUESS_UPLOAD_LOG_AS_ARTIFACT: dict[str, bool] = {
    "neb": True,
    "gsm": False,
}


def _resolve_ts_guess_path_search(method: object) -> str | None:
    """Return the TCKDB ``path_search_result.method`` enum value for an
    ARC TSGuess.method string, or ``None`` for geometry-only / unknown
    methods.

    Conservative: only matches strings explicitly listed in
    ``_TS_GUESS_PATH_SEARCH_METHODS`` (case- and whitespace-insensitive).
    Non-string and unknown values return ``None`` — the caller falls
    back to geometry-only TS provenance (no parent calc emitted).
    """
    if not isinstance(method, str):
        return None
    return _TS_GUESS_PATH_SEARCH_METHODS.get(method.strip().lower())


def _resolve_ts_guess_path_search_for_record(record: Mapping[str, Any]) -> str | None:
    """Return the TCKDB ``path_search_result.method`` enum for a TS record.

    First tries the chosen guess's primary method (``chosen_ts_method``) — unchanged behaviour.
    When that is geometry-only (e.g. ``gcn``) but a path-search method (``xtb_gsm`` / ``orca_neb``)
    was merged into the chosen guess during ARC's equivalent-guess clustering, recover it from the
    chosen ``ts_guesses`` entry's ``method_sources``. This gates emission of the ``path_search``
    parent calc off the merged provenance rather than the single primary method — it does NOT
    change ARC's TS selection (``chosen_ts_method`` is untouched).

    When several path-search methods merged into the chosen guess, prefer the one whose log field
    (``neb_log`` / ``gsm_log``) is actually populated on the record — the scheduler preserves exactly
    one path source's log — so this gate and the subsequent log lookup agree. Returns ``None`` for
    geometry-only / unknown methods.
    """
    method = _resolve_ts_guess_path_search(record.get("chosen_ts_method"))
    if method is not None:
        return method
    candidates: list[str] = []
    for tsg in (record.get("ts_guesses") or []):
        if isinstance(tsg, Mapping) and tsg.get("chosen"):
            for source in (tsg.get("method_sources") or []):
                resolved = _resolve_ts_guess_path_search(source)
                if resolved is not None and resolved not in candidates:
                    candidates.append(resolved)
            break
    for candidate in candidates:
        log_field = _TS_GUESS_LOG_FIELD_BY_METHOD.get(candidate)
        if log_field and record.get(log_field):
            return candidate
    return candidates[0] if candidates else None


def _resolve_log_field(role: str, record: Mapping[str, Any]) -> str | None:
    """Resolve the species-record field carrying the output-log path for ``role``.

    Ordinary roles dispatch through ``_LOG_FIELD_BY_CALC_KEY``. The
    ``ts_guess`` role is method-dispatched: the field name depends on
    which TS-guess adapter produced the chosen guess (orca_neb →
    ``neb_log``, xtb_gsm → ``gsm_log``). Returns ``None`` when the role
    has no log mapping or the TS-guess method is geometry-only.
    """
    if role == _CALC_KEY_TS_GUESS:
        method_enum = _resolve_ts_guess_path_search_for_record(record)
        if method_enum is None:
            return None
        # Some path-search methods (notably GSM) produce log artifacts
        # the backend's ESS-signature validator rejects. Skip the
        # artifact upload for those — the geometric data still rides
        # inside ``path_search_result.points``.
        if not _TS_GUESS_UPLOAD_LOG_AS_ARTIFACT.get(method_enum, False):
            return None
        return _TS_GUESS_LOG_FIELD_BY_METHOD.get(method_enum)
    return _LOG_FIELD_BY_CALC_KEY.get(role)


# IRC direction labels mirror TCKDB's ``IRCDirection`` enum. They are
# ESS path-direction labels only — the producer does not infer
# reactant/product side from them.
_IRC_DIRECTION_FORWARD = "forward"
_IRC_DIRECTION_REVERSE = "reverse"


def _detect_irc_direction(log_path: str) -> str | None:
    """Detect IRC direction (forward/reverse) from a log filename.

    ARC writes per-direction IRC logs whose filenames carry an explicit
    direction infix. The two patterns seen in production runs:

    - ``..._irc_f.log`` / ``..._irc_r.log``   (compact runtime convention)
    - ``..._forward.log`` / ``..._reverse.log`` (long-form / test fixtures)

    Returns ``None`` if the filename matches neither — caller still
    emits the trajectory but omits the per-point direction. ``None``
    is also the right answer when ARC has consolidated forward+reverse
    into a single log; current ARC adapters don't produce that shape,
    but the caller treats it correctly anyway.
    """
    name = Path(str(log_path)).name.lower()
    if "irc_f" in name or "forward" in name:
        return _IRC_DIRECTION_FORWARD
    if "irc_r" in name or "reverse" in name:
        return _IRC_DIRECTION_REVERSE
    return None


def _irc_level(
    output_doc: Mapping[str, Any], ts_record: Mapping[str, Any],
) -> tuple[Mapping[str, Any] | None, bool]:
    """``(exact IRC level or None, IRC level unattributable)`` for a TS.

    Exact when the adaptive levels name ``irc`` and ``restart.yml`` gives the
    TS's level, else when ``restart.yml`` records ``irc_level``. ``(None,
    False)`` means the level is the unreadable settings default (assumed to be
    ``opt_level``); ``(None, True)`` means the adaptive levels name ``irc``
    and the TS's level cannot be worked out.
    """
    if arc13.has_levels(ts_record):
        # Schema 1.3: ``levels.irc`` (or the one level every ``irc_log_levels``
        # entry shares) is the level the IRC jobs ran at, also under adaptive
        # levels; the header ``irc_level`` is the requested level of the run
        # and stands in only when the record states none. The program is
        # ``ess_software.irc`` (no level here states one). Nothing stated is
        # never assumed to be the opt level.
        stated, level = arc13.irc_recorded_level(ts_record)
        if not stated:
            header = output_doc.get("irc_level")
            level = header if isinstance(header, Mapping) and header.get("method") else None
            stated = level is not None
        if not stated or level is None:
            if _adaptive_kind_named(output_doc, "irc"):
                _note_adaptive_omission(output_doc, "irc", ts_record)
            return None, True
        return _with_observed_irc_program(level, ts_record), False
    if _adaptive_kind_named(output_doc, "irc"):
        status = _adaptive_level_status(output_doc, ts_record, "irc")
        if isinstance(status, Mapping):
            return _with_irc_program(status), False
        if status == UNDETERMINABLE:
            _note_adaptive_omission(output_doc, "irc", ts_record)
            return None, True
    restart = _restart_levels(output_doc)
    if restart is not None and restart.irc_level is not None:
        return _with_irc_program(restart.irc_level), False
    return None, False


# ARC's ``Level.deduce_software(job_type='irc')`` (arc/level.py:413-418, called
# by ``Scheduler.deduce_job_adapter``, arc/scheduler.py:1255) sets the software
# to Gaussian for an IRC whatever software the level dict names or DLPNO/other
# methods would pick: that is why a level's own ``software`` cannot be taken for
# an IRC (there is also no ``ess_software.irc``). It is not unconditional: UMA
# levels return early with ``ase`` (arc/level.py:386-388), and torchani / xtb /
# gfn methods are assigned afterwards (arc/level.py:421-426). Those give no
# stated program, so the IRC is not filed for them.
_IRC_NOT_GAUSSIAN_METHODS = ("uma", "uma-s-1", "uma-s-1p1", "uma-s-1p2", "uma-m-1p1")


def _with_observed_irc_program(
    level: Mapping[str, Any], ts_record: Mapping[str, Any],
) -> Mapping[str, Any]:
    """``level`` with the program ARC observed on the IRC logs (``ess_software.irc``), else none."""
    out = {k: v for k, v in level.items() if k != "software"}
    ess_software = ts_record.get("ess_software")
    observed = ess_software.get("irc") if isinstance(ess_software, Mapping) else None
    if observed:
        out["software"] = str(observed)
    return out


def _with_irc_program(level: Mapping[str, Any]) -> Mapping[str, Any]:
    """``level`` with ARC's IRC program: ``gaussian``, or none where ARC's rule gives another."""
    method = str(level.get("method") or "").lower()
    out = {k: v for k, v in level.items() if k != "software"}
    if not (method in _IRC_NOT_GAUSSIAN_METHODS
            or any(part in method for part in ("torchani", "xtb", "gfn"))):
        out["software"] = "gaussian"
    return out


def _parse_xtb_turbomole_energy_file(path: str | Path) -> float | None:
    """Parse the Turbomole-format ``energy`` file written by ``xtb --grad``.

    Provenance is xTB; ``Turbomole`` here names only the on-disk text
    format xTB emits when invoked with ``--grad`` (the same shape
    Turbomole's gradient driver writes — xTB borrows the layout to
    plug into Turbomole-compatible tooling like ``tm2orca.py``).

    File shape (one cycle per line, terminated by ``$end``)::

        $energy      SCF              SCFKIN            SCFPOT
             1     -28.12345678901   0.0   0.0
        $end

    The relevant value is on the line before ``$end``, second
    whitespace-separated field. Mirrors ``tm2orca.py``'s parse
    convention so the two stay in lockstep. Returns ``None`` for
    missing/malformed files (the caller treats that as
    "no energy known for this node").
    """
    try:
        with open(path) as fh:
            lines = fh.readlines()
        return float(lines[-2].strip().split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _parse_xtb_turbomole_gradient_file(
    path: str | Path,
) -> tuple[float | None, float | None]:
    """Parse the Turbomole-format ``gradient`` file written by ``xtb --grad``
    → ``(max_grad, rms_grad)`` in Hartree/Bohr. Returns ``(None, None)``
    for missing/malformed input.

    Provenance is xTB; ``Turbomole`` names only the on-disk text
    format xTB emits with ``--grad``.

    File shape::

        $grad   cycle = 1  SCF energy = ...
           x  y  z  symbol      ← N coord lines
           ...
           gx  gy  gz           ← N gradient-component lines (D-exponent)
           ...
        $end

    ``max_grad`` is the largest absolute gradient component; ``rms_grad``
    is ``sqrt(mean(g_i^2))`` over all ``3N`` components. Both are
    derived from the *gradient* (``dE/dx``), not the force — schema
    distinguishes the two and the source data is unambiguously the
    gradient (xTB writes its output in Turbomole's gradient layout
    when called with ``--grad``).
    """
    try:
        with open(path) as fh:
            lines = fh.readlines()
        natoms = int((len(lines) - 3) / 2)
        if natoms < 1:
            return None, None
        grad_lines = lines[2 + natoms : 2 + 2 * natoms]
        components: list[float] = []
        for line in grad_lines:
            parts = line.strip().replace("D", "E").split()
            if len(parts) < 3:
                continue
            components.extend(float(p) for p in parts[:3])
        if not components:
            return None, None
        import math
        max_g = max(abs(c) for c in components)
        rms_g = math.sqrt(sum(c * c for c in components) / len(components))
        return max_g, rms_g
    except (OSError, ValueError, IndexError):
        return None, None


# Matches xTB's final total-energy print. xTB emits it twice near the end
# of a ``--grad`` run, in two visually different frames but with the same
# value::
#
#     :: total energy              -9.441927748544 Eh    ::
#     ...
#     | TOTAL ENERGY               -9.441927748544 Eh   |
#
# We accept either (case-insensitive) and take the *last* match so a
# partially written/streamed file still yields the converged value. The
# unit is always Hartree (``Eh``); we assert the ``Eh`` tag is present so a
# stray ``total energy`` line in some other unit can't be misread.
_XTBOUT_TOTAL_ENERGY_RE = re.compile(
    r"total\s+energy\s+(-?\d+\.\d+(?:[eEdD][+-]?\d+)?)\s*Eh",
    re.IGNORECASE,
)


def _parse_xtb_xtbout_energy(path: str | Path) -> float | None:
    """Parse the total electronic energy (Hartree) from an xTB stdout
    (``.xtbout``) file.

    This is the load-bearing per-node energy source for xTB-GSM runs: the
    patched ``ograd`` wrapper copies ``<node>.xtbout`` into
    ``gsm_node_outputs/`` unconditionally, whereas the cleaner Turbomole
    ``.energy`` file depends on a ``tm2orca.py`` rename that frequently
    does not land, so ``.xtbout`` is often the only surviving record of
    the node's energy. The GSM stringfile itself carries no usable
    per-node energy (ARC's molecularGSM build writes ``0.000000`` for
    every node), so this is the sole absolute-energy source.

    Returns ``None`` for a missing/garbled file (no ``total energy ... Eh``
    line) — the caller treats that as "no energy known for this node".
    """
    try:
        # xTB banners carry non-ASCII glyphs (e.g. ``Eh/α``), so decode
        # explicitly as UTF-8 with replacement rather than relying on the
        # locale encoding — on a non-UTF-8 locale a bare ``open()`` would
        # raise ``UnicodeDecodeError`` and, uncaught, abort the whole
        # bundle build. ``ValueError`` (the ``UnicodeDecodeError`` base) is
        # also caught defensively, mirroring the sibling ``.energy`` parser.
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except (OSError, ValueError):
        return None
    matches = _XTBOUT_TOTAL_ENERGY_RE.findall(text)
    if not matches:
        return None
    try:
        # xTB writes fixed-decimal Hartrees; the ``D`` FORTRAN exponent
        # marker is tolerated defensively for atypical builds.
        return float(matches[-1].replace("D", "E").replace("d", "e"))
    except ValueError:
        return None


def _read_gsm_node_outputs(
    outputs_dir: str | Path,
) -> dict[int, dict[str, float]]:
    """Scan a ``gsm_node_outputs/`` directory for preserved per-node data
    written by ``xtb --grad`` (in Turbomole's on-disk format).

    The patched ``ograd`` wrapper writes files named like::

        gsm_node_outputs/
          0000.01.energy        ← xTB-generated, Turbomole-format (optional)
          0000.01.gradient      ← xTB-generated, Turbomole-format (optional)
          0000.01.xtbout        ← xTB stdout (always preserved)
          0000.02.xtbout
          ...

    where the ``0000`` prefix is GSM's iteration-round id and the
    ``.NN`` suffix is the per-round node label (1-based). This helper
    returns ``{label_int: {electronic_energy_hartree[, max_gradient,
    rms_gradient]}}`` for every label whose energy could be recovered.

    Two on-disk sources are consulted, in priority order:

    1. The Turbomole-format ``.energy`` / ``.gradient`` pair (cleaner,
       carries gradient metrics) — *preferred* when present.
    2. The xTB stdout ``.xtbout`` (energy only) — the *reliable
       fallback*. ``ograd`` copies ``.xtbout`` unconditionally, but the
       ``.energy``/``.gradient`` copies depend on a ``tm2orca.py`` rename
       that frequently does not land, so on most real runs ``.xtbout`` is
       the only surviving per-node record. Without this fallback the
       energies exist on disk but never reach the TCKDB payload.

    Nodes whose energy cannot be recovered from either source are omitted;
    the caller decides whether to leave the corresponding point's metadata
    null.

    The ESS provenance for the resulting calculation remains xTB /
    xTB-GSM; ``Turbomole`` names only the on-disk text shape the
    ``.energy``/``.gradient`` files use.
    """
    outputs_dir = Path(outputs_dir)
    if not outputs_dir.is_dir():
        return {}

    def _label_of(path: Path) -> int | None:
        # Filename is ``<round>.<NN>.<ext>`` — pull the trailing ``NN``
        # integer (the per-round node label). ``Path.stem`` drops only the
        # final extension, so ``0000.01.xtbout`` -> stem ``0000.01``.
        try:
            return int(path.stem.split(".")[-1])
        except (ValueError, IndexError):
            return None

    by_label: dict[int, dict[str, float]] = {}
    # Source 1 (preferred): Turbomole-format .energy/.gradient pair.
    for energy_file in sorted(outputs_dir.glob("*.energy")):
        label_int = _label_of(energy_file)
        if label_int is None:
            continue
        e_h = _parse_xtb_turbomole_energy_file(energy_file)
        if e_h is None:
            continue
        node_data: dict[str, float] = {"electronic_energy_hartree": e_h}
        gradient_file = energy_file.with_suffix(".gradient")
        if gradient_file.is_file():
            max_g, rms_g = _parse_xtb_turbomole_gradient_file(gradient_file)
            if max_g is not None:
                node_data["max_gradient"] = max_g
            if rms_g is not None:
                node_data["rms_gradient"] = rms_g
        by_label[label_int] = node_data
    # Source 2 (fallback): xTB stdout .xtbout — energy only, and only for
    # labels not already recovered from a cleaner .energy file above.
    for xtb_file in sorted(outputs_dir.glob("*.xtbout")):
        label_int = _label_of(xtb_file)
        if label_int is None or label_int in by_label:
            continue
        e_h = _parse_xtb_xtbout_energy(xtb_file)
        if e_h is None:
            continue
        by_label[label_int] = {"electronic_energy_hartree": e_h}
    return by_label


def _resolve_irc_zero_energy_reference(
    *,
    output_doc: Mapping[str, Any],
    ts_record: Mapping[str, Any],
    irc_level: Mapping[str, Any] | None = None,
) -> float | None:
    """Pick the TS reference electronic energy for an IRC path.

    The reference must live at the IRC calculation's level, or the relative
    energies it produces would mix two levels of theory. That level is
    ``irc_level`` when known exactly (restart.yml), else the assumed
    ``opt_level`` (see the IRC calculation construction site).

    Resolution order, conservative by design:

    1. ``ts_sp_result.electronic_energy_hartree`` *only if* the project
       SP level matches the IRC level (LoT keys equal).
    2. ``ts_record['opt_final_energy_hartree']`` -- the TS opt's converged
       SCF, at the opt level -- *only if* that matches the IRC level (always,
       when the IRC level is the assumed opt level).
    3. ``None`` -- never fabricate a reference. Downstream consumers
       interpret a null ``zero_energy_reference_hartree`` as "relative
       energies unavailable" and skip the ``relative_energy_kj_mol``
       per-point field accordingly.
    """
    opt_level = _resolve_level(output_doc, "opt", ts_record)
    ref_level = irc_level if irc_level is not None else opt_level
    sp_level = output_doc.get("sp_level")
    sp_level = (
        _resolve_level(output_doc, "sp", ts_record)
        if isinstance(sp_level, Mapping) or arc13.has_levels(ts_record) else None
    )
    sp_energy = ts_record.get("sp_energy_hartree")
    if sp_energy is None:
        sp_energy = ts_record.get("electronic_energy_hartree")
    if sp_energy is not None and _level_keys_match(ref_level, sp_level):
        try:
            return float(sp_energy)
        except (TypeError, ValueError):
            pass
    opt_energy = ts_record.get("opt_final_energy_hartree")
    if opt_energy is not None and (
            irc_level is None or _level_keys_match(ref_level, opt_level)):
        try:
            return float(opt_energy)
        except (TypeError, ValueError):
            pass
    return None
