"""pytest: 6T SRAM sizing, switch-level verification, 4-way mirrored placement, layout DRC / LVS /
connectivity. Runs for every tech file found: $DLA_TECH, tech/ihp_sg13g2.json, local/nspl05.json.
"""
import copy
import json
import os
import shutil
import subprocess
import tempfile

import pytest

from dla import sram as S
from dla.stdcell import writers
from dla.stdcell.rules import StdTech, snap_up

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _techs():
    out = []
    for p in (os.environ.get("DLA_TECH"), os.path.join(ROOT, "tech", "ihp_sg13g2.json"),
              os.path.join(ROOT, "local", "nspl05.json")):
        if p and os.path.isfile(p) and os.path.abspath(p) not in [os.path.abspath(q) for q in out]:
            if "stdcell" in json.load(open(p)):
                out.append(p)
    return out


TECHS = _techs()
pytestmark = pytest.mark.skipif(not TECHS, reason="no tech file with a stdcell section")


@pytest.fixture(scope="module", params=TECHS, ids=lambda p: os.path.basename(p))
def tech(request):
    return StdTech(request.param)


@pytest.fixture(scope="module")
def built(tech):
    d = tempfile.mkdtemp()
    return S.make_sram(tech, d, rows=4, cols=4, name="t_sram", log=lambda *a: None)


# ------------------------------------------------------------------ sizing
def test_sizing_ratios_and_quantization(tech):
    sz = S.size_6t(tech)
    q = 2 * tech.mfg
    cmin = tech.r["cont_size"] + 2 * tech.r["cont_activ_enc"]
    for w in (sz.w_pu, sz.w_pg, sz.w_pd):
        assert abs(w / q - round(w / q)) < 1e-6
        assert w >= cmin - 1e-9
    assert sz.w_pg >= tech.min_nmos_width and sz.w_pd >= tech.min_nmos_width and sz.w_pu >= tech.min_pmos_width
    assert sz.cr >= tech.sram["cr"] - 1e-6 and sz.pr <= tech.sram["pr"] + 1e-6
    assert sz.snm_read_factor > 0 and sz.write_margin_factor > 0


def test_sizing_targets_and_overrides(tech):
    base = S.size_6t(tech)
    hi = S.size_6t(tech, cr=2.5, pr=0.7)
    assert hi.cr >= 2.5 - 1e-6 and hi.pr <= 0.7 + 1e-6
    assert hi.w_pd > base.w_pd and hi.w_pg >= hi.w_pu / 0.7 - 1e-6
    big = S.size_6t(tech, w_pu=base.w_pu * 2)
    assert big.w_pu >= base.w_pu * 2 - 1e-9 and big.pr <= tech.sram["pr"] + 1e-6
    wide = S.size_6t(tech, w_pg=base.w_pg * 1.5)
    assert wide.w_pg >= base.w_pg * 1.5 - 1e-9 and wide.cr >= tech.sram["cr"] - 1e-6
    longer = S.size_6t(tech, l=tech.L * 2)
    assert longer.l == pytest.approx(tech.L * 2) and longer.cr >= tech.sram["cr"] - 1e-6
    with pytest.raises(ValueError):
        S.size_6t(tech, l=tech.r["poly_width"] / 2)


def test_sizing_invalid_parameters(tech):
    bad = [0, 0.0, -1.0, float("nan"), float("inf"), float("-inf"), True, False]
    for param in ("cr", "pr", "l", "w_pu", "w_pg", "w_pd"):
        for v in bad:
            with pytest.raises(ValueError):
                S.size_6t(tech, **{param: v})
    for key in ("cr", "pr", "l"):
        for v in bad:
            st_bad = copy.copy(tech)
            st_bad.sram = dict(tech.sram, **{key: v})
            with pytest.raises(ValueError):
                S.size_6t(st_bad)
    for attr in ("min_nmos_width", "min_pmos_width"):
        for v in bad:
            st_bad = copy.copy(tech)
            setattr(st_bad, attr, v)
            with pytest.raises(ValueError):
                S.size_6t(st_bad)
    assert S.size_6t(tech).as_dict()["model"] == "heuristic_ratio"
    for rc in ((0, 1), (1, 0), (True, 1), (1, False), (1.5, 1), (-2, 1)):
        with pytest.raises(ValueError):
            S.make_sram(tech, tempfile.mkdtemp(), rc[0], rc[1], "t_bad", log=lambda *a: None)


def test_min_width_from_tech_not_activ_width():
    p = os.path.join(ROOT, "local", "nspl05.json")
    if not os.path.isfile(p):
        pytest.skip("local/nspl05.json not available")
    st = StdTech(p)
    assert st.r["activ_width"] == 0.7 and st.min_nmos_width == 1.6
    sz = S.size_6t(st)
    assert min(sz.w_pg, sz.w_pd, sz.w_pu) >= 1.6 - 1e-9


# ------------------------------------------------------------------ switch level
def test_switch_level_passes(tech):
    sz = S.size_6t(tech)
    cn = S.make_sram_netlist(sz)
    assert S.check_sram_cell(cn, sz) == []
    assert S.check_sram_array(3, 5, cn) == []


def test_switch_level_catches_bad_sizing_and_broken_cell(tech):
    sz = S.size_6t(tech)
    weak_pd = copy.copy(sz)
    weak_pd.w_pd = sz.w_pg * 0.8                              # CR < 1: read upset
    errs = S.check_sram_cell(S.make_sram_netlist(weak_pd), weak_pd)
    assert any(e.startswith("read") for e in errs)
    strong_pu = copy.copy(sz)
    strong_pu.w_pu = sz.w_pg * 5                              # PR >> 1: cannot write
    assert any(e.startswith("write") for e in S.check_sram_cell(S.make_sram_netlist(strong_pu), strong_pu))
    for dead in ("MP_PUL", "MN_PDR", "MN_PGL"):
        cn = S.make_sram_netlist(sz)
        cn.devices = [d for d in cn.devices if d.name != dead]
        assert S.check_sram_cell(cn, sz), dead
    cn = S.make_sram_netlist(sz)
    cn.devices[0].g = "Q"                                     # pull-up gate shorted to its own drain
    assert S.check_sram_cell(cn, sz)
    sim = S.SramArraySim(2, 2, S.make_sram_netlist(strong_pu))
    sim.write_row(0, [1, 0])
    assert sim.read_row(0) != [1, 0]


# ------------------------------------------------------------------ 4-way mirroring
@pytest.mark.parametrize("orient", writers.ORIENTS)
def test_orientation_origin_and_points(orient):
    W, H, x, yb = 2.0, 1.0, 5.0, 3.0
    pts = {o: writers.orient_point(o, 0.3, 0.2, x, yb, W, H) for o in writers.ORIENTS}
    assert pts["R0"] == (5.3, 3.2)
    assert pts["MX"] == pytest.approx((5.3, 3.8))
    assert pts["MY"] == pytest.approx((6.7, 3.2))
    assert pts["R180"] == pytest.approx((6.7, 3.8))
    # the cell's corners always land inside [x, x+W] x [yb, yb+H]
    for u, v in ((0, 0), (W, 0), (0, H), (W, H)):
        px, py = writers.orient_point(orient, u, v, x, yb, W, H)
        assert x - 1e-9 <= px <= x + W + 1e-9 and yb - 1e-9 <= py <= yb + H + 1e-9
    with pytest.raises(ValueError):
        writers.orient_origin("R90", 0, 0, W, H)


def test_gds_writer_and_klayout_agree_on_four_orientations():
    db = pytest.importorskip("klayout.db")
    from dla import gds
    W, H = 2.0, 1.0
    d = tempfile.mkdtemp()
    p = os.path.join(d, "o.gds")
    gw = writers.GdsWriter("o")
    gw.begin("m")
    gw.box((1, 0), (0.1, 0.2, 0.6, 0.5))           # asymmetric marker
    gw.box((9, 0), (0, 0, W, H))
    gw.end()
    gw.begin("top")
    place = {}
    for k, o in enumerate(writers.ORIENTS):
        place[o] = (k * 3.0 + 0.5, 0.25)
        gw.sref("m", *place[o], orient=o, size=(W, H))
    gw.end()
    gw.write(p)
    ly = db.Layout()
    ly.read(p)
    top = ly.cell("top")
    li = ly.layer(1, 0)
    boxes = [pp.bbox() for pp in db.Region(top.begin_shapes_rec(li)).each()]
    for o, (x, yb) in place.items():
        (x1, y1), (x2, y2) = [writers.orient_point(o, u, v, x, yb, W, H) for u, v in ((0.1, 0.2), (0.6, 0.5))]
        want = db.DBox(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        assert any(abs(b.left * ly.dbu - want.left) < 1e-6 and abs(b.bottom * ly.dbu - want.bottom) < 1e-6
                   and abs(b.right * ly.dbu - want.right) < 1e-6 and abs(b.top * ly.dbu - want.top) < 1e-6
                   for b in boxes), o
    # gds.build_gds (KLayout transformations) lands on the very same boxes
    dj = {"top": "t2", "row_h": H, "insts": [["i%d" % k, "m", place[o][0], place[o][1], o] for k, o in enumerate(writers.ORIENTS)],
          "layers": {"m1": [1, 0], "v1": [2, 0], "m2": [3, 0], "v2": [4, 0], "m3": [5, 0], "boundary": None},
          "shapes": [], "vias": [], "via_size": {"via1": 0.1, "via2": 0.1}, "pins": [], "cells": {"m": {"w": W}}}
    q = os.path.join(d, "b.gds")
    gds.build_gds(dj, p, q)
    ly2 = db.Layout()
    ly2.read(q)
    got = sorted((b.left, b.bottom, b.right, b.top) for b in
                 (pp.bbox() for pp in db.Region(ly2.cell("t2").begin_shapes_rec(ly2.layer(1, 0))).each()))
    assert got == sorted((b.left, b.bottom, b.right, b.top) for b in boxes)


def test_array_orientations_and_pitch(built):
    a = built["array"]
    W, H = a.W, a.H
    for name, cname, x, yb, o in a.insts:
        _, r, c = name.split("_")
        r, c = int(r), int(c)
        assert o == {(0, 0): "R0", (0, 1): "MY", (1, 0): "MX", (1, 1): "R180"}[(r % 2, c % 2)]
        assert x == pytest.approx(a.x_arr + c * W) and yb == pytest.approx(r * H)
    # a bit line / word line access point is invariant along its column / row
    for c in range(a.cols):
        xs = {round(p[2], 6) for p in a.pinpos if p[4] == "BL[%d]" % c}
        assert len(xs) == 1
        xs = {round(p[2], 6) for p in a.pinpos if p[4] == "BLB[%d]" % c}
        assert len(xs) == 1
    for r in range(a.rows):
        assert len({round(p[3], 6) for p in a.pinpos if p[4] == "WL[%d]" % r}) == 1


# ------------------------------------------------------------------ layout checks
def _deck(tech, gds_path, tops):
    if "nspl" not in tech.name:
        return
    deck_py = os.path.join(ROOT, "local", "nspl05_drc.py")
    if not os.path.isfile(deck_py):
        pytest.skip("local/nspl05_drc.py not available")
    import importlib.util
    spec = importlib.util.spec_from_file_location("nspl05_drc", deck_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for t in tops:
        assert mod.run(gds_path, t) == {}, t


def test_bitcell_geometry(built):
    cl, st, sz = built["cell"], built["st"], built["sizing"]
    # 4 NMOS + 2 PMOS gate rectangles = one active island per transistor (+ the two taps)
    act = [r for l, r, n in cl.rects if l == "activ"]
    assert len(act) == 6 + 2
    heights = sorted(round(r[3] - r[1], 6) for r in act if st.nb - 1e-9 <= r[1] <= st.nb + 1e-9)
    assert heights == sorted([sz.w_pd, sz.w_pd, sz.w_pg, sz.w_pg])
    assert cl.width == pytest.approx(cl.info["sites"] * st.site_w)
    assert set(cl.access) == {"BL", "BLB", "WL"}
    assert len({round(x, 6) for x, y in cl.access.values()}) == 3       # distinct M2 tracks
    for x, y in cl.access.values():
        assert abs((x - st.x_off) / st.gx - round((x - st.x_off) / st.gx)) < 1e-6
    # PMOS band and n-well follow the sized bands
    assert st.pb == pytest.approx(st.pt - sz.w_pu) and st.nt == pytest.approx(st.nb + sz.w_pd)


def test_drc_lvs_bitcell_2x2_array(built, tech):
    check = pytest.importorskip("dla.stdcell.check")
    st, p, n = built["st"], built["paths"], built["name"]
    for top in (n + "_bitcell", n + "_2x2", n):
        assert check.drc(p["gds"], top, st) == {}, top
    stats = {}
    ok, msg = check.lvs(p["gds"], built["cell"], st, built["cell"].netlist.spice(st.nmos, st.pmos, st.L, st.spice_units), stats)
    assert ok, msg
    assert stats == {"raw": 6, "combined": 6}
    _deck(tech, p["gds"], [n + "_bitcell", n + "_2x2", n])


def test_lvs_catches_a_wrong_sizing(built):
    check = pytest.importorskip("dla.stdcell.check")
    st, p, cl = built["st"], built["paths"], built["cell"]
    wrong = copy.copy(built["sizing"])
    wrong.w_pg = built["sizing"].w_pg * 2
    spice = S.make_sram_netlist(wrong, cl.name).spice(st.nmos, st.pmos, st.L, st.spice_units)
    assert not check.lvs(p["gds"], cl, st, spice)[0]


def test_array_connectivity_and_backend_drc(built):
    gds = pytest.importorskip("dla.gds")
    st, p, n = built["st"], built["paths"], built["name"]
    d = json.load(open(p["json"]))
    c = gds.verify_conn(d, p["gds"])
    assert c["ok"], c
    assert c["probes"] >= 4 * 4 * 3 + 16 * 2       # every cell pin and rail, every label
    assert gds.drc(p["gds"], n, st) == {}
    m = d["sram_metrics"]
    assert m["model"] == "heuristic_ratio"
    for k in ("w_pd", "w_pg", "w_pu", "l", "cr", "pr", "snm_read_factor", "write_margin_factor", "cell_area_um2"):
        assert k in m
    # connectivity check notices a swapped net
    bad = copy.deepcopy(d)
    bad["pinpos"][0][4] = "BL[1]"
    assert not gds.verify_conn(bad, p["gds"])["ok"]


def test_views(built):
    p = built["paths"]
    lef = open(p["lef"]).read()
    assert "MACRO t_sram_bitcell" in lef and "MACRO t_sram\n" in lef and "PIN BL[3]" in lef and "PIN WL[3]" in lef
    sp = open(p["spice"]).read()
    assert sp.count("sg13") + sp.count("nmos") + sp.count("pmos") >= 6 and ".SUBCKT t_sram " in sp
    assert "Xbc_3_3" in sp
    assert os.path.getsize(p["layout_v"]) > 0 and os.path.getsize(p["rtl"]) > 0 and os.path.getsize(p["tb"]) > 0


@pytest.mark.skipif(not shutil.which("iverilog"), reason="iverilog not installed")
def test_verilog_testbench(built):
    p, d = built["paths"], tempfile.mkdtemp()
    exe = os.path.join(d, "a.out")
    subprocess.run(["iverilog", "-g2005", "-o", exe, p["rtl"], p["tb"]], check=True)
    out = subprocess.run(["vvp", exe], capture_output=True, text=True, check=True).stdout
    assert "PASS t_sram" in out
    subprocess.run(["iverilog", "-g2005", "-t", "null", p["layout_v"]], check=True)


@pytest.mark.parametrize("rows,cols", [(1, 1), (2, 3), (5, 2)])
def test_odd_array_shapes(tech, rows, cols):
    check = pytest.importorskip("dla.stdcell.check")
    gds = pytest.importorskip("dla.gds")
    d = tempfile.mkdtemp()
    res = S.make_sram(tech, d, rows, cols, "t_odd", log=lambda *a: None)
    assert res["errors"] == []
    p, st = res["paths"], res["st"]
    assert check.drc(p["gds"], "t_odd", st) == {}
    assert gds.drc(p["gds"], "t_odd", st) == {}
    _deck(tech, p["gds"], ["t_odd"])
    assert gds.verify_conn(json.load(open(p["json"])), p["gds"])["ok"]


def test_cli_sram(tech, capsys):
    import gen
    pytest.importorskip("klayout.db")
    d = tempfile.mkdtemp()
    rc = gen.main(["--tech", tech.path, "--out", d, "sram", "--rows", "2", "--cols", "2", "--name", "t_cli"])
    out = capsys.readouterr().out
    assert rc == 0 and "RESULT: PASS" in out
    for tag in ("[sim ]", "[drc ]", "[lvs ]", "[conn]", "[png ]"):
        assert tag in out
    for f in ("overview.png", "bitcell.png", "array_corner.png", "t_cli.gds", "t_cli.json"):
        assert os.path.isfile(os.path.join(d, "t_cli", f)), f
