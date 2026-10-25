"""SAT sweeping (FRAIG) and combinational equivalence checking of AIGs.

    g2 = fraig(g)                      # functionally reduced copy: equivalent nodes merged
    res = cec(spec, impl)              # PIs / FF outputs paired by name
    res.ok, res.counterexample, res.failed

The AIG is rebuilt node by node (topological order). Every new node gets a
simulation signature over random patterns; when it matches an existing node (or its
complement), a SAT call with a conflict limit decides whether they are equal; equal
nodes are merged. Counterexamples become new simulation patterns. This is the
classic FRAIG scheme (Mishchenko et al., "FRAIGs: a unifying representation for
logic synthesis and verification", 2005), here in plain Python.

`cec` copies both AIGs into one sweep with shared leaves (PIs by name, FF outputs by
FF name), then compares the outputs: POs, FF next-state functions, async controls.
"""
import random

from .aig import AIG, AND, CONST, FFQ, PI
from .sat import Solver


class Sweeper:
    """Builds a FRAIG incrementally: leaves first, then `and_` calls."""

    def __init__(self, npat=128, seed=1, conflict_limit=2000):
        self.g = AIG()
        self.rnd = random.Random(seed)
        self.npat = npat
        self.mask = (1 << npat) - 1
        self.sig = [0]                       # per node of self.g: simulation signature
        self.table = {}                      # normalised signature -> node
        self.s = Solver()
        self.var = [self.s.new_var()]        # node -> SAT var
        self.s.add_clause([-self.var[0]])    # node 0 is false
        self.limit = conflict_limit
        self.unknown = 0
        self.proved = 0
        self.refuted = 0
        self.table[0] = 0
        self.merged = {}                     # node -> equivalent literal it was merged into

    def _norm(self, sig):
        return (sig ^ self.mask) if sig & 1 else sig

    def _register(self, n):
        key = self._norm(self.sig[n])
        if key not in self.table:
            self.table[key] = n

    def leaf(self, kind, name, **kw):
        lit = self.g.pi(name) if kind == "pi" else self.g.ff(name, **kw)
        n = lit >> 1
        self.sig.append(self.rnd.getrandbits(self.npat))
        self.var.append(self.s.new_var())
        self._register(n)
        return lit

    def _lsig(self, lit):
        s = self.sig[lit >> 1]
        return s ^ self.mask if lit & 1 else s

    def _svar(self, lit):
        v = self.var[lit >> 1]
        return -v if lit & 1 else v

    def and_(self, a, b):
        g = self.g
        before = len(g.kind)
        lit = g.AND(a, b)
        n = lit >> 1
        if len(g.kind) == before:            # existing node or trivial
            if n in self.merged:
                return self.merged[n] ^ (lit & 1)
            return lit
        sig = self._lsig(a) & self._lsig(b)
        self.sig.append(sig)
        v = self.s.new_var()
        self.var.append(v)
        x, y = self._svar(a), self._svar(b)
        self.s.add_clause([-v, x])
        self.s.add_clause([-v, y])
        self.s.add_clause([v, -x, -y])
        key = self._norm(sig)
        rep = self.table.get(key)
        if rep is None:
            self.table[key] = n
            return lit
        # candidate: n == rep (or its complement)
        compl = (self.sig[rep] != sig)
        rl = 2 * rep + (1 if compl else 0)
        eq = self.equal(lit, rl)
        if eq:
            self.proved += 1
            self.merged[n] = rl                # node n stays as dead logic in g
            return rl
        if eq is None:
            self.unknown += 1
        else:
            self.refuted += 1
        return lit

    def equal(self, la, lb):
        """True / False / None (limit) for la == lb on the current SAT model."""
        s = self.s
        x, y = self._svar(la), self._svar(lb)
        for assume in ([x, -y], [-x, y]):
            r = s.solve(assumptions=assume, conflict_limit=self.limit)
            if r is None:
                return None
            if r:
                return False
        return True

    def counterexample(self, la, lb):
        """Leaf values distinguishing la and lb, or None if equal / unknown."""
        x, y = self._svar(la), self._svar(lb)
        for assume in ([x, -y], [-x, y]):
            r = self.s.solve(assumptions=assume, conflict_limit=None)
            if r:
                g = self.g
                out = {}
                for n, k in enumerate(g.kind):
                    if k in (PI, FFQ):
                        out[g.name[n]] = int(bool(self.s.value(self.var[n])))
                return out
        return None

    def copy(self, src, lit_map, lits):
        """Copy cones of `lits` from AIG src (leaf nodes already in lit_map)."""
        out = []
        for root in lits:
            stack = [root >> 1]
            while stack:
                n = stack[-1]
                if n in lit_map:
                    stack.pop()
                    continue
                k = src.kind[n]
                if k == CONST:
                    lit_map[n] = 0
                    stack.pop()
                    continue
                if k != AND:
                    raise KeyError("leaf %s not mapped" % src.name[n])
                a, b = src.f0[n] >> 1, src.f1[n] >> 1
                if a in lit_map and b in lit_map:
                    stack.pop()
                    lit_map[n] = self.and_(lit_map[a] ^ (src.f0[n] & 1), lit_map[b] ^ (src.f1[n] & 1))
                else:
                    stack += [x for x in (a, b) if x not in lit_map]
            out.append(lit_map[root >> 1] ^ (root & 1))
        return out


def fraig(src, npat=128, conflict_limit=2000, seed=1):
    """Functionally reduced copy of src (same PIs, POs, FFs)."""
    sw = Sweeper(npat, seed, conflict_limit)
    m = {0: 0}
    for name, n in src.pis.items():
        m[n] = sw.leaf("pi", name)
    for name, f in src.ffs.items():
        m[f.q] = sw.leaf("ff", name, clk=f.clk, edge=f.edge, arst_val=f.arst_val)
    for name, l in src.pos.items():
        sw.g.po(name, sw.copy(src, m, [l])[0])
    for name, f in src.ffs.items():
        sw.g.ffs[name].d = sw.copy(src, m, [f.d])[0]
        if f.arst is not None:
            sw.g.ffs[name].arst = sw.copy(src, m, [f.arst])[0]
    return sw.g.cleanup()


class CecResult:
    def __init__(self):
        self.ok = True
        self.failed = []               # [(kind, name, reason)]
        self.counterexample = None
        self.unknown = []

    def __repr__(self):
        return "CecResult(ok=%s, failed=%s, unknown=%s)" % (self.ok, self.failed[:5], self.unknown[:5])


def cec(spec, impl, conflict_limit=20000, npat=256):
    """Equivalence of two sequential AIGs at their FF boundaries.

    PIs and FFs must have the same names; FF clock / edge / reset value must match,
    and the next-state, async-control and output functions must be equal."""
    res = CecResult()
    for what, a, b in (("input", set(spec.pis), set(impl.pis)), ("ff", set(spec.ffs), set(impl.ffs)),
                       ("output", set(spec.pos), set(impl.pos))):
        for n in sorted(a ^ b):
            res.ok = False
            res.failed.append((what, n, "only in %s" % ("spec" if n in a else "impl")))
    if not res.ok:
        return res
    sw = Sweeper(npat, 7, 500)
    ms, mi = {0: 0}, {0: 0}
    for name in sorted(spec.pis):
        lit = sw.leaf("pi", name)
        ms[spec.pis[name]] = lit
        mi[impl.pis[name]] = lit
    for name in sorted(spec.ffs):
        lit = sw.leaf("ff", name)
        ms[spec.ffs[name].q] = lit
        mi[impl.ffs[name].q] = lit
    pairs = []
    for name in sorted(spec.pos):
        pairs.append(("output", name, spec.pos[name], impl.pos[name]))
    for name in sorted(spec.ffs):
        fs, fi = spec.ffs[name], impl.ffs[name]
        if (fs.clk, fs.edge) != (fi.clk, fi.edge):
            res.ok = False
            res.failed.append(("ff", name, "clock %s/%s vs %s/%s" % (fs.clk, fs.edge, fi.clk, fi.edge)))
        ra = fs.arst if fs.arst is not None else 0
        rb = fi.arst if fi.arst is not None else 0
        pairs.append(("ff.arst", name, ra, rb))
        if fs.arst is not None and fi.arst is not None and fs.arst_val != fi.arst_val:
            res.ok = False
            res.failed.append(("ff", name, "reset value %d vs %d" % (fs.arst_val, fi.arst_val)))
        pairs.append(("ff.d", name, fs.d, fi.d))
    for kind, name, la, lb in pairs:
        a = sw.copy(spec, ms, [la])[0]
        b = sw.copy(impl, mi, [lb])[0]
        if a == b:
            continue
        sw.limit = conflict_limit
        r = sw.equal(a, b)
        sw.limit = 500
        if r:
            continue
        if r is None:
            res.ok = False
            res.unknown.append((kind, name))
            continue
        res.ok = False
        res.failed.append((kind, name, "differs"))
        if res.counterexample is None:
            res.counterexample = sw.counterexample(a, b)
    return res
