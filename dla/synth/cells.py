"""Target cell library for technology mapping, derived from a Liberty library.

    tl = TargetLib(lib.liberty["typ"])            # dla.db.liberty.LibertyLib
    tl.matches(n, tt)      # [(Gate, perm, negmask)] implementing the n-input truth table tt
    tl.inv, tl.buf, tl.tiehi, tl.tielo, tl.ff_cells

Combinational gates: single-output cells with a Boolean `function`, no `dont_use`,
no three-state, no state; up to 6 inputs. For every gate all input permutations and
input inversions are tabulated, so matching a cut is one dictionary lookup:
key (n, tt) where bit k of tt is the output for leaf assignment k (leaf i = bit i of k).
A match (gate, perm, negmask) means: gate input perm[i] is driven by leaf i, complemented
if bit i of negmask is set.

Flip-flops: cells with an `ff` group whose next_state is a single input pin and whose
clock is one pin (rising or falling); clear / preset pins are recorded with their
active level. Scan flops and clock gates are not used by synthesis.
"""
import itertools
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class Gate:
    name: str
    inputs: List[str]
    output: str
    area: float
    tt: int                              # over `inputs` (input i = bit i of the minterm index)
    delay: Dict[str, float] = field(default_factory=dict)    # input -> nominal delay (ns)
    cap: Dict[str, float] = field(default_factory=dict)


@dataclass
class FFCell:
    name: str
    area: float
    clk: str
    edge: str                            # pos | neg
    d: str
    q: Optional[str]                     # pin carrying IQ
    qn: Optional[str]                    # pin carrying IQN
    clear: Optional[Tuple[str, int]] = None    # (pin, active level)
    preset: Optional[Tuple[str, int]] = None


def _tt_of(expr, inputs):
    n = len(inputs)
    tt = 0
    for k in range(1 << n):
        env = {p: (k >> i) & 1 for i, p in enumerate(inputs)}
        if expr.eval(env):
            tt |= 1 << k
    return tt


def permute_tt(tt, n, perm, negmask):
    """Truth table over leaves when gate input perm[i] is leaf i (complemented per negmask)."""
    out = 0
    for k in range(1 << n):
        g = 0
        for i in range(n):
            bit = ((k >> i) & 1) ^ ((negmask >> i) & 1)
            g |= bit << perm[i]
        if (tt >> g) & 1:
            out |= 1 << k
    return out


def _simple_pin(expr, pins):
    """Pin name if expr is a single pin (optionally negated): (pin, active level)."""
    if expr is None:
        return None
    node = expr.node
    if node[0] == "var" and node[1] in pins:
        return (node[1], 1)
    if node[0] == "not" and node[1][0] == "var" and node[1][1] in pins:
        return (node[1][1], 0)
    return None


class TargetLib:
    MAX_IN = 6

    def __init__(self, liberty, nominal_slew=0.1, nominal_load=0.01, exclude=()):
        self.liberty = liberty
        self.gates: List[Gate] = []
        self.ff_cells: List[FFCell] = []
        self.table: Dict[Tuple[int, int], List[tuple]] = {}
        self.tiehi = self.tielo = None
        self.inv = self.buf = None
        self.skipped = {}
        for name, c in sorted(liberty.cells.items()):
            why = self._classify(c, nominal_slew, nominal_load, exclude)
            if why:
                self.skipped[name] = why
        if self.inv is None:
            raise ValueError("library %s has no usable inverter" % liberty.name)
        self._build_table()

    def _classify(self, c, slew, load, exclude):
        if c.name in exclude:
            return "excluded"
        if c.dont_use:
            return "dont_use"
        if c.unsupported:
            return "unsupported: " + ",".join(c.unsupported)
        if c.attrs.get("test_cell"):
            return "scan cell"
        outs = c.outputs()
        ins = [p.name for p in c.inputs()]
        if c.is_sequential():
            return self._classify_ff(c, ins, outs)
        if len(outs) != 1 or outs[0].function is None:
            return "not a single-output logic cell"
        o = outs[0]
        if o.three_state is not None:
            return "three-state"
        if not ins:
            v = o.function.truth([])[0]
            tie = (c.area, c.name, o.name)
            if v and (self.tiehi is None or tie < self.tiehi):
                self.tiehi = tie
            if not v and (self.tielo is None or tie < self.tielo):
                self.tielo = tie
            return "tie"
        if len(ins) > self.MAX_IN:
            return "too many inputs"
        if set(o.function.vars()) - set(ins):
            return "function uses internal state"
        tt = _tt_of(o.function, ins)
        g = Gate(c.name, ins, o.name, c.area, tt)
        for p in ins:
            a = o.arc(p)
            ds = [a.tables[k].delay(slew, load) for k in ("cell_rise", "cell_fall") if a is not None and k in a.tables]
            g.delay[p] = max(ds) if ds else 0.0
            g.cap[p] = c.pins[p].capacitance
        self.gates.append(g)
        if len(ins) == 1 and tt == 0b01 and (self.inv is None or g.area < self.inv.area):
            self.inv = g
        if len(ins) == 1 and tt == 0b10 and (self.buf is None or g.area < self.buf.area):
            self.buf = g
        return None

    def _classify_ff(self, c, ins, outs):
        s = c.seq
        if s.kind != "ff":
            return "latch"
        ck = _simple_pin(s.clocked_on, ins)
        d = _simple_pin(s.next_state, ins)
        if ck is None or d is None or d[1] != 1:
            return "flip-flop with logic on clock / data"
        clear = _simple_pin(s.clear, ins) if s.clear is not None else None
        preset = _simple_pin(s.preset, ins) if s.preset is not None else None
        if (s.clear is not None and clear is None) or (s.preset is not None and preset is None):
            return "complex clear / preset"
        q = qn = None
        for o in outs:
            if o.function is None:
                continue
            if o.function.node == ("var", s.state[0]):
                q = o.name
            elif len(s.state) > 1 and o.function.node == ("var", s.state[1]):
                qn = o.name
        if q is None and qn is None:
            return "no state output"
        self.ff_cells.append(FFCell(c.name, c.area, ck[0], "pos" if ck[1] else "neg", d[0], q, qn, clear, preset))
        return None

    def _build_table(self):
        for g in self.gates:
            n = len(g.inputs)
            for perm in itertools.permutations(range(n)):
                for neg in range(1 << n):
                    key = (n, permute_tt(g.tt, n, perm, neg))
                    self.table.setdefault(key, []).append((g, perm, neg))
        for key, lst in self.table.items():
            # keep, per inversion pattern, only the smallest gate; fewer inverted inputs first
            best = {}
            for g, perm, neg in lst:
                k = neg
                if k not in best or g.area < best[k][0].area:
                    best[k] = (g, perm, neg)
            self.table[key] = sorted(best.values(), key=lambda m: (m[0].area, bin(m[2]).count("1")))

    def matches(self, n, tt):
        return self.table.get((n, tt), ())

    def ff_for(self, edge, reset):
        """Cheapest FF cell: reset in (None, 0, 1) -> (cell, store_inverted)."""
        cands = []
        for f in self.ff_cells:
            inv_state = False
            if reset is None:
                ok = True
            elif reset == 0:
                ok = f.clear is not None
                if not ok and f.preset is not None and f.qn is not None:
                    ok, inv_state = True, True
            else:
                ok = f.preset is not None
                if not ok and f.clear is not None and f.qn is not None:
                    ok, inv_state = True, True
            if not ok:
                continue
            cost = f.area + (0 if f.edge == edge else self.inv.area)
            cands.append((cost, f.name, f, inv_state))
        if not cands:
            raise ValueError("no flip-flop cell for %s edge, reset %s" % (edge, reset))
        cands.sort(key=lambda c: (c[0], c[1]))
        return cands[0][2], cands[0][3]
