"""TCKDB route endpoints, payload kinds and enum values shared by every producer.

These name the routes of the TCKDB upload contract and the retry policy of the
readiness probe; none of it depends on which tool produced the data.
"""

from tckdb_schemas.enums import ArtifactKind
from tckdb_schemas.fragments.artifact import KIND_ALLOWED_EXTENSIONS
from tckdb_schemas.fragments.calculation_origin import CalculationOriginKind

CONFORMER_UPLOAD_ENDPOINT = "/uploads/conformers"
PAYLOAD_KIND = "conformer_calculation"

ARTIFACTS_ENDPOINT_TEMPLATE = "/calculations/{calculation_id}/artifacts"

# Computed-species bundle endpoint. One self-contained payload that
# carries species_entry + conformers + calcs + artifacts + thermo, with
# all cross-references expressed as local string keys (no DB ids).
COMPUTED_SPECIES_ENDPOINT = "/uploads/computed-species"
COMPUTED_SPECIES_KIND = "computed_species"

# Computed-reaction bundle endpoint. Like computed-species but adds
# reactant/product species blocks, an inline transition_state, and a
# modified-Arrhenius kinetics block with producer-declared
# source_calculations.
COMPUTED_REACTION_ENDPOINT = "/uploads/computed-reaction"
COMPUTED_REACTION_KIND = "computed_reaction"

# Standalone transition-state endpoint. One payload per converged TS,
# carrying the embedded reaction (reactants/products by identity, family,
# reversibility), the saddle-point geometry, a required primary opt, and
# optional freq/sp/irc/path_search calculations. Unlike the computed-
# reaction bundle, no species minima or kinetics travel with it — the TS
# is the record. See ``submit_computed_ts_from_output``.
TRANSITION_STATE_ENDPOINT = "/uploads/transition-states"
TRANSITION_STATE_KIND = "transition_state"

# Standalone record routes. Each takes one request describing one record of its
# kind (the species or reaction it belongs to is named by identity inside the
# request, never by a database id). The core transports and validates a request a
# producer built; it builds none of them. See ``tckdb_core.standalone``.
THERMO_ENDPOINT = "/uploads/thermo"
THERMO_KIND = "thermo"

KINETICS_ENDPOINT = "/uploads/kinetics"
KINETICS_KIND = "kinetics"

STATMECH_ENDPOINT = "/uploads/statmech"
STATMECH_KIND = "statmech"

TRANSPORT_ENDPOINT = "/uploads/transport"
TRANSPORT_KIND = "transport"

REACTION_ENDPOINT = "/uploads/reactions"
REACTION_KIND = "reaction"

NETWORK_ENDPOINT = "/uploads/networks"
NETWORK_KIND = "network"

NETWORK_PDEP_ENDPOINT = "/uploads/networks/pdep"
NETWORK_PDEP_KIND = "network_pdep"

# Readiness-probe (/readyz) retry policy. During a long producer run the TCKDB
# server can be briefly not-ready (restart, rolling deploy, momentary
# load). Rather than fail the in-run upload on the first blip, the
# preflight probe retries with exponential backoff before giving up.
#
# PREFLIGHT_MAX_ATTEMPTS counts the initial probe plus retries (5 → 1
# initial + 4 retries). Delays follow PREFLIGHT_BASE_DELAY_SECONDS * 2**i
# (1s, 2s, 4s, 8s) capped at PREFLIGHT_MAX_DELAY_SECONDS, so the backoff
# gaps alone add ~1+2+4+8 = 15s. That is only the sleep budget: each probe
# can itself block for the client request timeout (config
# ``timeout_seconds``, 30s by default), and on a connect timeout httpx
# tries every resolved address of the host in turn, each for that full
# timeout. The worst case is therefore roughly
#     PREFLIGHT_MAX_ATTEMPTS * n_addresses * timeout_seconds + 15s
# e.g. 5 * 2 * 30s + 15s ~= 5.25 min for a host with two A records whose
# packets are silently dropped (observed from an offline compute node).
# A server that refuses the connection outright fails fast and costs
# close to the 15s backoff alone. Tunable here.
PREFLIGHT_MAX_ATTEMPTS = 5
PREFLIGHT_BASE_DELAY_SECONDS = 1.0

PREFLIGHT_MAX_DELAY_SECONDS = 8.0

# TCKDB's ``CalculationWithResultsPayload.origin_kind`` is a *validated*
# enum. The backend hoists ``parameters_json.tckdb_origin.origin_kind``
# into that top-level field (that is why the ``reused_result`` marker,
# which happens to be a valid member, round-trips cleanly), so any value
# a producer emits for ``origin_kind`` — even nested under the "free-form"
# parameters_json — MUST be one of these. Producer-specific provenance detail
# that is NOT an enum member (e.g. "screened_conformer") is carried on a
# separate ``origin_detail`` key, which the backend leaves opaque.
# The members are tckdb-schemas' own enum, not a copy of it.
VALID_TCKDB_ORIGIN_KINDS: frozenset[str] = frozenset(
    kind.value for kind in CalculationOriginKind
)

# The extensions TCKDB accepts for an ``output_log`` artifact, from
# tckdb-schemas' ``KIND_ALLOWED_EXTENSIONS`` (``.log``, ``.out``, ``.orca``).
# Non-traditional log artifacts (e.g. the GSM ``stringfile.xyz0000``)
# need their declared filename adapted to satisfy the allowlist while
# preserving the original basename for human inspection. The backend
# treats ``filename`` as provenance metadata only — storage is content-
# addressed and the filename does not influence the URI.
OUTPUT_LOG_ALLOWED_EXTS: frozenset[str] = frozenset(
    KIND_ALLOWED_EXTENSIONS[ArtifactKind.output_log]
)
_OUTPUT_LOG_ALLOWED_EXTS = OUTPUT_LOG_ALLOWED_EXTS  # earlier name, kept so existing imports resolve
