"""And-inverter graph with flip-flops: the bit-level representation synthesis works on.

    g = AIG()
    a, b = g.pi("a"), g.pi("b")
    y = g.OR(g.AND(a, b), g.NOT(a))
    g.po("y", y)
    q = g.ff("r", clk="clk")             # FF output literal; set its input later
    g.set_d("r", g.XOR(q, a))
    g.sim({"a": 0b1010, "b": 0b0110}, width=4)   # bit-parallel: 4 patterns at once

Literals are ints `2 * node + neg`; node 0 is constant false, so literal 0 is 0 and
literal 1 is 1. AND nodes are structurally hashed with constant propagation and
the trivial identities (a&a, a&!a), so equal structure is never built twice. Nodes
are created in topological order (fanins first), which every pass relies on.
FF outputs are leaves of the combinational logic, like primary inputs.
"""
from dataclasses import dataclass
from typing import Optional

CONST0, CONST1 = 0, 1
CONST, PI, AND, FFQ = 0, 1, 2, 3


@dataclass
class FF:
    name: str
    q: int                        # node id of the output
    d: int = 0                    # input literal
    clk: str = ""                 # clock signal (a PI name)
    edge: str = "pos"             # pos | neg
    arst: Optional[int] = None    # async reset/set literal, active high
    arst_val: int = 0             # value loaded while arst is 1


class AIG:
    def __init__(self):
        self.kind = [CONST]
        self.f0 = [0]
        self.f1 = [0]
        self.name = [None]
        self.strash = {}
        self.pis = {}             # name -> node
        self.pos = {}             # name -> literal
        self.ffs = {}             # name -> FF
        self.ff_of = {}           # q node -> FF name

    # ------------------------------------------------------------------ construction
    def _new(self, kind, f0=0, f1=0, name=None):
        self.kind.append(kind)
        self.f0.append(f0)
        self.f1.append(f1)
        self.name.append(name)
        return len(self.kind) - 1

    def pi(self, name):
        if name in self.pis:
            return 2 * self.pis[name]
        if name in self.ffs:
            raise KeyError("%s is already a flip-flop" % name)
        n = self._new(PI, name=name)
        self.pis[name] = n
        return 2 * n

    def ff(self, name, clk="", edge="pos", arst=None, arst_val=0):
        if name in self.ffs or name in self.pis:
            raise KeyError("duplicate signal %s" % name)
        n = self._new(FFQ, name=name)
        self.ffs[name] = FF(name, n, 0, clk, edge, arst, arst_val)
        self.ff_of[n] = name
        return 2 * n

    def set_d(self, name, lit):
        self.ffs[name].d = lit

    def po(self, name, lit):
        self.pos[name] = lit

    @staticmethod
    def NOT(a):
        return a ^ 1

    def AND(self, a, b):
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
        key = (a, b)
        n = self.strash.get(key)
        if n is None:
            n = self._new(AND, a, b)
            self.strash[key] = n
        return 2 * n

    def OR(self, a, b):
        return self.AND(a ^ 1, b ^ 1) ^ 1

    def XOR(self, a, b):
        if a <= 1 or b <= 1:
            if a <= 1:
                a, b = b, a
            return a ^ b                       # b is a constant
        return self.OR(self.AND(a, b ^ 1), self.AND(a ^ 1, b))

    def XNOR(self, a, b):
        return self.XOR(a, b) ^ 1

    def MUX(self, s, a1, a0):
        """s ? a1 : a0"""
        if s <= 1:
            return a1 if s else a0
        if a1 == a0:
            return a1
        return self.OR(self.AND(s, a1), self.AND(s ^ 1, a0))

    def AND_N(self, lits):
        lits = list(lits)
        if not lits:
            return 1
        while len(lits) > 1:                   # balanced tree
            lits = [self.AND(lits[i], lits[i + 1]) if i + 1 < len(lits) else lits[i]
                    for i in range(0, len(lits), 2)]
        return lits[0]

    def OR_N(self, lits):
        return self.AND_N([l ^ 1 for l in lits]) ^ 1

    def XOR_N(self, lits):
        lits = list(lits)
        if not lits:
            return 0
        while len(lits) > 1:
            lits = [self.XOR(lits[i], lits[i + 1]) if i + 1 < len(lits) else lits[i]
                    for i in range(0, len(lits), 2)]
        return lits[0]

    # ------------------------------------------------------------------ queries
    def n_ands(self):
        return sum(1 for k in self.kind if k == AND)

    def is_and(self, n):
        return self.kind[n] == AND

    def roots(self):
        """Literals the combinational logic must produce: POs, FF inputs and resets."""
        out = list(self.pos.values())
        for f in self.ffs.values():
            out.append(f.d)
            if f.arst is not None:
                out.append(f.arst)
        return out

    def live(self):
        """Set of nodes reachable from the roots."""
        seen = set()
        stack = [l >> 1 for l in self.roots()]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            if self.kind[n] == AND:
                stack += [self.f0[n] >> 1, self.f1[n] >> 1]
        return seen

    def levels(self):
        lev = [0] * len(self.kind)
        for n, k in enumerate(self.kind):
            if k == AND:
                lev[n] = 1 + max(lev[self.f0[n] >> 1], lev[self.f1[n] >> 1])
        return lev

    def depth(self):
        lev = self.levels()
        return max([lev[l >> 1] for l in self.roots()] or [0])

    def stats(self):
        live = self.live()
        return {"pis": len(self.pis), "pos": len(self.pos), "ffs": len(self.ffs),
                "ands": sum(1 for n in live if self.kind[n] == AND), "depth": self.depth()}

    def support(self, lit, limit=None):
        """Leaf nodes (PI / FF) in the cone of lit; None if more than `limit`."""
        seen, leaves = set(), []
        stack = [lit >> 1]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            k = self.kind[n]
            if k == AND:
                stack += [self.f0[n] >> 1, self.f1[n] >> 1]
            elif k != CONST:
                leaves.append(n)
                if limit is not None and len(leaves) > limit:
                    return None
        return sorted(leaves)

    def cone_truth(self, lit, leaves):
        """Truth table (int, bit k = value for leaf assignment k) of lit over `leaves`."""
        n = len(leaves)
        width = 1 << n
        mask = (1 << width) - 1
        val = {}
        for i, leaf in enumerate(leaves):        # standard projection patterns
            v = 0
            for k in range(width):
                if (k >> i) & 1:
                    v |= 1 << k
            val[leaf] = v
        val[0] = 0
        order = []
        stack = [(lit >> 1, False)]
        seen = set(leaves) | {0}
        while stack:
            node, done = stack.pop()
            if done:
                order.append(node)
                continue
            if node in seen:
                continue
            seen.add(node)
            stack.append((node, True))
            for f in (self.f0[node] >> 1, self.f1[node] >> 1):
                if f not in seen:
                    stack.append((f, False))
        for node in order:
            a, b = self.f0[node], self.f1[node]
            x = val[a >> 1] ^ (mask if a & 1 else 0)
            y = val[b >> 1] ^ (mask if b & 1 else 0)
            val[node] = x & y
        return val[lit >> 1] ^ (mask if lit & 1 else 0)

    def is_const(self, lit, max_vars=16):
        """0 / 1 if lit is that constant (checked exhaustively on small supports), else None."""
        if lit <= 1:
            return lit
        leaves = self.support(lit, max_vars)
        if leaves is None:
            return None
        t = self.cone_truth(lit, leaves)
        full = (1 << (1 << len(leaves))) - 1
        return 1 if t == full else 0 if t == 0 else None

    # ------------------------------------------------------------------ simulation
    def sim_nodes(self, pi_vals, ff_vals=None, width=1):
        """Value of every node, bit-parallel over `width` patterns (ints)."""
        mask = (1 << width) - 1
        v = [0] * len(self.kind)
        ff_vals = ff_vals or {}
        for n, k in enumerate(self.kind):
            if k == AND:
                a, b = self.f0[n], self.f1[n]
                x = v[a >> 1] ^ (mask if a & 1 else 0)
                y = v[b >> 1] ^ (mask if b & 1 else 0)
                v[n] = x & y
            elif k == PI:
                v[n] = pi_vals.get(self.name[n], 0) & mask
            elif k == FFQ:
                v[n] = ff_vals.get(self.name[n], 0) & mask
        return v

    @staticmethod
    def lit_val(v, lit, mask):
        return v[lit >> 1] ^ (mask if lit & 1 else 0)

    def sim(self, pi_vals, ff_vals=None, width=1):
        """{po name: int} for one combinational evaluation."""
        mask = (1 << width) - 1
        v = self.sim_nodes(pi_vals, ff_vals, width)
        return {name: self.lit_val(v, l, mask) for name, l in self.pos.items()}

    def step(self, state, pi_vals, width=1):
        """One clock cycle (every FF clocked once): returns (outputs, next state).

        Async resets act immediately (before outputs are sampled) and at the edge."""
        mask = (1 << width) - 1
        v = self.sim_nodes(pi_vals, state, width)
        cur = dict(state)
        changed = False
        for f in self.ffs.values():
            if f.arst is not None:
                r = self.lit_val(v, f.arst, mask)
                if r:
                    val = mask if f.arst_val else 0
                    new = (cur.get(f.name, 0) & ~r) | (val & r)
                    if new != cur.get(f.name, 0):
                        cur[f.name] = new
                        changed = True
        if changed:
            v = self.sim_nodes(pi_vals, cur, width)
        outs = {name: self.lit_val(v, l, mask) for name, l in self.pos.items()}
        nxt = {}
        for f in self.ffs.values():
            d = self.lit_val(v, f.d, mask)
            if f.arst is not None:
                r = self.lit_val(v, f.arst, mask)
                val = mask if f.arst_val else 0
                d = (d & ~r) | (val & r)
            nxt[f.name] = d & mask
        return outs, nxt

    # ------------------------------------------------------------------ rebuilding
    def copy_into(self, other, lit_map, lits):
        """Rebuild the cones of `lits` in `other`; lit_map maps this AIG's leaf *nodes*
        (PI / FF) to literals of `other` and is extended with AND nodes. Returns the
        translated literals."""
        out = []
        for root in lits:
            stack = [root >> 1]
            while stack:
                n = stack[-1]
                if n in lit_map:
                    stack.pop()
                    continue
                if self.kind[n] == CONST:
                    lit_map[n] = 0
                    stack.pop()
                    continue
                if self.kind[n] != AND:
                    raise KeyError("leaf %s not mapped" % self.name[n])
                a, b = self.f0[n] >> 1, self.f1[n] >> 1
                if a in lit_map and b in lit_map:
                    stack.pop()
                    fa = lit_map[a] ^ (self.f0[n] & 1)
                    fb = lit_map[b] ^ (self.f1[n] & 1)
                    lit_map[n] = other.AND(fa, fb)
                else:
                    stack += [x for x in (a, b) if x not in lit_map]
            out.append(lit_map[root >> 1] ^ (root & 1))
        return out

    def cleanup(self):
        """New AIG with only live logic (node order kept), same PIs, POs and FFs."""
        g = AIG()
        m = {0: 0}
        for name, n in self.pis.items():
            m[n] = g.pi(name)
        for name, f in self.ffs.items():
            m[f.q] = g.ff(name, f.clk, f.edge, None, f.arst_val)
        for name, l in self.pos.items():
            g.po(name, self.copy_into(g, m, [l])[0])
        for name, f in self.ffs.items():
            g.ffs[name].d = self.copy_into(g, m, [f.d])[0]
            if f.arst is not None:
                g.ffs[name].arst = self.copy_into(g, m, [f.arst])[0]
        return g
