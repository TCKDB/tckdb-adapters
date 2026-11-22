"""``tckdb_core.deck_qualifiers``: the typed level qualifiers an ORCA or Gaussian deck states.

The deck strings are the shape a producer records for one job: ORCA ``!`` lines and ``%`` blocks without the
coordinates, ``%maxcore`` and ``%pal``; a Gaussian route on one line followed by its later sections.
"""

from __future__ import annotations

import os

import pytest

from tckdb_core.deck_qualifiers import (
    COMPOUND_DECK,
    DECK_SHAPE_MISMATCH,
    ROUTE_COMMENT,
    DeckQualifiers,
    agree_with_echo,
    qualifiers_from_deck,
)


def _q(program, text):
    result = qualifiers_from_deck(program, text)
    return dict(result.values), result.conflicting, result.not_read


# ---------------------------------------------------------------- what is stated

ORCA_MAPPED = [
    # (deck text, expected qualifiers)
    ("!dlpno-ccsd(t) def2-tzvp tightpno", {"pno_truncation": "tight"}),
    ("!dlpno-ccsd(t) def2-tzvp NormalPNO", {"pno_truncation": "normal"}),
    ("!dlpno-ccsd(t) def2-tzvp LoosePNO", {"pno_truncation": "loose"}),
    ("!b3lyp def2-svp rijcosx", {"ri_approximation": "rijcosx"}),
    ("!b3lyp def2-svp RIJK", {"ri_approximation": "rijk"}),
    ("!b3lyp def2-svp rijonx", {"ri_approximation": "rijonx"}),
    ("!b3lyp def2-svp dkh", {"relativistic_hamiltonian": "dkh2"}),
    ("!b3lyp def2-svp DKH2", {"relativistic_hamiltonian": "dkh2"}),
    ("!b3lyp def2-svp zora", {"relativistic_hamiltonian": "zora"}),
    ("!b3lyp def2-svp IORA", {"relativistic_hamiltonian": "iora"}),
    ("!b3lyp def2-svp x2c", {"relativistic_hamiltonian": "x2c"}),
    ("!mp2 def2-svp FrozenCore", {"core_treatment": "frozen_core"}),
    ("!mp2 def2-svp NoFrozenCore", {"core_treatment": "all_electron"}),
    ("!mp2 def2-svp\n%method\nFrozenCore FC_EWIN\nend", {"core_treatment": "window"}),
    ("!mp2 def2-svp\n%method\nFrozenCore FC_ELECTRONS\nend", {"core_treatment": "frozen_core"}),
    ("!mp2 def2-svp\n%method\nFrozenCore FC_NONE\nend", {"core_treatment": "all_electron"}),
    ("!mp2 def2-svp\n%method FrozenCore FC_EWIN end", {"core_treatment": "window"}),
    ("!b3lyp def2-svp\n%rel\nMethod ZORA\nend", {"relativistic_hamiltonian": "zora"}),
    ("!b3lyp def2-svp\n%rel method x2c end", {"relativistic_hamiltonian": "x2c"}),
    ("!b3lyp def2-svp\n%rel\nMethod DKH\nend", {"relativistic_hamiltonian": "dkh2"}),
    ("!b3lyp def2-svp\n%rel\nMethod DKH\nOrder 2\nend", {"relativistic_hamiltonian": "dkh2"}),
    # ARC orca_test.py:115 (the sp deck written for a DLPNO level: ARC adds normalpno itself) and the options
    # recorded by the test at :257, which state ``['!sp', '%scf', 'MaxIter 999', 'end']`` after the keyword line.
    ("!uHF dlpno-ccsd(t) def2-tzvp def2-tzvp/c tightscf normalpno\n!sp\n%scf\nMaxIter 999\nend",
     {"pno_truncation": "normal"}),
    ("!rHF dlpno-ccsd(t) cc-pvtz cc-pvtz/c tightpno tightscf\n!sp\n%scf\nMaxIter 999\nend",
     {"pno_truncation": "tight"}),
    ("!TightPNO RIJCOSX DKH2 TightSCF",
     {"pno_truncation": "tight", "ri_approximation": "rijcosx", "relativistic_hamiltonian": "dkh2"}),
    ("!b3lyp def2-svp # RIJCOSX # zora", {"relativistic_hamiltonian": "zora"}),
]

GAUSSIAN_MAPPED = [
    ("#P opt wb97xd/def2tzvp int=dkh2", {"relativistic_hamiltonian": "dkh2"}),
    ("#P opt wb97xd/def2tzvp Integral=DKH", {"relativistic_hamiltonian": "dkh2"}),
    ("#P opt wb97xd/def2tzvp int=DouglasKrollHess", {"relativistic_hamiltonian": "dkh2"}),
    ("#P opt wb97xd/def2tzvp int(dkh2) scf=tight", {"relativistic_hamiltonian": "dkh2"}),
    ("#P opt wb97xd/def2tzvp int=(grid=ultrafine,dkh2)", {"relativistic_hamiltonian": "dkh2"}),
    ("#P opt wb97xd/def2tzvp int=dkh0", {"relativistic_hamiltonian": "dkh0"}),
    ("#P opt wb97xd/def2tzvp int=dkhso", {"relativistic_hamiltonian": "dkhso"}),
    ("#P opt wb97xd/def2tzvp integral=resc", {"relativistic_hamiltonian": "resc"}),
    # ARC gaussian_test.py:1373-1376 (a freq route holding one Integral keyword with ARC's options and the user's)
    ("#P uwb97xd/def2tzvp freq IOp(7/33=1) integral=(grid=ultrafine, Acc2E=12, dkh2) IOp(2/9=2000) scf=(direct,tight)",
     {"relativistic_hamiltonian": "dkh2"}),
    ("#P sp ccsd(t,full)/cc-pvtz", {"core_treatment": "all_electron"}),
    ("#P sp ccsd(t,fc)/cc-pvtz", {"core_treatment": "frozen_core"}),
    ("#P sp mp2(frozencore)/cc-pvtz", {"core_treatment": "frozen_core"}),
    ("#P sp mp2(window=(1,10))/cc-pvtz", {"core_treatment": "window"}),
    ("#P sp mp2(full) cc-pvtz", {"core_treatment": "all_electron"}),
    ("#P sp qcisd(t,full)/6-31g(d)", {"core_treatment": "all_electron"}),
]

# Each of these states nothing and reports nothing.
ORCA_NOTHING = [
    ("a method name with RI", "!ri-mp2 def2-svp def2/c"),
    ("a DLPNO method name alone", "!dlpno-ccsd(t) def2-tzvp normalscf"),
    ("bare RI", "!b3lyp def2-svp ri def2/j"),
    ("NoCOSX", "!b3lyp def2-svp nocosx"),
    ("DKH1", "!b3lyp def2-svp dkh1"),
    ("DLU-X2C", "!b3lyp def2-svp dlu-x2c"),
    ("a numeric PNO threshold alone", "!dlpno-ccsd(t) def2-tzvp\n%mdci\nTCutPNO 1e-8\nend"),
    ("the Gaussian spelling", "!b3lyp def2-svp int=dkh2"),
    ("a keyword inside a comment", "!b3lyp def2-svp # tightpno # tightscf"),
    ("the SCF block", "!b3lyp def2-svp\n%scf\nMaxIter 999\nend"),
    ("a %rel method DKH2 (TCKDB reads only DKH here)", "!b3lyp def2-svp\n%rel\nMethod DKH2\nend"),
    ("FrozenCore in %scf", "!b3lyp def2-svp\n%scf\nFrozenCore FC_EWIN\nend"),
]
GAUSSIAN_NOTHING = [
    ("a non-qualifier SCF option", "#P opt wb97xd/def2tzvp scf=(maxcycle=200)"),
    ("an integration grid", "#P opt wb97xd/def2tzvp int=ultrafine"),
    ("a bare dkh2 outside Int", "#P opt wb97xd/def2tzvp dkh2"),
    ("the ORCA spelling", "#P opt wb97xd/def2tzvp tightpno rijcosx zora"),
    ("an Int option holding =", "#P opt wb97xd/def2tzvp int=(grid=ultrafine, Acc2E=12)"),
    ("Pop=(Full) is not a core choice", "#P sp wb97xd/def2tzvp pop=(full)"),
    ("Full on a DFT method", "#P sp wb97xd(full)/def2tzvp"),
    ("an IOp coordinate", "#P opt wb97xd/def2tzvp IOp(3/32=2,dkh2)"),
    ("a later section", "#P opt wb97xd/def2tzvp\n\no 0\ndkh2\n****"),
]


@pytest.mark.parametrize("text, expected", ORCA_MAPPED)
def test_orca_tokens_map_to_their_value(text, expected):
    assert _q("orca", text) == (expected, (), None)


@pytest.mark.parametrize("text, expected", GAUSSIAN_MAPPED)
def test_gaussian_tokens_map_to_their_value(text, expected):
    assert _q("gaussian", text) == (expected, (), None)


@pytest.mark.parametrize("name, text", ORCA_NOTHING)
def test_orca_states_nothing_here(name, text):
    assert _q("orca", text) == ({}, (), None), name


@pytest.mark.parametrize("name, text", GAUSSIAN_NOTHING)
def test_gaussian_states_nothing_here(name, text):
    assert _q("gaussian", text) == ({}, (), None), name


def test_matching_is_case_insensitive_and_the_value_is_lower_case():
    assert _q("orca", "!TIGHTPNO RIJCOSX DKH2")[0] == {
        "pno_truncation": "tight", "ri_approximation": "rijcosx", "relativistic_hamiltonian": "dkh2"}
    assert _q("GAUSSIAN", "#P INT=DKH2")[0] == {"relativistic_hamiltonian": "dkh2"}


def test_a_default_token_the_producer_wrote_is_read_like_any_other():
    assert _q("orca", "!dlpno-ccsd(t) def2-tzvp def2-tzvp/c normalpno")[0] == {"pno_truncation": "normal"}


def test_a_gaussian_route_is_the_first_line_and_a_later_section_is_not_read():
    text = "#P opt=(calcfc) wb97xd/def2tzvp int=dkh2\n\nint=dkso\ncc-pvtz"
    assert _q("gaussian", text)[0] == {"relativistic_hamiltonian": "dkh2"}


# ---------------------------------------------------------------- conflicts, negations

def test_a_field_with_two_values_is_left_out_and_the_others_are_kept():
    assert _q("orca", "!zora dkh2 tightpno") == ({"pno_truncation": "tight"}, ("relativistic_hamiltonian",), None)
    assert _q("orca", "!tightpno loosepno rijk") == ({"ri_approximation": "rijk"}, ("pno_truncation",), None)
    assert _q("gaussian", "#P sp int=(dkh2,dkh0) ccsd(t,full)/cc-pvtz")[1:] == (("relativistic_hamiltonian",), None)


def test_two_values_across_the_keyword_line_and_a_block_conflict():
    text = "!b3lyp def2-svp zora\n%rel\nMethod X2C\nend"
    assert _q("orca", text) == ({}, ("relativistic_hamiltonian",), None)
    text = "!mp2 def2-svp FrozenCore\n%method FrozenCore FC_EWIN end"
    assert _q("orca", text) == ({}, ("core_treatment",), None)


def test_a_negation_is_never_stated_and_it_contradicts_a_stated_value():
    assert _q("orca", "!b3lyp def2-svp NoRI") == ({}, (), None)
    assert _q("orca", "!b3lyp def2-svp RIJCOSX NoRI") == ({}, ("ri_approximation",), None)
    assert _q("gaussian", "#P opt wb97xd/def2tzvp int=nodkh") == ({}, (), None)
    assert _q("gaussian", "#P opt wb97xd/def2tzvp int=nonrelativistic") == ({}, (), None)
    assert _q("gaussian", "#P opt wb97xd/def2tzvp int=(dkh2,nodkh)") == ({}, ("relativistic_hamiltonian",), None)


@pytest.mark.parametrize("block, key", [
    ("mdci", "TCutPNO 1e-8"), ("mdci", "TCutPairs 1e-5"), ("mdci", "TCutMKN 1e-3"), ("mdci", "TCutDO 1e-2"),
    ("mdci", "TCutPNOSingles 1e-9"), ("mdci", "TCutDOij 1e-7"), ("mdci", "TCutPre 1e-6"),
    ("mdci", "tcutpno 1e-8"), ("mp2", "TCutPNO 1e-9"),
])
def test_a_numeric_threshold_overrides_the_pno_preset(block, key):
    text = f"!dlpno-ccsd(t) def2-tzvp tightpno rijk\n%{block}\n{key}\nend"
    result = qualifiers_from_deck("orca", text)
    assert dict(result.values) == {"ri_approximation": "rijk"}
    assert result.overridden == ("pno_truncation",) and result.conflicting == ()
    one_line = qualifiers_from_deck("orca", f"!dlpno-ccsd(t) def2-tzvp tightpno\n%{block} {key} end")
    assert dict(one_line.values) == {} and one_line.overridden == ("pno_truncation",)


def test_a_threshold_with_no_preset_overrides_nothing_and_another_block_does_not_count():
    assert qualifiers_from_deck("orca", "!dlpno-ccsd(t) def2-tzvp\n%mdci\nTCutPNO 1e-8\nend") == DeckQualifiers()
    text = "!dlpno-ccsd(t) def2-tzvp tightpno\n%scf\nTCutFoo 1\nend"
    assert _q("orca", text) == ({"pno_truncation": "tight"}, (), None)


def test_nocosx_cancels_rijcosx_and_bare_ri_contradicts_a_stated_value():
    assert _q("orca", "!b3lyp def2-svp RIJCOSX NoCOSX") == ({}, ("ri_approximation",), None)
    assert _q("orca", "!b3lyp def2-svp NoCOSX") == ({}, (), None)
    assert _q("orca", "!b3lyp def2-svp RIJK NoCOSX") == ({"ri_approximation": "rijk"}, (), None)
    assert _q("orca", "!b3lyp def2-svp RI RIJCOSX") == ({}, ("ri_approximation",), None)
    assert _q("orca", "!b3lyp def2-svp RI") == ({}, (), None)
    assert _q("orca", "!b3lyp def2-svp RI RIJK RIJONX tightpno")[1] == ("ri_approximation",)


def test_dkh2_is_dkh2_only_at_order_two_like_dkh():
    assert _q("orca", "!b3lyp def2-svp dkh2\n%rel Order 3 end") == ({}, (), None)
    assert _q("orca", "!b3lyp def2-svp dkh2\n%rel Order 2 end")[0] == {"relativistic_hamiltonian": "dkh2"}
    assert _q("orca", "!b3lyp def2-svp zora\n%rel Order 3 end")[0] == {"relativistic_hamiltonian": "zora"}


def test_the_gaussian_route_is_split_as_tckdb_splits_it():
    # A tab is no separator: the second keyword is an option of the first.
    assert _q("gaussian", "#P int=dkh\tccsd(full)/cc-pvtz") == ({"relativistic_hamiltonian": "dkh2"}, (), None)
    assert _q("gaussian", "#P int=dkh  ccsd(full)/cc-pvtz") == (
        {"relativistic_hamiltonian": "dkh2", "core_treatment": "all_electron"}, (), None)
    # After an unbalanced ")" no space separates tokens any more.
    assert _q("gaussian", "# opt) int=dkh2 ccsd(full)/x") == ({}, (), None)
    assert _q("gaussian", "#P opt int=dkh2 ccsd(full)/x")[0] == {
        "relativistic_hamiltonian": "dkh2", "core_treatment": "all_electron"}


@pytest.mark.parametrize("order, expected", [("2", {"relativistic_hamiltonian": "dkh2"}), ("3", {})])
def test_dkh_is_dkh2_only_at_order_two(order, expected):
    text = f"!b3lyp def2-svp\n%rel\nMethod DKH\nOrder {order}\nend"
    assert _q("orca", text) == (expected, (), None)
    assert _q("orca", f"!b3lyp def2-svp dkh\n%rel Order {order} end") == (expected, (), None)


def test_other_relativistic_methods_do_not_depend_on_the_order():
    assert _q("orca", "!b3lyp def2-svp\n%rel\nMethod ZORA\nOrder 3\nend")[0] == {"relativistic_hamiltonian": "zora"}


# ---------------------------------------------------------------- not read

def test_a_compound_orca_deck_is_not_read():
    assert _q("orca", "!b3lyp def2-svp tightpno\n$new_job\n!mp2 def2-svp") == ({}, (), COMPOUND_DECK)


def test_a_gaussian_deck_with_link1_is_not_read():
    assert _q("gaussian", "#P opt b3lyp/6-31g int=dkh2\n\n--Link1--\n#P freq b3lyp/6-31g") == ({}, (), COMPOUND_DECK)


def test_a_gaussian_route_with_a_comment_is_not_read():
    assert _q("gaussian", "#P opt b3lyp/6-31g int=dkh2 ! scf=tight") == ({}, (), ROUTE_COMMENT)


@pytest.mark.parametrize("program, text", [
    ("orca", "#P opt b3lyp/6-31g int=dkh2"),
    ("orca", "dlpno-ccsd(t) tightpno"),
    ("gaussian", "!b3lyp def2-svp zora"),
    ("gaussian", "opt b3lyp/6-31g int=dkh2"),
])
def test_a_deck_of_another_shape_is_not_read(program, text):
    assert _q(program, text) == ({}, (), DECK_SHAPE_MISMATCH)


@pytest.mark.parametrize("program", ["qchem", "molpro", "cfour", "terachem", "xtb", "psi4", "pyscf", None, ""])
def test_another_program_is_not_read_and_not_reported(program):
    assert qualifiers_from_deck(program, "!tightpno rijcosx zora\n#P int=dkh2 $rem") == DeckQualifiers()


@pytest.mark.parametrize("text", [None, "", "   \n  "])
def test_no_text_states_nothing(text):
    assert qualifiers_from_deck("orca", text) == DeckQualifiers()


# ---------------------------------------------------------------- the log echo

def test_the_echo_can_only_veto():
    deck = qualifiers_from_deck("orca", "!dlpno-ccsd(t) def2-tzvp tightpno rijcosx")
    assert agree_with_echo(deck, qualifiers_from_deck("orca", "!dlpno-ccsd(t) def2-tzvp tightpno rijcosx")) == deck
    # silent echo: the deck stands
    assert agree_with_echo(deck, qualifiers_from_deck("orca", "!dlpno-ccsd(t) def2-tzvp")) == deck
    assert agree_with_echo(deck, DeckQualifiers()) == deck
    # an echo that states another value vetoes that field alone
    vetoed = agree_with_echo(deck, qualifiers_from_deck("orca", "!dlpno-ccsd(t) def2-tzvp normalpno rijcosx"))
    assert dict(vetoed.values) == {"ri_approximation": "rijcosx"}
    assert vetoed.conflicting == ("pno_truncation",)


def test_an_echo_with_two_values_vetoes_and_a_value_only_the_echo_states_is_not_added():
    deck = qualifiers_from_deck("orca", "!b3lyp def2-svp tightpno")
    echo = qualifiers_from_deck("orca", "!b3lyp def2-svp tightpno loosepno rijk")
    result = agree_with_echo(deck, echo)
    assert dict(result.values) == {}
    assert result.conflicting == ("pno_truncation",)
    only_echo = agree_with_echo(DeckQualifiers(), qualifiers_from_deck("orca", "!b3lyp zora"))
    assert dict(only_echo.values) == {}
    assert only_echo.conflicting == ()


# ---------------------------------------------------------------- the backend's own parsers

def _backend_parsers():
    from tckdb_core.testing.backend_import import backend_deck_qualifier_parsers

    parsers, reason = backend_deck_qualifier_parsers()
    if parsers is None:
        if os.environ.get("TCKDB_REQUIRE_BACKEND") == "1":
            pytest.fail(f"TCKDB_REQUIRE_BACKEND=1 but {reason}")
        pytest.skip(reason)
    return parsers


_PARITY_DECKS = (
    [("orca", text) for text, _ in ORCA_MAPPED]
    + [("orca", text) for _, text in ORCA_NOTHING]
    + [("gaussian", text) for text, _ in GAUSSIAN_MAPPED]
    + [("gaussian", text) for _, text in GAUSSIAN_NOTHING]
    + [
        ("orca", "!b3lyp def2-svp\n%rel\nMethod DKH\nOrder 3\nend"),
        ("orca", "!b3lyp def2-svp dkh2\n%rel Order 3 end"),
        ("orca", "!b3lyp def2-svp RIJCOSX NoCOSX"),
        ("orca", "!b3lyp def2-svp RI RIJCOSX"),
        ("orca", "!b3lyp def2-svp RI"),
        ("orca", "!dlpno-ccsd(t) def2-tzvp tightpno\n%mdci\nTCutPNOSingles 1e-9\nend"),
        ("orca", "!dlpno-ccsd(t) def2-tzvp tightpno\n%mp2\nTCutPNO 1e-9\nend"),
        ("orca", "!b3lyp  def2-svp   zora\n%rel\n  Method   X2C\nend"),
        ("orca", "!b3lyp def2-svp\ttightpno rijk"),
        ("orca", "!b3lyp def2-svp tightpno\n%rel method zora"),
        ("orca", "!b3lyp def2-svp tightpno\n%rel\n method zora\n%mdci\n  TCutPairs 1e-5"),
        ("gaussian", "#P int=dkh\tccsd(full)/cc-pvtz"),
        ("gaussian", "#P int=dkh  ccsd(full)/cc-pvtz"),
        ("gaussian", "#P   opt   int=dkh2     ccsd(t,fc)/cc-pvtz"),
        ("gaussian", "# opt) int=dkh2 ccsd(full)/x"),
        ("gaussian", "#P opt) int=dkh2 ccsd(full)/x"),
        ("gaussian", "#P opt int=(dkh2 ccsd(full)/x"),
        ("gaussian", "#P opt (int=dkh2) ccsd(full)/x"),
        ("gaussian", "#P opt int = dkh2 ccsd ( full ) /x"),
        ("gaussian", "#P int(dkh2,\tdkh0) mp2(full)"),
        ("gaussian", "#P iop(3/32=2) int=dkh2"),
        ("orca", "!b3lyp def2-svp zora dkh2"),
        ("orca", "!dlpno-ccsd(t) def2-tzvp tightpno\n%mdci\nTCutPNO 1e-8\nend"),
        ("orca", "!b3lyp def2-svp RIJCOSX NoRI"),
        ("gaussian", "#P opt wb97xd/def2tzvp int=(dkh2,nodkh)"),
    ]
)


@pytest.mark.parametrize("program, text", _PARITY_DECKS)
def test_every_value_stated_is_a_value_the_backend_reads(program, text):
    """The module never states more than TCKDB's own parser reads from the same deck."""
    backend = _backend_parsers()[program](text)
    stated = qualifiers_from_deck(program, text)
    for name, value in stated.values.items():
        assert value in backend.get(name, set()), (name, value, backend)
