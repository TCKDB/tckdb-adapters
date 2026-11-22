"""Recorded input-deck options (output.yml 1.3) for the tests of the typed level qualifiers.

The ``fixtures/arc_1_3_levels`` records state no ``ess_input_options``, ``conformer_input_options``,
``conformer_energy_input_options`` or ``irc_log_input_options`` (they were written before those keys
existed, and regenerating them would change unrelated tests), so a test overlays the decks it needs.

The deck strings are what ARC's ``arc.job.input_options.get_input_options`` records for a job, written out
by hand from ARC's own adapter tests (nothing here imports ARC):

* Gaussian: the route on one line (Link 0 lines, title and molecule left out), then the later sections. The
  shapes are ``arc/job/adapters/gaussian_test.py`` lines 645, 737, 770 and 918, and the ``freq`` route that
  holds one ``integral=(grid=ultrafine, Acc2E=12, dkh2)`` keyword at lines 1373-1376.
* ORCA: the ``!`` keyword lines, then the ``%`` blocks, without the coordinates, ``%maxcore`` and ``%pal``.
  The shapes are ``arc/job/adapters/orca_test.py`` line 115 (``!uHF dlpno-ccsd(t) def2-tzvp def2-tzvp/c
  tightscf normalpno``, where ARC adds ``normalpno`` itself) and lines 257-300 (``!sp`` / ``%scf`` /
  ``MaxIter 999`` / ``end`` after the keyword line, ``TightPNO`` from the level's args).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

# --- Gaussian (wb97xd/def2tzvp) ---------------------------------------------------------------------------

#: gaussian_test.py:737, an opt route with ARC's default integral options and a ModRedundant line after it.
GAUSSIAN_OPT = ("#P opt=(calcfc,maxStep=5,modredundant,noeigentest) integral=(grid=ultrafine, Acc2E=12) "
                "guess=mix wb97xd/def2tzvp   IOp(2/9=2000)    scf=(direct,tight)\n\nD 1 2 3 4 F")
#: The same job with the user's ``int=dkh2`` folded into the one Integral keyword (gaussian_test.py:1373-1376).
GAUSSIAN_OPT_DKH2 = ("#P opt=(calcfc,maxStep=5,modredundant,noeigentest) integral=(grid=ultrafine, Acc2E=12, dkh2) "
                     "guess=mix wb97xd/def2tzvp   IOp(2/9=2000)    scf=(direct,tight)\n\nD 1 2 3 4 F")
GAUSSIAN_FREQ = ("#P  wb97xd/def2tzvp freq IOp(7/33=1)  integral=(grid=ultrafine, Acc2E=12)  "
                 "IOp(2/9=2000)    scf=(direct,tight)")
GAUSSIAN_FREQ_DKH2 = ("#P  wb97xd/def2tzvp freq IOp(7/33=1)  integral=(grid=ultrafine, Acc2E=12, dkh2)  "
                      "IOp(2/9=2000)    scf=(direct,tight)")
GAUSSIAN_SP = "#P wb97xd/def2tzvp   IOp(2/9=2000)    scf=(direct,tight)"
GAUSSIAN_SP_DKH2 = "#P wb97xd/def2tzvp integral=(grid=ultrafine, Acc2E=12, dkh2)  IOp(2/9=2000)    scf=(direct,tight)"
#: A composite job (CBS-QB3) with the user's ``int=dkh2``.
GAUSSIAN_COMPOSITE_DKH2 = "#CBS-QB3 opt freq integral=(grid=ultrafine, Acc2E=12, dkh2)"
GAUSSIAN_IRC_FORWARD = ("#P irc=(CalcAll, forward, maxpoints=50, stepsize=7) guess=read wb97xd/def2tzvp "
                        "IOp(2/9=2000) scf=xqc")
GAUSSIAN_IRC_REVERSE = GAUSSIAN_IRC_FORWARD.replace("forward", "reverse")
GAUSSIAN_IRC_FORWARD_DKH2 = GAUSSIAN_IRC_FORWARD.replace("scf=xqc", "scf=xqc integral=(grid=ultrafine, dkh2)")
GAUSSIAN_IRC_REVERSE_DKH2 = GAUSSIAN_IRC_FORWARD_DKH2.replace("forward", "reverse")

# --- ORCA (DLPNO-CCSD(T)-F12 sp, the shape of the rotor_scan_rxn_210 corpus) -------------------------------

_ORCA_F12 = "dlpno-ccsd(t)-f12 cc-pvtz-f12 aug-cc-pvtz/c cc-pvtz-f12-cabs"
#: orca_test.py:115 and 257-300: the keyword line, ``!sp``, then the ``%scf`` block ARC adds.
ORCA_SP_NORMALPNO = f"!uhf {_ORCA_F12} tightscf normalpno\n!sp\n%scf\nMaxIter 999\nend"
ORCA_SP_TIGHTPNO = f"!uhf {_ORCA_F12} tightscf tightpno\n!sp\n%scf\nMaxIter 999\nend"
ORCA_SP_TIGHTPNO_RIJCOSX = f"!uhf {_ORCA_F12} tightscf tightpno rijcosx\n!sp\n%scf\nMaxIter 999\nend"
#: The H atom ARC runs at HF under a DLPNO level: ARC adds ``normalpno`` only for a ``dlpno`` method.
ORCA_SP_HF_NO_PNO = "!uhf hf cc-pvtz-f12 aug-cc-pvtz/c cc-pvtz-f12-cabs tightscf\n!sp\n%scf\nMaxIter 999\nend"
ORCA_SP_HF_TIGHTPNO = "!uhf hf cc-pvtz-f12 aug-cc-pvtz/c cc-pvtz-f12-cabs tightscf tightpno\n!sp\n%scf\nMaxIter 999\nend"
#: The ``%method`` block of an ORCA job with frozen-core window options.
ORCA_BLOCK_FC_EWIN = "%method\nFrozenCore FC_EWIN\nend"
ORCA_SP_DLPNO_TIGHTPNO = ("!rHF dlpno-ccsd(t) cc-pvtz cc-pvtz/c tightpno tightscf\n!sp\n%scf\nMaxIter 999\nend")


def overlay_decks(
    record: dict[str, Any],
    *,
    opt: str | None = None,
    freq: str | None = None,
    sp: str | None = None,
    composite: str | None = None,
    irc: list[str | None] | None = None,
    conformers: list[str | None] | None = None,
    conformer_energies: list[str | None] | None = None,
) -> dict[str, Any]:
    """Set the recorded deck keys of ``record`` (in place) and return it; an argument left ``None`` is null."""
    record["ess_input_options"] = {"opt": opt, "freq": freq, "sp": sp, "composite": composite}
    if irc is not None:
        record["irc_log_input_options"] = list(irc)
    if conformers is not None:
        record["conformer_input_options"] = list(conformers)
    if conformer_energies is not None:
        record["conformer_energy_input_options"] = list(conformer_energies)
    return record


def without_decks(doc: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of ``doc`` whose records carry none of the four deck keys (a document that never had them)."""
    out = copy.deepcopy(dict(doc))
    for record in (*out.get("species", ()), *out.get("transition_states", ())):
        for key in ("ess_input_options", "conformer_input_options", "conformer_energy_input_options",
                    "irc_log_input_options"):
            record.pop(key, None)
    return out
