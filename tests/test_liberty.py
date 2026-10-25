"""pytest: Liberty reader (dla.db.liberty) and its join with the LEF library.

The tiny.lib tests need nothing; the IHP tests need DLA_TECH with `files.liberty`.
"""
import itertools
import os

import pytest
from pytest import approx

from dla.db import Library, ParseError
from dla.db.liberty import (format_liberty, parse_function, parse_liberty, parse_liberty_tree,
                            read_liberty)

TINY = os.path.join(os.path.dirname(__file__), "data", "tiny.lib")


# ----------------------------------------------------------------------------- tiny.lib
def test_units_pins_and_cells():
    lib = read_liberty(TINY)
    assert lib.name == "tiny" and lib.time_scale == approx(1e-3) and lib.cap_scale == approx(1e-3)
    assert lib.voltage == 1.2 and lib.thresholds["input_threshold_pct_rise"] == 50
    assert lib.slew_derate == 1.0                       # attribute without ';' at end of line
    assert lib.default_max_transition == approx(0.5)
    nand = lib.cells["NAND2"]
    assert nand.area == 2.5 and nand.footprint == "nand2" and not nand.dont_use
    assert list(nand.pins) == ["A", "B", "Y"]           # pin (A, B) declares two pins
    assert nand.pins["A"].capacitance == approx(0.002) and nand.pins["B"].rise_cap == approx(0.0021)
    y = nand.pins["Y"]
    assert y.max_cap == approx(0.1) and [a.related for a in y.arcs] == ["A", "B"]
    assert [p.name for p in nand.inputs()] == ["A", "B"] and [p.name for p in nand.outputs()] == ["Y"]
    dff = lib.cells["DFFR"]
    assert dff.dont_use and dff.is_sequential() and dff.seq.kind == "ff"
    assert dff.seq.clocked_on.text == "CK" and dff.seq.clear.truth(["RN"]) == [1, 0]
    assert dff.pins["CK"].clock and dff.pins["Q"].arc("CK", "rising_edge").sense == "non_unate"
    assert lib.cells["LATCH"].seq.kind == "latch" and lib.cells["LATCH"].seq.enable.text == "G"
    assert lib.cells["ICG"].unsupported == ["statetable"]


def test_tables():
    lib = read_liberty(TINY)
    arc = lib.cells["NAND2"].pins["Y"].arc("A")
    rise = arc.tables["cell_rise"]
    assert rise.vars == ["total_output_net_capacitance", "input_net_transition"]   # load first
    assert rise.index[0] == approx([0.001, 0.011]) and rise.index[1] == approx([0.01, 0.11, 0.21])
    # grid points
    for i, load in enumerate((0.001, 0.011)):
        for j, slew in enumerate((0.01, 0.11, 0.21)):
            assert rise.delay(slew, load) == approx(rise.values[i][j])
    assert rise.delay(0.06, 0.006) == approx(0.035)               # bilinear
    assert rise.delay(0.31, 0.001) == approx(0.04)                # linear extrapolation in slew
    assert rise.delay(0.01, 0.021) == approx(0.09)                # ... and in load
    assert rise.delay(0.0, 0.0) == approx(0.005)                  # below both first indices
    assert arc.tables["cell_fall"].index[0] == approx([0.002, 0.012])   # table overrides template index
    assert arc.tables["rise_transition"].delay(1.0, 1.0) == approx(0.015)          # scalar
    fall_tr = arc.tables["fall_transition"]                                       # 1-D in slew
    assert fall_tr.delay(0.06, 0.5) == approx(0.03) and fall_tr.delay(0.21, 0) == approx(0.06)
    setup = lib.cells["DFFR"].pins["D"].arc("CK", "setup_rising")
    assert setup.is_check() and setup.tables["rise_constraint"].constraint(0.11, 0.06) == approx(0.055)
    assert setup.tables["fall_constraint"].constraint(1, 1) == approx(0.025)


def test_functions():
    cases = {
        "!(A*B)": [1, 1, 1, 0], "(A B)'": [1, 1, 1, 0], "A & !B": [0, 0, 1, 0],
        "A+B": [0, 1, 1, 1], "A|B'": [1, 0, 1, 1], "A^B": [0, 1, 1, 0], "!(A^B)": [1, 0, 0, 1],
    }
    for text, want in cases.items():
        assert parse_function(text).truth(["A", "B"]) == want, text
    # precedence: ^ binds tighter than AND, AND tighter than OR (as in OpenSTA's grammar)
    f = parse_function("A*B^C+D")
    for a, b, c, d in itertools.product((0, 1), repeat=4):
        assert f.eval(dict(A=a, B=b, C=c, D=d)) == ((a & (b ^ c)) | d)
    assert parse_function("1").truth([]) == [1] and parse_function("0").truth([]) == [0]
    mux = parse_function("(!S*A0)+(S*A1)")
    assert mux.vars() == ["S", "A0", "A1"]
    assert mux.unateness("A0") == "positive_unate" and mux.unateness("S") == "non_unate"
    assert parse_function("!(A+B)").unateness("B") == "negative_unate"
    assert mux.unateness("S", when=parse_function("!A0 * A1")) == "positive_unate"
    for bad in ("A*", "(A", "A $ B"):
        with pytest.raises(ParseError):
            parse_function(bad)


def test_round_trip_and_errors():
    text = open(TINY).read()
    tree = parse_liberty_tree(text, TINY)
    out = format_liberty(tree)
    assert parse_liberty_tree(out).to_data() == tree.to_data()
    assert format_liberty(parse_liberty_tree(out)) == out
    lib2 = parse_liberty(out)
    assert lib2.cells["NAND2"].pins["Y"].arc("B").tables["cell_rise"].delay(0.06, 0.006) == approx(0.035)
    with pytest.raises(ParseError, match=r"t.lib:3: .*not closed"):
        parse_liberty_tree("library (x) {\n  time_unit : \"1ns\" ;\n  cell (a) {\n", "t.lib")
    with pytest.raises(ParseError, match=r"t.lib:2: expected ':' or '\('"):
        parse_liberty_tree("library (x) {\n  area 1 ;\n}\n", "t.lib")
    with pytest.raises(ParseError, match="unsupported time_unit"):
        parse_liberty('library (x) { time_unit : "1parsec" ; }')
    with pytest.raises(ParseError, match="does not match its indices"):
        parse_liberty('library (x) { lu_table_template (t) { variable_1 : input_net_transition ; '
                      'variable_2 : total_output_net_capacitance ; index_1 ("1, 2") ; index_2 ("1, 2") ; } '
                      'cell (c) { pin (Y) { direction : output ; timing () { related_pin : A ; '
                      'cell_rise (t) { values ("1, 2") ; } } } } }')


# ----------------------------------------------------------------------------- IHP (needs the PDK)
@pytest.fixture(scope="module")
def ihp():
    if not os.environ.get("DLA_TECH"):
        pytest.skip("set DLA_TECH to a tech file to run the PDK tests")
    from dla.tech import load
    tech = load()
    if not tech.liberty:
        pytest.skip("tech file has no files.liberty")
    return tech, Library.from_tech(tech, corners=list(tech.liberty))


def test_ihp_join(ihp):
    tech, lib = ihp
    for corner, ll in lib.liberty.items():
        assert len(ll.cells) == 84, corner
        assert ll.time_scale == 1.0 and ll.cap_scale == 1.0
        assert lib.check_join(corner) == [], corner
    assert lib.default_corner == list(tech.liberty)[0]
    assert lib.timing("sg13g2_inv_1").pins["A"].capacitance > 0


_LOGIC = {
    "inv": lambda v: 1 - v[0], "buf": lambda v: v[0],
    "nand": lambda v: 1 - all(v), "and": lambda v: int(all(v)),
    "nor": lambda v: 1 - any(v), "or": lambda v: int(any(v)),
    "xor": lambda v: v[0] ^ v[1], "xnor": lambda v: 1 - (v[0] ^ v[1]),
}


def test_ihp_functions_match_names(ihp):
    """Truth table of every plain logic cell agrees with its name."""
    _, lib = ihp
    checked = 0
    for name, c in lib.liberty[lib.default_corner].cells.items():
        base = name.split("_")[1]
        kind = base.rstrip("0123456789")
        if kind not in _LOGIC or c.is_sequential():
            continue
        (out,) = c.outputs()
        ins = sorted(p.name for p in c.inputs())
        assert out.function.truth(ins) == [_LOGIC[kind](v) for v in itertools.product((0, 1), repeat=len(ins))], name
        checked += 1
    assert checked >= 30
    cells = lib.liberty[lib.default_corner].cells
    mux = cells["sg13g2_mux2_1"].pins["X"].function
    assert all(mux.eval(dict(S=s, A0=a, A1=b)) == (b if s else a) for s, a, b in itertools.product((0, 1), repeat=3))
    aoi = cells["sg13g2_a21oi_1"].pins["Y"].function
    assert all(aoi.eval(dict(A1=a, A2=b, B1=c)) == 1 - ((a & b) | c) for a, b, c in itertools.product((0, 1), repeat=3))
    assert cells["sg13g2_tiehi"].pins["L_HI"].function.truth([]) == [1]
    # timing_sense in the library agrees with the function's unateness
    for name, c in cells.items():
        for p in c.outputs():
            if p.function is None or c.is_sequential():
                continue
            for a in p.arcs:
                if a.type == "combinational" and a.sense:
                    when = parse_function(a.when) if a.when else None
                    assert p.function.unateness(a.related, when=when) == a.sense, (name, p.name, a.related, a.when)


def test_ihp_dff(ihp):
    _, lib = ihp
    c = lib.timing("sg13g2_dfrbp_1")
    s = c.seq
    assert s.kind == "ff" and s.state == ["IQ", "IQN"]
    assert s.clocked_on.text == "CLK" and s.next_state.text == "D" and s.clear.text == "!RESET_B"
    assert c.pins["CLK"].clock
    types = {(a.related, a.type) for a in c.pins["D"].arcs}
    assert {("CLK", "setup_rising"), ("CLK", "hold_rising")} <= types
    q = c.pins["Q"].arc("CLK", "rising_edge")
    assert q is not None and set(q.tables) == {"cell_rise", "cell_fall", "rise_transition", "fall_transition"}
    assert c.pins["Q"].arc("RESET_B", "clear").sense == "positive_unate"
    assert lib.timing("sg13g2_dlhq_1").seq.kind == "latch"


def test_ihp_table_sanity(ihp):
    """Delay / transition grow with load; slow > typ > fast for the same arc and point."""
    _, lib = ihp
    typ, slow, fast = (lib.liberty[c] for c in ("typ", "slow", "fast"))
    n = 0
    for name, c in typ.cells.items():
        for p in c.pins.values():
            for a in p.arcs:
                if a.type.startswith("three_state"):
                    continue       # enable/disable arcs are not load driven
                key = (a.related, a.type, a.when)
                sa = [x for x in slow.cells[name].pins[p.name].arcs if (x.related, x.type, x.when) == key]
                fa = [x for x in fast.cells[name].pins[p.name].arcs if (x.related, x.type, x.when) == key]
                for k in ("cell_rise", "cell_fall", "rise_transition", "fall_transition"):
                    t = a.tables.get(k)
                    if t is None or len(t.vars) != 2:
                        continue
                    n += 1
                    li = t.vars.index("total_output_net_capacitance")
                    rows = t.values if li == 1 else [list(r) for r in zip(*t.values)]
                    for r in rows:
                        assert all(r[i + 1] >= r[i] for i in range(len(r) - 1)), (name, p.name, key, k)
                    assert len(sa) == len(fa) == 1 and k in sa[0].tables and k in fa[0].tables
                    for slew, load in ((0.1, 0.02), (0.6, 0.1)):
                        ts, tt, tf = (x.delay(slew, load) for x in (sa[0].tables[k], t, fa[0].tables[k]))
                        assert ts > tt > tf, (name, p.name, key, k)
    assert n > 900


def test_ihp_lookup_spot(ihp):
    _, lib = ihp
    t = lib.timing("sg13g2_nand2_1").pins["Y"].arc("A").tables["cell_rise"]
    assert t.delay(0.0186, 0.001) == approx(0.022718)
    assert t.delay(0.0966, 0.0234) == approx(0.129513)
    assert t.delay(2.5074, 0.3) == approx(2.05647)
    mid = t.delay((0.0186 + 0.0966) / 2, (0.001 + 0.0234) / 2)
    assert mid == approx((0.022718 + 0.0868603 + 0.0437056 + 0.129513) / 4)


def test_ihp_all_corner_files(ihp):
    """Every .lib next to the tech file's first corner parses and joins with the LEF."""
    import glob
    tech, lib = ihp
    files = sorted(glob.glob(os.path.join(os.path.dirname(tech.liberty[lib.default_corner][0]), "*.lib")))
    assert len(files) >= 3
    for f in files:
        ll = read_liberty(f)
        assert len(ll.cells) == len(lib.liberty[lib.default_corner].cells), f
        for name, c in ll.cells.items():
            assert set(c.pins) == set(lib.timing(name).pins), (f, name)
