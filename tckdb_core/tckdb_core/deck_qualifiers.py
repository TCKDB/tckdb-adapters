"""The typed method qualifiers of a level of theory, read from the text of an ESS input deck.

TCKDB's level of theory carries four optional typed fields: ``pno_truncation``, ``ri_approximation``,
``relativistic_hamiltonian`` and ``core_treatment``. This module reads them from the input-deck text a producer
recorded for one job, in the program's own syntax (ORCA and Gaussian). It reads program syntax and no producer's
key, so a producer passes the deck text and the program name.

What is read, and what is not:

* A value is read only from a token the deck states. Absence is never a value, a program default is never
  inferred, and a method name (``RI-MP2``, ``DLPNO-CCSD(T)``) states nothing. A token a producer wrote by default
  is read like any other token.
* Matching is case-insensitive and the lower-case enum value is returned.
* A field stated with two different values anywhere in the deck is left out and listed in ``conflicting``; the
  other fields are kept. An explicit negation (``NoRI``, ``NoCOSX`` against ``RIJCOSX``, Gaussian ``NoDKH`` /
  ``NonRelativistic``) and a value this module does not state (bare ``RI``, which TCKDB reads as ``rij``) are
  recognised only so that they can contradict a stated value; they are never returned.
* A deck whose first line does not have the program's shape is not read (``not_read="deck_shape_mismatch"``), nor
  is an ORCA deck with ``$new_job`` or a Gaussian deck with ``--Link1--`` (``"compound_deck"``: the text holds
  several jobs and the caller cannot tell which one it is reading), nor a Gaussian route with a ``!`` comment
  (``"route_comment"``: once a route is joined onto one line the end of a comment cannot be found).
* Any other program is not read and is not reported.

Every row of the tables below is also read by TCKDB's own ORCA and Gaussian parameter parsers, and the adapter
tests compare the two on the same deck text (``tckdb_core.testing.backend_import``), so this module never states
more than TCKDB reads. Keyword lines are split the way TCKDB splits them
(``tckdb_schemas.hessian_method_detection.orca_keyword_lines``).

ORCA (``!`` keyword lines and ``%`` blocks):

* ``TightPNO`` / ``NormalPNO`` / ``LoosePNO`` -> ``pno_truncation`` tight / normal / loose.
* ``RIJCOSX`` / ``RIJK`` / ``RIJONX`` -> ``ri_approximation`` of the same name, lower case.
* ``DKH`` / ``DKH2`` -> ``relativistic_hamiltonian`` dkh2; ``ZORA`` / ``IORA`` / ``X2C`` -> zora / iora / x2c;
  ``%rel Method DKH`` (or ``ZORA``, ``IORA``, ``X2C``) maps the same way.
* ``FrozenCore`` / ``NoFrozenCore`` -> ``core_treatment`` frozen_core / all_electron; ``%method FrozenCore``
  with ``FC_EWIN`` / ``FC_ELECTRONS`` / ``FC_NONE`` -> window / frozen_core / all_electron.

``DKH`` and ``DKH2`` state ``dkh2`` only when every ``Order`` of the ``%rel`` block is 2 or none is given;
otherwise the field is not read. Any key beginning with ``TCut`` (``TCutPNO``, ``TCutPairs``, ``TCutDOij``,
``TCutPNOSingles``, ...) in ``%mdci`` or ``%mp2`` is a numeric threshold that overrides the PNO preset, so a stated
``pno_truncation`` is left out and listed in ``overridden``. ``RIJCOSX`` with ``NoCOSX`` (which turns the
chain of spheres off) and any two different ``ri_approximation`` tokens, bare ``RI`` included, leave the field out
and list it in ``conflicting``. Not read: ``DKH1``, ``DLU-X2C`` and numeric PNO thresholds. Recognised only as a
negation: ``NoRI``.

Gaussian (the first line of the text, the route):

* The route is split on a space outside parentheses only, as TCKDB splits it, and ``Int`` / ``Integral`` with the option ``DKH``, ``DKH2`` or ``DouglasKrollHess`` -> ``relativistic_hamiltonian``
  dkh2; with ``DKH0``, ``DKHSO`` or ``RESC`` -> dkh0 / dkhso / resc. The forms are ``int=x``, ``int=(a,b)`` and
  ``int(a,b)``; options are split on commas and spaces and an option holding ``=`` (``grid=ultrafine``,
  ``Acc2E=12``) is ignored.
* The options ``Full`` / ``FC`` (or ``FrozenCore``) / ``Window`` of a correlated method (the prefixes TCKDB
  lists), written as the method of ``method/basis`` or as a keyword, as in ``CCSD(T,Full)/cc-pVTZ`` ->
  ``core_treatment`` all_electron / frozen_core / window.
* ``IOp(...)`` is ignored. Recognised only as negations: ``NoDKH`` and ``NonRelativistic``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from tckdb_schemas.hessian_method_detection import orca_keyword_lines

#: The four typed qualifier fields of a level of theory.
QUALIFIER_FIELDS = ("pno_truncation", "ri_approximation", "relativistic_hamiltonian", "core_treatment")

#: ``DeckQualifiers.not_read`` values.
DECK_SHAPE_MISMATCH = "deck_shape_mismatch"
COMPOUND_DECK = "compound_deck"
ROUTE_COMMENT = "route_comment"

#: A recognised negation. It contradicts a stated value of its field and is never sent.
_NONE = "none"
#: Values recognised only to contradict another value of their field: a negation, and ORCA's bare ``RI`` (TCKDB
#: reads it as ``rij``, which is not stated here).
_RIJ = "rij"
_NOT_STATED = frozenset({_NONE, _RIJ})


@dataclass(frozen=True)
class DeckQualifiers:
    """What a deck states: ``values`` to send, the fields left out and why, and why a deck was ``not_read``.

    ``conflicting`` fields are stated with two values (or contradicted by a negation or the log echo);
    ``overridden`` fields are a PNO preset a numeric threshold overrides.
    """

    values: Mapping[str, str] = field(default_factory=dict)
    conflicting: tuple[str, ...] = ()
    not_read: str | None = None
    overridden: tuple[str, ...] = ()


def qualifiers_from_deck(program: str | None, text: str | None) -> DeckQualifiers:
    """The qualifiers the deck ``text`` states for ``program`` (see the module docstring)."""
    name = str(program or "").strip().lower()
    reader = _READERS.get(name)
    if reader is None or not isinstance(text, str) or not text.strip():
        return DeckQualifiers()
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return reader(lines)


def agree_with_echo(deck: DeckQualifiers, echo: DeckQualifiers) -> DeckQualifiers:
    """``deck`` with every field the echo contradicts left out and listed in ``conflicting``.

    The echo (what the program printed about the job it ran) can only veto: a field the echo is silent about
    stays, and a field only the echo states is not added. An echo that states two values for a field contradicts
    it.
    """
    overridden = [name for name in deck.values if name in echo.overridden]
    vetoed = [
        name for name, value in deck.values.items()
        if name not in overridden and (name in echo.conflicting or (name in echo.values and echo.values[name] != value))
    ]
    if not vetoed and not overridden:
        return deck
    return DeckQualifiers(
        values={k: v for k, v in deck.values.items() if k not in vetoed and k not in overridden},
        conflicting=tuple(dict.fromkeys((*deck.conflicting, *vetoed))),
        not_read=deck.not_read,
        overridden=tuple(dict.fromkeys((*deck.overridden, *overridden))),
    )


def _resolve(found: Iterable[tuple[str, str]], overriding: Iterable[str] = ()) -> DeckQualifiers:
    """One value per field; a field with two values (a negation counts as one) is left out and listed.

    A field in ``overriding`` that the deck states (and does not state twice) is left out and listed as
    overridden.
    """
    stated: dict[str, set[str]] = {}
    for name, value in found:
        stated.setdefault(name, set()).add(value)
    conflicting = [name for name in QUALIFIER_FIELDS if len(stated.get(name, ())) > 1]
    overridden = [name for name in QUALIFIER_FIELDS if name in overriding and name in stated and name not in conflicting]
    values: dict[str, str] = {}
    for name in QUALIFIER_FIELDS:
        only = stated.get(name, set())
        if name not in conflicting and name not in overridden and len(only) == 1:
            (value,) = only
            if value not in _NOT_STATED:
                values[name] = value
    return DeckQualifiers(values=values, conflicting=tuple(conflicting), overridden=tuple(overridden))


# ------------------------------------------------------------------------------------------------ ORCA

_ORCA_KEYWORDS = {
    "tightpno": ("pno_truncation", "tight"),
    "normalpno": ("pno_truncation", "normal"),
    "loosepno": ("pno_truncation", "loose"),
    "rijcosx": ("ri_approximation", "rijcosx"),
    "rijk": ("ri_approximation", "rijk"),
    "rijonx": ("ri_approximation", "rijonx"),
    "nori": ("ri_approximation", _NONE),
    "ri": ("ri_approximation", _RIJ),
    "dkh": ("relativistic_hamiltonian", "dkh2"),
    "dkh2": ("relativistic_hamiltonian", "dkh2"),
    "zora": ("relativistic_hamiltonian", "zora"),
    "iora": ("relativistic_hamiltonian", "iora"),
    "x2c": ("relativistic_hamiltonian", "x2c"),
    "frozencore": ("core_treatment", "frozen_core"),
    "nofrozencore": ("core_treatment", "all_electron"),
}
_ORCA_REL_METHODS = {"dkh": "dkh2", "zora": "zora", "iora": "iora", "x2c": "x2c"}
_ORCA_CORE_VALUES = {"fc_none": "all_electron", "fc_ewin": "window", "fc_electrons": "frozen_core"}
#: Blocks whose ``TCut*`` keys are numeric truncation thresholds.
_ORCA_TCUT_BLOCKS = ("mdci", "mp2")
_NEW_JOB = re.compile(r"^\$new_job\s*$", re.IGNORECASE)
_BLOCK_START = re.compile(r"^%(\w+)\s*(.*?)$")
#: Sub-sections of a block that open their own ``end``-terminated block.
_NESTED_OPENERS = frozenset({"constraints", "scan"})


def _orca_drop_coordinates(lines: list[str]) -> list[str]:
    """The lines without a ``* xyz ... *`` (or ``* int``) coordinate block, whose atom rows are not keywords."""
    out: list[str] = []
    in_block = False
    for line in lines:
        if in_block:
            in_block = line != "*"
        elif line.startswith("*") and ("xyz" in line.lower() or "int" in line.lower()):
            in_block = True
        else:
            out.append(line)
    return out


def _orca_blocks(lines: list[str]) -> list[tuple[str, list[str], bool]]:
    """``(name, content lines, single_line)`` for each ``%`` block, ended the way TCKDB ends it (an ``end`` line).

    ``%name rest`` with a ``rest`` is a one-line block (a trailing ``end`` removed); a bare ``%name`` runs to its
    ``end``, past the ``end`` lines of nested sub-blocks. ``#`` starts a comment that runs to the end of the line.
    """
    blocks: list[tuple[str, list[str], bool]] = []
    index = 0
    while index < len(lines):
        line = lines[index].split("#", 1)[0].strip()
        match = _BLOCK_START.match(line)
        if match is None:
            index += 1
            continue
        name, rest = match.group(1).lower(), match.group(2).strip()
        if rest and rest.lower() != "end":
            if rest.lower().endswith(" end"):
                rest = rest[:-4].strip()
            blocks.append((name, [rest], True))
            index += 1
            continue
        content: list[str] = []
        depth = 0
        index += 1
        while index < len(lines):
            block_line = lines[index].split("#", 1)[0].strip()
            lowered = block_line.lower()
            index += 1
            if lowered in _NESTED_OPENERS:
                depth += 1
            elif lowered == "end":
                if depth == 0:
                    break
                depth -= 1
            elif block_line.startswith("{") and block_line.endswith("}"):
                continue
            elif block_line:
                content.append(block_line)
        blocks.append((name, content, False))
    return blocks


def _orca_pairs(content: list[str], single_line: bool) -> list[tuple[str, str]]:
    """``(key, value)`` pairs of a block: one per line, or key and value alternating in a one-line block."""
    pairs: list[tuple[str, str]] = []
    for line in content:
        tokens = line.split()
        if single_line:
            index = 0
            while index < len(tokens):
                if index + 1 < len(tokens):
                    pairs.append((tokens[index].lower(), tokens[index + 1].lower()))
                    index += 2
                else:
                    pairs.append((tokens[index].lower(), "true"))
                    index += 1
        elif tokens:
            pairs.append((tokens[0].lower(), line.split(None, 1)[1].strip().lower() if len(tokens) > 1 else "true"))
    return pairs


def _read_orca(lines: list[str]) -> DeckQualifiers:
    if not lines[0].startswith(("!", "%", "$")):
        return DeckQualifiers(not_read=DECK_SHAPE_MISMATCH)
    if any(_NEW_JOB.match(line) for line in lines):
        return DeckQualifiers(not_read=COMPOUND_DECK)
    lines = _orca_drop_coordinates(lines)
    found: list[tuple[str, str]] = []
    overriding: list[str] = []
    dkh_written = False
    no_cosx = False
    for keyword_line in orca_keyword_lines(lines):
        for token in keyword_line.split():
            lowered = token.lower()
            entry = _ORCA_KEYWORDS.get(lowered)
            if entry is not None:
                found.append(entry)
                dkh_written = dkh_written or lowered in ("dkh", "dkh2")
            no_cosx = no_cosx or lowered == "nocosx"
    orders: list[str] = []
    for name, content, single_line in _orca_blocks(lines):
        pairs = _orca_pairs(content, single_line)
        if name == "rel":
            for key, value in pairs:
                if key == "order":
                    orders.append(value)
                elif key == "method" and value in _ORCA_REL_METHODS:
                    found.append(("relativistic_hamiltonian", _ORCA_REL_METHODS[value]))
                    dkh_written = dkh_written or value == "dkh"
        elif name == "method":
            found += [("core_treatment", _ORCA_CORE_VALUES[v]) for k, v in pairs if k == "frozencore" and v in _ORCA_CORE_VALUES]
        elif name in _ORCA_TCUT_BLOCKS and any(key.startswith("tcut") for key, _ in pairs):
            overriding.append("pno_truncation")
    if dkh_written and any(order != "2" for order in orders):
        found = [entry for entry in found if entry != ("relativistic_hamiltonian", "dkh2")]
    if no_cosx and ("ri_approximation", "rijcosx") in found:
        found.append(("ri_approximation", _NONE))
    return _resolve(found, overriding)


# ------------------------------------------------------------------------------------------------ Gaussian

_GAUSSIAN_INTEGRAL_OPTIONS = {
    "dkh": "dkh2",
    "dkh2": "dkh2",
    "douglaskrollhess": "dkh2",
    "dkh0": "dkh0",
    "dkhso": "dkhso",
    "resc": "resc",
    "nodkh": _NONE,
    "nonrelativistic": _NONE,
}
_GAUSSIAN_CORE_OPTIONS = {"full": "all_electron", "fc": "frozen_core", "frozencore": "frozen_core", "window": "window"}
#: The correlated methods whose options can carry the frozen-core choice (not ``Pop=(Full)``, which means
#: something else).
_GAUSSIAN_CORE_METHOD_PREFIXES = (
    "mp2", "mp3", "mp4", "mp5", "ccsd", "ccd", "cid", "cisd", "qcisd", "bd", "eomccsd", "ci",
    "ump2", "ump3", "ump4", "rmp2", "ccsdt",
)
_GAUSSIAN_IOP = re.compile(r"IOp\(([^)]+)\)")
_GAUSSIAN_KEYWORD = re.compile(r"^([A-Za-z][A-Za-z0-9]*)(?:=(.*)|(\(.*\)))?$", re.DOTALL)
_LINK1 = re.compile(r"^--link1--$", re.IGNORECASE)


def _gaussian_tokens(route: str) -> list[str]:
    """Route tokens, split as TCKDB splits them: on a space at parenthesis depth 0 and nowhere else.

    The ``#P`` prefix and ``IOp(...)`` are removed first, and space around ``=`` and before ``(`` does not
    separate tokens. A tab, or a space after an unbalanced ``)``, does not separate tokens either.
    """
    route = route.strip()
    prefix = re.match(r"#([PNTpnt])?\s*", route)
    route = route[prefix.end():] if prefix else route
    route = _GAUSSIAN_IOP.sub("", route).strip()
    route = re.sub(r"\s*=\s*", "=", route)
    route = re.sub(r"(?<=[\w)])\s+\(", "(", route)
    tokens: list[str] = []
    current: list[str] = []
    depth = 0
    for char in route:
        if char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth -= 1
            current.append(char)
        elif char == " " and depth == 0:
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


def _gaussian_split(token: str) -> tuple[str, str] | None:
    """``(lower-case keyword, option text)`` for ``Key``, ``Key=value``, ``Key=(..)`` and ``Key(..)``."""
    match = _GAUSSIAN_KEYWORD.match(token)
    if match is None:
        return None
    body = (match.group(2) if match.group(2) is not None else match.group(3) or "").strip()
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1].strip()
    return match.group(1).lower(), body


def _gaussian_options(body: str) -> list[str]:
    """Top-level options of ``body``: commas and spaces separate them and parentheses group."""
    pieces: list[str] = []
    current: list[str] = []
    depth = 0
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if depth == 0 and char in ", \t":
            if current:
                pieces.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        pieces.append("".join(current))
    return pieces


def _gaussian_method_part(token: str) -> str | None:
    """The method of a ``method/basis`` token, or ``None`` when the token is not one (an ``=`` before the ``/``)."""
    depth = 0
    for index, char in enumerate(token):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and char == "=":
            return None
        elif depth == 0 and char == "/":
            return token[:index]
    return None


def _gaussian_core(token: str) -> list[tuple[str, str]]:
    split = _gaussian_split(token)
    if split is None or not split[0].startswith(_GAUSSIAN_CORE_METHOD_PREFIXES):
        return []
    found = []
    for option in _gaussian_options(split[1]):
        value = _GAUSSIAN_CORE_OPTIONS.get(option.split("=", 1)[0].lower())
        if value is not None:
            found.append(("core_treatment", value))
    return found


def _read_gaussian(lines: list[str]) -> DeckQualifiers:
    if not lines[0].startswith("#"):
        return DeckQualifiers(not_read=DECK_SHAPE_MISMATCH)
    if any(_LINK1.match(line) for line in lines):
        return DeckQualifiers(not_read=COMPOUND_DECK)
    route = lines[0]
    if "!" in route:
        return DeckQualifiers(not_read=ROUTE_COMMENT)
    found: list[tuple[str, str]] = []
    for token in _gaussian_tokens(route):
        method = _gaussian_method_part(token)
        if method is not None:
            found += _gaussian_core(method)
            continue
        split = _gaussian_split(token)
        if split is None:
            continue
        keyword, body = split
        if keyword in ("int", "integral"):
            for option in _gaussian_options(body):
                value = _GAUSSIAN_INTEGRAL_OPTIONS.get(option.lower())
                if value is not None:
                    found.append(("relativistic_hamiltonian", value))
        elif keyword.startswith(_GAUSSIAN_CORE_METHOD_PREFIXES):
            found += _gaussian_core(token)
    return _resolve(found)


_READERS = {"orca": _read_orca, "gaussian": _read_gaussian}
