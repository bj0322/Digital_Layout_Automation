"""AIG restructuring by refactoring: collapse a node's cone, re-derive a smaller structure.

    g2 = refactor(g, max_leaves=8)

For every AND node (topological order) a reconvergence-driven cut of at most
`max_leaves` leaves is taken, its function computed as a truth table, an irredundant
sum-of-products found for the function and for its complement (Minato-Morreale ISOP),
and the SOP factored algebraically. The factored form is costed against the nodes
the cone frees (its maximum fanout-free cone), counting AND nodes that already exist
in the new graph as free. The cheaper structure is kept. This follows the
"refactor" step of ABC's dc2 script (Mishchenko, Chatterjee, Brayton, "DAG-aware AIG
rewriting", DAC 2006); the implementation here is independent.

The result is functionally identical node by node at the roots (checked by tests
with SAT).
"""
from .aig import AIG, AND, CONST


# ----------------------------------------------------------------------------- truth tables
def _proj(n):
    full = (1 << (1 << n)) - 1
    out = []
    for i in range(n):
        v = 0
        block = 1 << i
        pat = ((1 << block) - 1) << block            # ones in the upper half of each 2*block run
        period = block * 2
        rep = 0
        for k in range(0, 1 << n, period):
            rep |= pat << k
        v = rep & full
        out.append(v)
    return out


_PROJ = {}


def proj(n):
    if n not in _PROJ:
        _PROJ[n] = _proj(n)
    return _PROJ[n]


def cof(t, i, val, n):
    """Cofactor of t w.r.t. variable i, expanded so it does not depend on i."""
    p = proj(n)[i]
    s = 1 << i
    if val:
        x = t & p
        return x | (x >> s)
    x = t & ~p & ((1 << (1 << n)) - 1)
    return x | (x << s)


def isop(L, U, n, var=None):
    """Cubes (list of {var: phase}) with L <= cover <= U; returns (cubes, cover tt)."""
    full = (1 << (1 << n)) - 1
    if L == 0:
        return [], 0
    if U == full:
        return [{}], full
    if var is None:
        var = n - 1
    # highest variable that L or U depends on
    while var >= 0 and cof(L, var, 0, n) == cof(L, var, 1, n) and cof(U, var, 0, n) == cof(U, var, 1, n):
        var -= 1
    if var < 0:
        return [{}], full
    L0, L1 = cof(L, var, 0, n), cof(L, var, 1, n)
    U0, U1 = cof(U, var, 0, n), cof(U, var, 1, n)
    c0, t0 = isop(L0 & ~U1 & full, U0, n, var - 1)
    c1, t1 = isop(L1 & ~U0 & full, U1, n, var - 1)
    Ln = (L0 & ~t0 & full) | (L1 & ~t1 & full)
    c2, t2 = isop(Ln, U0 & U1, n, var - 1)
    p = proj(n)[var]
    cubes = [{**c, var: 0} for c in c0] + [{**c, var: 1} for c in c1] + c2
    cover = (t0 & ~p & full) | (t1 & p) | t2
    return cubes, cover


# ----------------------------------------------------------------------------- factoring
def factor(cubes):
    """Algebraic factoring of an SOP -> expression tree
    ('lit', var, phase) | ('and', [..]) | ('or', [..]) | ('const', v)."""
    if not cubes:
        return ("const", 0)
    if any(not c for c in cubes):
        return ("const", 1)
    if len(cubes) == 1:
        lits = [("lit", v, p) for v, p in sorted(cubes[0].items())]
        return lits[0] if len(lits) == 1 else ("and", lits)
    count = {}
    for c in cubes:
        for v, p in c.items():
            count[(v, p)] = count.get((v, p), 0) + 1
    (v, p), k = max(count.items(), key=lambda kv: (kv[1], -kv[0][0], kv[0][1]))
    if k < 2:
        return ("or", [factor([c]) for c in cubes])
    inside = [{x: y for x, y in c.items() if x != v} for c in cubes if c.get(v) == p]
    rest = [c for c in cubes if c.get(v) != p]
    # common sub-cube of the quotient goes into the product too
    q = factor(inside)
    term = ("and", [("lit", v, p), q]) if q != ("const", 1) else ("lit", v, p)
    if not rest:
        return term
    return ("or", [term, factor(rest)])


# ----------------------------------------------------------------------------- building
class _Builder:
    """Builds an expression in an AIG; dry runs count new nodes without creating them."""

    def __init__(self, g):
        self.g = g

    def cost(self, expr, leaves):
        self.fake = -2
        self.new = 0
        self._dry(expr, leaves)
        return self.new

    def _lookup(self, a, b):
        if a < 0 or b < 0:
            return None
        if a > b:
            a, b = b, a
        if a == 0:
            return 0
        if a == 1:
            return b
        if a == b:
            return a
        if a ^ 1 == b:
            return 0
        n = self.g.strash.get((a, b))
        return None if n is None else 2 * n

    def _and(self, a, b):
        r = self._lookup(a, b)
        if r is None:
            self.new += 1
            self.fake -= 2
            return self.fake
        return r

    def _dry(self, e, leaves):
        k = e[0]
        if k == "const":
            return e[1]
        if k == "lit":
            return leaves[e[1]] ^ (0 if e[2] else 1)
        vals = [self._dry(c, leaves) for c in e[1]]
        if k == "and":
            acc = vals[0]
            for v in vals[1:]:
                acc = self._and(acc, v)
            return acc
        acc = self._neg(vals[0])
        for v in vals[1:]:
            acc = self._and(acc, self._neg(v))
        return self._neg(acc)

    @staticmethod
    def _neg(x):
        if x >= 0:
            return x ^ 1
        return x - 1 if x % 2 == 0 else x + 1        # fake literals: even = pos, odd = neg

    def build(self, e, leaves):
        g = self.g
        k = e[0]
        if k == "const":
            return e[1]
        if k == "lit":
            return leaves[e[1]] ^ (0 if e[2] else 1)
        vals = [self.build(c, leaves) for c in e[1]]
        if k == "and":
            acc = vals[0]
            for v in vals[1:]:
                acc = g.AND(acc, v)
            return acc
        acc = vals[0] ^ 1
        for v in vals[1:]:
            acc = g.AND(acc, v ^ 1)
        return acc ^ 1


# ----------------------------------------------------------------------------- cuts / MFFC
def _fanouts(g):
    fo = [0] * len(g.kind)
    for n, k in enumerate(g.kind):
        if k == AND:
            fo[g.f0[n] >> 1] += 1
            fo[g.f1[n] >> 1] += 1
    for l in g.roots():
        fo[l >> 1] += 1
    return fo


def reconv_cut(g, n, k):
    """Reconvergence-driven cut of node n with at most k leaves."""
    leaves = {g.f0[n] >> 1, g.f1[n] >> 1}
    inside = {n}
    while True:
        best, best_cost = None, None
        for l in leaves:
            if g.kind[l] != AND:
                continue
            fa, fb = g.f0[l] >> 1, g.f1[l] >> 1
            cost = (fa not in leaves and fa not in inside) + (fb not in leaves and fb not in inside) - 1
            if best is None or cost < best_cost or (cost == best_cost and l > best):
                best, best_cost = l, cost
        if best is None or len(leaves) + best_cost > k:
            break
        leaves.discard(best)
        inside.add(best)
        for f in (g.f0[best] >> 1, g.f1[best] >> 1):
            if f not in inside:
                leaves.add(f)
    leaves.discard(0)
    return sorted(leaves), inside


def mffc_size(g, n, leaves, fo):
    """Nodes freed if n is removed (cone bounded by `leaves`), by reference counting."""
    lv = set(leaves)
    ref = {}
    count = 0
    stack = [n]
    while stack:
        x = stack.pop()
        count += 1
        for f in (g.f0[x] >> 1, g.f1[x] >> 1):
            if f in lv or g.kind[f] != AND:
                continue
            ref[f] = ref.get(f, fo[f]) - 1
            if ref[f] == 0:
                stack.append(f)
    return count


# ----------------------------------------------------------------------------- refactor
def refactor(src, max_leaves=8, zero_gain=False):
    g = AIG()
    m = {0: 0}
    for name, n in src.pis.items():
        m[n] = g.pi(name)
    for name, f in src.ffs.items():
        m[f.q] = g.ff(name, f.clk, f.edge, None, f.arst_val)
    fo = _fanouts(src)
    b = _Builder(g)
    live = src.live()
    for n in range(len(src.kind)):
        if src.kind[n] != AND or n not in live:
            continue
        a = m[src.f0[n] >> 1] ^ (src.f0[n] & 1)
        c = m[src.f1[n] >> 1] ^ (src.f1[n] & 1)
        leaves, inside = reconv_cut(src, n, max_leaves)
        if len(leaves) < 3 or fo[n] == 0:
            m[n] = g.AND(a, c)
            continue
        size = mffc_size(src, n, leaves, fo)
        if size < 2:
            m[n] = g.AND(a, c)
            continue
        k = len(leaves)
        tt = src.cone_truth(2 * n, leaves)
        full = (1 << (1 << k)) - 1
        nl = [m[l] for l in leaves]
        best = None
        for compl in (0, 1):
            t = tt ^ full if compl else tt
            cubes, _ = isop(t, t, k)
            e = factor(cubes)
            cost = b.cost(e, nl)
            if best is None or cost < best[0]:
                best = (cost, e, compl)
        cost, e, compl = best
        if cost < size or (zero_gain and cost == size):
            m[n] = b.build(e, nl) ^ compl
        else:
            m[n] = g.AND(a, c)
    for name, l in src.pos.items():
        g.po(name, m[l >> 1] ^ (l & 1))
    for name, f in src.ffs.items():
        g.ffs[name].d = m[f.d >> 1] ^ (f.d & 1)
        if f.arst is not None:
            g.ffs[name].arst = m[f.arst >> 1] ^ (f.arst & 1)
    return g.cleanup()


def balance(src):
    """Rebuild AND trees (maximal single-fanout AND clusters) as balanced trees by level."""
    g = AIG()
    m = {0: 0}
    for name, n in src.pis.items():
        m[n] = g.pi(name)
    for name, f in src.ffs.items():
        m[f.q] = g.ff(name, f.clk, f.edge, None, f.arst_val)
    fo = _fanouts(src)
    lev = {}

    def level(lit):
        return lev.get(lit >> 1, 0)

    def collect(lit, out, root):
        n = lit >> 1
        if (lit & 1) == 0 and src.kind[n] == AND and (root or fo[n] == 1):
            collect(src.f0[n], out, False)
            collect(src.f1[n], out, False)
        else:
            out.append(lit)

    def build(lit):
        n = lit >> 1
        if n in m:
            return m[n] ^ (lit & 1)
        stack = [n]
        while stack:
            x = stack[-1]
            if x in m:
                stack.pop()
                continue
            leaves = []
            collect(2 * x, leaves, True)
            todo = [l >> 1 for l in leaves if (l >> 1) not in m]
            if todo:
                stack += todo
                continue
            stack.pop()
            items = sorted(((lev.get(m[l >> 1] >> 1, 0), m[l >> 1] ^ (l & 1)) for l in leaves))
            items = [list(t) for t in items]
            while len(items) > 1:
                (la, a), (lb, b_) = items[0], items[1]
                r = g.AND(a, b_)
                lev[r >> 1] = max(lev.get(r >> 1, 0), max(la, lb) + 1)
                items = items[2:] + [[lev[r >> 1], r]]
                items.sort()
            m[x] = items[0][1]
        return m[n] ^ (lit & 1)

    for name, l in src.pos.items():
        g.po(name, build(l))
    for name, f in src.ffs.items():
        g.ffs[name].d = build(f.d)
        if f.arst is not None:
            g.ffs[name].arst = build(f.arst)
    return g.cleanup()
