"""Optional PDK-aware SPICE artifacts: tech schema, structural SPICE, metadata, SVG schematics,
opt-in simulation policy, and the three output paths (std-cell library, decoder/block, SRAM).

Simulation is exercised with a fake simulator script (behaviour picked by FAKE_MODE) so no PDK or
ngspice is needed; one test runs the real ngspice with generic Level-1 models when it is installed
(``DLA_SPICE`` or ``ngspice`` on PATH).  The device model file is a throw-away generic one.
"""
import copy
import json
import os
import sys
import types
import xml.etree.ElementTree as ET

import pytest

from dla import schematic
from dla import spice_artifacts as SA
from dla.spice import SpiceRunner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IHP = os.path.join(ROOT, "tech", "ihp_sg13g2.json")
GENERIC_MODELS = (".model sg13_lv_nmos nmos level=1 vto=0.45 kp=300u lambda=0.05\n"
                  ".model sg13_lv_pmos pmos level=1 vto=-0.45 kp=100u lambda=0.05\n")

FAKE = r'''
import os, pathlib, re, sys
a = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "ok")
log = pathlib.Path(a[a.index("-o") + 1])
deck = pathlib.Path(a[-1]).read_text()
vdd = float(os.environ.get("FAKE_VDD", "1.2"))
if mode == "exit2":
    sys.stderr.write("fatal: cannot open model file\n")
    sys.exit(2)
rows = []
for m in re.finditer(r"^\.measure\s+(\w+)\s+(\w+)\s+(.*)$", deck, re.M | re.I):
    kind, name = m.group(1).lower(), m.group(2)
    if kind == "tran":
        val = "1.5e-10"
    elif name.startswith(("hi_", "lo_")):
        high = name.startswith("hi_") ^ (mode == "flip")
        val = "%.6g" % (vdd * (0.995 if high else 0.004))
    else:                                    # sram: q_<scenario> / qb_<scenario>
        scen = name.split("_", 1)[1]
        want_q = scen in ("hold1", "read1", "write1")
        high = (name.startswith("q_") == want_q) ^ (mode == "flip")
        val = "%.6g" % (vdd * (0.995 if high else 0.004))
        if scen.startswith("read") and not high:
            val = "%.6g" % (vdd * 0.05)
    rows.append("%-20s =  %s" % (name, "failed" if mode == "failed" else val))
log.write_text("Circuit: fake\n" + "\n".join(rows) + "\n")
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "fake_spice.py"
    script.write_text(FAKE)
    monkeypatch.setenv("FAKE_VDD", "1.2")
    monkeypatch.delenv("DLA_SPICE", raising=False)
    monkeypatch.delenv("FAKE_MODE", raising=False)
    return "%s %s" % (sys.executable, script)


def make_tech(tmp_path, spice=True, name="tech.json", **over):
    d = json.load(open(IHP))
    d.pop("files", None)
    if spice:
        (tmp_path / "models.lib").write_text(GENERIC_MODELS)
        d["spice"] = dict({"includes": ["models.lib"], "vdd": 1.2, "temperature": 27, "corner": "tt"}, **over)
    path = tmp_path / name
    path.write_text(json.dumps(d))
    return str(path)


# ---------------------------------------------------------------------------- schema
def test_config_absent_is_unconfigured_and_not_ready():
    cfg = SA.SpiceConfig.from_tech({"name": "x"})
    assert not cfg.configured and cfg.vdd is None and cfg.includes == []
    assert any("spice.includes" in p for p in cfg.readiness())
    assert any("spice.vdd" in p for p in cfg.readiness())


def test_config_full_parse_and_paths(tmp_path, monkeypatch):
    (tmp_path / "m.lib").write_text("*")
    monkeypatch.setenv("PDK_ROOT_T", str(tmp_path))
    raw = {"includes": ["${PDK_ROOT_T}/m.lib", {"file": "m.lib", "section": "tt"}], "cell_library": "cells.cdl",
           "vdd": "3.3", "temperature": -40, "corner": "ss",
           "testbench": {"edge": "0.1n", "load": "10f", "max_vectors": 5, "high_fraction": 0.8}}
    cfg = SA.SpiceConfig(raw, str(tmp_path), "t")
    assert cfg.configured and cfg.vdd == pytest.approx(3.3) and cfg.temperature == -40
    assert cfg.includes[1]["section"] == "tt" and cfg.cell_library == ["cells.cdl"]
    assert cfg.testbench["edge"] == pytest.approx(1e-10) and cfg.testbench["max_vectors"] == 5
    assert cfg.testbench["low_fraction"] == 0.1                      # default kept
    assert cfg.resolve("m.lib")[0] == str(tmp_path / "m.lib")
    assert cfg.readiness() == []
    d = cfg.describe()
    assert d["includes"][0]["exists"] and len(d["includes"][0]["sha256"]) == 64
    monkeypatch.delenv("PDK_ROOT_T")
    path, why = cfg.resolve("${PDK_ROOT_T}/m.lib")
    assert path is None and "PDK_ROOT_T" in why
    assert any("PDK_ROOT_T" in p for p in cfg.readiness())


@pytest.mark.parametrize("raw,msg", [
    ({"bogus": 1}, "unknown spice key"),
    ({"vdd": 0}, "must be positive"),
    ({"vdd": "abc"}, "finite number"),
    ({"includes": [{"section": "tt"}]}, "includes entry"),
    ({"includes": [{"file": "a", "section": "bad name"}]}, "section"),
    ({"cell_library": [3]}, "cell_library"),
    ({"corner": 5}, "corner"),
    ({"testbench": {"nope": 1}}, "testbench"),
    ({"testbench": {"max_vectors": 0}}, "max_vectors"),
    ({"testbench": {"high_fraction": 0.05}}, "low_fraction"),
    ("text", "must be an object"),
])
def test_config_rejects_bad_input(raw, msg):
    with pytest.raises(ValueError, match=msg):
        SA.SpiceConfig(raw, None, "t")


def test_options_validate():
    assert SA.SpiceOptions().simulate == "off"
    with pytest.raises(ValueError):
        SA.SpiceOptions("sometimes")


# ---------------------------------------------------------------------------- netlist parsing
CDL = """* comment
.SUBCKT INV A Y VDD VSS
MN0 Y A VSS VSS nmos4 W=3u L=0.5u $ trailing comment
MP0 Y A VDD VDD pmos4 W=6u
+ L=0.5u
.ENDS INV
.SUBCKT NAND2 A B Y VDD VSS
MN0 Y A x VSS nmos4
MN1 x B VSS VSS nmos4
MP0 Y A VDD VDD pmos4
MP1 Y B VDD VDD pmos4
.ENDS NAND2
.SUBCKT AND2 A B Y VDD VSS
XN A B n0 VDD VSS NAND2
XI n0 Y VDD VSS INV
R1 a b 1k
.ENDS AND2
.SUBCKT AOI A B C Y VDD VSS
MN0 Y A x VSS nmos4
MN1 x B VSS VSS nmos4
MN2 Y C VSS VSS nmos4
MP0 Y A y1 VDD pmos4
MP1 y1 B VDD VDD pmos4
MP2 y1 C z VDD pmos4
.ENDS AOI
"""


def test_parse_classify_and_count():
    subs = SA.parse_netlist(CDL)
    assert list(subs) == ["INV", "NAND2", "AND2", "AOI"]
    inv = subs["INV"]
    assert [m.kind for m in inv.mos] == ["n", "p"] and inv.mos[1].params == {"w": "6u", "l": "0.5u"}
    assert subs["AND2"].insts == [("XN", ["A", "B", "n0", "VDD", "VSS"], "NAND2"),
                                  ("XI", ["n0", "Y", "VDD", "VSS"], "INV")] and subs["AND2"].other == ["R1"]
    assert SA.lookup(subs, "nand2") is subs["NAND2"] and SA.lookup(subs, "zzz") is None
    assert SA.classify_cell(subs["INV"])["kind"] == "inv"
    assert SA.classify_cell(subs["NAND2"])["kind"] == "nand2"
    assert SA.classify_cell(subs["AND2"])["kind"] == "unknown"        # hierarchical: not a leaf
    assert SA.count_devices(subs, "AND2") == {"nmos": 3, "pmos": 3, "other": 1}
    info = SA.describe_netlist(subs, "AND2")
    assert info["subckt_count"] == 4 and info["top_pins"] == ["A", "B", "Y", "VDD", "VSS"]
    assert info["devices"]["nmos"] == 3


def test_classify_functions_by_switch_level():
    from dla.stdcell.netlist import CATALOG, make_cell
    for base, want in (("inv", "inv"), ("nand2", "nand2"), ("nand3", "nand3"), ("nor2", "nor2"), ("nor3", "nor3"),
                       ("aoi21", "complex")):
        cn = make_cell(base)
        subs = SA.parse_netlist(cn.spice("nm", "pm", 0.13))
        assert SA.classify_cell(subs[cn.name])["kind"] == want, base
    assert "and2" in CATALOG


# ---------------------------------------------------------------------------- structural SPICE
def fake_block(tmp_path, cell_lib=True, missing=False, enable_tech=True):
    lib = tmp_path / "cells.cdl"
    lib.write_text(CDL)
    raw = {"cell_library": ["cells.cdl"]} if cell_lib else None
    cfg = SA.SpiceConfig(raw, str(tmp_path), "fake")
    t = types.SimpleNamespace(
        name="fake", vdd="VDD", vss="VSS", path=str(tmp_path / "t.json"), raw={"spice": raw} if raw else {},
        cells={"inv": {"cell": "INV", "pins": {"A": "A", "Y": "Y"}},
               "nand2": {"cell": "NAND2", "pins": {"A": "A", "B": "B", "Y": "Y"}},
               "nor2": {"cell": "NOR2", "pins": {"A": "A", "B": "B", "Y": "Y"}},
               "nor3": {"cell": "NOR3", "pins": {"A": "A", "B": "B", "C": "C", "Y": "Y"}}})
    insts = [("u_inv", "INV", 0, 0, "R0"), ("u_nand", "NAND2" if not missing else "MISSING", 1, 0, "R0"),
             ("fill0", "FILL1", 2, 0, "R0")]
    conn = {"A": [("u_inv", "A"), ("u_nand", "A")], "An0": [("u_inv", "Y"), ("u_nand", "B")],
            "Y": [("u_nand", "Y")]}
    blk = types.SimpleNamespace(name="blk", t=t, insts=insts, conn=conn, ports=[("A", "input", 1), ("Y", "output", 1)],
                                meta={"block": "other"})
    return blk, cfg


def test_block_netlist_uses_real_cell_pins(tmp_path):
    blk, cfg = fake_block(tmp_path)
    n = SA.block_netlist(blk, cfg)
    assert n["status"] == "ok" and n["pins"] == ["A", "Y", "VDD", "VSS"]
    lines = n["text"].splitlines()
    assert ".SUBCKT blk A Y VDD VSS" in lines
    assert "Xu_inv A An0 VDD VSS INV" in lines                       # INV pin order from the library: A Y VDD VSS
    assert "Xu_nand A An0 Y VDD VSS NAND2" in lines               # power pins defaulted, signal pins by name
    assert not any("fill0" in x for x in lines) and n["dangling"] == []
    assert n["text"].count(".SUBCKT INV") == 1 and ".SUBCKT NOR2" not in n["text"]   # only used cells are copied
    subs = SA.parse_netlist(n["text"])
    assert SA.count_devices(subs, "blk") == {"nmos": 3, "pmos": 3, "other": 0}


def test_block_netlist_skips_instead_of_fabricating(tmp_path):
    blk, cfg = fake_block(tmp_path, cell_lib=False)
    n = SA.block_netlist(blk, cfg)
    assert n["status"] == "skipped" and "spice.cell_library" in n["reason"] and n["text"] is None
    blk, cfg = fake_block(tmp_path, missing=True)
    n = SA.block_netlist(blk, cfg)
    assert n["status"] == "skipped" and n["missing"] == ["MISSING"] and "MISSING" in n["reason"]
    cfg2 = SA.SpiceConfig({"cell_library": ["nofile.cdl"]}, str(tmp_path), "f")
    assert "not found" in SA.block_netlist(blk, cfg2)["reason"]


def test_finalize_block_metadata_and_schematic_without_simulation(tmp_path):
    blk, cfg = fake_block(tmp_path)
    out = SA.finalize_block(blk, str(tmp_path / "o"), {}, SA.SpiceOptions())
    meta = json.load(open(out["paths"]["spice_json"]))
    assert meta["schema"] == SA.SCHEMA and meta["artifact"] == {"kind": "block", "name": "blk"}
    assert meta["netlist"]["status"] == "ok" and meta["netlist"]["devices"] == {"nmos": 3, "pmos": 3, "other": 0}
    assert meta["netlist"]["top_pins"] == ["A", "Y", "VDD", "VSS"]
    assert meta["simulation"]["status"] == "not_requested" and "measurements" not in meta["simulation"]
    assert meta["spice_config"]["vdd_v"] is None and meta["provenance"]["tech"] == "fake"
    assert out["ok"] and os.path.isfile(out["paths"]["schematic"]) and os.path.isfile(out["paths"]["spice"])
    # without a cell library: still a schematic from the layout netlist, honest metadata, no .spice
    blk2, _ = fake_block(tmp_path, cell_lib=False)
    out2 = SA.finalize_block(blk2, str(tmp_path / "o2"), {}, SA.SpiceOptions(simulate="auto"))
    m2 = json.load(open(out2["paths"]["spice_json"]))
    assert "spice" not in out2["paths"] and m2["netlist"]["status"] == "skipped"
    assert m2["simulation"]["status"] == "unavailable" and "no structural netlist" in m2["simulation"]["reason"]
    assert out2["ok"] is True                       # auto tolerates unavailable ...
    out3 = SA.finalize_block(blk2, str(tmp_path / "o3"), {}, SA.SpiceOptions(simulate="require"))
    assert out3["ok"] is False                      # ... require does not


# ---------------------------------------------------------------------------- SVG
def valid_svg(text):
    root = ET.fromstring(text)
    assert root.tag.endswith("svg")
    return root


def test_cell_svg_content_determinism_and_escaping():
    from dla.stdcell.netlist import make_cell
    cn = make_cell("nand2", 2)
    sub = SA.parse_netlist(cn.spice("nm", "pm", 0.13))[cn.name]
    svg1, w, h = schematic.cell_svg(sub, title='dla_<nand2> & "x"', notes=[("SPICE simulation: not requested", "k")],
                                    function="nand2")
    svg2, _, _ = schematic.cell_svg(sub, title='dla_<nand2> & "x"', notes=[("SPICE simulation: not requested", "k")],
                                    function="nand2")
    assert svg1 == svg2                                     # deterministic
    valid_svg(svg1)
    assert "&lt;nand2&gt; &amp; &quot;x&quot;" in svg1 and "<nand2>" not in svg1
    for needle in (">VDD<", ">VSS<", ">A<", ">B<", "MN0", "MP3", "NMOS", "PMOS", "x1", "SPICE simulation"):
        assert needle in svg1, needle
    assert svg1.count(">NMOS<") == 4 and svg1.count(">PMOS<") == 4 and w > 300 and h > 200


def test_cell_svg_handles_cross_coupled_and_multistage_cells():
    from dla.sram import size_6t, make_sram_netlist
    from dla.stdcell.rules import StdTech
    st = StdTech(IHP)
    sub = SA.parse_netlist(make_sram_netlist(size_6t(st), "bit").spice("nm", "pm", 0.13))["bit"]
    svg, _, _ = schematic.cell_svg(sub, function="6T")
    valid_svg(svg)
    for needle in (">BL<", ">BLB<", ">WL<", "MN_PGL", "MP_PUR", ">Q<", ">QB<"):
        assert needle in svg, needle
    from dla.stdcell.netlist import make_cell
    and2 = SA.parse_netlist(make_cell("and2", prefix="").spice("nm", "pm", 0.13))["and2_1"]
    svg, _, _ = schematic.cell_svg(and2)
    valid_svg(svg)
    assert ">n0<" in svg


def test_block_svg_small_and_collapsed(tmp_path):
    blk, cfg = fake_block(tmp_path)
    subs = SA.parse_netlist(CDL)
    sim = {"requested": "off", "status": "not_requested", "reason": "x"}
    svg, w, h = schematic.block_svg(blk, subs, cfg, sim)
    assert svg == schematic.block_svg(blk, subs, cfg, sim)[0]
    valid_svg(svg)
    for needle in ("blk", ">A<", ">Y<", "2 gates", "not requested"):
        assert needle in svg, needle
    # many identical gates collapse into counted groups
    insts, conn = [], {}
    for i in range(80):
        insts.append(("g%d" % i, "INV", i, 0, "R0"))
        conn["i%d" % i] = [("g%d" % i, "A")]
        conn["Y[%d]" % i] = [("g%d" % i, "Y")]
    big = types.SimpleNamespace(**dict(vars(blk), insts=insts, conn=conn, ports=[("Y", "output", 80)]))
    svg, _, _ = schematic.block_svg(big, subs, cfg, sim)
    valid_svg(svg)
    assert "x80" in svg and "grouped" in svg


def test_array_svg():
    cfg = SA.SpiceConfig(None)
    svg, _, _ = schematic.array_svg("sr", 8, 8, "sr_bitcell", "VDD", "VSS", cfg,
                                    {"status": "unavailable", "reason": "no simulator"})
    valid_svg(svg)
    for needle in ("WL[0]", "BLB[3]", "sr_bitcell", "4 x 4 shown of 8 x 8", "UNAVAILABLE", "no simulator"):
        assert needle in svg, needle
    assert "WL[5]" not in svg


# ---------------------------------------------------------------------------- policy
def test_policy_matrix():
    ok = SA.policy_ok
    assert ok({"requested": "off", "status": "not_requested"})
    assert ok({"requested": "auto", "status": "passed"}) and ok({"requested": "auto", "status": "unavailable"})
    assert not ok({"requested": "auto", "status": "failed"})
    assert ok({"requested": "require", "status": "passed"})
    assert not ok({"requested": "require", "status": "unavailable"}) and not ok({"requested": "require", "status": "failed"})
    assert "UNAVAILABLE" in SA.summary_line({"requested": "require", "status": "unavailable", "reason": "r"})
    assert "skipped" in SA.summary_line({"requested": "auto", "status": "unavailable", "reason": "r"})


def test_vector_selection():
    assert SA.select_vectors(3, False, 24) == [(a, 1) for a in range(8)]
    assert len(SA.select_vectors(4, True, 100)) == 32
    v = SA.select_vectors(10, True, 24)
    assert len(v) == 24 and v[0] == (0, 1) and (1023, 1) in v and (512, 1) in v and v == SA.select_vectors(10, True, 24)


# ---------------------------------------------------------------------------- the three output paths
@pytest.fixture(scope="module")
def lib_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("lib")
    return d


@pytest.fixture(scope="module")
def built_lib(lib_dir):
    """Std-cell library (the four block roles) generated with SPICE artifacts enabled but no simulation."""
    from dla.stdcell import make_library
    tech = make_tech(lib_dir)
    lib = make_library(tech, str(lib_dir / "lib"), "tlib", "t_", ["inv_1", "nand2_1", "nor2_1", "nor3_1"],
                       log=lambda *a: None,
                       spice=SA.SpiceOptions())
    return lib, tech


def test_stdcell_outputs(built_lib):
    lib, tech = built_lib
    p = lib["paths"]
    meta = json.load(open(p["spice_json"]))
    assert meta["artifact"] == {"kind": "stdcell", "name": "tlib"}
    assert meta["netlist"]["subckt_count"] >= 2 and meta["netlist"]["subckts"]["t_inv_1"]["mos"] == 2
    assert meta["cells"]["t_nand2_1"] == {"function": "nand2", "devices": 4, "inputs": ["A", "B"], "outputs": ["Y"]}
    assert meta["spice_config"]["vdd_v"] == 1.2 and meta["spice_config"]["corner"] == "tt"
    assert meta["spice_config"]["includes"][0]["file"] == "models.lib"
    assert meta["simulation"]["status"] == "not_requested"
    assert os.path.isfile(os.path.join(os.path.dirname(p["spice_json"]), "schematics", "t_inv_1.svg"))
    sheet = open(p["schematic"], encoding="utf-8").read()
    valid_svg(sheet)
    assert "t_inv_1" in sheet and "t_nand2_1" in sheet
    # the generated tech file carries the model paths (rebased) and the new cell library
    gen = json.load(open(p["tech"]))
    assert gen["spice"]["cell_library"] == ["tlib.spice"] and gen["spice"]["vdd"] == 1.2
    assert os.path.isfile(os.path.join(os.path.dirname(p["tech"]), gen["spice"]["includes"][0]))


def test_stdcell_default_call_is_unchanged(tmp_path):
    from dla.stdcell import make_library
    tech = make_tech(tmp_path, spice=False)
    lib = make_library(tech, str(tmp_path / "l"), "plain", "t_", ["inv_1"], log=lambda *a: None)
    assert "spice" not in lib and "spice_json" not in lib["paths"]
    assert sorted(os.listdir(str(tmp_path / "l"))) == ["plain.gds", "plain.json", "plain.lef", "plain.spice", "plain.v"]


def test_generated_tech_spice_section(tmp_path):
    from dla.stdcell import writers
    from dla.stdcell.rules import StdTech
    src = tmp_path / "src"
    src.mkdir()
    d = json.load(open(IHP))
    d["spice"] = {"includes": ["models/m.lib", {"file": "${PDK}/x.lib", "section": "tt"}, "/abs/y.lib"], "vdd": 1.2}
    (src / "t.json").write_text(json.dumps(d))
    st = StdTech(str(src / "t.json"))
    dst = tmp_path / "out" / "lib" / "lib.tech.json"
    dst.parent.mkdir(parents=True)
    writers.write_tech(str(dst), st, {}, {}, [], spice_file=str(dst.parent / "lib.spice"))
    sp = json.load(open(str(dst)))["spice"]
    assert sp["cell_library"] == ["lib.spice"] and sp["vdd"] == 1.2
    assert sp["includes"][0] == "../../src/models/m.lib"                       # relative -> re-based
    assert sp["includes"][1] == {"file": "${PDK}/x.lib", "section": "tt"}      # ${VAR} untouched
    assert sp["includes"][2] == "/abs/y.lib"
    writers.write_tech(str(dst), StdTech(IHP), {}, {}, [])                     # no section, no library: no key
    assert "spice" not in json.load(open(str(dst)))
    # a tech file without a spice section only gains the cell library
    writers.write_tech(str(dst), StdTech(IHP), {}, {}, [], spice_file="x/plain.spice")
    assert json.load(open(str(dst)))["spice"] == {"cell_library": ["plain.spice"]}


def test_stdcell_simulation_passes_fails_and_reports(tmp_path, fake, monkeypatch):
    from dla.stdcell import make_library
    tech = make_tech(tmp_path)
    kw = dict(log=lambda *a: None)
    monkeypatch.setenv("FAKE_MODE", "ok")
    lib = make_library(tech, str(tmp_path / "ok"), "s", "t_", ["inv_1", "nand2_1"],
                       spice=SA.SpiceOptions("require", command=fake), **kw)
    sim = lib["spice"]["sim"]
    assert lib["spice"]["ok"] and sim["status"] == "passed" and sim["reason"] is None
    assert {c["name"] for c in sim["checks"]} == {"t_inv_1.truth_table", "t_nand2_1.truth_table"}
    assert all(c["passed"] for c in sim["checks"])
    meas = {m["name"]: m["value"] for m in sim["measurements"]}
    assert meas["t_inv_1.v_out_high_min"] == pytest.approx(1.2 * 0.995) and meas["t_inv_1.v_out_low_max"] == pytest.approx(0.0048)
    assert sim["conditions"]["vdd_v"] == 1.2 and sim["not_measured"] == ["delay arcs: tech spice.testbench has no edge/load (not measured)"]
    assert os.path.isfile(sim["decks"][0]["deck"]) and os.path.isfile(sim["decks"][0]["log"])
    meta = json.load(open(lib["spice"]["paths"]["spice_json"]))
    assert meta["simulation"]["checks"] == sim["checks"]
    svg = open(lib["paths"]["schematic"], encoding="utf-8").read()
    assert "PASSED" in svg and "v_out_high_min=1.19" in svg

    monkeypatch.setenv("FAKE_MODE", "flip")                       # wrong logic levels -> actionable failure
    lib = make_library(tech, str(tmp_path / "bad"), "s", "t_", ["inv_1"], spice=SA.SpiceOptions("auto", command=fake), **kw)
    sim = lib["spice"]["sim"]
    assert sim["status"] == "failed" and not lib["spice"]["ok"] and "t_inv_1.truth_table" in sim["reason"]
    assert sim["checks"][0]["failed"][0]["v_out"] == pytest.approx(0.0048) or sim["checks"][0]["failed"]
    assert "FAILED" in open(lib["paths"]["schematic"], encoding="utf-8").read()

    monkeypatch.setenv("FAKE_MODE", "failed")                     # a '.measure ... failed' never passes silently
    lib = make_library(tech, str(tmp_path / "meas"), "s", "t_", ["inv_1"], spice=SA.SpiceOptions("auto", command=fake), **kw)
    assert lib["spice"]["sim"]["status"] == "failed" and "SpiceMeasureError" in lib["spice"]["sim"]["reason"]

    monkeypatch.setenv("FAKE_MODE", "exit2")                      # simulator crash -> failure with log pointer
    lib = make_library(tech, str(tmp_path / "crash"), "s", "t_", ["inv_1"], spice=SA.SpiceOptions("require", command=fake), **kw)
    sim = lib["spice"]["sim"]
    assert sim["status"] == "failed" and "SpiceFailure" in sim["reason"] and "fatal: cannot open model file" in sim["reason"]
    assert ".log" in sim["reason"]


def test_stdcell_delay_arcs_when_testbench_configured(tmp_path, fake, monkeypatch):
    from dla.stdcell import make_library
    tech = make_tech(tmp_path, testbench={"edge": "0.1n", "load": "10f"})
    monkeypatch.setenv("FAKE_MODE", "ok")
    lib = make_library(tech, str(tmp_path / "d"), "s", "t_", ["nand2_1"], log=lambda *a: None,
                       spice=SA.SpiceOptions("require", command=fake))
    sim = lib["spice"]["sim"]
    assert sim["status"] == "passed" and sim["not_measured"] == []
    arcs = {(d["input"], d["input_edge"]): d for d in sim["delays"]}
    assert set(arcs) == {("A", "rise"), ("A", "fall"), ("B", "rise"), ("B", "fall")}
    a = arcs[("A", "rise")]
    assert a["output_edge"] == "fall" and a["side_inputs"] == {"B": 1} and a["load_f"] == pytest.approx(1e-14)
    assert a["edge_s"] == pytest.approx(1e-10) and a["delay_s"] == pytest.approx(1.5e-10)
    svg = open(os.path.join(lib["paths"]["schematic_dir"], "t_nand2_1.svg"), encoding="utf-8").read()
    assert "tpd A rise -&gt; out fall = 1.5e-10 s" in svg


def test_stdcell_simulation_unavailable_reasons(tmp_path, fake, monkeypatch):
    from dla.stdcell import make_library
    kw = dict(log=lambda *a: None)
    tech = make_tech(tmp_path)
    lib = make_library(tech, str(tmp_path / "nosim"), "s", "t_", ["inv_1"],
                       spice=SA.SpiceOptions("auto", command=str(tmp_path / "no_such_ngspice")), **kw)
    sim = lib["spice"]["sim"]
    assert sim["status"] == "unavailable" and "not found" in sim["reason"] and lib["spice"]["ok"]
    assert "measurements" not in sim                                  # nothing fabricated
    lib = make_library(tech, str(tmp_path / "nosim2"), "s", "t_", ["inv_1"],
                       spice=SA.SpiceOptions("require", command=str(tmp_path / "no_such_ngspice")), **kw)
    assert not lib["spice"]["ok"]
    t2 = make_tech(tmp_path, spice=False, name="nospice.json")        # no models / vdd configured at all
    lib = make_library(t2, str(tmp_path / "nocfg"), "s", "t_", ["inv_1"], spice=SA.SpiceOptions("auto", command=fake), **kw)
    sim = lib["spice"]["sim"]
    assert sim["status"] == "unavailable" and "spice.includes" in sim["reason"] and "spice.vdd" in sim["reason"]
    assert lib["spice"]["ok"]
    for svg in (open(lib["paths"]["schematic"], encoding="utf-8").read(),
                open(os.path.join(lib["paths"]["schematic_dir"], "t_inv_1.svg"), encoding="utf-8").read()):
        assert "UNAVAILABLE" in svg and "VDD not configured" in svg and "spice.includes" in svg
    t3 = make_tech(tmp_path, name="nomodel.json")
    os.remove(str(tmp_path / "models.lib"))
    lib = make_library(t3, str(tmp_path / "nofile"), "s", "t_", ["inv_1"], spice=SA.SpiceOptions("auto", command=fake), **kw)
    assert "model file not found" in lib["spice"]["sim"]["reason"]
    monkeypatch.setenv("FAKE_MODE", "ok")


@pytest.fixture(scope="module")
def sram_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("sram")


def test_sram_outputs_and_simulation(sram_dir, fake, monkeypatch):
    from dla.sram import make_sram
    tech = make_tech(sram_dir)
    monkeypatch.setenv("FAKE_MODE", "ok")
    res = make_sram(tech, str(sram_dir / "s1"), 2, 2, "sr", log=lambda *a: None,
                    spice=SA.SpiceOptions("require", command=fake))
    sim = res["spice"]["sim"]
    assert sim["status"] == "passed" and {c["name"] for c in sim["checks"]} == {
        "bitcell." + s for s in ("hold1", "hold0", "read1", "read0", "write0", "write1")}
    meas = {m["name"]: m["value"] for m in sim["measurements"]}
    assert meas["read1.read_bump_v"] == pytest.approx(0.06) and "DC state checks only" in sim["not_measured"][0]
    meta = json.load(open(res["paths"]["spice_json"]))
    assert meta["artifact"] == {"kind": "sram", "name": "sr"} and meta["sram"]["bitcell_devices"] == 6
    assert meta["netlist"]["devices"] == {"nmos": 16, "pmos": 8, "other": 0}      # 2x2 cells x (4 N + 2 P)
    for k in ("schematic", "schematic_bitcell"):
        valid_svg(open(res["paths"][k], encoding="utf-8").read())
    assert "MN_PGL" in open(res["paths"]["schematic_bitcell"], encoding="utf-8").read()
    monkeypatch.setenv("FAKE_MODE", "flip")
    bad = make_sram(tech, str(sram_dir / "s2"), 2, 2, "sr", log=lambda *a: None,
                    spice=SA.SpiceOptions("auto", command=fake))
    assert bad["spice"]["sim"]["status"] == "failed" and not bad["spice"]["ok"]
    default = make_sram(tech, str(sram_dir / "s3"), 2, 2, "sr", log=lambda *a: None)
    assert "spice" not in default and "spice_json" not in default["paths"]


def test_decoder_outputs_from_generated_library(built_lib, tmp_path, fake, monkeypatch):
    """A block built from the generated cells gets structural SPICE; simulation uses the real netlist."""
    from dla.blocks import make_decoder
    from dla.export import write_all
    from dla.tech import Tech
    lib, _ = built_lib
    gen_tech = lib["paths"]["tech"]
    if "tech" not in lib["paths"]:
        pytest.skip("library lacks the roles for a block tech")
    tech = Tech(gen_tech)
    blk = make_decoder(4, tech=tech)
    paths = write_all(blk, str(tmp_path / "dec"))
    assert "spice" not in paths and "spice_json" not in paths                    # opt-in: old output unchanged
    monkeypatch.setenv("FAKE_MODE", "ok")
    paths = write_all(blk, str(tmp_path / "dec2"), spice=SA.SpiceOptions("require", command=fake))
    text = open(paths["spice"]).read()
    assert ".SUBCKT %s " % blk.name in text and ".SUBCKT t_inv_1" in text
    subs = SA.parse_netlist(text)
    total = SA.count_devices(subs, blk.name)
    logic = [i for i in blk.insts if "fill" not in i[1]]
    assert total["nmos"] + total["pmos"] > 0 and len(subs[blk.name].insts) == len(logic)
    meta = json.load(open(paths["spice_json"]))
    assert meta["artifact"]["kind"] == "block" and meta["block"]["type"] == "decoder"
    assert meta["netlist"]["top_pins"][-2:] == ["VDD", "VSS"] and meta["netlist"]["top_pins"][0] == "A[0]"
    sim = blk.spice["sim"]
    assert sim["requested"] == "require" and sim["status"] in ("passed", "failed")
    if sim["status"] == "passed":
        assert sim["checks"][0]["vectors"] == 16 and sim["conditions"]["outputs_checked"] == "all"
    svg = open(paths["schematic"], encoding="utf-8").read()
    valid_svg(svg)
    assert blk.name in svg and ">Y[15]<" in svg and ">A[3]<" in svg


def test_decoder_structural_names_match_layout_connections(built_lib, tmp_path):
    from dla.blocks import make_decoder
    from dla.tech import Tech
    lib, _ = built_lib
    tech = Tech(lib["paths"]["tech"])
    blk = make_decoder(4, tech=tech)
    cfg = SA.SpiceConfig.from_tech(tech)
    n = SA.block_netlist(blk, cfg)
    assert n["status"] == "ok" and n["missing"] == [] and n["dangling"] == []
    assert set(n["cells_used"]) <= {"t_inv_1", "t_nand2_1", "t_nor2_1", "t_nor3_1"} and n["cells_used"]
    wired = {i for ips in blk.conn.values() for i, _ in ips}
    assert n["text"].count("\nX") == len(wired)


# ---------------------------------------------------------------------------- real simulator
def _real_runner():
    r = SpiceRunner()
    return r if r.available else None


@pytest.mark.skipif(_real_runner() is None, reason="ngspice not installed (set DLA_SPICE or put it on PATH)")
def test_real_ngspice_all_three_artifacts(tmp_path):
    """Generic Level-1 models through the real simulator: stdcell truth tables, decoder, SRAM bit-cell."""
    from dla.blocks import make_decoder
    from dla.export import write_all
    from dla.sram import make_sram
    from dla.stdcell import make_library
    from dla.tech import Tech
    tech = make_tech(tmp_path)
    opts = SA.SpiceOptions("require", timeout=240)
    lib = make_library(tech, str(tmp_path / "lib"), "rl", "r_", ["inv_1", "nand2_1", "nor2_1", "nor3_1"],
                       log=lambda *a: None, spice=opts)
    sim = lib["spice"]["sim"]
    assert sim["status"] == "passed", sim["reason"]
    assert all(c["passed"] for c in sim["checks"]) and len(sim["checks"]) == 4
    res = make_sram(tech, str(tmp_path / "sr"), 2, 2, "sr", log=lambda *a: None, spice=opts)
    assert res["spice"]["sim"]["status"] == "passed", res["spice"]["sim"]["reason"]
    blk = make_decoder(4, tech=Tech(lib["paths"]["tech"]))
    write_all(blk, str(tmp_path / "dec"), spice=opts)
    assert blk.spice["sim"]["status"] == "passed", blk.spice["sim"]["reason"]
    assert min(m["value"] for m in blk.spice["sim"]["measurements"] if m["name"] == "selected_output_high_min") > 1.1


# ---------------------------------------------------------------------------- helpers and CLI
def test_netlist_info_missing_file_shape(tmp_path):
    info, subs = SA._netlist_info(str(tmp_path / "nope.spice"), "top", "VDD", "VSS")
    assert info == {"status": "skipped", "file": None} and subs == {}
    info, subs = SA._netlist_info(None, "top", "VDD", "VSS")
    assert info["status"] == "skipped" and subs == {}


def _gen():
    sys.path.insert(0, ROOT)
    import importlib
    return importlib.import_module("gen")


def test_cli_policy_exit_codes_and_heuristic_line(tmp_path, capsys):
    gen = _gen()
    tech = make_tech(tmp_path)
    missing = str(tmp_path / "no_such_ngspice")
    base = ["--tech", tech, "--out", str(tmp_path / "o"), "--no-gds"]
    sram = ["sram", "--rows", "2", "--cols", "2"]
    assert gen.main(base + sram) == 0
    out = capsys.readouterr().out
    assert "simulation not requested" in out and "[meta ]" in out and "[schem]" in out and "RESULT: PASS" in out
    assert gen.main(base + ["--simulate", "auto", "--spice-cmd", missing] + sram) == 0
    assert "[spice] skipped:" in capsys.readouterr().out
    assert gen.main(base + ["--simulate", "require", "--spice-cmd", missing] + sram) == 1
    out = capsys.readouterr().out
    assert "[spice] UNAVAILABLE:" in out and "RESULT: FAIL" in out
    assert "checks on" not in out and ": PASS" in out.split("[sim ]")[1].splitlines()[0]    # heuristic line unaffected
    assert gen.main(base + ["--no-schematic"] + sram) == 0
    assert "[schem]" not in capsys.readouterr().out
    assert gen.main(base + ["--simulate", "require", "--spice-cmd", missing, "stdcell", "--cells", "inv_1"]) == 1
    assert "RESULT: FAIL" in capsys.readouterr().out
