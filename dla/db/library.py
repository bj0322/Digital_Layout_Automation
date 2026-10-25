"""Cell library: technology and macros read from LEF (Liberty timing joins in M1).

    from dla.db import Library
    lib = Library.read_lef(["sg13g2_tech.lef", "sg13g2_stdcell.lef"])
    lib.routing_layers()            # ['Metal1', 'Metal2', ...] bottom-up
    m = lib.macros["sg13g2_inv_1"]
    m.w, m.h, m.pins["Y"].direction, m.pins["Y"].rects()

    lib = Library.from_tech(tech)   # dla.tech.Tech: files.tech_lef (optional) + files.lef

All distances in um. Rects are (x1, y1, x2, y2) in the macro frame with the LEF ORIGIN
already applied, so (0, 0) is the macro's lower-left corner.
LEF statements the flow does not use yet are kept verbatim in ``extra`` (layers,
pins) or skipped (unknown top-level blocks); a malformed file raises ParseError with
file name and line.
"""
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .geom import norm_rect, r6
from .lex import ParseError, Tokens

Rect = Tuple[float, float, float, float]


@dataclass
class LayerDef:
    name: str
    type: str = ""                       # ROUTING CUT MASTERSLICE OVERLAP IMPLANT ...
    direction: Optional[str] = None      # HORIZONTAL / VERTICAL (routing)
    pitch: Optional[Tuple[float, float]] = None      # (x, y); a single LEF value is used for both
    offset: Optional[Tuple[float, float]] = None
    width: float = 0.0
    min_width: float = 0.0
    max_width: float = 0.0
    spacing: float = 0.0                 # smallest plain SPACING
    spacing_table: Optional[dict] = None  # {"prl": [..], "rows": [(width, [space per prl])]}
    area: float = 0.0
    enclosure: Dict[str, Tuple[float, float]] = field(default_factory=dict)  # "" / BELOW / ABOVE
    rpersq: Optional[float] = None       # ohm / square (routing), ohm per cut (CUT: RESISTANCE)
    cpersqdist: Optional[float] = None   # pF / um^2
    edgecap: Optional[float] = None      # pF / um
    thickness: Optional[float] = None
    height: Optional[float] = None
    extra: List[List[str]] = field(default_factory=list)

    def min_spacing(self, width=0.0, prl=0.0):
        """Spacing for a wire of `width` running in parallel for `prl` (spacing table aware)."""
        t = self.spacing_table
        if not t:
            return self.spacing
        s = self.spacing
        for w, row in t["rows"]:
            if width + 1e-9 >= w:
                for p, v in zip(t["prl"], row):
                    if prl + 1e-9 >= p:
                        s = v
        return max(s, self.spacing) if self.spacing else s


@dataclass
class ViaDef:
    name: str
    default: bool = False
    resistance: Optional[float] = None
    rects: List[Tuple[str, Rect]] = field(default_factory=list)    # (layer, rect), centred on (0, 0)

    def layers(self):
        out = []
        for l, _ in self.rects:
            if l not in out:
                out.append(l)
        return out


@dataclass
class ViaRuleDef:
    name: str
    generate: bool = False
    default: bool = False
    layers: List[Tuple[str, List[List[str]]]] = field(default_factory=list)  # (layer, statements)
    vias: List[str] = field(default_factory=list)


@dataclass
class Site:
    name: str
    cls: str = ""
    symmetry: List[str] = field(default_factory=list)
    w: float = 0.0
    h: float = 0.0


@dataclass
class Port:
    """One PORT of a pin (or the OBS block): shapes on layers."""
    rects: List[Tuple[str, Rect]] = field(default_factory=list)
    polys: List[Tuple[str, List[Tuple[float, float]]]] = field(default_factory=list)
    vias: List[Tuple[str, float, float]] = field(default_factory=list)   # (via name, x, y)
    cls: Optional[str] = None


@dataclass
class MacroPin:
    name: str
    direction: str = "INPUT"
    use: str = "SIGNAL"
    shape: Optional[str] = None
    ports: List[Port] = field(default_factory=list)
    extra: List[List[str]] = field(default_factory=list)   # antenna data etc., verbatim

    def rects(self, layer=None):
        return [r for p in self.ports for l, r in p.rects if layer is None or l == layer]


@dataclass
class Macro:
    name: str
    cls: str = "CORE"
    subclass: Optional[str] = None
    foreign: Optional[str] = None
    origin: Tuple[float, float] = (0.0, 0.0)
    w: float = 0.0
    h: float = 0.0
    symmetry: List[str] = field(default_factory=list)
    site: Optional[str] = None
    pins: Dict[str, MacroPin] = field(default_factory=dict)
    obs: Port = field(default_factory=Port)

    def signal_pins(self):
        return [p for p in self.pins.values() if p.use not in ("POWER", "GROUND")]


class Library:
    def __init__(self):
        self.dbu = 1000
        self.mfg_grid = None
        self.busbit = "[]"
        self.divider = "/"
        self.layers: Dict[str, LayerDef] = {}
        self.vias: Dict[str, ViaDef] = {}
        self.viarules: Dict[str, ViaRuleDef] = {}
        self.sites: Dict[str, Site] = {}
        self.macros: Dict[str, Macro] = {}
        self.files: List[str] = []

    # ------------------------------------------------------------------ queries
    def layer_order(self):
        return list(self.layers)

    def routing_layers(self):
        return [n for n, l in self.layers.items() if l.type == "ROUTING"]

    def cut_layers(self):
        return [n for n, l in self.layers.items() if l.type == "CUT"]

    # ------------------------------------------------------------------ input
    @classmethod
    def read_lef(cls, paths):
        lib = cls()
        for p in [paths] if isinstance(paths, str) else paths:
            lib.add_lef(p)
        return lib

    @classmethod
    def from_tech(cls, tech):
        """Library of a dla.tech.Tech: its technology LEF(s) (optional) and cell LEF(s)."""
        return cls.read_lef(list(getattr(tech, "tech_lef", [])) + list(tech.lef))

    def add_lef(self, path):
        with open(path, errors="replace") as f:
            text = f.read()
        self.add_lef_text(text, path)
        self.files.append(os.path.abspath(path))

    def add_lef_text(self, text, fname="<string>"):
        _LefParser(self, Tokens(text, fname)).run()
        return self


def _upper(t):
    return t.upper() if t else t


class _LefParser:
    def __init__(self, lib, tk):
        self.lib, self.tk = lib, tk

    def run(self):
        tk, lib = self.tk, self.lib
        while not tk.eof():
            k = _upper(tk.next())
            if k == "END":
                nxt = tk.next()
                if _upper(nxt) == "LIBRARY":
                    return
                raise tk.error("unexpected END %s" % nxt)
            elif k == "UNITS":
                self.units()
            elif k == "MANUFACTURINGGRID":
                lib.mfg_grid = tk.num()
                tk.expect(";")
            elif k == "BUSBITCHARS":
                lib.busbit = tk.next().strip('"')
                tk.expect(";")
            elif k == "DIVIDERCHAR":
                lib.divider = tk.next().strip('"')
                tk.expect(";")
            elif k == "LAYER":
                self.layer(tk.next())
            elif k == "VIA":
                self.via(tk.next())
            elif k == "VIARULE":
                self.viarule(tk.next())
            elif k == "SITE":
                self.site(tk.next())
            elif k == "MACRO":
                self.macro(tk.next())
            elif k in ("PROPERTYDEFINITIONS", "SPACING", "NONDEFAULTRULE", "BEGINEXT",
                       "NOISETABLE", "CORRECTIONTABLE", "IRDROP", "ARRAY", "DENSITY"):
                name = None if k in ("PROPERTYDEFINITIONS", "SPACING", "IRDROP") else tk.next()
                self.skip_block(name or k)
            else:                         # VERSION, NAMESCASESENSITIVE, CLEARANCEMEASURE, ...
                tk.statement()

    def skip_block(self, name):
        tk = self.tk
        while True:
            t = tk.next()
            if _upper(t) == "END" and tk.peek() is not None and tk.peek().upper() == name.upper():
                tk.next()
                return

    def units(self):
        tk = self.tk
        while True:
            s = tk.statement() if _upper(tk.peek()) != "END" else None
            if s is None:
                tk.next()
                tk.expect("UNITS")
                return
            if [x.upper() for x in s[:2]] == ["DATABASE", "MICRONS"]:
                self.lib.dbu = int(float(s[2]))

    def _body(self, name):
        """Statements of a named block up to END name: yields token lists."""
        tk = self.tk
        while True:
            if _upper(tk.peek()) == "END" and tk.peek(1) == name:
                tk.next()
                tk.next()
                return
            yield tk.statement()

    # ------------------------------------------------------------------ technology
    def layer(self, name):
        L = self.lib.layers.setdefault(name, LayerDef(name))
        for s in self._body(name):
            if not s:
                continue
            k = s[0].upper()
            try:
                if k == "TYPE":
                    L.type = s[1].upper()
                elif k == "DIRECTION":
                    L.direction = s[1].upper()
                elif k == "PITCH":
                    v = [float(x) for x in s[1:3]]
                    L.pitch = (v[0], v[-1])
                elif k == "OFFSET":
                    v = [float(x) for x in s[1:3]]
                    L.offset = (v[0], v[-1])
                elif k == "WIDTH" and len(s) == 2:
                    L.width = float(s[1])
                elif k == "MINWIDTH":
                    L.min_width = float(s[1])
                elif k == "MAXWIDTH":
                    L.max_width = float(s[1])
                elif k == "AREA":
                    L.area = float(s[1])
                elif k == "SPACING" and len(s) == 2:
                    v = float(s[1])
                    L.spacing = min(L.spacing, v) if L.spacing else v
                elif k == "SPACINGTABLE" and len(s) > 1 and s[1].upper() == "PARALLELRUNLENGTH":
                    L.spacing_table = _spacing_table(s)
                elif k == "ENCLOSURE":
                    side = s[1].upper() if s[1].upper() in ("BELOW", "ABOVE") else ""
                    v = [float(x) for x in s[2 if side else 1:][:2]]
                    L.enclosure[side] = (v[0], v[1])
                elif k == "RESISTANCE":
                    L.rpersq = float(s[2] if s[1].upper() == "RPERSQ" else s[1])
                elif k == "CAPACITANCE" and s[1].upper() == "CPERSQDIST":
                    L.cpersqdist = float(s[2])
                elif k == "EDGECAPACITANCE":
                    L.edgecap = float(s[1])
                elif k == "THICKNESS":
                    L.thickness = float(s[1])
                elif k == "HEIGHT":
                    L.height = float(s[1])
                else:
                    L.extra.append(s)
            except (IndexError, ValueError):
                raise self.tk.error("layer %s: malformed %s statement" % (name, k))

    def via(self, name):
        tk = self.tk
        v = ViaDef(name)
        while _upper(tk.peek()) in ("DEFAULT", "GENERATED"):
            v.default |= tk.next().upper() == "DEFAULT"
        layer = None
        for s in self._body(name):
            if not s:
                continue
            k = s[0].upper()
            if k == "RESISTANCE":
                v.resistance = float(s[1])
            elif k == "LAYER":
                layer = s[1]
            elif k == "RECT":
                v.rects.append((layer, _rect(s[1:], tk)))
        self.lib.vias[name] = v

    def viarule(self, name):
        tk = self.tk
        vr = ViaRuleDef(name)
        while _upper(tk.peek()) in ("GENERATE", "DEFAULT"):
            t = tk.next().upper()
            vr.generate |= t == "GENERATE"
            vr.default |= t == "DEFAULT"
        for s in self._body(name):
            if not s:
                continue
            k = s[0].upper()
            if k == "LAYER":
                vr.layers.append((s[1], []))
            elif k == "VIA":
                vr.vias.append(s[1])
            elif vr.layers:
                vr.layers[-1][1].append(s)
        self.lib.viarules[name] = vr

    def site(self, name):
        st = Site(name)
        for s in self._body(name):
            k = s[0].upper() if s else ""
            if k == "CLASS":
                st.cls = s[1].upper()
            elif k == "SYMMETRY":
                st.symmetry = [x.upper() for x in s[1:]]
            elif k == "SIZE":
                st.w, st.h = float(s[1]), float(s[3])
        self.lib.sites[name] = st

    # ------------------------------------------------------------------ macros
    def macro(self, name):
        tk = self.tk
        m = Macro(name)
        while True:
            k = _upper(tk.peek())
            if k == "END" and tk.peek(1) == name:
                tk.next()
                tk.next()
                break
            if k == "PIN":
                tk.next()
                p = self.pin(tk.next())
                m.pins[p.name] = p
            elif k == "OBS":
                tk.next()
                self.port_body(m.obs)
            elif k == "DENSITY":
                tk.next()
                self.skip_block_bare()
            else:
                s = tk.statement()
                if not s:
                    continue
                k = s[0].upper()
                if k == "CLASS":
                    m.cls = s[1].upper()
                    m.subclass = s[2].upper() if len(s) > 2 else None
                elif k == "FOREIGN":
                    m.foreign = s[1]
                elif k == "ORIGIN":
                    m.origin = (float(s[1]), float(s[2]))
                elif k == "SIZE":
                    m.w, m.h = float(s[1]), float(s[3])
                elif k == "SYMMETRY":
                    m.symmetry = [x.upper() for x in s[1:]]
                elif k == "SITE":
                    m.site = s[1]
        if m.origin != (0.0, 0.0):          # shapes are relative to the origin
            ox, oy = m.origin
            for port in [m.obs] + [pt for p in m.pins.values() for pt in p.ports]:
                port.rects = [(l, norm_rect((r[0] + ox, r[1] + oy, r[2] + ox, r[3] + oy))) for l, r in port.rects]
                port.polys = [(l, [(r6(x + ox), r6(y + oy)) for x, y in pts]) for l, pts in port.polys]
                port.vias = [(v, r6(x + ox), r6(y + oy)) for v, x, y in port.vias]
        self.lib.macros[name] = m

    def skip_block_bare(self):
        tk = self.tk
        while _upper(tk.next()) != "END":
            pass

    def pin(self, name):
        tk = self.tk
        p = MacroPin(name)
        while True:
            k = _upper(tk.peek())
            if k == "END" and tk.peek(1) == name:
                tk.next()
                tk.next()
                return p
            if k == "PORT":
                tk.next()
                pt = Port()
                self.port_body(pt)
                p.ports.append(pt)
                continue
            s = tk.statement()
            if not s:
                continue
            k = s[0].upper()
            if k == "DIRECTION":
                p.direction = " ".join(x.upper() for x in s[1:])
            elif k == "USE":
                p.use = s[1].upper()
            elif k == "SHAPE":
                p.shape = s[1].upper()
            else:
                p.extra.append(s)

    def port_body(self, port):
        """LAYER / RECT / POLYGON / VIA statements up to a bare END."""
        tk = self.tk
        layer = None
        while True:
            if _upper(tk.peek()) == "END":
                tk.next()
                return
            s = tk.statement()
            if not s:
                continue
            k = s[0].upper()
            args = [x for x in s[1:]]
            if args and args[0].upper() == "MASK":
                args = args[2:]
            if k == "LAYER":
                layer = s[1]
            elif k == "CLASS":
                port.cls = s[1].upper()
            elif k == "RECT":
                if args and args[0].upper() == "ITERATE":
                    raise tk.error("RECT ITERATE is not supported")
                port.rects.append((layer, _rect(args, tk)))
            elif k == "POLYGON":
                v = [float(x) for x in args]
                port.polys.append((layer, [(r6(v[i]), r6(v[i + 1])) for i in range(0, len(v) - 1, 2)]))
            elif k == "VIA":
                port.vias.append((args[2], r6(float(args[0])), r6(float(args[1]))))
            elif k == "PATH":
                raise tk.error("PATH geometry in a pin/OBS is not supported")


def _rect(args, tk):
    if len(args) < 4:
        raise tk.error("malformed RECT %s" % " ".join(args))
    try:
        return norm_rect(tuple(float(x) for x in args[:4]))
    except (ValueError, TypeError):
        raise tk.error("malformed RECT %s" % " ".join(args))


def _spacing_table(s):
    """SPACINGTABLE PARALLELRUNLENGTH p1 p2 .. WIDTH w s1 s2 .. WIDTH .."""
    toks = s[2:]
    i = 0
    prl = []
    while i < len(toks) and toks[i].upper() != "WIDTH":
        prl.append(float(toks[i]))
        i += 1
    rows = []
    while i < len(toks):
        if toks[i].upper() != "WIDTH":
            raise ParseError("malformed SPACINGTABLE")
        w = float(toks[i + 1])
        vals = [float(x) for x in toks[i + 2:i + 2 + len(prl)]]
        rows.append((w, vals))
        i += 2 + len(prl)
    return {"prl": prl, "rows": rows}
