"""IRC and path-search result payload builders of the ARC adapter.

The IRC result payload with its direction and endpoint findings, and the path-search result payload of a
NEB or GSM transition-state guess.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tckdb_arc.warning_codes import ArcWarning
from tckdb_core.outcomes import UploadOutcome
from tckdb_arc.self_check import _self_check
from tckdb_arc.geometry import _normalize_xyz_text
from tckdb_arc.levels import _restart_levels
from tckdb_arc.path_search import _IRC_DIRECTION_FORWARD, _IRC_DIRECTION_REVERSE
from tckdb_arc._logging import get_logger

logger = get_logger()


# Per-method static properties of the path-search algorithm itself.
# These are intrinsic to the *method*, not the run — both ARC-supported
# adapters (orca_neb, xtb_gsm) are double-ended path-search methods that
# consume two endpoint geometries (reactant + product). Single-ended
# methods (growing-string, freezing-string) would set is_double_ended
# to False and source_endpoint_count to 1; not currently exposed by
# any ARC adapter, so absent here.
_PATH_SEARCH_METHOD_PROPERTIES: dict[str, dict[str, Any]] = {
    "neb": {"is_double_ended": True, "source_endpoint_count": 2},
    "gsm": {"is_double_ended": True, "source_endpoint_count": 2},
}

# Thermochemical kcal → kJ. ``molecularGSM`` stringfile comment-line
# energies are relative energies in kcal/mol (first node = 0.0), so the
# stringfile fallback below converts with this factor. (Node-output
# energies, when preserved, are absolute Hartrees and use ``E_h_kJmol``
# instead — see the primary energy path in
# ``_build_path_search_result_payload``.)
_KCAL_MOL_TO_KJ_MOL = 4.184
# A GSM stringfile whose relative-energy column spans less than this
# (kcal/mol) is treated as the molecularGSM "no energy emitted" sentinel
# (the entire column is ``0.000000`` in ARC's current xtb_gsm build)
# rather than a real, physically-flat profile — so we leave relative
# energies null instead of uploading a fabricated all-zero profile.
_GSM_STRINGFILE_ENERGY_EPS = 1e-6


_W_IRC_ENDPOINT_SPECIES_SKIPPED = ArcWarning.IRC_ENDPOINT_SPECIES_SKIPPED.value


def _irc_endpoint_skip(
    output_doc: Mapping[str, Any], species_record: Mapping[str, Any],
) -> "UploadOutcome | None":
    """A ``skipped`` outcome when the species is one of ARC's IRC endpoints.

    Output.yml 1.3 marks them (``irc_endpoint_of``).
    Older output writes no marker, but ``restart.yml`` records ``irc_label``
    (see ``RestartInfo.irc_endpoint_ts``); without ``restart.yml`` or the
    species' entry there is no answer and the species is treated as an
    ordinary one. These species only ever get an opt
    (ARC skips their freq and sp and computes no thermo), and are not wells.
    """
    label = species_record.get("label")
    if "irc_endpoint_of" in species_record:
        # Output.yml 1.3 marks them itself (``irc_endpoint_of``: the TS label for
        # an endpoint species, ``null`` for every ordinary one). That statement is
        # authoritative both ways, so restart.yml is not consulted.
        ts_label = species_record.get("irc_endpoint_of")
        if not isinstance(ts_label, str) or not ts_label:
            return None
        source = "output.yml irc_endpoint_of"
        direction = species_record.get("irc_endpoint_direction")
        if direction in ("forward", "reverse"):
            source += f", {direction} IRC job"
    else:
        restart = _restart_levels(output_doc)
        if restart is None:
            return None
        ts_labels = {
            str(r.get("label")) for r in (output_doc.get("transition_states") or [])
            if isinstance(r, Mapping) and r.get("label")
        }
        ts_label = restart.irc_endpoint_ts(label, ts_labels)
        if ts_label is None:
            return None
        source = "restart.yml irc_label"
    message = (
        f"{label!r} is an IRC endpoint of {ts_label!r} ({source}), "
        "not a stationary species of the run, so it is not uploaded."
    )
    logger.warning("TCKDB %s: %s", _W_IRC_ENDPOINT_SPECIES_SKIPPED, message)
    return UploadOutcome(
        status="skipped", payload_path=None, sidecar_path=None, idempotency_key="",
        warnings=[_self_check(
            _W_IRC_ENDPOINT_SPECIES_SKIPPED, message, "species",
            {"action": "species_skipped", "ts_label": ts_label})],
    )


_IRC_DIRECTION_BOTH = "both"


_W_IRC_DIRECTION_NOT_STATED = ArcWarning.IRC_DIRECTION_NOT_STATED.value


def _build_irc_result_payload(
    trajectories: list[dict[str, Any]],
    zero_energy_reference_hartree: float | None = None,
    ts_marker: Mapping[str, Any] | None = None,
    *,
    warnings: list[dict[str, Any]] | None = None,
    warning_field: str = "transition_state.irc_result",
) -> dict[str, Any] | None:
    """Build a TCKDB ``IRCResultPayload``-shaped dict from parsed trajectories.

    Each trajectory is a
    ``{"direction": str|None,
       "rich_points": [<rich-point dict>, ...] | None,
       "geom_points": [<xyz_dict>, ...] | None}``
    as produced by :meth:`TCKDBAdapter._parse_irc_trajectories`. When
    ``rich_points`` is populated, per-point ``electronic_energy_hartree``,
    ``reaction_coordinate``, ``max_gradient``, ``rms_gradient``, and (when
    Gaussian provides them) per-point ``direction`` flow through. When
    only ``geom_points`` is available the result reduces to the original
    geometry-only IRC payload behavior — energies/gradients are simply
    omitted, the schema makes them optional.

    ``point_index`` is allocated globally across both branches so the
    server-side uniqueness invariant holds. Direction labels are passed
    straight through — forward/reverse are ESS path labels, not
    reactant/product designators.

    ``zero_energy_reference_hartree`` is the TS reference energy at the
    IRC level of theory, resolved by
    :func:`_resolve_irc_zero_energy_reference`. When non-null, per-point
    ``relative_energy_kj_mol`` is computed against it and the result-
    level field is stamped on the payload. When null, both stay absent
    rather than fabricated.

    ``ts_marker`` is an optional dict
    ``{"xyz_text": <normalized xyz>, "electronic_energy_hartree": float|None}``
    representing the *seed* TS of the IRC path — the optimized saddle
    that ARC handed to the IRC job. When provided, a single synthesized
    point is appended after all trajectory points with ``is_ts=True``,
    ``reaction_coordinate=0.0`` (the TS is the origin by definition),
    ``relative_energy_kj_mol=0.0`` (when ``zero_energy_reference_hartree``
    is set), and the supplied geometry/energy. The result's
    ``ts_point_index`` is then set to this synthesized point's index.

    This is distinct from inferring the TS from per-point trajectory
    data (which ARC's parsers don't reliably do): the TS marker is
    ARC's own seed for the IRC, not a derivative of the parsed log.
    Ordering: the marker is *appended* rather than inserted between
    branches, so existing trajectory indices stay stable and ARC's
    ESS-emitted trajectory order is not reshuffled.

    ``direction`` is required by ``IRCResultPayload``. When any point's
    direction is unknown (no ``irc_log_directions`` entry, no per-point
    direction from the parser, no filename naming one), ARC's output does
    not state the run mode, so no result is built: ``None`` is returned and
    ``irc_direction_not_stated`` is appended to ``warnings`` under
    ``warning_field`` (the caller keeps the ``irc`` calculation itself).
    """
    if not trajectories:
        return None
    # Vendored xyz formatter + Hartree->kJ/mol constant (no ARC
    # dependency). Same constant ``path_search_result`` uses below, so
    # the two ``relative_energy_kj_mol`` fields agree bit-for-bit rather
    # than differing by the ~7e-8 relative amount a second, independently
    # rounded inline literal introduced.
    from tckdb_arc._vendor import E_h_kJmol, xyz_to_str

    points: list[dict[str, Any]] = []
    has_forward = False
    has_reverse = False
    # Trajectory points whose branch no source states.
    unstated_points = 0
    for traj in trajectories:
        traj_direction = traj.get("direction")
        rich_points = traj.get("rich_points") or []
        geom_points = traj.get("geom_points") or []
        # Rich points carry per-point direction (Gaussian's FORWARD/
        # REVERSE announcement). Geometry-only points inherit the
        # trajectory-level direction resolved upstream from the
        # scheduler list / filename heuristic.
        if rich_points:
            iter_records = (
                {
                    "xyz": rp.get("xyz"),
                    "geometry_xyz_text": rp.get("geometry_xyz_text"),
                    "direction": rp.get("direction") or traj_direction,
                    "electronic_energy_hartree": rp.get("electronic_energy_hartree"),
                    "reaction_coordinate": rp.get("reaction_coordinate"),
                    "max_gradient": rp.get("max_gradient"),
                    "rms_gradient": rp.get("rms_gradient"),
                }
                for rp in rich_points
            )
        else:
            iter_records = (
                {
                    "xyz": xyz,
                    "geometry_xyz_text": None,
                    "direction": traj_direction,
                    "electronic_energy_hartree": None,
                    "reaction_coordinate": None,
                    "max_gradient": None,
                    "rms_gradient": None,
                }
                for xyz in geom_points
            )

        for record in iter_records:
            direction = record["direction"]
            if direction == _IRC_DIRECTION_FORWARD:
                has_forward = True
            elif direction == _IRC_DIRECTION_REVERSE:
                has_reverse = True
            else:
                unstated_points += 1
            point: dict[str, Any] = {"point_index": len(points)}
            if direction in (_IRC_DIRECTION_FORWARD, _IRC_DIRECTION_REVERSE):
                point["direction"] = direction
            xyz_dict = record["xyz"]
            normalized = record.get("geometry_xyz_text")
            if normalized is None and xyz_dict is not None:
                try:
                    xyz_str = xyz_to_str(xyz_dict=xyz_dict)
                except Exception as exc:
                    logger.debug(
                        "TCKDB computed-reaction: IRC point xyz_to_str failed: %s",
                        exc,
                    )
                    xyz_str = None
                normalized = _normalize_xyz_text(xyz_str, None)
            if normalized:
                point["geometry"] = {"xyz_text": normalized}
            energy = record["electronic_energy_hartree"]
            if energy is not None:
                point["electronic_energy_hartree"] = float(energy)
                if zero_energy_reference_hartree is not None:
                    point["relative_energy_kj_mol"] = (
                        (float(energy) - zero_energy_reference_hartree)
                        * E_h_kJmol
                    )
            rc = record["reaction_coordinate"]
            if rc is not None:
                point["reaction_coordinate"] = float(rc)
            max_grad = record["max_gradient"]
            if max_grad is not None:
                point["max_gradient"] = float(max_grad)
            rms_grad = record["rms_gradient"]
            if rms_grad is not None:
                point["rms_gradient"] = float(rms_grad)
            points.append(point)
    if not points:
        return None
    if unstated_points:
        # A branch whose direction no source states. ARC runs every IRC job
        # in one direction (arc/job/adapters/common.py refuses an irc job
        # without 'forward' or 'reverse') and current output records it as
        # ``irc_log_directions``; without that (older runs pad the list
        # with None), a parsed per-point direction or a filename naming
        # one, which branch those points lie on is unknown. The run mode
        # (``direction``, required) and ``has_forward``/``has_reverse``
        # would both be guesses, so refuse the block rather than claim
        # ``both`` or file the unlabelled points under the other branch.
        message = (
            f"{unstated_points} of {len(points)} IRC point(s) from the "
            f"{len(trajectories)} IRC log(s) have no stated direction (no "
            f"irc_log_directions entry, no per-point direction, no forward/"
            f"reverse filename), and IRCResultPayload.direction is required, "
            f"so the IRC result is omitted; the irc calculation is still sent."
        )
        logger.warning("TCKDB %s: %s: %s", warning_field, _W_IRC_DIRECTION_NOT_STATED, message)
        if warnings is not None:
            warnings.append(_self_check(
                _W_IRC_DIRECTION_NOT_STATED, message, warning_field,
                {"action": "irc_result_omitted"}))
        return None
    if has_forward and has_reverse:
        overall = _IRC_DIRECTION_BOTH
    elif has_forward:
        overall = _IRC_DIRECTION_FORWARD
    else:
        overall = _IRC_DIRECTION_REVERSE

    # TS marker: synthesized point identifying the optimized saddle the
    # IRC was seeded from. ``direction`` is intentionally omitted (the
    # schema treats null direction as the TS marker); ``is_ts=True``
    # and a matching ``ts_point_index`` on the result let downstream
    # consumers locate the TS without scanning energies. Only emit
    # when we actually have a TS xyz to attach — the marker is
    # geometry-anchored, not a pure flag.
    ts_point_index: int | None = None
    if ts_marker and ts_marker.get("xyz_text"):
        ts_index = len(points)
        ts_point: dict[str, Any] = {
            "point_index": ts_index,
            "is_ts": True,
            "reaction_coordinate": 0.0,
            "geometry": {"xyz_text": str(ts_marker["xyz_text"])},
        }
        ts_energy = ts_marker.get("electronic_energy_hartree")
        if ts_energy is not None:
            try:
                ts_energy_f = float(ts_energy)
            except (TypeError, ValueError):
                ts_energy_f = None
            if ts_energy_f is not None:
                ts_point["electronic_energy_hartree"] = ts_energy_f
                if zero_energy_reference_hartree is not None:
                    ts_point["relative_energy_kj_mol"] = (
                        (ts_energy_f - zero_energy_reference_hartree)
                        * E_h_kJmol
                    )
        points.append(ts_point)
        ts_point_index = ts_index

    payload: dict[str, Any] = {
        "direction": overall,
        "has_forward": has_forward,
        "has_reverse": has_reverse,
        "point_count": len(points),
        "points": points,
    }
    if ts_point_index is not None:
        payload["ts_point_index"] = ts_point_index
    if zero_energy_reference_hartree is not None:
        payload["zero_energy_reference_hartree"] = float(
            zero_energy_reference_hartree
        )
    return payload


_NEB_TS_GUESS_NOTE = (
    "TS guess: the saddle point ORCA's NEB-TS optimized from the climbing image; "
    "not a path image. It is this calculation's final output geometry."
)
_NEB_GUESS_NOTE = (
    "TS guess: the last geometry in the ORCA log, not identified with a path image. "
    "It is this calculation's final output geometry."
)


def _build_path_search_result_payload(
    *,
    method: str,
    log_path: str | Path | None,
    fallback_xyz_text: str | None,
    node_outputs_dir: str | Path | None = None,
    gsm_evidence: Mapping[str, Any] | None = None,
    evidence_unavailable: bool = False,
) -> dict[str, Any] | None:
    """Build a TCKDB ``PathSearchResultPayload``-shaped dict for the
    chosen TS-guess parent calc.

    The backend requires ``points`` with ``min_length=1`` (see
    ``backend/app/schemas/fragments/calculation.py``), so a bare
    ``{method: ...}`` payload won't validate.

    Strategy by method:

    * ``method == 'gsm'``: parse the GSM stringfile via
      :func:`arc.parser.parser.parse_trajectory` (handles the
      ``stringfile.xyz0000`` multi-frame XYZ format). Each frame
      becomes one point with ``geometry``. ``selected_ts_point_index``
      and the corresponding point's ``is_ts_guess`` flag track the
      middle-ish frame xtb_gsm picks as the TS guess (mirroring
      ``xTBGSMAdapter.process_run``).

    * ``method == 'gsm'`` with ``gsm_evidence`` (ARC's parser-evidence
      sidecar): the sidecar's points, carrying xTB energies and gradient
      norms only where ARC matched a node evaluation to that frame by
      geometry.

    * ``method == 'neb'``: every image of the ORCA band summary
      (:func:`tckdb_arc.orca_neb.parse_orca_neb_log`) becomes a point with
      its distance (Angstrom), energy and force norms (Eh/Bohr); only the
      climbing image's geometry is in the log. No point is marked as the TS
      guess: ARC's guess is the log's last coordinate block (for NEB-TS the
      saddle point refined from the climbing image, not an image), which the
      caller files as the calculation's final output geometry; ``note`` says
      so. ``converged`` is set only from ORCA's convergence banner.

    * Unparseable logs: fall back to a single TS-guess point from
      ``fallback_xyz_text`` (the chosen guess's geometry, also the
      ts_opt's input XYZ), or return ``None`` if no fallback geometry is
      available.

    Returns ``None`` (caller skips the calc) when even the fallback
    can't be built — the conservative gate then leaves ``ts_opt``
    edge-less rather than emitting a path_search calc with no points.
    """
    # Late-imported to keep the adapter's import surface lean when
    # path-search emission isn't exercised.
    from tckdb_arc._vendor import E_h_kJmol, xyz_to_str
    # ARC-only trajectory parsers + Kabsch alignment via the optional boundary.
    # These import cleanly and raise OptionalArcUnavailable only at call time;
    # each call below degrades (GSM sub-payload omitted) when ARC is absent.
    from tckdb_arc._arc_optional import (
        kabsch,
        parse_gsm_stringfile_energies,
        parse_trajectory,
    )

    # Archived ograd ids identify invocations, not final stringfile frames.
    # Only portable evidence supplies verified geometry matches. Raw fallback
    # keeps geometries and stringfile relative energies, never index-attached
    # absolute energies or gradients from node_outputs_dir.
    if gsm_evidence is None and not evidence_unavailable and node_outputs_dir is not None and method == "gsm":
        logger.warning(
            "TCKDB path_search: archived GSM energies/gradients omitted without geometry-matched parser evidence"
        )

    points: list[dict[str, Any]] = []
    selected_index: int | None = None
    evidence_relative_kcal: dict[int, float] = {}

    if method == "gsm" and gsm_evidence is not None:
        selected_index = int(gsm_evidence["selected_source_point_index"])
        for source_point in gsm_evidence["points"]:
            index = int(source_point["source_point_index"])
            point: dict[str, Any] = {
                "point_index": index,
                "geometry": {"xyz_text": source_point["geometry_xyz_text"]},
            }
            if "path_coordinate_angstrom" in source_point:
                point["path_coordinate"] = source_point["path_coordinate_angstrom"]
            if "cumulative_com_superposed_displacement_angstrom" in source_point:
                point["path_coordinate"] = source_point["cumulative_com_superposed_displacement_angstrom"]
            if index == selected_index:
                point["is_ts_guess"] = True
            for evidence_key, payload_key in (
                ("electronic_energy_hartree", "electronic_energy_hartree"),
                ("max_gradient_hartree_per_bohr", "max_gradient"),
                ("rms_gradient_hartree_per_bohr", "rms_gradient"),
            ):
                if evidence_key in source_point:
                    point[payload_key] = source_point[evidence_key]
            if "stringfile_relative_energy_kcal_mol" in source_point:
                evidence_relative_kcal[index] = source_point["stringfile_relative_energy_kcal_mol"]
            points.append(point)

    if method == "gsm" and gsm_evidence is None and not evidence_unavailable and log_path is not None:
        path_str = str(log_path)
        try:
            traj = parse_trajectory(path_str)
        except Exception as exc:
            logger.warning(
                "TCKDB path_search: GSM trajectory parse failed for %s: %s",
                path_str, exc,
            )
            traj = None
        if traj:
            # xtb_gsm.process_run picks the TS guess at the middle
            # frame: ``int((len(traj) - 1) / 2) + 1``. Mirror that
            # here so the selected point matches the geometry that
            # actually became ``ts_opt``'s input.
            selected_index = int((len(traj) - 1) / 2) + 1
            # path_coordinate holds the physical cumulative arc length in
            # Angstrom along the string — the analog of ORCA NEB's "Dist.(Ang.)"
            # the backend's ``path_coordinate`` column stores (a monotonically
            # increasing distance, not a normalized 0-1 fraction). Each node is
            # Kabsch-aligned onto its predecessor before the displacement is
            # summed: GSM does NOT keep every node in a common frame (the
            # reactant endpoint is frequently translated/rotated relative to the
            # interior nodes), so the raw inter-node displacement would be
            # inflated by that pure frame shift. ``kabsch`` returns the aligned
            # root-sum-square displacement; node 0 = 0.0. If alignment fails for
            # any pair, path_coordinate is left null on every point (an honest
            # null beats a mislabeled proxy that would clash with ORCA's Angstrom
            # rows).
            arc_lengths: list[float] | None = None
            if len(traj) >= 2:
                try:
                    arc_lengths = [0.0]
                    for idx in range(1, len(traj)):
                        arc_lengths.append(float(
                            arc_lengths[-1] + kabsch(traj[idx], traj[idx - 1])
                        ))
                except (ValueError, KeyError, TypeError, IndexError):
                    arc_lengths = None
            for i, frame in enumerate(traj):
                try:
                    atom_only = xyz_to_str(frame)
                except Exception:
                    continue
                # TCKDB's ``GeometryPayload.xyz_text`` requires the
                # canonical XYZ-file shape (atom-count header + comment
                # + atom lines). ``xyz_to_str`` emits atom-lines only,
                # so route through ``_normalize_xyz_text`` to add the
                # header — same translation the species-side calcs use.
                xyz_text = _normalize_xyz_text(
                    atom_only, label=f"gsm_point_{i}",
                )
                if not xyz_text:
                    continue
                point: dict[str, Any] = {
                    "point_index": i,
                    "geometry": {"xyz_text": xyz_text},
                }
                if arc_lengths is not None:
                    point["path_coordinate"] = arc_lengths[i]
                if i == selected_index:
                    point["is_ts_guess"] = True
                points.append(point)

    converged: bool | None = None
    climbing_image_index: int | None = None
    note: str | None = None
    if method == "neb" and log_path is not None:
        from tckdb_arc.orca_neb import parse_orca_neb_log

        band = parse_orca_neb_log(log_path)
        if band is not None:
            converged = band["converged"]
            climbing_image_index = band["climbing_image_index"]
            # ARC takes the guess from the log's last coordinate block
            # (``OrcaParser.parse_geometry``), never from a band image: for its
            # NEB-TS runs that is the saddle point optimized from the climbing
            # image. No point is the TS guess; the caller files ARC's guess as
            # this calculation's final output geometry.
            note = _NEB_TS_GUESS_NOTE if band["refines_ts"] else _NEB_GUESS_NOTE
            for image in band["images"]:
                index = image["index"]
                point = {
                    "point_index": index,
                    "electronic_energy_hartree": image["energy_hartree"],
                    "max_force": image["max_force_hartree_per_bohr"],
                    "rms_force": image["rms_force_hartree_per_bohr"],
                }
                if image["distance_angstrom"] is not None:
                    point["path_coordinate"] = image["distance_angstrom"]
                if index == climbing_image_index:
                    point["is_climbing_image"] = True
                if image["atoms"]:
                    xyz_text = _normalize_xyz_text("\n".join(image["atoms"]), label=f"neb_image_{index}")
                    if xyz_text:
                        point["geometry"] = {"xyz_text": xyz_text}
                points.append(point)

    if not points and evidence_unavailable:
        return None
    if not points:
        # Single-point fallback: emit the chosen guess's geometry as
        # one TS-guess point. Honest about scope (lossy: we know one
        # point, not the full path) without faking other points.
        normalized_fallback = _normalize_xyz_text(
            fallback_xyz_text, label="ts_guess_point",
        )
        if not normalized_fallback:
            return None
        points = [{
            "point_index": 0,
            "geometry": {"xyz_text": normalized_fallback},
            "is_ts_guess": True,
        }]
        selected_index = 0

    # Relative-energy convention. Reference every point that carries an
    # absolute electronic energy to the minimum absolute energy present,
    # and leave points without one with no ``relative_energy_kj_mol`` (an
    # explicit gap — never a fabricated value; a null is not a "misleading
    # dip" the way a made-up number would be).
    #
    # Only geometrically attached evidence points have absolute energies.
    # Unmatched points retain missing values; the minimum known energy is a
    # numerical reference, not an inferred reactant assignment.
    energies = [
        p.get("electronic_energy_hartree") for p in points
    ]
    known = [e for e in energies if e is not None]
    zero_e_h: float | None = None
    if known:
        zero_e_h = min(known)
        for p in points:
            e_h = p.get("electronic_energy_hartree")
            if e_h is not None:
                p["relative_energy_kj_mol"] = (e_h - zero_e_h) * E_h_kJmol

    # Fallback relative-energy source: the GSM stringfile's per-node
    # comment column (relative energies in kcal/mol, first node = 0).
    # Only used when absolute per-node Hartrees weren't preserved (no
    # ``zero_e_h`` above) — the stringfile carries *relative* energies
    # only, so ``electronic_energy_hartree`` and
    # ``zero_energy_reference_hartree`` legitimately stay null here.
    #
    # Guard: ARC's current molecularGSM build writes ``0.000000`` for
    # every comment line (verified benchmark-wide), so a column that
    # spans < ``_GSM_STRINGFILE_ENERGY_EPS`` is the "no energy emitted"
    # sentinel — we leave relative energies null rather than upload a
    # fabricated flat-zero profile. When a build *does* emit energies,
    # this populates ``relative_energy_kj_mol`` for every node. The peak
    # node is not marked ``is_climbing_image``: the contract defines that
    # flag for NEB-CI images (string methods ignore it), and the highest
    # node of a string is not evidence that a climbing image was run.
    if zero_e_h is None and not any(e is not None for e in energies) and method == "gsm":
        if gsm_evidence is not None:
            rel_kcal = [evidence_relative_kcal.get(p["point_index"]) for p in points]
            if not rel_kcal or any(value is None for value in rel_kcal):
                rel_kcal = None
        elif log_path is not None:
            try:
                rel_kcal = parse_gsm_stringfile_energies(str(log_path))
            except Exception as exc:  # OptionalArcUnavailable (no [arc]) or parse error
                logger.debug("TCKDB path_search: GSM string-file energies unavailable (%s)", exc)
                rel_kcal = None
        else:
            rel_kcal = None
        if rel_kcal is not None:
            point_indices = [p["point_index"] for p in points]
            # Evidence values were collected in payload point order above;
            # source indices need not be contiguous offsets into that list.
            if point_indices and (gsm_evidence is not None or all(
                0 <= ix < len(rel_kcal) for ix in point_indices
            )):
                vals = rel_kcal if gsm_evidence is not None else [rel_kcal[ix] for ix in point_indices]
                if max(vals) - min(vals) > _GSM_STRINGFILE_ENERGY_EPS:
                    base_kcal = min(vals)
                    for p, v in zip(points, vals):
                        p["relative_energy_kj_mol"] = (
                            (v - base_kcal) * _KCAL_MOL_TO_KJ_MOL
                        )

    # ``converged`` is sent only when the program's own verdict was read (the
    # ORCA NEB banner). ``TSGuess.success`` and ``log_path`` mean only that
    # the output file exists, and ARC archives no GSM stdout, so a GSM
    # string's convergence is not stated.
    payload: dict[str, Any] = {
        "method": method,
        "n_points": len(points),
        "points": points,
    }
    if converged is not None:
        payload["converged"] = converged
    if climbing_image_index is not None:
        payload["climbing_image_index"] = climbing_image_index
    if note is not None:
        payload["note"] = note
    if zero_e_h is not None:
        payload["zero_energy_reference_hartree"] = zero_e_h
    # Static method properties (is_double_ended, source_endpoint_count)
    # — intrinsic to the algorithm, not the run. See
    # ``_PATH_SEARCH_METHOD_PROPERTIES``.
    payload.update(_PATH_SEARCH_METHOD_PROPERTIES.get(method, {}))
    if selected_index is not None and any(
        p["point_index"] == selected_index for p in points
    ):
        payload["selected_ts_point_index"] = selected_index
    return payload
