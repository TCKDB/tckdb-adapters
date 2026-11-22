"""The typed qualifiers of a calculation's level of theory, read from the deck ARC recorded for its job.

A TCKDB level of theory carries four optional typed fields (``pno_truncation``, ``ri_approximation``,
``relativistic_hamiltonian``, ``core_treatment``). They are stated only from the input-deck text ARC records
per job in output.yml 1.3 (``ess_input_options``, ``irc_log_input_options``, ``conformer_input_options`` and
``conformer_energy_input_options``), parsed by :mod:`tckdb_core.deck_qualifiers` as the program of the job.
They are never read from a requested level or from its ``args``, so a calculation ARC records no deck for
(a rotor scan, a path search, the coarse stage of an optimization, a placeholder opt for a job ARC did not run)
and a level that is no job's (a frequency-scale-factor level, a correction-scheme level, a header level) carry
none, and no job's qualifiers are copied onto another level.

When the record also has the log's echo of the job (``*_route`` and ``*_blocks``), the echo can only veto: a
field the deck states and the echo states differently is left out. A field only the echo states is not sent.

What the deck cannot be read for is returned as notes (a registered warning code and one line of detail), which
the adapter reports once the build is done:

* ``level_qualifiers_conflicting``: a field is left out because the deck states two values, a negation or the
  log echo contradicts it, a numeric threshold overrides the PNO preset, or the decks of an IRC pair disagree;
* ``level_qualifiers_deck_not_read``: the deck's shape does not match the program, it holds several jobs, or it
  is a Gaussian route with a ``!`` comment.

A deck that is absent states nothing and is not reported.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tckdb_arc import arc13
from tckdb_arc.warning_codes import ArcWarning
from tckdb_core.deck_qualifiers import DeckQualifiers, agree_with_echo, qualifiers_from_deck

#: ``job_input_options`` value meaning "the job behind this calculation's own kind": the calculation reads its
#: deck from the record, by the job key its program is read from. Any sequence of deck texts (or ``None``
#: entries) replaces it, and an empty one states no deck at all.
AUTO_JOB_OPTIONS: Any = object()

_CODE_CONFLICTING = ArcWarning.LEVEL_QUALIFIERS_CONFLICTING.value
_CODE_NOT_READ = ArcWarning.LEVEL_QUALIFIERS_DECK_NOT_READ.value

_NOT_READ_REASONS = {
    "deck_shape_mismatch": "the deck does not have the shape of the job's program",
    "compound_deck": "the deck holds more than one job",
    "route_comment": "the route holds a ! comment",
}


@dataclass(frozen=True)
class QualifierNote:
    """One finding of the read: a registered warning ``code`` and one line of ASCII ``detail``."""

    code: str
    detail: str


@dataclass(frozen=True)
class JobQualifiers:
    """The qualifiers to send on a calculation's level and what could not be read."""

    values: Mapping[str, str]
    notes: tuple[QualifierNote, ...] = ()


NO_QUALIFIERS = JobQualifiers({})


def _echo_deck(program: str, route: str | None, blocks: str | None) -> str | None:
    """The log echo of a job as deck text: ORCA keyword lines and blocks, or the Gaussian route."""
    if program == "orca":
        parts = [f"!{route}" if route else None, blocks]
        return "\n".join(part for part in parts if part) or None
    return route


def _read_job(program: str, deck: str, echo: tuple[str | None, str | None]) -> tuple[DeckQualifiers, DeckQualifiers]:
    """``(read of the deck, read after the echo's veto)``."""
    read = qualifiers_from_deck(program, deck)
    echo_text = _echo_deck(program, *echo)
    if read.not_read is not None or echo_text is None:
        return read, read
    return read, agree_with_echo(read, qualifiers_from_deck(program, echo_text))


def _conflict_detail(fields: Sequence[str], why: str) -> QualifierNote:
    return QualifierNote(_CODE_CONFLICTING, f"{', '.join(fields)}: {why}")


def read_job_qualifiers(
    record: Mapping[str, Any],
    *,
    program: str | None,
    calc_type: str,
    ess_job_key: str,
    job_input_options: Any = AUTO_JOB_OPTIONS,
) -> JobQualifiers:
    """The qualifiers of the calculation ``calc_type`` run as ``program``, from the deck(s) of its job.

    ``job_input_options`` is :data:`AUTO_JOB_OPTIONS` (the default), or the deck texts of the jobs behind
    the calculation: ``()`` for a calculation whose job has no deck of its own (the coarse stage of an
    optimization, whose record holds only the fine opt's), one entry for a conformer's job. With the default:

    * an IRC calculation reads every entry of ``irc_log_input_options``, and states a qualifier only when
      every IRC log has a deck and all of them state the same qualifiers;
    * an opt, freq or sp calculation run under its own kind reads ``ess_input_options`` at the job key its
      program is read from (:func:`tckdb_arc.arc13.input_options_for`);
    * every other calculation (rotor scans, path searches) states none, because ARC records no deck for them.

    A record before 1.3 states none.
    """
    name = str(program or "").strip().lower()
    if not name or not arc13.has_levels(record):
        return NO_QUALIFIERS
    if (job_input_options is AUTO_JOB_OPTIONS and calc_type == "opt"
            and arc13.primary_opt_placeholder(record) == "no_opt_job"):
        return NO_QUALIFIERS    # no opt job ran: the opt slot of ``ess_input_options`` is not a deck of this opt
    echoes: list[tuple[str | None, str | None]]
    if job_input_options is AUTO_JOB_OPTIONS:
        if calc_type == "irc":
            decks = arc13.irc_input_options(record) or []
            echoes = arc13.irc_echoes(record, len(decks))
        elif calc_type == ess_job_key and calc_type in ("opt", "freq", "sp"):
            decks = [arc13.input_options_for(record, calc_type)]
            echoes = [arc13.echo_for(record, calc_type)]
        else:
            return NO_QUALIFIERS
    else:
        decks = list(job_input_options)
        echoes = [(None, None)] * len(decks)
    if not decks or any(not isinstance(deck, str) or not deck.strip() for deck in decks):
        return NO_QUALIFIERS
    reads = [_read_job(name, deck, echo) for deck, echo in zip(decks, echoes)]
    unread = [deck_read.not_read for deck_read, _ in reads if deck_read.not_read is not None]
    if unread:
        return JobQualifiers({}, (QualifierNote(_CODE_NOT_READ, _NOT_READ_REASONS.get(unread[0], unread[0])),))
    notes: list[QualifierNote] = []
    within = [field for deck_read, _ in reads for field in deck_read.conflicting]
    vetoed = [field for deck_read, after in reads for field in after.conflicting if field not in deck_read.conflicting]
    thresholds = [field for _, after in reads for field in after.overridden]
    if within:
        notes.append(_conflict_detail(tuple(dict.fromkeys(within)), "the deck states two values"))
    if vetoed:
        notes.append(_conflict_detail(tuple(dict.fromkeys(vetoed)), "the log echo states another value"))
    if thresholds:
        notes.append(_conflict_detail(
            tuple(dict.fromkeys(thresholds)), "a numeric threshold overrides the preset"))
    stated = [dict(after.values) for _, after in reads]
    if any(values != stated[0] for values in stated):
        differing = sorted({k for values in stated for k, v in values.items() if any(o.get(k) != v for o in stated)})
        notes.append(QualifierNote(
            _CODE_CONFLICTING,
            f"the IRC decks disagree on {', '.join(differing)}, so no qualifier is stated for the IRC level"))
        return JobQualifiers({}, tuple(notes))
    return JobQualifiers(stated[0], tuple(notes))
