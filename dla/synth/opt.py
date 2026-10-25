"""AIG clean-up passes used before mapping.

    g = sweep(g)        # drop logic and flip-flops that cannot reach an output
"""
from .aig import AIG, AND


def sweep(g):
    """Copy of g without dead logic and without FFs whose output never reaches a PO."""
    live_ff = set()
    seen = set()
    stack = [l >> 1 for l in g.pos.values()]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        if g.kind[n] == AND:
            stack += [g.f0[n] >> 1, g.f1[n] >> 1]
        elif n in g.ff_of:
            name = g.ff_of[n]
            live_ff.add(name)
            f = g.ffs[name]
            stack.append(f.d >> 1)
            if f.arst is not None:
                stack.append(f.arst >> 1)
    h = AIG()
    m = {0: 0}
    for name, n in g.pis.items():
        m[n] = h.pi(name)
    for name, f in g.ffs.items():
        if name in live_ff:
            m[f.q] = h.ff(name, f.clk, f.edge, None, f.arst_val)
    for name, l in g.pos.items():
        h.po(name, g.copy_into(h, m, [l])[0])
    for name in g.ffs:
        if name in live_ff:
            f = g.ffs[name]
            h.ffs[name].d = g.copy_into(h, m, [f.d])[0]
            if f.arst is not None:
                h.ffs[name].arst = g.copy_into(h, m, [f.arst])[0]
    return h
