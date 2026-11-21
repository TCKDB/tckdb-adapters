"""Provenance helpers of the ARC adapter.

Output-schema version checks, the species and transition-state indexes of an output document,
and the software releases and data revisions the adapter reads from ARC's output header.
"""

import re
from collections.abc import Mapping
from typing import Any


def _is_output_schema_1_0(output_doc: Mapping[str, Any]) -> bool:
    """Whether the document is ARC output schema 1.0 (keyed on the version alone).

    1.0 wrote thermo Cp points as ``thermo.cp_data`` and the atom-energy table
    as ``parameter_table``; 1.1 renamed them ``thermo_points`` and
    ``reference_atom_energies``. The old names are read only for 1.0 documents.
    """
    return output_doc.get("schema_version") == "1.0"


def _is_output_schema_1_3_or_later(output_doc: Mapping[str, Any]) -> bool:
    """Whether the document is ARC output schema 1.3 or later (keyed on the version alone).

    1.3 states what the Arkane run behind each number actually did: a Petersson
    correction's ``components`` are the bonds it applied (``skipped_components``
    the bonds it could not), a statmech block carries ``arkane_treatment`` and the
    E0 switches, a TS record its frequencies in ESS order and the reaction-
    coordinate index, a reaction its ``reversible`` and the kinetics run's
    ``comment`` / ``atom_corrections_applied`` / ``ts_validation``, and the header
    the ``rmg_database`` identity. A document of an earlier version never states
    these, so they are read from 1.3 documents only.
    """
    version = output_doc.get("schema_version") if isinstance(output_doc, Mapping) else None
    if not isinstance(version, str):
        return False
    try:
        major, minor = (int(part) for part in version.split(".")[:2])
    except ValueError:
        return False
    return (major, minor) >= (1, 3)


def _index_species(output_doc: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Build a label → species record map from ``output_doc['species']``.

    Later occurrences win on collision — that matches the implicit
    contract of ARC's output.yml (one record per label) but lets the
    builder fail loudly when a record is genuinely missing rather than
    silently picking a stale duplicate.
    """
    out: dict[str, Mapping[str, Any]] = {}
    for record in output_doc.get("species") or []:
        if not isinstance(record, Mapping):
            continue
        label = record.get("label") or record.get("original_label")
        if label:
            out[str(label)] = record
    return out


def _index_transition_states(
    output_doc: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    """Build a ts-label → record map from ``output_doc['transition_states']``."""
    out: dict[str, Mapping[str, Any]] = {}
    for record in output_doc.get("transition_states") or []:
        if not isinstance(record, Mapping):
            continue
        label = record.get("label") or record.get("original_label")
        if label:
            out[str(label)] = record
    return out


def _arc_workflow_tool_release(
    output_doc: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Build the ARC ``WorkflowToolReleaseRef``-shaped dict, or ``None``.

    Single source of truth for the ARC release identity used in the
    bundle. Returns ``None`` when neither version nor git commit is
    available (rare — usually at least one is set).
    """
    arc_version = output_doc.get("arc_version")
    arc_git_commit = output_doc.get("arc_git_commit")
    if not (arc_version or arc_git_commit):
        return None
    wt: dict[str, Any] = {"name": "ARC"}
    if arc_version:
        wt["version"] = str(arc_version)
    if arc_git_commit:
        wt["git_commit"] = str(arc_git_commit)
    return wt


def _arkane_workflow_tool_release(
    output_doc: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Arkane as the ``WorkflowToolReleaseRef`` of a table-derived scheme.

    Contract: ``workflow_tool_release`` is the "workflow tool (e.g.
    ARC/Arkane) whose data file was the proximate source, when the scheme was
    looked up from a tool table". Arkane's tables are what ARC looked up, so
    a scheme's identity should include the Arkane build (a later database
    revision changing a parameter would otherwise collide with the stored
    value). Only what ARC recorded: ``arkane_version`` and ``arkane_git_commit``
    (``git_commit`` is 1-40 characters); ``None`` when neither is usable. This
    is not a calculation's software (``calculation_software_is_workflow_tool``)
    and never ARC.
    """
    version = output_doc.get("arkane_version")
    commit = output_doc.get("arkane_git_commit")
    release: dict[str, Any] = {"name": "Arkane"}
    if version and str(version).strip():
        release["version"] = str(version).strip()
    if commit and 1 <= len(str(commit).strip()) <= 40:
        release["git_commit"] = str(commit).strip()
    return release if len(release) > 1 else None


def _sha256_or_none(value: Any) -> str | None:
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else None


# ``EnergyCorrectionSchemeRef.data_revision`` is at most 200 characters (0.62).
_DATA_REVISION_MAX_CHARS = 200


def _rmg_database_data_revision(output_doc: Mapping[str, Any]) -> str | None:
    """The revision of the RMG-database tables Arkane loaded, as a ``data_revision``, or ``None``.

    Output 1.3's header ``rmg_database`` identifies the ``quantum_corrections/
    data.py`` Arkane actually loaded (it need not be the file under ARC's
    ``RMG_DB_PATH``): its SHA-256, and the git ``HEAD`` of its checkout
    (``path_kind: git``) or the conda ``rmgdatabase`` package version
    (``path_kind: package``). tckdb-schemas 0.62's ``data_revision`` is "the
    revision of the *data* that holds the parameter tables, for example the
    RMG-database commit that holds Arkane's atom-energy and BAC tables" and joins
    the scheme's identity, so two Arkane builds that read one revision are one
    scheme. It is the git commit when the path is a git checkout, else the
    package version, else the ``data.py`` digest as a plain hex string (that is
    the exact table; TCKDB reads a 7-64 hex value as a git commit and lower-cases
    it, and keeps anything else as written). Never filled from anything else:
    ``None`` (the scheme then keeps its pre-0.62 identity, which includes the
    Arkane build) when the document is not 1.3, the block is absent, or it states
    no usable commit, version or digest.

    Not stated by ARC, so not checked here: whether the checkout is dirty or the
    tables were overridden (Arkane ``atomEnergies``/BAC overrides). The contract
    asks for a revision "only when the tables really came from that repository
    revision"; ARC's own ``data/AEC.yml`` override is handled separately
    (``_arc_aec_yml_digest``).
    """
    if not _is_output_schema_1_3_or_later(output_doc):
        return None
    identity = output_doc.get("rmg_database")
    if not isinstance(identity, Mapping):
        return None
    kind = identity.get("path_kind")
    commit = identity.get("git_commit")
    commit = commit.strip() if isinstance(commit, str) else ""
    version = identity.get("version")
    version = version.strip() if isinstance(version, str) else ""
    digest = _sha256_or_none(identity.get("quantum_corrections_sha256"))
    if kind == "git" and 1 <= len(commit) <= _DATA_REVISION_MAX_CHARS:
        return commit.lower() if re.fullmatch(r"[0-9a-fA-F]{7,64}", commit) else commit
    if kind == "package" and 1 <= len(version) <= _DATA_REVISION_MAX_CHARS:
        return version
    return digest


def _arc_aec_yml_digest(output_doc: Mapping[str, Any]) -> str | None:
    """Output 1.3's ``arc_aec_yml_sha256`` when it is a SHA-256, else ``None``."""
    if not _is_output_schema_1_3_or_later(output_doc):
        return None
    return _sha256_or_none(output_doc.get("arc_aec_yml_sha256"))


# The scheme kinds whose parameters Arkane reads from the RMG-database tables.
_RMG_DATABASE_SCHEME_KINDS = ("atom_energy", "bac_petersson", "bac_melius")


def _scheme_data_revisions(output_doc: Mapping[str, Any]) -> dict[str, str]:
    """``scheme.data_revision`` per scheme kind from an output 1.3 header.

    Every record ARC exports (``atom_energy``, ``bac_petersson``, ``bac_melius``) is
    computed by Arkane's own correction functions from the RMG-database
    ``quantum_corrections/data.py`` it loaded, so the scheme's data revision is that
    table's (``_rmg_database_data_revision``). A revised table is then a new scheme
    identity, and an unrelated RMG-Py (Arkane) commit no longer
    splits one: the Arkane build stays on ``scheme.workflow_tool_release`` as
    provenance only (tckdb-schemas 0.62: "send the RMG-database commit here, keep
    stamping the tool release").

    The header's ``arc_aec_yml_sha256`` (ARC's own ``data/AEC.yml``, rendered
    as Arkane's ``atomEnergies``) is deliberately not a revision: ARC writes
    no ``atom_energy`` record for energies that came from that file (the record
    is keyed on a ``data.py`` entry), so no exported scheme has it as its source.

    Empty when the document states no database identity.
    """
    revision = _rmg_database_data_revision(output_doc)
    if revision is None:
        return {}
    return {kind: revision for kind in _RMG_DATABASE_SCHEME_KINDS}


def _arc_analysis_software_release(
    output_doc: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Build the Arkane ``SoftwareReleaseRef``-shaped dict, or ``None``.

    ARC runs Arkane for the thermo, statmech and kinetics it reports, so
    Arkane is the post-processing software of those products (their
    ``software_release`` / ``analysis_software_release``), never a
    calculation's ``software_release``, which TCKDB refuses for a
    workflow tool (``calculation_software_is_workflow_tool``). output.yml
    records ``arkane_version`` (e.g. ``4.0.0``) and ``arkane_git_commit``
    (the RMG-Py commit Arkane ran from). The version goes to ``version``;
    the commit stays in ``revision``, where earlier adapters put it. Each
    is sent only when ARC recorded it; ``None`` when neither is.
    """
    arkane_version = output_doc.get("arkane_version")
    arkane_git_commit = output_doc.get("arkane_git_commit")
    if not (arkane_version or arkane_git_commit):
        return None
    release: dict[str, Any] = {"name": "Arkane"}
    if arkane_version:
        release["version"] = str(arkane_version)
    if arkane_git_commit:
        release["revision"] = str(arkane_git_commit)
    return release
