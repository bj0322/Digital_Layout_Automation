"""Gate-level Design -> AIG (using the Liberty functions), for equivalence checks and simulation.

    g = design_to_aig(design, liberty, ffmap)
    cec(rtl_aig, g).ok

Combinational cells contribute their `function`; flip-flops (Liberty `ff` groups)
become AIG FFs named through `ffmap` ({instance: (name, stored_inverted)}; instances
not in the map keep their own name). Clock pins are traced back through buffers and
inverters to an input port, so a clock inverter flips the FF's edge. Clear / preset
pins tied to their inactive level are dropped; a connected one becomes the FF's async
control. Undriven nets are an error.
"""
from ..db.design import PIN
from .aig import AIG


class NetlistError(RuntimeError):
    pass


def _expr_aig(g, node, env):
    k = node[0]
    if k == "var":
        return env[node[1]]
    if k == "const":
        return node[1]
    if k == "not":
        return _expr_aig(g, node[1], env) ^ 1
    vals = [_expr_aig(g, c, env) for c in node[1]]
    if k == "and":
        return g.AND_N(vals)
    if k == "or":
        return g.OR_N(vals)
    return g.XOR_N(vals)


def design_to_aig(d, lib, ffmap=None):
    """lib: dla.db.liberty.LibertyLib."""
    ffmap = ffmap or {}
    g = AIG()
    pins = d.inst_pins()
    driver = {}
    for n in d.nets.values():
        for inst, pin in n.conns:
            if inst == PIN:
                p = d.ports[pin]
                if p.direction == "INPUT":
                    driver.setdefault(n.name, []).append(("PIN", pin))
                continue
            c = lib.cells.get(d.insts[inst].master)
            if c is None:
                raise NetlistError("cell %s of %s not in the library" % (d.insts[inst].master, inst))
            if c.pins[pin].direction == "output":
                driver.setdefault(n.name, []).append((inst, pin))
    for net, drv in driver.items():
        if len(drv) > 1:
            raise NetlistError("net %s has %d drivers: %s" % (net, len(drv), drv[:3]))
    lit = {}
    for p in d.ports.values():
        if p.direction == "INPUT":
            lit[p.net or p.name] = g.pi(p.name)
    # flip-flops first: their outputs are leaves
    seq = {}
    for name, inst in d.insts.items():
        c = lib.cells[inst.master]
        if c.is_sequential():
            s = c.seq
            if s.kind != "ff":
                raise NetlistError("%s: latches are not supported" % name)
            ffname, inv = ffmap.get(name, (name, False))
            q = g.ff(ffname)
            iq = q ^ (1 if inv else 0)
            seq[name] = (c, ffname, inv)
            for pn, net in pins.get(name, {}).items():
                pc = c.pins[pn]
                if pc.direction == "output" and pc.function is not None:
                    st = {s.state[0]: iq}
                    if len(s.state) > 1:
                        st[s.state[1]] = iq ^ 1
                    lit[net] = _expr_aig(g, pc.function.node, st)
    busy = set()

    def net_lit(net):
        if net in lit:
            return lit[net]
        if net in ("1'b0", "1'b1"):
            return 1 if net == "1'b1" else 0
        drv = driver.get(net)
        if not drv:
            raise NetlistError("net %s is not driven" % net)
        inst, pin = drv[0]
        if net in busy:
            raise NetlistError("combinational loop through net %s" % net)
        busy.add(net)
        c = lib.cells[d.insts[inst].master]
        env = {}
        for pn, n2 in pins[inst].items():
            if c.pins[pn].direction == "input":
                env[pn] = net_lit(n2)
        f = c.pins[pin].function
        if f is None:
            raise NetlistError("%s.%s has no function" % (inst, pin))
        v = _expr_aig(g, f.node, env)
        busy.discard(net)
        lit[net] = v
        return v

    def trace_clock(net):
        edge_flip = 0
        for _ in range(64):
            drv = driver.get(net)
            if not drv:
                raise NetlistError("clock net %s is not driven" % net)
            inst, pin = drv[0]
            if inst == "PIN":
                return pin, edge_flip
            c = lib.cells[d.insts[inst].master]
            f = c.pins[pin].function
            ins = [pn for pn, _ in pins[inst].items() if c.pins[pn].direction == "input"]
            if f is None or len(ins) != 1:
                raise NetlistError("clock of net %s passes through %s (only buffers / inverters allowed)" % (net, inst))
            tt = f.truth(ins)
            if tt == [1, 0]:
                edge_flip ^= 1
            elif tt != [0, 1]:
                raise NetlistError("clock passes through non-buffer %s" % inst)
            net = pins[inst][ins[0]]
        raise NetlistError("clock tree too deep at %s" % net)

    for name, (c, ffname, inv) in seq.items():
        s = c.seq
        f = g.ffs[ffname]
        conn = pins.get(name, {})
        ck = s.clocked_on.node
        ckpin, cklvl = (ck[1], 1) if ck[0] == "var" else (ck[1][1], 0)
        port, flip = trace_clock(conn[ckpin])
        f.clk = port
        f.edge = "pos" if (cklvl ^ flip) else "neg"
        ns = s.next_state.node
        if ns[0] != "var":
            raise NetlistError("%s: next_state %s is not a pin" % (name, s.next_state.text))
        f.d = net_lit(conn[ns[1]]) ^ (1 if inv else 0)
        for kind, ex in (("clear", s.clear), ("preset", s.preset)):
            if ex is None:
                continue
            node = ex.node
            pn, lvl = (node[1], 1) if node[0] == "var" else (node[1][1], 0)
            v = net_lit(conn[pn])
            active = v if lvl else v ^ 1
            if active == 0:
                continue
            if f.arst is not None:
                raise NetlistError("%s: both clear and preset connected" % name)
            f.arst = active
            f.arst_val = (0 if kind == "clear" else 1) ^ (1 if inv else 0)
    for p in d.ports.values():
        if p.direction == "OUTPUT":
            g.po(p.name, net_lit(p.net or p.name))
    return g
