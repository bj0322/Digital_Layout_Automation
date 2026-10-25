"""pytest: logic optimisation, technology mapping and the synthesis flow on tests/data/tiny.lib
(NAND2, INV, DFFR with clear, tie cells), proven with SAT. No PDK needed."""
import itertools
import os
import random

import pytest

from dla.db import format_verilog, parse_verilog
from dla.db.liberty import read_liberty
from dla.rtl.elab import elaborate
from dla.synth.cells import TargetLib
from dla.synth.fraig import cec
from dla.synth.flow import synthesize
from dla.synth.mapper import Mapper
from dla.synth.rewrite import balance, factor, isop, refactor
from dla.synth.verify import design_to_aig

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TINY = os.path.join(HERE, "data", "tiny.lib")


@pytest.fixture(scope="module")
def lib():
    return read_liberty(TINY)


def _write(tmp_path, text, name="t.v"):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


# ----------------------------------------------------------------------------- ISOP / factoring
def _eval_expr(e, env):
    k = e[0]
    if k == "const":
        return e[1]
    if k == "lit":
        return env[e[1]] if e[2] else 1 - env[e[1]]
    vals = [_eval_expr(c, env) for c in e[1]]
    return int(all(vals)) if k == "and" else int(any(vals))


def test_isop_and_factor_random():
    rnd = random.Random(5)
    for _ in range(200):
        n = rnd.randint(1, 6)
        full = (1 << (1 << n)) - 1
        tt = rnd.getrandbits(1 << n) & full
        cubes, cover = isop(tt, tt, n)
        assert cover == tt
        e = factor(cubes)
        for k in range(1 << n):
            env = {i: (k >> i) & 1 for i in range(n)}
            assert _eval_expr(e, env) == (tt >> k) & 1


# ----------------------------------------------------------------------------- target library
def test_target_lib(lib):
    tl = TargetLib(lib)
    assert {g.name for g in tl.gates} == {"NAND2", "INV"}
    assert tl.inv.name == "INV" and tl.tiehi[1] == "TIEHI" and tl.tielo[1] == "TIELO"
    (ff,) = tl.ff_cells
    assert (ff.name, ff.clk, ff.edge, ff.d, ff.q, ff.qn, ff.clear) == ("DFFR", "CK", "pos", "D", "Q", "QN", ("RN", 0))
    assert tl.skipped["LATCH"] == "dont_use" and tl.skipped["ICG"].startswith("unsupported")
    # every input inversion of NAND2 is in the table: !(a & b) with a negated -> !(!a & b)
    nand = 0b0111
    assert any(m[0].name == "NAND2" for m in tl.matches(2, nand))
    assert any(m[0].name == "NAND2" and m[2] in (1, 2) for m in tl.matches(2, 0b1011))   # !(!a & b)
    assert tl.ff_for("pos", 1) == (ff, True)            # set = clear on the inverted state


# ----------------------------------------------------------------------------- flow
SRC = """
module t (input clk, rst_n, input [3:0] a, b, input [1:0] op, output reg [3:0] q, output [3:0] y, output z);
  assign y = op[0] ? a + b : a ^ b;
  assign z = (a == b) | &op;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) q <= 4'b1010;          // bits set to 1 need inverted storage
    else if (op == 2'd3) q <= y;
endmodule
"""


@pytest.mark.parametrize("mode", ["area", "delay"])
def test_synthesize_small(tmp_path, lib, mode):
    r = synthesize(_write(tmp_path, SRC), "t", lib, mode=mode)
    d = r.design
    assert r.report["equivalence"] == "PASS"
    masters = {i.master for i in d.insts.values()}
    assert masters <= {"NAND2", "INV", "DFFR", "TIEHI", "TIELO"}
    assert sum(1 for i in d.insts.values() if i.master == "DFFR") == 4
    assert sorted(v for _, v in r.ffmap.values()).count(True) == 2      # q[3], q[1] reset to 1
    # independent re-check from the written netlist
    d2 = parse_verilog(format_verilog(d))
    assert d2 == d
    assert cec(r.rtl_aig, design_to_aig(d2, lib, r.ffmap)).ok


def test_delay_mode_is_not_slower(tmp_path, lib):
    src = "module t(input [7:0] a, b, output [7:0] s); assign s = a + b; endmodule"
    p = _write(tmp_path, src)
    ra = synthesize(p, "t", lib, mode="area")
    rd = synthesize(p, "t", lib, mode="delay")
    assert rd.report["depth_ns"] <= ra.report["depth_ns"] + 1e-9


def test_mutated_netlist_fails_cec(tmp_path, lib):
    r = synthesize(_write(tmp_path, SRC), "t", lib)
    d = r.design
    # swap the inputs of one NAND2 for an inverter output: change the function
    inst = next(n for n, i in d.insts.items() if i.master == "NAND2")
    pins = d.inst_pins(inst)
    net_a = pins["A"]
    d.nets[net_a].conns.remove((inst, "A"))
    d.connect(inst, "A", "a[0]")
    assert not cec(r.rtl_aig, design_to_aig(d, lib, r.ffmap)).ok


def test_refactor_and_balance_preserve_function():
    g = elaborate(os.path.join(ROOT, "bench", "uart_rx.v"), top="uart_rx").aig
    h = balance(refactor(g, 8))
    assert h.stats()["ands"] < g.stats()["ands"]
    assert cec(g, h).ok


def test_mapper_maps_single_gates(lib):
    from dla.synth.aig import AIG
    tl = TargetLib(lib)
    g = AIG()
    a, b = g.pi("a"), g.pi("b")
    m = Mapper(g, tl).map([g.AND(a, b) ^ 1])
    assert [m.choice[i].gate.name for i in m.used() if m.choice[i].kind == "gate"] == ["NAND2"]
    assert m.area() == pytest.approx(2.5)
    m = Mapper(g, tl).map([g.AND(a, b)])                    # AND = NAND2 + INV
    assert m.area() == pytest.approx(4.0)


def test_unsupported_ff_style(tmp_path, lib):
    src = "module t(input c, d, output reg q); always @(negedge c) q <= d; endmodule"
    r = synthesize(_write(tmp_path, src), "t", lib)               # clock inverter + posedge FF
    assert r.report["equivalence"] == "PASS"
    assert any(i.master == "INV" for i in r.design.insts.values())


def test_deterministic_across_hash_seeds(tmp_path):
    """Same input -> same netlist bytes, whatever Python's string hash seed is."""
    import subprocess
    import sys
    src = _write(tmp_path, SRC)
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from dla.db.liberty import read_liberty\nfrom dla.db import format_verilog\n"
            "from dla.synth.flow import synthesize\n"
            "r = synthesize(%r, 't', read_liberty(%r))\nsys.stdout.write(format_verilog(r.design))\n"
            % (ROOT, src, TINY))
    outs = set()
    for seed in ("1", "2", "3"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        outs.add(subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                                check=True).stdout)
    assert len(outs) == 1
