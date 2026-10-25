"""Cut-based technology mapping of an AIG onto library gates (area oriented, delay aware).

    m = Mapper(aig, target_lib, k=6, cuts=8)
    m.map(roots)               # roots: literals that must be produced
    m.cover                    # {id: choice} for every used node-phase id = 2 * node + phase
    m.area(), m.depth()

Every AIG node is mapped in both phases (id 2n: the node, 2n+1: its complement). A
phase is implemented by a gate matching one of the node's k-feasible cuts (any input
permutation, any input inversions: the leaves are then needed in the other phase) or
by an inverter on the opposite phase. Primary inputs / FF outputs are free in
positive phase.

Algorithm (standard cut-based mapping, cf. Mishchenko, Chatterjee, Brayton,
"Combinational and sequential mapping with priority cuts", ICCAD 2007):
  1. priority cuts: per node at most `cuts` cuts of at most `k` leaves, ranked by
     area flow;
  2. area-flow mapping in topological order (two passes, fanout estimates refined
     from the previous cover);
  3. exact local area recovery (reference / dereference of the cone a choice frees),
     two passes.
Arrival times use each gate's nominal pin-to-output delay (load independent). In
"delay" mode the first pass minimises arrival, and area recovery only accepts
choices that keep every node's required time (from the critical delay).
"""
from .aig import AND, CONST, FFQ, PI


class Choice:
    __slots__ = ("kind", "gate", "leaves", "pins", "area", "delays")

    def __init__(self, kind, gate=None, leaves=(), pins=(), area=0.0, delays=()):
        self.kind = kind            # leaf | inv | gate | const
        self.gate = gate
        self.leaves = leaves        # node-phase ids feeding the gate (in `pins` order)
        self.pins = pins
        self.area = area
        self.delays = delays        # pin-to-output delay per leaf (ns)

    def arrival(self, arr):
        return max([arr[l] + d for l, d in zip(self.leaves, self.delays)] or [0.0])


class Mapper:
    def __init__(self, g, tl, k=6, cuts=8, free_inv_leaves=()):
        self.g, self.tl, self.K, self.C = g, tl, k, cuts
        self.ttc = {}
        self.inv = tl.inv
        self.inv_d = max(tl.inv.delay.values()) if tl.inv.delay else 0.0
        self.free_inv = set(free_inv_leaves)       # leaves whose complement is free (FF with QN)

    # ------------------------------------------------------------------ cuts
    def _cut_tt(self, n, leaves):
        key = (n, leaves)
        t = self.ttc.get(key)
        if t is None:
            t = self.g.cone_truth(2 * n, list(leaves))
            self.ttc[key] = t
        return t

    def enumerate_cuts(self, nodes, af):
        g, K, C = self.g, self.K, self.C
        for n in nodes:
            kind = g.kind[n]
            if kind != AND:
                self.cuts[n] = [(n,)]
                continue
            a, b = g.f0[n] >> 1, g.f1[n] >> 1
            cand = set()
            for ca in self.cuts[a]:
                for cb in self.cuts[b]:
                    u = tuple(sorted(set(ca) | set(cb)))
                    if len(u) <= K:
                        cand.add(u)
            cand.discard((0,))
            cand = [c for c in cand if 0 not in c]
            # dominance: drop cuts that contain another cut
            cand.sort(key=len)
            keep = []
            for c in cand:
                sc = set(c)
                if not any(set(d) <= sc for d in keep):
                    keep.append(c)
            keep.sort(key=lambda c: (sum(min(af[2 * l], af[2 * l + 1]) / max(1, self.fanout[l]) for l in c), len(c)))
            self.cuts[n] = keep[:C] + [(n,)]

    # ------------------------------------------------------------------ choices
    def _inv_choice(self, other):
        return Choice("inv", self.inv, (other,), (self.inv.inputs[0],), self.inv.area, (self.inv_d,))

    def candidates(self, n, p):
        """All gate choices for node-phase (n, p)."""
        out = []
        for cut in self.cuts[n][:-1]:
            m = len(cut)
            tt = self._cut_tt(n, cut)
            full = (1 << (1 << m)) - 1
            target = tt if p == 0 else tt ^ full
            for gate, perm, neg in self.tl.matches(m, target):
                leaves, pins = [], []
                for i, leaf in enumerate(cut):
                    leaves.append(2 * leaf + ((neg >> i) & 1))
                    pins.append(gate.inputs[perm[i]])
                out.append(Choice("gate", gate, tuple(leaves), tuple(pins), gate.area,
                                  tuple(gate.delay.get(p, 0.0) for p in pins)))
        return out

    def map(self, roots, passes=2, recover=2, mode="area"):
        """mode "area": minimum area (arrival only breaks ties);
        "delay": minimum arrival first, then area recovery that keeps every required time."""
        self.mode = mode
        self.req = None
        g = self.g
        n = len(g.kind)
        self.fanout = [0] * n
        for i in range(n):
            if g.kind[i] == AND:
                self.fanout[g.f0[i] >> 1] += 1
                self.fanout[g.f1[i] >> 1] += 1
        self.cuts = [None] * n
        self.roots = list(roots)
        live = self._live(roots)
        order = sorted(live)
        INF = float("inf")
        af = [INF] * (2 * n)
        arr = [0.0] * (2 * n)
        choice = [None] * (2 * n)
        est = [max(1, self.fanout[i]) for i in range(n) for _ in (0, 1)]
        for i in order:
            if g.kind[i] in (PI, FFQ):
                af[2 * i], choice[2 * i] = 0.0, Choice("leaf")
                if i in self.free_inv:
                    af[2 * i + 1], choice[2 * i + 1] = 0.0, Choice("leaf")
                else:
                    af[2 * i + 1] = self.inv.area
                    arr[2 * i + 1] = self.inv_d
                    choice[2 * i + 1] = self._inv_choice(2 * i)
            elif g.kind[i] == CONST:
                af[0] = af[1] = 0.0
                choice[0], choice[1] = Choice("const"), Choice("const")
        self.enumerate_cuts(order, af)
        ands = [i for i in order if g.kind[i] == AND]
        for it in range(passes):
            for i in ands:
                for p in (0, 1):
                    best = None
                    for c in self.candidates(i, p):
                        cost = c.area + sum(af[l] / est[l] for l in c.leaves)
                        t = c.arrival(arr)
                        key = (t, cost) if (mode == "delay" and it == 0) else (cost, t)
                        if best is None or key < best[0]:
                            best = (key, cost, t, c)
                    if best is not None:
                        af[2 * i + p], arr[2 * i + p], choice[2 * i + p] = best[1], best[2], best[3]
                for p in (0, 1):
                    o = 2 * i + (1 - p)
                    cost = self.inv.area + af[o] / est[o]
                    t = arr[o] + self.inv_d
                    if choice[2 * i + p] is None:
                        better = True
                    elif mode == "delay" and it == 0:
                        better = (t, cost) < (arr[2 * i + p], af[2 * i + p])
                    else:
                        better = cost < af[2 * i + p]
                    if choice[o] is not None and choice[o].kind != "inv" and better:
                        af[2 * i + p] = cost
                        arr[2 * i + p] = t
                        choice[2 * i + p] = self._inv_choice(o)
            self.choice = choice
            refs = self._cover()
            est = [max(1, r) for r in refs]
        self.choice = choice
        self.refs = self._cover()
        for _ in range(recover):
            self.req = self._required() if mode == "delay" else None
            self._recover(ands)
        self.refs = self._cover()
        self.arrival = self._arrival()
        return self

    def _live(self, roots):
        g = self.g
        seen = set()
        stack = [r >> 1 for r in roots]
        while stack:
            i = stack.pop()
            if i in seen:
                continue
            seen.add(i)
            if g.kind[i] == AND:
                stack += [g.f0[i] >> 1, g.f1[i] >> 1]
        return seen

    def _cover(self):
        refs = [0] * len(self.choice)
        stack = list(self.roots)
        while stack:
            i = stack.pop()
            refs[i] += 1
            if refs[i] == 1:
                c = self.choice[i]
                if c is None:
                    raise RuntimeError("node-phase %d has no implementation" % i)
                stack += list(c.leaves)
        return refs

    # ------------------------------------------------------------------ exact area recovery
    def _ref(self, i):
        refs = self.refs
        refs[i] += 1
        if refs[i] > 1:
            return 0.0
        c = self.choice[i]
        return c.area + sum(self._ref(l) for l in c.leaves)

    def _deref(self, i):
        refs = self.refs
        refs[i] -= 1
        if refs[i] > 0:
            return 0.0
        c = self.choice[i]
        return c.area + sum(self._deref(l) for l in c.leaves)

    def _cost_of(self, c):
        a = c.area + sum(self._ref(l) for l in c.leaves)
        for l in c.leaves:
            self._deref(l)
        return a

    def _recover(self, ands):
        refs, choice = self.refs, self.choice
        for i in ands:
            for p in (0, 1):
                idx = 2 * i + p
                if refs[idx] == 0:
                    continue
                cur = choice[idx]
                for l in cur.leaves:
                    self._deref(l)
                best, best_cost = cur, self._cost_of(cur)
                cands = self.candidates(i, p)
                o = idx ^ 1
                if choice[o] is not None and choice[o].kind != "inv":
                    cands.append(self._inv_choice(o))
                for c in cands:
                    if c is cur:
                        continue
                    if self.req is not None and c.arrival(self.arr) > self.req.get(idx, float("inf")) + 1e-12:
                        continue
                    cost = self._cost_of(c)
                    if cost < best_cost - 1e-9:
                        best, best_cost = c, cost
                choice[idx] = best
                if self.req is not None:
                    self.arr[idx] = best.arrival(self.arr)
                for l in best.leaves:
                    self._ref(l)

    def _required(self):
        """Required time of every used node-phase for the current cover's critical delay;
        also refreshes self.arr (arrival per node-phase)."""
        self.arr = self._arrival(full=True)
        dmax = max([self.arr[r] for r in self.roots] or [0.0])
        req = {}
        for r in self.roots:
            req[r] = dmax
        for i in sorted((i for i, r in enumerate(self.refs) if r > 0), key=lambda i: -self.arr[i]):
            c = self.choice[i]
            if i not in req:
                continue
            for l, d in zip(c.leaves, c.delays):
                v = req[i] - d
                if v < req.get(l, float("inf")):
                    req[l] = v
        return req

    # ------------------------------------------------------------------ results
    def _arrival(self, full=False):
        """Arrival per node-phase: a dict over the cover's cone, or (full) a list over all ids."""
        if not full:
            arr = {}
            for r in self.roots:
                self._at_rec(r, arr)
            return arr
        arr = [0.0] * len(self.choice)
        for n in range(len(self.choice) // 2):
            ids = [2 * n, 2 * n + 1]
            ids.sort(key=lambda i: self.choice[i] is not None and self.choice[i].kind == "inv")
            for i in ids:
                c = self.choice[i]
                arr[i] = 0.0 if c is None or c.kind in ("leaf", "const") else c.arrival(arr)
        return arr

    def _at_rec(self, i, arr):
        stack = [i]
        while stack:
            x = stack[-1]
            if x in arr:
                stack.pop()
                continue
            c = self.choice[x]
            todo = [l for l in c.leaves if l not in arr]
            if todo:
                stack += todo
                continue
            stack.pop()
            arr[x] = 0.0 if c.kind in ("leaf", "const") else c.arrival(arr)

    def used(self):
        return [i for i, r in enumerate(self.refs) if r > 0]

    def area(self):
        return sum(self.choice[i].area for i in self.used())

    def depth(self):
        return max([self.arrival.get(r, 0.0) for r in self.roots] or [0.0])
