"""The table of warning codes the shared code emits, and the registry type producers use.

Every self-check finding an adapter writes to a sidecar carries a stable ``code``. Codes are
registered, not scattered: a producer defines its own codes as a :class:`CodedEnum` (each
member is the code string plus a one-line description) and emits them through
:class:`~tckdb_core.adapter_warnings.WarningSink` / :class:`~tckdb_core.adapter_warnings.AdapterWarning`,
so the dict shape is the one the sidecar has always held. Codes emitted by this package's own
code live here (:class:`CoreWarning`); codes that depend on how a producer states its results
stay with the producer.

A member is a ``str``, so ``code == "calculation_ref_not_returned"`` holds and the member
serialises as that string; use ``.value`` where a plain ``str`` is wanted.
"""

from __future__ import annotations

import enum
from collections.abc import Iterator


class CodedEnum(str, enum.Enum):
    """An enum whose members are warning-code strings with a one-line ``description``."""

    description: str

    def __new__(cls, code: str, description: str):
        member = str.__new__(cls, code)
        member._value_ = code
        member.description = description
        return member

    # ``str`` formatting, not ``Enum``'s: ``f"{member}"`` and ``str(member)`` are the code.
    __str__ = str.__str__
    __format__ = str.__format__


class CoreWarning(CodedEnum):
    """Codes emitted by ``tckdb_core`` itself (every producer inherits them)."""

    CALCULATION_REF_NOT_RETURNED = (
        "calculation_ref_not_returned",
        "The upload response carried no `calculation_ref`, so a calculation's artifacts were not "
        "uploaded: TCKDB (0.116) addresses a calculation by its `calc_` ref only and refuses an integer id.",
    )
    PAYLOAD_FAILED_LOCAL_VALIDATION = (
        "payload_failed_local_validation",
        "A request did not match the tckdb-schemas contract for its route, so it was not posted: the payload "
        "file stays on disk and the sidecar is `failed`. This is the adapter's own check, not a server refusal; "
        "`validate_before_upload = false` turns it off.",
    )
    PAYLOAD_LOCAL_VALIDATION_UNAVAILABLE = (
        "payload_local_validation_unavailable",
        "The adapter's own pre-POST contract check raised an error of its own, so the request was posted "
        "unchecked: an internal checker failure is not a verdict on the request.",
    )


def registry(*enums: type[CodedEnum]) -> Iterator[tuple[str, str, str]]:
    """``(code, description, member name)`` for every member of ``enums``, in definition order."""
    for enum_cls in enums:
        for member in enum_cls:
            yield member.value, member.description, member.name
