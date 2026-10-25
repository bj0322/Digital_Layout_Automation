"""pytest: AIG basics and the RTL front end (lexer, parser, elaboration) without oracles."""
import itertools
import random

import pytest

from dla.db.lex import ParseError
from dla.rtl.elab import elaborate
from dla.rtl.lexer import lex_text
from dla.synth.aig import AIG


# ----------------------------------------------------------------------------- AIG
def test_aig_strash_and_constants():
    g = AIG()
    a, b = g.pi("a"), g.pi("b")
    assert g.AND(a, b) == g.AND(b, a)                      # structural hashing
    assert g.AND(a, 0) == 0 and g.AND(a, 1) == a and g.AND(a, a) == a and g.AND(a, a ^ 1) == 0
    assert g.XOR(a, 0) == a and g.XOR(a, 1) == a ^ 1 and g.MUX(1, a, b) == a
    n = g.n_ands()
    g.OR(a, b)
    g.OR(b, a)
    assert g.n_ands() == n + 1
    assert g.is_const(g.OR(a, a ^ 1)) == 1 and g.is_const(g.AND(g.OR(a, b), g.AND(a ^ 1, b ^ 1))) == 0
    assert g.is_const(a) is None


def test_aig_bit_parallel_sim_and_step():
    g = AIG()
    a, b = g.pi("a"), g.pi("b")
    g.po("x", g.XOR(a, b))
    g.po("m", g.MUX(a, b, b ^ 1))
    out = g.sim({"a": 0b1100, "b": 0b1010}, width=4)
    assert out == {"x": 0b0110, "m": 0b1001}
    q = g.ff("r", clk="clk", arst=g.pi("rst"), arst_val=1)
    g.set_d("r", g.XOR(q, a))
    g.po("q", q)
    st = {"r": 0}
    o, st = g.step(st, {"a": 1, "rst": 0})
    assert o["q"] == 0 and st["r"] == 1
    o, st = g.step(st, {"a": 0, "rst": 1})             # async: visible immediately
    assert o["q"] == 1 and st["r"] == 1


# ----------------------------------------------------------------------------- lexer
def test_preprocessor():
    toks = lex_text("`define W 8\n`ifdef W\nwire [`W-1:0] a;\n`else\nbad\n`endif\n`timescale 1ns/1ps\n")
    assert [t.value for t in toks] == ["wire", "[", "8", "-", "1", ":", "0", "]", "a", ";"]
    with pytest.raises(ParseError, match=r"t.v:1: macros with arguments"):
        lex_text("`define F(x) x\n", "t.v")
    with pytest.raises(ParseError, match="missing `endif"):
        lex_text("`ifdef A\n", "t.v")
    with pytest.raises(ParseError, match=r"t.v:2: unknown directive"):
        lex_text("\n`FOO\n", "t.v")


# ----------------------------------------------------------------------------- elaboration
def _comb(text, top="t", **pis):
    r = elaborate(text=text, top=top)
    return r, r.aig.sim(pis, width=1)


def _bus(out, name, w):
    return sum(out["%s[%d]" % (name, i)] << i for i in range(w))


def _pis(name, val, w):
    return {"%s[%d]" % (name, i): (val >> i) & 1 for i in range(w)}


SRC_OPS = """
module t (input [7:0] a, b, input signed [7:0] sa, sb, input [2:0] n,
          output [8:0] add, output [7:0] sub, output [15:0] mul, output lt, slt, eq,
          output [7:0] shl, shr, asr, output [9:0] sext, output [3:0] cnt);
  assign add = a + b;
  assign sub = a - b;
  assign mul = a * b;
  assign lt = a < b;
  assign slt = sa < sb;
  assign eq = a == b;
  assign shl = a << n;
  assign shr = a >> n;
  assign asr = sa >>> n;
  assign sext = sa + sb;
  assign cnt = a[0] + a[1] + a[2] + a[3] + a[4] + a[5] + a[6] + a[7];
endmodule
"""


def test_operators_against_python():
    r = elaborate(text=SRC_OPS, top="t")
    rnd = random.Random(3)
    for _ in range(300):
        a, b, sa, sb, n = (rnd.getrandbits(8), rnd.getrandbits(8), rnd.getrandbits(8),
                           rnd.getrandbits(8), rnd.getrandbits(3))
        pis = {}
        for name, v, w in (("a", a, 8), ("b", b, 8), ("sa", sa, 8), ("sb", sb, 8), ("n", n, 3)):
            pis.update(_pis(name, v, w))
        o = r.aig.sim(pis)
        s8 = lambda v: v - 256 if v & 128 else v      # noqa: E731
        assert _bus(o, "add", 9) == a + b
        assert _bus(o, "sub", 8) == (a - b) & 0xFF
        assert _bus(o, "mul", 16) == a * b
        assert o["lt"] == int(a < b) and o["slt"] == int(s8(sa) < s8(sb)) and o["eq"] == int(a == b)
        assert _bus(o, "shl", 8) == (a << n) & 0xFF and _bus(o, "shr", 8) == a >> n
        assert _bus(o, "asr", 8) == (s8(sa) >> n) & 0xFF
        assert _bus(o, "sext", 10) == (s8(sa) + s8(sb)) & 0x3FF
        assert _bus(o, "cnt", 4) == bin(a).count("1") & 0xF       # 1-bit + 1-bit context: 4 bits


def test_width_rules():
    # comparison operands are sized to each other, not to the 1-bit result
    r, o = _comb("module t(input [3:0] a, output y, output [4:0] z); assign y = (a + 4'd1) == 5'd16;"
                 " assign z = a + 4'd1; endmodule", **_pis("a", 15, 4))
    assert o["y"] == 1 and _bus(o, "z", 5) == 16
    # concatenation operands are self-determined
    r, o = _comb("module t(input [3:0] a, output [4:0] z); assign z = {a + 4'd1}; endmodule", **_pis("a", 15, 4))
    assert _bus(o, "z", 5) == 0


def test_case_casez_full_and_latch():
    src = """module t(input [1:0] s, input [3:0] a, output reg [3:0] y);
      always @* casez (s) 2'b00: y = a; 2'b01: y = ~a; 2'b1?: y = 4'd9; endcase endmodule"""
    r = elaborate(text=src, top="t")
    for s, a in itertools.product(range(4), range(16)):
        o = r.aig.sim(dict(_pis("s", s, 2), **_pis("a", a, 4)))
        assert _bus(o, "y", 4) == (a if s == 0 else (~a & 15) if s == 1 else 9)
    with pytest.raises(ParseError, match=r"t.v:2: latch inferred for y\[0\]"):
        _elab_named("module t(input c, input [1:0] a, output reg [1:0] y);\n always @* if (c) y = a;\nendmodule")
    with pytest.raises(ParseError, match=r"latch inferred"):      # plain case: '?' never matches
        _elab_named(src.replace("casez", "case"))


def _elab_named(text, top="t"):
    import os
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.v")
    with open(p, "w") as f:
        f.write(text)
    try:
        return elaborate(p, top=top)
    except ParseError as e:
        raise ParseError(str(e).replace(p, "t.v"))


@pytest.mark.parametrize("text,msg", [
    ("module t(input a, output y);\n assign y = b;\nendmodule", r"t.v:2: undeclared identifier b"),
    ("module t(input a, output y);\n assign y = a;\n assign y = ~a;\nendmodule", r"t.v:3: multiple drivers for y"),
    ("module t(input a, output y);\n wire w;\n assign w = y;\n assign y = w ^ a;\nendmodule", r"combinational loop"),
    ("module t(input a, output y);\n initial y = 0;\nendmodule", r"t.v:2: unsupported: initial"),
    ("module t(input a, output reg y);\n always @(a) y <= #1 a;\nendmodule", r"t.v:2: unsupported: delays"),
    ("module t(input [3:0] a, input [3:0] b, output [3:0] y);\n assign y = a / b;\nendmodule",
     r"t.v:2: unsupported: / with non-constant"),
    ("module t(input c, d, output reg q);\n always @(posedge c) q <= d;\n always @(posedge c) q <= ~d;\nendmodule",
     r"t.v:3: multiple drivers for q"),
    ("module t(input a, output y);\n sub u (.x(a));\nendmodule", r"t.v:2: unknown module sub"),
    ("module t(input [3:0] a, output reg y);\n integer i;\n always @* for (i = 0; i < a; i = i + 1) y = 1;\nendmodule",
     r"t.v:3: for loop condition is not constant"),
    ("module t(input c, input r, d, output reg q);\n wire g = c & r;\n always @(posedge g) q <= d;\nendmodule",
     r"clock g must be a primary input"),
    ("module t(input c, r, d, output reg q);\n always @(posedge c or posedge r) if (r) q <= d; else q <= ~d;\nendmodule",
     r"async reset/set value of q is not constant"),
])
def test_errors(text, msg):
    with pytest.raises(ParseError, match=msg):
        _elab_named(text)


def test_sequential_semantics():
    src = """module t(input clk, rst_n, en, input [3:0] d, output reg [3:0] a, b, output reg [3:0] c);
      always @(posedge clk or negedge rst_n)
        if (!rst_n) begin a <= 4'd1; b <= 4'd2; end
        else begin a <= d; b <= a; end              // non-blocking: b gets the old a
      always @(posedge clk) if (en) begin c = d; c = c + 1; end   // blocking chain, enable
    endmodule"""
    r = elaborate(text=src, top="t")
    g = r.aig
    assert all(f.clk == "clk" and f.edge == "pos" for f in g.ffs.values())
    assert g.ffs["a[0]"].arst is not None and g.ffs["a[0]"].arst_val == 1 and g.ffs["c[0]"].arst is None
    st = {n: 0 for n in g.ffs}
    seq = [(0, 0, 5), (1, 1, 7), (1, 0, 9), (1, 1, 3)]
    a = b = c = 0
    for rst_n, en, d in seq:
        pis = dict(_pis("d", d, 4), rst_n=rst_n, en=en)
        o, st = g.step(st, pis)
        if not rst_n:
            a, b = 1, 2
        assert (_bus(o, "a", 4), _bus(o, "b", 4)) == (a, b)
        if rst_n:
            a, b = d, a
        if en:
            c = (d + 1) & 15
        assert sum(st["c[%d]" % i] << i for i in range(4)) == c


def test_hierarchy_params_and_names():
    src = """module inc #(parameter W = 2, STEP = 1) (input [W-1:0] x, output [W-1:0] y); assign y = x + STEP; endmodule
      module t(input clk, input [5:0] a, output [5:0] y, output reg [5:0] r);
        inc #(.W(6), .STEP(3)) u (.x(a), .y(y));
        always @(posedge clk) r <= y;
      endmodule"""
    r = elaborate(text=src, top="t")
    o = r.aig.sim(_pis("a", 10, 6))
    assert _bus(o, "y", 6) == 13
    assert set(r.aig.ffs) == {"r[%d]" % i for i in range(6)}
    assert r.ports[1] == ("a", "input", 5, 0)
    with pytest.raises(ParseError, match="has no parameter Q"):
        elaborate(text=src.replace(".STEP(3)", ".Q(3)"), top="t")
