"""Design database: a flat netlist with placement and routing, the hub of the RTL-to-GDS flow.

    from dla.db import Design, read_def, write_def, read_verilog, write_verilog
    d = read_verilog("synth.v")              # netlist only
    d = read_def("place.def")                # netlist + rows, placement, routing
    d.insts["u1"].x, d.insts["u1"].orient    # um, DEF orientation (N, FS, ...)
    d.nets["n1"].conns                       # [(inst, pin)], ("PIN", port) for ports
    d.inst_pins("u1")                        # {pin: net}
    write_def(d, "out.def")

Names are bit level: bus bit 3 of `a` is the net/port "a[3]"; a Verilog escaped
identifier `\\a[3] ` becomes "a\\[3\\]" (brackets escaped, as in DEF).
Coordinates are um (r6-rounded floats); DEF database units only exist in the files.
Constant connections use the pseudo-nets "1'b0", "1'b1" (and "1'bx", "1'bz").
"""
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

from .geom import placed_size, r6, xform_rect

CONSTS = ("1'b0", "1'b1", "1'bx", "1'bz")
PIN = "PIN"                              # pseudo instance name of a top-level port in Net.conns


@dataclass
class Inst:
    name: str
    master: str
    x: float = 0.0
    y: float = 0.0
    orient: str = "N"
    status: str = "UNPLACED"             # UNPLACED PLACED FIXED COVER
    source: Optional[str] = None


@dataclass
class Port:
    name: str                            # bit-level port name
    direction: str = "INPUT"             # INPUT OUTPUT INOUT FEEDTHRU
    net: Optional[str] = None            # net it connects to (defaults to its own name)
    use: Optional[str] = None            # SIGNAL POWER GROUND CLOCK ...
    special: bool = False
    shapes: List[Tuple[str, Tuple[float, float, float, float]]] = field(default_factory=list)
    status: Optional[str] = None         # PLACED FIXED COVER (None: unplaced)
    x: float = 0.0
    y: float = 0.0
    orient: str = "N"


@dataclass
class Path:
    """One DEF routing statement (ROUTED/FIXED/COVER/NOSHIELD layer ... or NEW layer ...).

    elems: ("pt", x, y, ext|None) | ("via", name, orient|None) | ("rect", dx1, dy1, dx2, dy2)
           | ("virtual", x, y); '*' coordinates are already resolved.
    width is only used by special nets.
    """
    status: str
    layer: str
    width: Optional[float] = None
    shape: Optional[str] = None
    style: Optional[int] = None
    elems: List[tuple] = field(default_factory=list)


@dataclass
class Net:
    name: str
    conns: List[Tuple[str, str]] = field(default_factory=list)   # (inst | "PIN" | "*", pin)
    use: Optional[str] = None
    source: Optional[str] = None
    weight: Optional[int] = None
    wiring: List[Path] = field(default_factory=list)


@dataclass
class Row:
    name: str
    site: str
    x: float
    y: float
    orient: str = "N"
    nx: int = 1
    ny: int = 1
    sx: float = 0.0
    sy: float = 0.0


@dataclass
class Tracks:
    axis: str                            # "X": vertical tracks at x = start + k*step
    start: float
    num: int
    step: float
    layers: List[str] = field(default_factory=list)


@dataclass
class GCellGrid:
    axis: str
    start: float
    num: int
    step: float


@dataclass
class DefVia:
    """DEF VIAS entry: either fixed shapes or via-rule parameters (kept in file order)."""
    name: str
    rects: List[Tuple[str, Tuple[float, float, float, float]]] = field(default_factory=list)
    rule: Optional[str] = None
    params: List[Tuple[str, list]] = field(default_factory=list)   # (+KEY, values) after VIARULE


@dataclass
class Blockage:
    kind: str                            # PLACEMENT / LAYER
    layer: Optional[str] = None
    rects: List[Tuple[float, float, float, float]] = field(default_factory=list)
    soft: bool = False
    partial: Optional[float] = None
    component: Optional[str] = None


class Design:
    def __init__(self, name="top", dbu=1000):
        self.name = name
        self.dbu = dbu
        self.version = "5.8"
        self.divider = "/"
        self.busbit = "[]"
        self.die: Optional[list] = None              # [(x, y), ...] 2 points = rectangle
        self.insts: Dict[str, Inst] = {}
        self.ports: Dict[str, Port] = {}
        self.nets: Dict[str, Net] = {}
        self.specialnets: Dict[str, Net] = {}
        self.rows: List[Row] = []
        self.tracks: List[Tracks] = []
        self.gcells: List[GCellGrid] = []
        self.vias: Dict[str, DefVia] = {}
        self.blockages: List[Blockage] = []
        self.buses: Dict[str, Tuple[int, int]] = {}   # Verilog bus ranges, for writing
        self.propdefs: List[List[str]] = []           # DEF PROPERTYDEFINITIONS, verbatim

    # ------------------------------------------------------------------ editing
    def add_inst(self, name, master, **kw):
        if name in self.insts:
            raise KeyError("duplicate instance %s" % name)
        self.insts[name] = Inst(name, master, **kw)
        return self.insts[name]

    def add_net(self, name, special=False):
        nets = self.specialnets if special else self.nets
        if name not in nets:
            nets[name] = Net(name)
        return nets[name]

    def add_port(self, name, direction="INPUT", net=None, **kw):
        if name in self.ports:
            raise KeyError("duplicate port %s" % name)
        p = self.ports[name] = Port(name, direction, net or name, **kw)
        if not p.special:
            self.add_net(p.net).conns.append((PIN, name))
        return p

    def rename_net(self, old, new):
        if new in self.nets:
            raise KeyError("net %s exists" % new)
        n = self.nets.pop(old)
        n.name = new
        self.nets[new] = n
        for p in self.ports.values():
            if p.net == old:
                p.net = new

    def connect(self, inst, pin, net):
        self.add_net(net).conns.append((inst, pin))

    # ------------------------------------------------------------------ queries
    def inst_pins(self, inst=None):
        """{pin: net} of one instance, or {inst: {pin: net}} for all."""
        out = {}
        for n in self.nets.values():
            for i, p in n.conns:
                if i != PIN and (inst is None or i == inst):
                    out.setdefault(i, {})[p] = n.name
        return out.get(inst, {}) if inst is not None else out

    def inst_bbox(self, name, lib):
        """Placed bounding box of an instance (um)."""
        i = self.insts[name]
        m = lib.macros[i.master]
        w, h = placed_size(i.orient, m.w, m.h)
        return (i.x, i.y, r6(i.x + w), r6(i.y + h))

    def pin_shapes(self, inst, pin, lib):
        """[(layer, rect)] of a master pin in design coordinates."""
        i = self.insts[inst]
        m = lib.macros[i.master]
        return [(l, xform_rect(r, i.orient, (i.x, i.y), m.w, m.h)) for l, r in
                ((l, r) for pt in m.pins[pin].ports for l, r in pt.rects)]

    def stats(self):
        return {"insts": len(self.insts), "nets": len(self.nets), "ports": len(self.ports),
                "specialnets": len(self.specialnets), "rows": len(self.rows)}

    # ------------------------------------------------------------------ comparison
    def canonical(self):
        """Plain data for equality checks; connection order inside a net does not matter."""
        d = {k: v for k, v in self.__dict__.items()}
        for key in ("insts", "ports", "vias"):
            d[key] = {k: asdict(v) for k, v in d[key].items()}
        for key in ("nets", "specialnets"):
            d[key] = {k: dict(asdict(v), conns=sorted(v.conns)) for k, v in d[key].items()}
        for key in ("rows", "tracks", "gcells", "blockages"):
            d[key] = [asdict(v) for v in d[key]]
        return d

    def __eq__(self, other):
        return isinstance(other, Design) and self.canonical() == other.canonical()

    def __repr__(self):
        return "Design(%s, %s)" % (self.name, self.stats())
