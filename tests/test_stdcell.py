"""pytest: generated std cells are functionally correct, DRC clean and LVS clean.

The netlist / Euler tests need nothing; layout tests need a tech file with a
``stdcell`` section (DLA_TECH, e.g. tech/ihp_sg13g2.json) but no PDK files.
"""
import json
import os
import tempfile

import pytest

from dla.stdcell.euler import orders
from dla.stdcell.netlist import CATALOG, check_function, make_cell

CELLS = ["%s_%d" % (b, d) for b, (_, _, _, ds) in CATALOG.items() for d in ds]


@pytest.mark.parametrize("spec", CELLS)
def test_networks(spec):
    base, d = spec.rsplit("_", 1)
    cn = make_cell(base, int(d))
    assert check_function(cn) == 0
    breaks, seqs = orders(cn)
    assert breaks == 0 and seqs


def test_switch_sim_catches_a_wrong_network():
    cn = make_cell("nand2")
    cn.devices = [d for d in cn.devices if d.name != "MP1"]      # drop one pull-up
    assert check_function(cn) > 0


def test_series_finger_folding_and_single_row_breaks():
    # Multi-finger series gate folds whole branches so each finger has its own internal node
    cn = make_cell("nand2", 2)
    assert len(cn.internal) == 2
    breaks, seqs = orders(cn)
    assert breaks == 0 and seqs
    # Network where NMOS is continuous (Y-A-x1-B-VSS) but PMOS must break (no shared net):
    # single-row break ("p") keeps NMOS diffusion unbroken
    from dla.stdcell.netlist import CellNetlist, Transistor
    c = CellNetlist("asym", "inv", "AB", [], lambda v: 0, 1)
    c.devices = [
        Transistor("MN0", "n", "A", "Y", "x1", 1.0),
        Transistor("MN1", "n", "B", "x1", "VSS", 1.0),
        Transistor("MP0", "p", "A", "VDD", "Y", 1.0),
        Transistor("MP1", "p", "B", "n2", "n3", 1.0),
    ]
    brk, s = orders(c)
    assert brk == 1 and any(col[3] == "p" for col in s[0])


def test_pimp_cont_space_rule():
    st = _tech()
    import copy
    from dla.stdcell import cell as C
    from dla.stdcell.rules import StdTech
    d = tempfile.mkdtemp()
    raw = copy.deepcopy(st.raw)
    raw["stdcell"]["rules"]["pimp_cont_space"] = 0.25
    raw["stdcell"]["row_height"] = max(raw["stdcell"]["row_height"], 4.20)
    p = os.path.join(d, "strict_pimp.json")
    json.dump(raw, open(p, "w"))
    st2 = StdTech(p)
    c, ca = st2.r["cont_size"], st2.r["cont_activ_enc"]
    # VSS tap contact top is c/2; ptap_imp must enclose it by >= 0.25
    assert st2.ptap_imp >= c / 2 + 0.25 - 1e-6
    # NMOS active contact bottom is nb + ca; must sit >= 0.25 above ptap_imp
    assert (st2.nb + ca) - st2.ptap_imp >= 0.25 - 1e-6
    # Single-row break placement in _columns produces split active on the broken row only
    from dla.stdcell.netlist import CellNetlist, Transistor
    cn = CellNetlist("asym", "inv", "AB", [], lambda v: 0, 1)
    cn.devices = [
        Transistor("MN0", "n", "A", "Y", "x1", 1.0),
        Transistor("MN1", "n", "B", "x1", "VSS", 1.0),
        Transistor("MP0", "p", "A", "VDD", "Y", 1.0),
        Transistor("MP1", "p", "B", "VDD", "x2", 1.0),
    ]
    _, chains, _ = C._columns(st2, cn, [("A", ("Y", "x1"), ("VDD", "Y"), False, ("MN0", "MP0")),
                                        ("B", ("x1", "VSS"), ("x2", "VDD"), "p", ("MN1", "MP1"))])
    assert sum(1 for ch in chains if "n" in ch["rows"]) == 1
    assert sum(1 for ch in chains if "p" in ch["rows"]) == 2


def _tech():
    path = os.environ.get("DLA_TECH")
    if not path or "stdcell" not in json.load(open(path)):
        pytest.skip("set DLA_TECH to a tech file with a stdcell section")
    from dla.stdcell.rules import StdTech
    return StdTech(path)


@pytest.fixture(scope="module")
def lib():
    st = _tech()
    from dla.stdcell import make_library
    d = tempfile.mkdtemp()
    return make_library(st, d, "t_lib", cells=["inv_1", "nand2_1", "nor2_1"], log=lambda *a: None)


def test_library_files(lib):
    for k in ("gds", "lef", "spice", "verilog", "json"):
        assert os.path.getsize(lib["paths"][k]) > 0
    lef = open(lib["paths"]["lef"]).read()
    assert "MACRO dla_nand2_1" in lef and "PIN VDD" in lef


def test_pin_access_on_tracks(lib):
    st = lib["st"]
    for cl in lib["cells"]:
        if cl.netlist is None:
            continue
        xs = [cl.access[p][0] for p in cl.netlist.inputs + [cl.netlist.output]]
        assert len(set(xs)) == len(xs)
        assert all(abs((x - st.x_off) / st.gx - round((x - st.x_off) / st.gx)) < 1e-6 for x in xs)


def test_drc_lvs(lib):
    check = pytest.importorskip("dla.stdcell.check")
    st, p = lib["st"], lib["paths"]
    for cl in lib["cells"]:
        assert check.drc(p["gds"], cl.name, st) == {}, cl.name
        if cl.netlist is not None:
            ok, msg = check.lvs(p["gds"], cl, st, cl.netlist.spice(st.nmos, st.pmos, st.L))
            assert ok, (cl.name, msg)
    assert check.drc(p["gds"], "t_lib_test", st) == {}


def test_lvs_catches_a_wrong_netlist(lib):
    check = pytest.importorskip("dla.stdcell.check")
    st, p = lib["st"], lib["paths"]
    cl = [c for c in lib["cells"] if c.name == "dla_nand2_1"][0]
    wrong = make_cell("nor2", 1, "dla_", wn=st.wn, wp=st.wp).spice(st.nmos, st.pmos, st.L)
    assert not check.lvs(p["gds"], cl, st, wrong.replace("dla_nor2_1", "dla_nand2_1"))[0]
