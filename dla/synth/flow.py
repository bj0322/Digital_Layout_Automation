"""RTL -> gate-level netlist.

    from dla.synth.flow import synthesize
    r = synthesize(["bench/gcd.v"], "gcd", liberty)          # liberty: dla.db.liberty.LibertyLib
    r.design                    # dla.db.Design (write with dla.db.write_verilog)
    r.ffmap                     # {FF instance: (RTL register bit, stored inverted)}
    r.report                    # cells, area, depth, run times, equivalence result

Steps: elaborate -> sweep -> FRAIG -> refactor / balance / FRAIG until no gain
-> FF cell choice -> cut mapping (area recovery)
-> netlist (gates, FFs, tie cells) -> equivalence check of the netlist against the
elaborated RTL (SAT, at FF boundaries). A failed check raises SynthError: the netlist
is never returned unverified.
"""
import time
from collections import Counter

from ..rtl.elab import _run_big_stack, elaborate
from .cells import TargetLib
from .fraig import cec, fraig
from .mapper import Mapper
from .netgen import build_design
from .opt import sweep
from .rewrite import balance, refactor
from .verify import design_to_aig


class SynthError(RuntimeError):
    pass


class SynthResult:
    def __init__(self, design, ffmap, report, rtl_aig, warnings):
        self.design, self.ffmap, self.report, self.rtl_aig, self.warnings = design, ffmap, report, rtl_aig, warnings


_TL_CACHE = {}


def target_lib(liberty):
    key = id(liberty)
    if key not in _TL_CACHE:
        _TL_CACHE[key] = (liberty, TargetLib(liberty))
    return _TL_CACHE[key][1]


def _ff_roots(g, choice):
    roots = []
    for name, f in g.ffs.items():
        cell, inv = choice[name]
        roots.append(f.d ^ (1 if inv else 0))
        if f.arst is not None:
            want = f.arst_val ^ (1 if inv else 0)
            pinlvl = cell.clear if want == 0 else cell.preset
            roots.append(f.arst ^ (0 if pinlvl[1] else 1))
    return roots


def synthesize(paths, top, liberty, params=None, verify=True, k=6, cuts=8, opt_rounds=4, mode="area",
               defines=None):
    def run():
        t = {}
        t0 = time.time()
        r = elaborate(paths, top=top, params=params, defines=defines)
        t["elaborate"] = time.time() - t0
        g0 = sweep(r.aig)
        t1 = time.time()
        g = fraig(g0)
        for _ in range(opt_rounds):
            h = fraig(balance(refactor(g, 8)))
            if h.stats()["ands"] >= g.stats()["ands"]:
                break
            g = h
        t["optimize"] = time.time() - t1
        tl = target_lib(liberty)
        choice = {}
        for name, f in g.ffs.items():
            reset = None if f.arst is None else f.arst_val
            choice[name] = tl.ff_for(f.edge, reset)
        t2 = time.time()
        roots = list(g.pos.values()) + _ff_roots(g, choice)
        free = {g.ffs[n].q for n, (c, inv) in choice.items() if (c.qn if not inv else c.q)}
        m = Mapper(g, tl, k, cuts, free).map(roots, mode=mode)
        # upgrade FFs whose complement is built with an inverter to a cell with a QN output
        changed = False
        for name, f in g.ffs.items():
            idx = 2 * f.q + 1
            cell, inv = choice[name]
            if f.q in free or m.refs[idx] == 0:
                continue
            alts = [c for c in tl.ff_cells if c.edge == cell.edge and c.clear == cell.clear and
                    c.preset == cell.preset and c.q and c.qn]
            alts.sort(key=lambda c: (c.area, c.name))
            if alts and alts[0].area - cell.area < tl.inv.area:
                choice[name] = (alts[0], inv)
                changed = True
        if changed:
            roots = list(g.pos.values()) + _ff_roots(g, choice)
            free = {g.ffs[n].q for n, (c, inv) in choice.items() if (c.qn if not inv else c.q)}
            m = Mapper(g, tl, k, cuts, free).map(roots, mode=mode)
        t["map"] = time.time() - t2
        d, ffmap = build_design(g, r.ports, m, tl, top, choice)
        rep = {"cells": dict(Counter(i.master for i in d.insts.values())),
               "instances": len(d.insts),
               "area": round(sum(liberty.cells[i.master].area for i in d.insts.values()), 4),
               "flops": len(g.ffs), "aig_ands": g0.stats()["ands"], "opt_ands": g.stats()["ands"],
               "depth_ns": round(m.depth(), 4), "mode": mode}
        if verify:
            t3 = time.time()
            ga = design_to_aig(d, liberty, ffmap)
            res = cec(g0, ga)
            t["verify"] = time.time() - t3
            rep["equivalence"] = "PASS" if res.ok else "FAIL"
            if not res.ok:
                raise SynthError("netlist is not equivalent to the RTL: %s" % res)
        rep["time_s"] = {k_: round(v, 2) for k_, v in t.items()}
        return SynthResult(d, ffmap, rep, g0, r.warnings)
    return _run_big_stack(run)
