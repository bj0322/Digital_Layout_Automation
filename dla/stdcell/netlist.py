"""Static CMOS cells: catalogue, transistor networks, switch-level check.

A cell is one or more static CMOS *stages*. A stage is ``(output, pull_down)`` where the
pull-down network is a series/parallel expression over input (or earlier stage) nets:

    "A"                    one transistor
    ("s", e1, e2, ...)     series      (AND in the pull-down)
    ("p", e1, e2, ...)     parallel    (OR in the pull-down)

The pull-up network is the dual (series <-> parallel). ``fingers`` puts that many
parallel devices for every transistor of a stage (drive strength).
"""
import itertools

# name: (inputs, stages, reference function, drive strengths)
# a stage is (out, pull_down) ; the last stage drives Y and gets the drive's fingers
CATALOG = {
    "inv":   ("A", [("Y", "A")], lambda v: not v["A"], (1, 2)),
    "nand2": ("AB", [("Y", ("s", "A", "B"))], lambda v: not (v["A"] and v["B"]), (1,)),
    "nand3": ("ABC", [("Y", ("s", "A", "B", "C"))], lambda v: not (v["A"] and v["B"] and v["C"]), (1,)),
    "nor2":  ("AB", [("Y", ("p", "A", "B"))], lambda v: not (v["A"] or v["B"]), (1,)),
    "nor3":  ("ABC", [("Y", ("p", "A", "B", "C"))], lambda v: not (v["A"] or v["B"] or v["C"]), (1,)),
    "and2":  ("AB", [("n0", ("s", "A", "B")), ("Y", "n0")], lambda v: v["A"] and v["B"], (1,)),
    "aoi21": (("A1", "A2", "B1"), [("Y", ("p", ("s", "A1", "A2"), "B1"))],
              lambda v: not ((v["A1"] and v["A2"]) or v["B1"]), (1,)),
}

FILL_WIDTHS = (1, 2, 4, 8)


class Transistor:
    __slots__ = ("name", "kind", "g", "a", "b", "w")

    def __init__(self, name, kind, g, a, b, w):
        self.name, self.kind, self.g, self.a, self.b, self.w = name, kind, g, a, b, w

    def __repr__(self):
        return "%s(%s %s %s-%s)" % (self.name, self.kind, self.g, self.a, self.b)


class CellNetlist:
    def __init__(self, name, base, inputs, stages, func, drive, vdd="VDD", vss="VSS", wn=1.0, wp=1.0):
        self.name, self.base, self.drive = name, base, drive
        self.inputs = list(inputs)
        self.output = "Y"
        self.func = func
        self.vdd, self.vss = vdd, vss
        self.devices = []
        self._k = {"n": 0, "p": 0, "x": 0}
        for si, (out, pdn) in enumerate(stages):
            nf = drive if si == len(stages) - 1 else 1
            self._build("n", pdn, out, vss, nf, wn)
            self._build("p", _dual(pdn), vdd, out, nf, wp)
        self.internal = sorted({n for d in self.devices for n in (d.a, d.b, d.g)}
                               - set(self.inputs) - {self.output, vdd, vss})

    def _node(self):
        self._k["x"] += 1
        return "x%d" % self._k["x"]

    def _build(self, kind, e, top, bot, nf, w):
        if isinstance(e, str):
            for _ in range(nf):
                i = self._k[kind]
                self._k[kind] += 1
                self.devices.append(Transistor("M%s%d" % (kind.upper(), i), kind, e, top, bot, w))
            return
        op, items = e[0], e[1:]
        if op == "p":
            for it in items:
                self._build(kind, it, top, bot, nf, w)
        else:
            nodes = [top] + [self._node() for _ in items[:-1]] + [bot]
            for it, a, b in zip(items, nodes, nodes[1:]):
                self._build(kind, it, a, b, nf, w)

    @property
    def pins(self):
        return self.inputs + [self.output, self.vdd, self.vss]

    def spice(self, nmos, pmos, l, units="u"):
        def fmt(v):
            return ("%gu" % v) if units == "u" else ("%ge-6" % v)
        lines = [".SUBCKT %s %s" % (self.name, " ".join(self.pins)),
                 "*.PININFO %s" % " ".join(["%s:I" % p for p in self.inputs] + ["%s:O" % self.output,
                                                                                    "%s:B" % self.vdd, "%s:B" % self.vss])]
        for d in self.devices:
            model, bulk = (nmos, self.vss) if d.kind == "n" else (pmos, self.vdd)
            lines.append("%s %s %s %s %s %s w=%s l=%s" % (d.name, d.a, d.g, d.b, bulk, model, fmt(d.w), fmt(l)))
        lines.append(".ENDS %s" % self.name)
        return "\n".join(lines) + "\n"

    def verilog_expr(self):
        return _vexpr(self)


def _dual(e):
    if isinstance(e, str):
        return e
    return ("p" if e[0] == "s" else "s",) + tuple(_dual(x) for x in e[1:])


def _vexpr(cell):
    """Verilog expression of Y from the stage expressions (kept for the model file)."""
    _, stages, _, _ = CATALOG[cell.base]
    env = {}

    def ex(e):
        if isinstance(e, str):
            return env.get(e, e)
        j = " & " if e[0] == "s" else " | "
        return "(" + j.join(ex(x) for x in e[1:]) + ")"

    for out, pdn in stages:
        env[out] = "~%s" % ex(pdn)
    return env["Y"]


def make_cell(base, drive=1, prefix="", vdd="VDD", vss="VSS", wn=1.0, wp=1.0):
    inputs, stages, func, drives = CATALOG[base]
    inputs = list(inputs)
    return CellNetlist("%s%s_%d" % (prefix, base, drive), base, inputs, stages, func, drive, vdd, vss, wn, wp)


# ----------------------------------------------------------------------------- switch level
def switch_eval(devices, inputs, vdd="VDD", vss="VSS", max_iter=20):
    """Node values (1 / 0 / None = floating, 'X' = fight) of a transistor netlist."""
    val = dict(inputs)
    val[vdd], val[vss] = 1, 0
    for _ in range(max_iter):
        parent = {}

        def find(x):
            while parent.get(x, x) != x:
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        nodes = {vdd, vss}
        for d in devices:
            nodes |= {d.a, d.b}
            gv = val.get(d.g)
            if gv in (0, 1) and (gv == 1) == (d.kind == "n"):
                union(d.a, d.b)
        new = dict(inputs)
        new[vdd], new[vss] = 1, 0
        hi, lo = find(vdd), find(vss)
        for n in nodes:
            if n in inputs or n in (vdd, vss):
                continue
            r = find(n)
            new[n] = "X" if (r == hi and r == lo) else 1 if r == hi else 0 if r == lo else None
        if hi == lo:
            new["__fight__"] = 1
        if new == val:
            break
        val = new
    return val


def check_function(cell):
    """Exhaustive switch-level simulation against the reference function. Returns #errors."""
    errors = 0
    for bits in itertools.product((0, 1), repeat=len(cell.inputs)):
        v = dict(zip(cell.inputs, bits))
        out = switch_eval(cell.devices, v, cell.vdd, cell.vss)
        if out.get("__fight__") or out.get(cell.output) != int(bool(cell.func(v))):
            errors += 1
    return errors
