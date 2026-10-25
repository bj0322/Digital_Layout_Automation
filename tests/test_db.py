"""pytest: design database (dla.db) - LEF read, DEF and structural Verilog round trips.

The tests on the hand-written files in tests/data need nothing; the IHP tests need a
tech file with `files.tech_lef` (DLA_TECH, e.g. tech/ihp_sg13g2.json with PDK_ROOT set).
"""
import os

import pytest

from dla.db import (Design, Library, ParseError, format_def, format_verilog, parse_def,
                    parse_verilog, read_def, read_verilog, write_def)
from dla.db.geom import ORIENTS, from_dbu, placed_size, to_dbu, xform_rect

DATA = os.path.join(os.path.dirname(__file__), "data")


def _data(name):
    return os.path.join(DATA, name)


# ----------------------------------------------------------------------------- geometry
def test_orientations():
    w, h, at = 2.0, 3.0, (10.0, 20.0)
    r = (0.0, 0.0, 0.5, 1.0)            # lower-left corner of the master
    want = {
        "N": (10.0, 20.0, 10.5, 21.0),
        "S": (11.5, 22.0, 12.0, 23.0),
        "FN": (11.5, 20.0, 12.0, 21.0),
        "FS": (10.0, 22.0, 10.5, 23.0),
        "W": (12.0, 20.0, 13.0, 20.5),      # R90: x' = -y, y' = x
        "E": (10.0, 21.5, 11.0, 22.0),      # R270: x' = y, y' = -x
        "FW": (10.0, 20.0, 11.0, 20.5),     # transpose
        "FE": (12.0, 21.5, 13.0, 22.0),
    }
    for o in ORIENTS:
        assert xform_rect(r, o, at, w, h) == want[o], o
        pw, ph = placed_size(o, w, h)
        box = xform_rect((0, 0, w, h), o, at, w, h)
        assert box == (at[0], at[1], at[0] + pw, at[1] + ph), o


def test_dbu():
    assert to_dbu(1.234, 1000) == 1234
    assert from_dbu(1234, 1000) == 1.234
    with pytest.raises(ValueError):
        to_dbu(1.2345, 1000)          # off the database grid: error, not a silent snap


# ----------------------------------------------------------------------------- LEF
def test_lef_tiny():
    lib = Library.read_lef(_data("tiny.lef"))
    assert lib.dbu == 2000 and lib.mfg_grid == 0.005
    assert lib.layer_order() == ["M1", "V1", "M2"]
    assert lib.routing_layers() == ["M1", "M2"] and lib.cut_layers() == ["V1"]
    m1, v1, m2 = (lib.layers[k] for k in ("M1", "V1", "M2"))
    assert (m1.direction, m1.pitch, m1.width, m1.spacing, m1.area) == ("HORIZONTAL", (0.4, 0.4), 0.14, 0.14, 0.06)
    assert (m1.rpersq, m1.cpersqdist, m1.edgecap) == (0.1, 3e-05, 2.5e-05)
    assert ["PROPERTY", "foo", '"a ; b"'] in m1.extra
    assert v1.enclosure == {"BELOW": (0.0, 0.05), "ABOVE": (0.01, 0.04)} and v1.rpersq == 4.5
    assert m2.pitch == (0.4, 0.5) and m2.offset == (0.2, 0.2)
    assert m2.min_spacing(0.2, 2.0) == 0.16 and m2.min_spacing(0.6, 2.0) == 0.30 and m2.min_spacing(0.6, 0.5) == 0.16
    v = lib.vias["V1_0"]
    assert v.default and v.layers() == ["M1", "V1", "M2"] and v.rects[1] == ("V1", (-0.075, -0.075, 0.075, 0.075))
    vr = lib.viarules["V1_GEN"]
    assert vr.generate and [l for l, _ in vr.layers] == ["M1", "M2", "V1"]
    assert lib.sites["core"].w == 0.4 and lib.sites["core"].h == 2.8
    inv = lib.macros["INV"]
    assert (inv.w, inv.h, inv.site, inv.symmetry) == (1.2, 2.8, "core", ["X", "Y"])
    assert list(inv.pins) == ["A", "Y", "VDD", "VSS"]
    assert [p.name for p in inv.signal_pins()] == ["A", "Y"]
    assert inv.pins["Y"].direction == "OUTPUT" and inv.pins["VDD"].use == "POWER"
    assert inv.pins["Y"].ports[0].polys[0][1][0] == (0.8, 2.2)
    assert inv.obs.rects == [("M1", (0.5, 0.6, 0.6, 2.2))]
    # ORIGIN 0.1 0: shapes are moved into the macro frame
    assert lib.macros["NAND2"].pins["A"].rects() == [(0.2, 1.0, 0.4, 1.6)]


def test_lef_errors():
    with pytest.raises(ParseError, match=r"bad.lef:6: malformed RECT"):
        Library().add_lef_text("MACRO X\n  SIZE 1 BY 1 ;\n  PIN A\n    PORT\n      LAYER M1 ;\n"
                               "      RECT 0 0 ;\n", "bad.lef")
    with pytest.raises(ParseError, match="end of file"):
        Library().add_lef_text("LAYER M1\n  TYPE ROUTING ;\n")


# ----------------------------------------------------------------------------- DEF
def test_def_round_trip():
    d = read_def(_data("small.def"))
    assert d.name == "small" and d.dbu == 2000 and d.die == [(0.0, 0.0), (8.0, 5.6)]
    assert len(d.insts) == 6 and len(d.nets) == 5 and len(d.specialnets) == 2 and len(d.ports) == 5
    u3 = d.insts["u3"]
    assert (u3.x, u3.y, u3.orient, u3.status, u3.source) == (0.4, 3.2, "FS", "FIXED", "NETLIST")
    assert d.insts["u5"].status == "UNPLACED" and "u\\[6\\]" in d.insts
    assert d.rows[1].orient == "FS" and d.rows[0].nx == 35 and d.rows[0].sx == 0.4
    assert d.tracks[1].layers == ["M1", "M2"]
    assert d.ports["y[0]"].status == "FIXED" and d.ports["y[0]"].shapes == [("M2", (-0.08, 0.0, 0.08, 0.2))]
    assert d.ports["VDD"].special
    a = d.nets["a"]
    assert a.conns[0] == ("PIN", "a") and a.use == "SIGNAL"
    assert [p.layer for p in a.wiring] == ["M2", "M1", "M1"]
    assert a.wiring[0].elems == [("pt", 0.6, 0.0, None), ("pt", 0.6, 1.2, None), ("via", "V1_0", None)]
    assert a.wiring[1].elems[1] == ("pt", 1.0, 1.2, 0.0)          # '*' resolved, extension kept
    assert a.wiring[2].elems[1] == ("virtual", 1.2, 1.2) and a.wiring[2].elems[-1] == ("via", "V1_2x1", "N")
    vdd = d.specialnets["VDD"]
    assert vdd.wiring[1].width == 0.3 and vdd.wiring[1].shape == "STRIPE" and vdd.conns == [("*", "VDD")]
    assert [p.status for p in d.nets["n2"].wiring] == ["ROUTED", "FIXED"]
    assert d.vias["V1_gen"].rule == "V1_GEN" and d.vias["V1_2x1"].rects[3][0] == "M2"
    assert d.blockages[0].soft and len(d.blockages[1].rects) == 2
    assert d.inst_pins("u4") == {"B": "n1", "Y": "y[1]", "A": "n2"}

    text = format_def(d)
    d2 = parse_def(text)
    assert d2 == d                                  # read -> write -> read: identical
    assert format_def(d2) == text                   # writing is deterministic


def test_def_write_file(tmp_path):
    d = read_def(_data("small.def"))
    p = write_def(d, str(tmp_path / "x.def"))
    assert read_def(p) == d


@pytest.mark.parametrize("text,msg", [
    ("DESIGN x ;\nUNITS DISTANCE MICRONS 1000 ;\nREGIONS 1 ;\n", ":3: unsupported DEF statement or section REGIONS"),
    ("DESIGN x ;\nCOMPONENTS 2 ;\n - a INV + PLACED ( 0 0 ) N ;\nEND COMPONENTS\nEND DESIGN\n",
     "header says 2 items, found 1"),
    ("DESIGN x ;\nCOMPONENTS 1 ;\n - a INV + PLACED ( 0 0 ) Q ;\nEND COMPONENTS\n", ":3: bad orientation"),
    ("DESIGN x ;\nCOMPONENTS 1 ;\n - a INV + HALO 1 2 3 4 ;\nEND COMPONENTS\n", "unsupported COMPONENTS option HALO"),
    ("DESIGN x ;\nNETS 1 ;\n - n ( a Y ) + ROUTED M1 ( 0 0 ) MASK 1 ( 1 0 ) ;\nEND NETS\n", "unsupported routing element"),
    ("DESIGN x ;\n", "missing END DESIGN"),
])
def test_def_errors(text, msg):
    with pytest.raises(ParseError, match=msg.replace("(", r"\(")):
        parse_def(text, "t.def")


def test_def_refuses_constants_and_off_grid():
    d = Design("c")
    d.add_inst("u1", "INV")
    d.connect("u1", "A", "1'b0")
    with pytest.raises(ValueError, match="tie cells"):
        format_def(d)
    d = Design("g", dbu=1000)
    d.add_inst("u1", "INV", x=0.0005, y=0.0, status="PLACED")
    with pytest.raises(ValueError, match="database grid"):
        format_def(d)


# ----------------------------------------------------------------------------- Verilog
def test_verilog_round_trip():
    d = read_verilog(_data("small.v"))
    assert d.name == "small"
    assert list(d.ports)[:6] == ["clk", "a[3]", "a[2]", "a[1]", "a[0]", "b"]
    assert d.buses == {"a": (3, 0), "y": (1, 0), "k": (2, 0)}
    # assign aliases: a port names the net, a constant beats everything
    assert d.ports["z"].net == "z" and ("u4", "A") in d.nets["z"].conns
    assert d.ports["odd.name"].net == "a[0]"
    assert d.ports["k[2]"].net == "1'b0" and d.ports["k[1]"].net == "k[1]"
    assert ("u1", "Y") in d.nets["k[1]"].conns                # t[3] is k[1]
    assert d.inst_pins("u5") == {"A": "esc\\[0\\]", "B": "1'b1", "Y": "y[0]"}
    assert "u\\[3\\]" in d.insts
    assert d.inst_pins("r0") == {"D": "z", "CK": "clk", "QN": "t[0]"}      # .Q() unconnected
    assert "unused" not in d.nets and "t[1]" not in d.nets

    text = format_verilog(d)
    d2 = parse_verilog(text)
    assert d2 == d
    assert format_verilog(d2) == text


def test_verilog_ansi_and_constants():
    d = parse_verilog("""
        module top (input wire [1:0] a, output [3:0] y, output z);
          assign y = {{2{a[0]}}, 2'b10};
          assign z = 1'bx;
          BUF u0 (.A(a[1]), .X());
        endmodule
        module unused_cell (input q); endmodule
    """, top="top")
    assert [d.ports[p].net for p in ("y[3]", "y[2]", "y[1]", "y[0]")] == ["a[0]", "a[0]", "1'b1", "1'b0"]
    assert d.ports["z"].net == "1'bx" and d.inst_pins("u0") == {"A": "a[1]"}


@pytest.mark.parametrize("text,msg", [
    ("module m (a);\n input a;\n always @(a) ;\nendmodule\n", ":3: unsupported statement 'always'"),
    ("module m (a);\n input a;\n INV u (a);\nendmodule\n", ":3: unsupported: positional connections"),
    ("module m (a);\n input [1:0] a;\n INV u (.A(a));\nendmodule\n", ":3: u.A: multi-bit connection"),
    ("module m (a);\n input a;\n INV u (.A(a[0]));\nendmodule\n", ":3: a is not a bus"),
    ("module m (a);\n input a;\n sub s (.x(a));\nendmodule\nmodule sub (x);\n input x;\nendmodule\n",
     "hierarchical instance s"),
    ("module m (a);\nendmodule\n", "port a has no direction"),
    ("module m (a);\n input a;\n /* open\n", ":3: unterminated"),
])
def test_verilog_errors(text, msg):
    with pytest.raises(ParseError, match=msg.replace("(", r"\(")):
        parse_verilog(text, top="m", fname="t.v")


# ----------------------------------------------------------------------------- IHP (needs the PDK)
def _ihp_lib():
    if not os.environ.get("DLA_TECH"):
        pytest.skip("set DLA_TECH to a tech file to run the PDK tests")
    from dla.tech import load
    tech = load()
    if not tech.tech_lef:
        pytest.skip("tech file has no files.tech_lef")
    return tech, Library.from_tech(tech)


def test_ihp_lef():
    tech, lib = _ihp_lib()
    assert lib.dbu == 1000 and lib.mfg_grid == 0.005
    assert lib.routing_layers()[:5] == ["Metal1", "Metal2", "Metal3", "Metal4", "Metal5"]
    assert [lib.layers[l].direction for l in lib.routing_layers()[:3]] == ["HORIZONTAL", "VERTICAL", "HORIZONTAL"]
    assert lib.layers["Metal2"].rpersq == 0.103 and lib.layers["Metal2"].min_spacing(0.4, 2.0) == 0.24
    assert lib.vias["Via1_XX"].rects[1] == ("Via1", (-0.095, -0.095, 0.095, 0.095))
    assert (lib.sites["CoreSite"].w, lib.sites["CoreSite"].h) == (0.48, 3.78)
    assert len(lib.macros) == len(tech.macros)
    for name, m in tech.macros.items():         # agrees with the block generators' LEF reader
        M = lib.macros[name]
        assert (M.w, M.h) == (m["w"], m["h"]), name
        for pin, rects in m["pins"].items():
            assert sorted(M.pins[pin].rects(tech.m1.lef)) == sorted(rects), (name, pin)
    dff = lib.macros["sg13g2_dfrbp_1"]
    assert dff.pins["CLK"].direction == "INPUT" and dff.pins["Q"].direction == "OUTPUT"


def test_ihp_def_round_trip_and_klayout(tmp_path):
    tech, lib = _ihp_lib()
    d = read_def(_data("ihp_small.def"))
    for i in d.insts.values():
        assert i.master in lib.macros, i.master
    for n in d.nets.values():
        for inst, pin in n.conns:
            assert inst == "PIN" or pin in lib.macros[d.insts[inst].master].pins, (inst, pin)
    out = str(tmp_path / "ihp_small.def")
    write_def(d, out)
    assert read_def(out) == d

    # independent check of syntax and orientation convention: KLayout's LEF/DEF reader
    db = pytest.importorskip("klayout.db")
    ly = db.Layout()
    opt = db.LoadLayoutOptions()
    cfg = db.LEFDEFReaderConfiguration()
    cfg.lef_files = tech.tech_lef + tech.lef
    opt.lefdef_config = cfg
    ly.read(out, opt)
    top = ly.top_cell()
    got = {}
    for inst in top.each_inst():
        name = inst.cell.name
        if name.startswith("VIA_"):
            continue
        m = lib.macros[name]
        b = inst.dcplx_trans * db.DBox(0, 0, m.w, m.h)
        got.setdefault(name, []).append((round(b.left, 6), round(b.bottom, 6), round(b.right, 6), round(b.top, 6)))
    want = {}
    for i in d.insts.values():
        want.setdefault(i.master, []).append(d.inst_bbox(i.name, lib))
    assert {k: sorted(v) for k, v in got.items()} == {k: sorted(v) for k, v in want.items()}
    # pin geometry follows the instance orientation
    assert ("Metal1", (15.22, 4.38, 15.52, 5.54)) in d.pin_shapes("u_buf", "X", lib)
