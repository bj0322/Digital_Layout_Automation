"""DEF read / write (subset of DEF 5.8 used by flat std-cell designs).

    d = read_def("in.def")          # -> Design
    write_def(d, "out.def")

Sections: VERSION, DIVIDERCHAR, BUSBITCHARS, DESIGN, TECHNOLOGY, UNITS, PROPERTYDEFINITIONS
(kept verbatim), DIEAREA, ROW, TRACKS, GCELLGRID, VIAS, COMPONENTS, PINS, BLOCKAGES,
SPECIALNETS, NETS. Any other section or statement option raises ParseError with the line
number: dropping placement or routing silently would be worse than stopping.
"""
from .design import CONSTS, PIN, Blockage, DefVia, Design, GCellGrid, Net, Path, Port, Row, Tracks
from .geom import ORIENTS, from_dbu, to_dbu
from .lex import ParseError, Tokens

_SECTIONS = ("VIAS", "COMPONENTS", "PINS", "BLOCKAGES", "SPECIALNETS", "NETS")


# ============================================================================= read
def read_def(path):
    with open(path, errors="replace") as f:
        return parse_def(f.read(), path)


def parse_def(text, fname="<string>"):
    return _DefReader(Tokens(text, fname)).run()


class _DefReader:
    def __init__(self, tk):
        self.tk = tk
        self.d = Design()

    def u(self):
        return from_dbu(self.tk.num(), self.d.dbu)

    def pt(self, prev=None):
        """( x y [ext] ) with '*' = previous coordinate -> (x, y, ext)."""
        tk = self.tk
        tk.expect("(")
        vals = []
        while tk.peek() != ")":
            t = tk.next()
            if t == "*":
                if prev is None:
                    raise tk.error("'*' without a previous point")
                vals.append(prev[len(vals)])
            else:
                try:
                    vals.append(from_dbu(float(t), self.d.dbu))
                except ValueError:
                    raise tk.error("expected a coordinate, got %r" % t)
        tk.next()
        if len(vals) not in (2, 3):
            raise tk.error("a point needs 2 or 3 values")
        return (vals[0], vals[1], vals[2] if len(vals) == 3 else None)

    def rect2(self):
        a, b = self.pt(), self.pt()
        return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))

    def run(self):
        tk, d = self.tk, self.d
        while not tk.eof():
            k = tk.next().upper()
            if k == "END":
                tk.expect("DESIGN")
                return d
            if k == "VERSION":
                d.version = tk.next()
                tk.expect(";")
            elif k == "DIVIDERCHAR":
                d.divider = tk.next().strip('"')
                tk.expect(";")
            elif k == "BUSBITCHARS":
                d.busbit = tk.next().strip('"')
                tk.expect(";")
            elif k == "DESIGN":
                d.name = tk.next()
                tk.expect(";")
            elif k in ("TECHNOLOGY", "NAMESCASESENSITIVE"):
                tk.statement()
            elif k == "UNITS":
                tk.expect("DISTANCE")
                tk.expect("MICRONS")
                d.dbu = tk.int()
                tk.expect(";")
            elif k == "PROPERTYDEFINITIONS":
                while tk.peek().upper() != "END":
                    d.propdefs.append(tk.statement())
                tk.next()
                tk.expect("PROPERTYDEFINITIONS")
            elif k == "DIEAREA":
                pts = []
                while tk.peek() == "(":
                    pts.append(self.pt()[:2])
                tk.expect(";")
                d.die = pts
            elif k == "ROW":
                self.row()
            elif k == "TRACKS":
                self.tracks()
            elif k == "GCELLGRID":
                ax = tk.expect("X", "Y")
                start = self.u()
                tk.expect("DO")
                n = tk.int()
                tk.expect("STEP")
                d.gcells.append(GCellGrid(ax.upper(), start, n, self.u()))
                tk.expect(";")
            elif k in _SECTIONS:
                count = tk.int()
                tk.expect(";")
                items = getattr(self, "s_" + k.lower())()
                if items != count:
                    raise tk.error("%s: header says %d items, found %d" % (k, count, items))
            else:
                raise tk.error("unsupported DEF statement or section %s" % k)
        raise tk.error("missing END DESIGN")

    def _items(self, section, fn):
        """'- ...' items up to END section; returns their number."""
        tk = self.tk
        n = 0
        while True:
            t = tk.next()
            if t.upper() == "END":
                tk.expect(section)
                return n
            if t != "-":
                raise tk.error("expected '-' or END %s, got %r" % (section, t))
            fn()
            n += 1

    # ------------------------------------------------------------------ simple statements
    def row(self):
        tk = self.tk
        name, site = tk.next(), tk.next()
        x, y = self.u(), self.u()
        orient = self.orient()
        r = Row(name, site, x, y, orient)
        if tk.peek().upper() == "DO":
            tk.next()
            r.nx = tk.int()
            tk.expect("BY")
            r.ny = tk.int()
            if tk.peek().upper() == "STEP":
                tk.next()
                r.sx, r.sy = self.u(), self.u()
        if tk.peek() != ";":
            raise tk.error("unsupported ROW option %s" % tk.peek())
        tk.next()
        self.d.rows.append(r)

    def tracks(self):
        tk = self.tk
        ax = tk.expect("X", "Y").upper()
        start = self.u()
        tk.expect("DO")
        n = tk.int()
        tk.expect("STEP")
        step = self.u()
        layers = []
        if tk.peek().upper() == "LAYER":
            tk.next()
            while tk.peek() != ";":
                layers.append(tk.next())
        tk.expect(";")
        self.d.tracks.append(Tracks(ax, start, n, step, layers))

    def orient(self):
        o = self.tk.next().upper()
        if o not in ORIENTS:
            raise self.tk.error("bad orientation %s" % o)
        return o

    # ------------------------------------------------------------------ sections
    def s_vias(self):
        def one():
            tk = self.tk
            v = DefVia(tk.next())
            while tk.peek() != ";":
                tk.expect("+")
                k = tk.next().upper()
                if k == "RECT":
                    layer = tk.next()
                    v.rects.append((layer, self.rect2()))
                elif k == "VIARULE":
                    v.rule = tk.next()
                elif k in ("CUTSIZE", "CUTSPACING", "ORIGIN"):
                    v.params.append((k, [self.u(), self.u()]))
                elif k in ("ENCLOSURE", "OFFSET"):
                    v.params.append((k, [self.u() for _ in range(4)]))
                elif k == "LAYERS":
                    v.params.append((k, [tk.next() for _ in range(3)]))
                elif k == "ROWCOL":
                    v.params.append((k, [tk.int(), tk.int()]))
                elif k == "PATTERN":
                    v.params.append((k, [tk.next()]))
                else:
                    raise tk.error("unsupported VIAS option %s" % k)
            tk.next()
            self.d.vias[v.name] = v
        return self._items("VIAS", one)

    def s_components(self):
        def one():
            tk, d = self.tk, self.d
            name, master = tk.next(), tk.next()
            if name in d.insts:
                raise tk.error("duplicate component %s" % name)
            inst = d.add_inst(name, master)
            while tk.peek() != ";":
                tk.expect("+")
                k = tk.next().upper()
                if k in ("PLACED", "FIXED", "COVER"):
                    x, y, _ = self.pt()
                    inst.x, inst.y, inst.orient, inst.status = x, y, self.orient(), k
                elif k == "UNPLACED":
                    inst.status = k
                elif k == "SOURCE":
                    inst.source = tk.next().upper()
                else:
                    raise tk.error("unsupported COMPONENTS option %s" % k)
            tk.next()
        return self._items("COMPONENTS", one)

    def s_pins(self):
        def one():
            tk, d = self.tk, self.d
            p = Port(tk.next())
            if p.name in d.ports:
                raise tk.error("duplicate pin %s" % p.name)
            while tk.peek() != ";":
                tk.expect("+")
                k = tk.next().upper()
                if k == "NET":
                    p.net = tk.next()
                elif k == "SPECIAL":
                    p.special = True
                elif k == "DIRECTION":
                    p.direction = tk.next().upper()
                elif k == "USE":
                    p.use = tk.next().upper()
                elif k == "LAYER":
                    layer = tk.next()
                    p.shapes.append((layer, self.rect2()))
                elif k in ("PLACED", "FIXED", "COVER"):
                    x, y, _ = self.pt()
                    p.x, p.y, p.orient, p.status = x, y, self.orient(), k
                else:
                    raise tk.error("unsupported PINS option %s" % k)
            tk.next()
            d.ports[p.name] = p
        return self._items("PINS", one)

    def s_blockages(self):
        def one():
            tk = self.tk
            kind = tk.expect("PLACEMENT", "LAYER").upper()
            b = Blockage(kind, layer=tk.next() if kind == "LAYER" else None)
            while tk.peek() != ";":
                t = tk.next().upper()
                if t == "+":
                    k = tk.next().upper()
                    if k == "SOFT":
                        b.soft = True
                    elif k == "PARTIAL":
                        b.partial = tk.num()
                    elif k == "COMPONENT":
                        b.component = tk.next()
                    else:
                        raise tk.error("unsupported BLOCKAGES option %s" % k)
                elif t == "RECT":
                    b.rects.append(self.rect2())
                else:
                    raise tk.error("unsupported BLOCKAGES statement %s" % t)
            tk.next()
            self.d.blockages.append(b)
        return self._items("BLOCKAGES", one)

    def s_specialnets(self):
        return self._items("SPECIALNETS", lambda: self.net(special=True))

    def s_nets(self):
        return self._items("NETS", lambda: self.net(special=False))

    def net(self, special):
        tk, d = self.tk, self.d
        name = tk.next()
        nets = d.specialnets if special else d.nets
        if name in nets:
            raise tk.error("duplicate net %s" % name)
        n = nets[name] = Net(name)
        while tk.peek() == "(":
            tk.next()
            inst, pin = tk.next(), tk.next()
            if tk.peek() != ")":
                raise tk.error("unsupported connection option %s" % tk.peek())
            tk.next()
            n.conns.append((PIN if inst == PIN else inst, pin))
        while tk.peek() != ";":
            tk.expect("+")
            k = tk.next().upper()
            if k in ("ROUTED", "FIXED", "COVER", "NOSHIELD"):
                n.wiring += self.wiring(k, special)
            elif k == "USE":
                n.use = tk.next().upper()
            elif k == "SOURCE":
                n.source = tk.next().upper()
            elif k == "WEIGHT":
                n.weight = tk.int()
            else:
                raise tk.error("unsupported %sNETS option %s" % ("SPECIAL" if special else "", k))
        tk.next()

    def wiring(self, status, special):
        """layer [width] [+ SHAPE s] [+ STYLE n] elems { NEW layer ... }"""
        tk = self.tk
        out = []
        while True:
            p = Path(status, tk.next())
            if special:
                p.width = self.u()
            while tk.peek() == "+" and tk.peek(1).upper() in ("SHAPE", "STYLE"):
                tk.next()
                if tk.next().upper() == "SHAPE":
                    p.shape = tk.next().upper()
                else:
                    p.style = tk.int()
            prev = None
            while True:
                t = tk.peek()
                if t == "(":
                    x, y, ext = self.pt(prev)
                    p.elems.append(("pt", x, y, ext))
                    prev = (x, y, ext)
                elif t is None or t in ("+", ";") or t.upper() == "NEW":
                    break
                elif t.upper() == "RECT":
                    tk.next()
                    tk.expect("(")
                    v = [self.u() for _ in range(4)]
                    tk.expect(")")
                    p.elems.append(("rect",) + tuple(v))
                elif t.upper() == "VIRTUAL":
                    tk.next()
                    x, y, _ = self.pt(prev)
                    p.elems.append(("virtual", x, y))
                    prev = (x, y, None)
                elif t.upper() in ("MASK", "TAPER", "TAPERRULE"):
                    raise tk.error("unsupported routing element %s" % t)
                else:
                    tk.next()
                    o = tk.next().upper() if (tk.peek() or "").upper() in ORIENTS else None
                    p.elems.append(("via", t, o))
            out.append(p)
            if (tk.peek() or "").upper() != "NEW":
                return out
            tk.next()


# ============================================================================= write
def write_def(d, path):
    text = format_def(d)
    with open(path, "w") as f:
        f.write(text)
    return path


def format_def(d):
    dbu = d.dbu

    def u(v, what="coordinate"):
        return str(to_dbu(v, dbu, what))

    def pt(x, y, ext=None):
        return "( %s %s%s )" % (u(x), u(y), "" if ext is None else " " + u(ext))

    def rect(r):
        return "%s %s" % (pt(r[0], r[1]), pt(r[2], r[3]))

    L = ["VERSION %s ;" % d.version, 'DIVIDERCHAR "%s" ;' % d.divider, 'BUSBITCHARS "%s" ;' % d.busbit,
         "DESIGN %s ;" % d.name, "UNITS DISTANCE MICRONS %d ;" % dbu]
    if d.propdefs:
        L.append("PROPERTYDEFINITIONS")
        L += ["  %s ;" % " ".join(s) for s in d.propdefs]
        L.append("END PROPERTYDEFINITIONS")
    if d.die:
        L.append("DIEAREA %s ;" % " ".join(pt(x, y) for x, y in d.die))
    for r in d.rows:
        s = "ROW %s %s %s %s %s" % (r.name, r.site, u(r.x), u(r.y), r.orient)
        s += " DO %d BY %d STEP %s %s" % (r.nx, r.ny, u(r.sx), u(r.sy))
        L.append(s + " ;")
    for t in d.tracks:
        L.append("TRACKS %s %s DO %d STEP %s%s ;" % (t.axis, u(t.start), t.num, u(t.step),
                                                     " LAYER " + " ".join(t.layers) if t.layers else ""))
    for g in d.gcells:
        L.append("GCELLGRID %s %s DO %d STEP %s ;" % (g.axis, u(g.start), g.num, u(g.step)))

    if d.vias:
        L.append("VIAS %d ;" % len(d.vias))
        for v in d.vias.values():
            s = "  - %s" % v.name
            for layer, r in v.rects:
                s += "\n    + RECT %s %s" % (layer, rect(r))
            if v.rule:
                s += "\n    + VIARULE %s" % v.rule
            for k, vals in v.params:
                if k in ("LAYERS", "PATTERN"):
                    s += " + %s %s" % (k, " ".join(vals))
                elif k == "ROWCOL":
                    s += " + %s %d %d" % (k, vals[0], vals[1])
                else:
                    s += " + %s %s" % (k, " ".join(u(x) for x in vals))
            L.append(s + " ;")
        L.append("END VIAS")

    if d.insts:
        L.append("COMPONENTS %d ;" % len(d.insts))
        for i in d.insts.values():
            s = "  - %s %s" % (i.name, i.master)
            if i.source:
                s += " + SOURCE %s" % i.source
            if i.status == "UNPLACED":
                s += " + UNPLACED"
            else:
                s += " + %s %s %s" % (i.status, pt(i.x, i.y), i.orient)
            L.append(s + " ;")
        L.append("END COMPONENTS")

    if d.ports:
        L.append("PINS %d ;" % len(d.ports))
        for p in d.ports.values():
            if p.net in CONSTS:
                raise ValueError("port %s is tied to a constant: insert tie cells before writing DEF" % p.name)
            s = "  - %s + NET %s" % (p.name, p.net or p.name)
            if p.special:
                s += " + SPECIAL"
            s += " + DIRECTION %s" % p.direction
            if p.use:
                s += " + USE %s" % p.use
            for layer, r in p.shapes:
                s += "\n    + LAYER %s %s" % (layer, rect(r))
            if p.status:
                s += "\n    + %s %s %s" % (p.status, pt(p.x, p.y), p.orient)
            L.append(s + " ;")
        L.append("END PINS")

    if d.blockages:
        L.append("BLOCKAGES %d ;" % len(d.blockages))
        for b in d.blockages:
            s = "  - %s" % ("PLACEMENT" if b.kind == "PLACEMENT" else "LAYER " + b.layer)
            if b.component:
                s += " + COMPONENT %s" % b.component
            if b.soft:
                s += " + SOFT"
            if b.partial is not None:
                s += " + PARTIAL %g" % b.partial
            s += "".join("\n    RECT %s" % rect(r) for r in b.rects)
            L.append(s + " ;")
        L.append("END BLOCKAGES")

    for title, nets, special in (("SPECIALNETS", d.specialnets, True), ("NETS", d.nets, False)):
        if not nets and special:
            continue
        L.append("%s %d ;" % (title, len(nets)))
        for n in nets.values():
            if n.name in CONSTS:
                raise ValueError("constant net %s: insert tie cells before writing DEF" % n.name)
            s = "  - %s" % n.name
            line = s
            parts = []
            for i, p in n.conns:
                c = "( %s %s )" % (i, p)
                if len(line) + len(c) > 100:
                    parts.append(line)
                    line = "   "
                line += " " + c
            parts.append(line)
            s = "\n".join(parts)
            if n.use:
                s += " + USE %s" % n.use
            if n.source:
                s += " + SOURCE %s" % n.source
            if n.weight is not None:
                s += " + WEIGHT %d" % n.weight
            prev_status = None
            for w in n.wiring:
                head = "NEW" if w.status == prev_status else "+ %s" % w.status
                prev_status = w.status
                seg = "\n    %s %s" % (head, w.layer)
                if special:
                    seg += " " + u(w.width, "width")
                if w.shape:
                    seg += " + SHAPE %s" % w.shape
                if w.style is not None:
                    seg += " + STYLE %d" % w.style
                for e in w.elems:
                    if e[0] == "pt":
                        seg += " " + pt(e[1], e[2], e[3])
                    elif e[0] == "via":
                        seg += " %s%s" % (e[1], " " + e[2] if e[2] else "")
                    elif e[0] == "rect":
                        seg += " RECT ( %s )" % " ".join(u(v) for v in e[1:])
                    elif e[0] == "virtual":
                        seg += " VIRTUAL " + pt(e[1], e[2])
                s += seg
            L.append(s + " ;")
        L.append("END %s" % title)
    L.append("END DESIGN")
    return "\n".join(L) + "\n"
