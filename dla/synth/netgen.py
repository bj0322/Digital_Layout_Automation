"""Build the gate-level Design from a mapped AIG: gates, flip-flops, tie cells, ports.

    d, ffmap = build_design(aig, ports, mapper, tl, top)
    # ffmap: {instance name: (AIG FF name, stored inverted)} for equivalence checking

Net names: primary inputs keep their port bit names, FF outputs are named after the
FF ("cnt[3]"), a net that drives an output port takes the port's name when it has no
name yet; everything else is "_N<k>_". Instances: "_G<k>_" for gates, the FF name plus "_reg"
for flip-flops (e.g. Verilog `\\cnt[3]_reg `, stored as "cnt\\[3\\]_reg").
"""
import re

from ..db.design import Design


def _esc(name):
    return name.replace("[", "\\[").replace("]", "\\]")


_BUSBIT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(\[\d+\])?$")


def canon(name):
    """Internal (DEF-style) form of an RTL bit name: plain bus bits stay "a[3]", anything
    else that needs escaping in Verilog gets escaped brackets ("u1.cnt\\[3\\]")."""
    return name if _BUSBIT.match(name) else _esc(name)


class NetGen:
    def __init__(self, g, tl, top):
        self.g, self.tl = g, tl
        self.d = Design(top)
        self.net = {}              # node-phase id -> net name
        self.k = 0
        self.gk = 0
        self.tk = 0

    def new_net(self):
        self.k += 1
        return "_N%d_" % self.k

    def tie(self, value):
        cell = self.tl.tiehi if value else self.tl.tielo
        if cell is None:
            raise ValueError("library has no tie-%s cell" % ("high" if value else "low"))
        self.tk += 1
        name = "_T%d_" % self.tk
        net = self.new_net()
        self.d.add_inst(name, cell[1])
        self.d.connect(name, cell[2], net)
        return net

    def gate_inst(self, gate, ins, out_net):
        self.gk += 1
        name = "_G%d_" % self.gk
        self.d.add_inst(name, gate.name)
        for pin, net in ins:
            self.d.connect(name, pin, net)
        self.d.connect(name, gate.output, out_net)
        return name


def build_design(g, ports, m, tl, top, ff_choice):
    """g: AIG; ports: [(name, dir, msb, lsb)]; m: Mapper after map(); ff_choice: {ff: (FFCell, inverted)}."""
    ng = NetGen(g, tl, top)
    d = ng.d
    for name, n in g.pis.items():
        ng.net[2 * n] = name
    ff_nodes = {}
    for name, f in g.ffs.items():
        cell, inv = ff_choice[name]
        ng.net[2 * f.q] = canon(name)
        ff_nodes[f.q] = (name, cell, inv)
        if (cell.qn if not inv else cell.q):         # complement output of the cell is free
            ng.net[2 * f.q + 1] = ng.new_net()
    # ports (in port order, bit order MSB first like the RTL)
    for pname, dr, msb, lsb in ports:
        bits = [pname] if msb is None else ["%s[%d]" % (pname, i) for i in
                                            (range(msb, lsb - 1, -1) if msb >= lsb else range(msb, lsb + 1))]
        for b in bits:
            d.ports[b] = None                         # placeholder to keep the order
    if any(len(p) == 4 and p[2] is not None for p in ports):
        d.buses = {p[0]: (p[2], p[3]) for p in ports if p[2] is not None}
    # names for nets driving outputs
    for pname in list(d.ports):
        if pname in g.pos:
            lit = g.pos[pname]
            if lit > 1 and lit not in ng.net and m.choice[lit].kind != "leaf":
                ng.net[lit] = pname
    # nets for every used node-phase
    for i in m.used():
        if i in ng.net:
            continue
        c = m.choice[i]
        if c.kind == "const":
            continue
        ng.net[i] = ng.new_net()
    # gates
    for i in m.used():
        c = m.choice[i]
        if c.kind in ("leaf", "const"):
            continue
        ng.gate_inst(c.gate, [(pin, net_of(ng, m, l)) for pin, l in zip(c.pins, c.leaves)], ng.net[i])
    # ports
    from ..db.design import PIN, Port
    for pname in list(d.ports):
        dr = None
        for p in ports:
            if p[0] == pname or pname.startswith(p[0] + "["):
                dr = p[1]
        if dr == "input":
            d.ports[pname] = Port(pname, "INPUT", pname)
            d.add_net(pname).conns.insert(0, (PIN, pname))
        else:
            lit = g.pos[pname]
            net = net_of(ng, m, lit)
            d.ports[pname] = Port(pname, "OUTPUT", net)
            d.add_net(net).conns.append((PIN, pname))
    # flip-flops
    ffmap = {}
    for name, f in g.ffs.items():
        cell, inv = ff_choice[name]
        inst = _esc(name) + "_reg"                    # Verilog: instances and nets share one namespace
        d.add_inst(inst, cell.name)
        ffmap[inst] = (name, inv)
        clk = f.clk
        if cell.edge != f.edge:
            n2 = ng.new_net()
            ng.gate_inst(tl.inv, [(tl.inv.inputs[0], clk)], n2)
            clk = n2
        d.connect(inst, cell.clk, clk)
        d.connect(inst, cell.d, net_of(ng, m, f.d ^ (1 if inv else 0)))
        qpin, qnpin = (cell.q, cell.qn) if not inv else (cell.qn, cell.q)
        d.connect(inst, qpin, canon(name))
        if qnpin and (2 * f.q + 1) in ng.net and m.refs[2 * f.q + 1] > 0:
            d.connect(inst, qnpin, ng.net[2 * f.q + 1])
        # async control: stored value after clear is IQ = 0
        want = None if f.arst is None else (f.arst_val ^ (1 if inv else 0))   # IQ value to force
        for kind, pinlvl in (("clear", cell.clear), ("preset", cell.preset)):
            if pinlvl is None:
                continue
            pin, lvl = pinlvl
            forces = 0 if kind == "clear" else 1
            if want is not None and want == forces:
                d.connect(inst, pin, net_of(ng, m, f.arst ^ (0 if lvl else 1)))
            else:
                d.connect(inst, pin, ng.tie(0 if lvl else 1))      # inactive
    # an output port driven by an internal net gives the net its name
    for pname, p in d.ports.items():
        if p.direction == "OUTPUT" and p.net != pname and p.net.startswith("_N") and pname not in d.nets:
            if sum(1 for q in d.ports.values() if q.net == p.net) == 1:
                d.rename_net(p.net, pname)
    return d, ffmap


def net_of(ng, m, lit):
    if lit in (0, 1):
        return ng.tie(lit)
    if lit in ng.net:
        return ng.net[lit]
    raise KeyError("literal %d has no net" % lit)
