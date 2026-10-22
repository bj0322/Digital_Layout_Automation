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
