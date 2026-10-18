"""Tiny pure-Python gate-level simulator for generated combinational blocks.

It evaluates the placed instances by logic *role* (the same connectivity that goes
into the layout and the structural Verilog), so it needs no simulator and no cell
models. For sign-off, run the generated testbench with your PDK's Verilog models.
"""
GATES = {
    "inv": lambda v: 1 - v["A"],
    "nand2": lambda v: 1 - (v["A"] & v["B"]),
    "nor2": lambda v: 1 - (v["A"] | v["B"]),
    "nor3": lambda v: 1 - (v["A"] | v["B"] | v["C"]),
}


class Netlist:
    def __init__(self, blk):
        inv = {role: {p: lp for lp, p in c["pins"].items()} for role, c in blk.t.cells.items()}
        pins = {}
        for net, ips in blk.conn.items():
            for i, p in ips:
                pins.setdefault(i, {})[inv[blk.roles[i]][p]] = net
        driven = {ps["Y"]: (blk.roles[i], ps) for i, ps in pins.items()}
        order, seen = [], set()

        def visit(net):
            if net in seen or net not in driven:
                return
            seen.add(net)
            role, ps = driven[net]
            for lp, n in ps.items():
                if lp != "Y":
                    visit(n)
            order.append((role, ps))

        for net in driven:
            visit(net)
        self.order = order

    def eval(self, inputs):
        v = dict(inputs)
        for role, ps in self.order:
            v[ps["Y"]] = GATES[role]({lp: v[n] for lp, n in ps.items() if lp != "Y"})
        return v


def check_decoder(blk):
    """Exhaustive check: for every A (and EN) exactly Y[A] (or nothing) is high."""
    n, en = blk.meta["n_bits"], blk.meta["enable"]
    nl = Netlist(blk)
    errors = 0
    for e in ((0, 1) if en else (1,)):
        for a in range(1 << n):
            ins = {"A[%d]" % b: (a >> b) & 1 for b in range(n)}
            if en:
                ins["EN"] = e
            v = nl.eval(ins)
            high = {i for i in range(1 << n) if v["Y[%d]" % i]}
            if high != ({a} if e else set()):
                errors += 1
    return errors
