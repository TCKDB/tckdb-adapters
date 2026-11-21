"""The sidecar warning dict for one of the ARC adapter's own findings.

``_self_check`` renders a registered warning code, its message, the payload field it concerns and
its context through ``AdapterWarning`` with this adapter's producer tag, so every finding the
adapter records has the same ``{code, message, field, context}`` shape.
"""

from collections.abc import Mapping
from typing import Any

from tckdb_core.adapter_warnings import AdapterWarning


#: Source tag of this adapter's sidecar warnings: ``context.source == "tckdb_arc_self_check"``.
_PRODUCER_TAG = "tckdb_arc"


def _self_check(
    code: str, message: str, field: str | None = None, context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The sidecar dict of one self-check finding, rendered through ``AdapterWarning``.

    ``context`` is the finding's own details (``action``, ids, ...); ``source`` is added
    first. Appending the result to a plain list or to a ``WarningSink`` stores the same
    ``{code, message, field, context}`` dict the sidecar has always held.
    """
    return AdapterWarning(code, message, field, context or {}).to_dict(_PRODUCER_TAG)
