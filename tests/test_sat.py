"""pytest: CDCL SAT solver, SAT sweeping (FRAIG) and combinational equivalence checking."""
import itertools
import os
import random

import pytest

from dla.rtl.elab import elaborate
from dla.synth.aig import AIG
from dla.synth.fraig import cec, fraig
from dla.synth.sat import Solver

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _brute(n, cls):
    return any(all(any((x[abs(l) - 1] if l > 0 else not x[abs(l) - 1]) for l in c) for c in cls)
               for x in itertools.product((False, True), repeat=n))


def test_random_3sat_against_brute_force():
    rnd = random.Random(1)
    for _ in range(300):
        n, m = rnd.randint(3, 10), rnd.randint(1, 45)
        cls = [[rnd.choice((1, -1)) * rnd.randint(1, n) for _ in range(3)] for _ in range(m)]
        s = Solver()
        for _ in range(n):
            s.new_var()
        for c in cls:
            s.add_clause(c)
        r = s.solve()
        assert r == _brute(n, cls)
        if r:
            assert all(any(s.value(l) for l in c) for c in cls)


def _php(p, h):
    s = Solver()
    v = {(i, j): s.new_var() for i in range(p) for j in range(h)}
    for i in range(p):
        s.add_clause([v[i, j] for j in range(h)])
    for j in range(h):
        for a in range(p):
            for b in range(a + 1, p):
                s.add_clause([-v[a, j], -v[b, j]])
    return s


def test_pigeonhole_assumptions_and_limit():
    assert _php(7, 6).solve() is False
    assert _php(6, 6).solve() is True
    assert _php(9, 8).solve(conflict_limit=10) is None
    s = Solver()
    a, b, c = s.new_var(), s.new_var(), s.new_var()
    s.add_clause([a, b])
    s.add_clause([-a, c])
    assert s.solve(assumptions=[-b, -c]) is False
    assert s.solve(assumptions=[-b]) is True and s.value(a) and s.value(c)
    assert s.solve() is True                         # assumptions are not permanent


def test_fraig_merges_equivalent_logic():
    g = AIG()
    a, b, c = g.pi("a"), g.pi("b"), g.pi("c")
    x1 = g.AND(g.AND(a, b), c)
    x2 = g.AND(a, g.AND(b, c))                       # same function, different structure
    g.po("x1", x1)
    g.po("x2", x2)
    g.po("m", g.OR(g.AND(a, b), g.AND(a ^ 1, b)))    # = b
    f = fraig(g)
    assert f.pos["x1"] == f.pos["x2"] and f.pos["m"] == 2 * f.pis["b"]
    assert cec(g, f).ok


@pytest.mark.parametrize("top", ["counter8", "gcd", "uart_rx", "alu32"])
def test_fraig_and_cec_on_benches(top):
    g = elaborate(os.path.join(ROOT, "bench", top + ".v"), top=top).aig
    f = fraig(g)
    assert f.stats()["ands"] <= g.stats()["ands"]
    assert cec(g, f).ok
    pats = {n: random.Random(2).getrandbits(64) for n in g.pis}
    st = {n: random.Random(hash(n) & 0xFFFF).getrandbits(64) for n in g.ffs}
    assert g.step(st, pats, 64) == f.step(st, pats, 64)


def test_cec_finds_a_bug():
    src = open(os.path.join(ROOT, "bench", "alu32.v")).read()
    a = elaborate(text=src, top="alu32").aig
    b = elaborate(text=src.replace("SLTU:  r = {32'd0, a < b};", "SLTU:  r = {32'd0, a <= b};"), top="alu32").aig
    r = cec(a, b)
    assert not r.ok and ("ff.d", "y[0]", "differs") in r.failed
    ce = r.counterexample
    assert [ce["op[%d]" % i] for i in range(4)] == [0, 1, 0, 1]           # op == SLTU
    av = sum(ce["a[%d]" % i] << i for i in range(32))
    bv = sum(ce["b[%d]" % i] << i for i in range(32))
    assert av == bv                                                        # the only differing case
    # structural mismatches are reported, not proved
    c = elaborate(text=src.replace("output reg        carry", "output reg        carry2")
                  .replace("carry <=", "carry2 <=").replace("carry <= 1'b0", "carry2 <= 1'b0"), top="alu32").aig
    r = cec(a, c)
    assert not r.ok and ("ff", "carry", "only in spec") in r.failed
