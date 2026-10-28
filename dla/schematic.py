"""Dependency-light SVG circuit schematics built from SPICE/netlist data (not from layout geometry).

    svg = cell_svg(subckt, vdd="VDD", vss="VSS", title="inv_1", notes=["SPICE: not run"])

* ``cell_svg``      transistor-level schematic of one sub-circuit: channel-connected stages, a
                    pull-up network above and a pull-down network below each stage output
                    (series = stacked, parallel = side by side), VDD/VSS rails, net tags
* ``block_svg``     gate-level block diagram from instances and nets (levelised left to right,
                    orthogonal wires); big blocks are collapsed into counted groups
* ``array_svg``     SRAM array hierarchy (word lines, bit-line pairs, bit-cell instances)
* ``write_*``       file writers used by ``dla.spice_artifacts`` (layout PNGs are a different thing)

Output is deterministic: no timestamps, ids or random values; every collection is ordered.
"""
import os
import re
from xml.sax.saxutils import escape

DW, DH, AX = 170, 84, 105         # device cell: width, height, x of the S/D axis
PB = 16                           # parallel bus margin
STYLE = (
    "text{font-family:Consolas,Menlo,'DejaVu Sans Mono',monospace;font-size:10px;fill:#1c2430}"
    ".t{font-size:15px;font-weight:bold}.s{font-size:11px;fill:#4a5568}.k{font-size:9px;fill:#4a5568}"
    ".w{stroke:#1c2430;stroke-width:1.4;fill:none;stroke-linecap:round;stroke-linejoin:round}"
    ".d{fill:#1c2430}.sym{stroke:#1c2430;stroke-width:1.6;fill:#fff}.ch{stroke:#1c2430;stroke-width:2.4;fill:none}"
    ".vdd{stroke:#b4232a;stroke-width:2.2}.vss{stroke:#2563a8;stroke-width:2.2}"
    ".tag{fill:#eef2f7;stroke:#7a8798;stroke-width:1}.tin{fill:#e3f1e6;stroke:#2f7d43}.tout{fill:#fdf0d5;stroke:#b7791f}"
    ".box{fill:#f7f9fb;stroke:#9aa7b8;stroke-width:1}.ok{fill:#2f7d43;font-weight:bold}.bad{fill:#b4232a;font-weight:bold}"
    ".na{fill:#8a6d1d;font-weight:bold}"
)


def esc(v):
    return escape(str(v), {'"': "&quot;", "'": "&apos;"})


def num(v):
    s = ("%.2f" % v).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


class _G(object):
    """Tiny SVG element collector."""

    def __init__(self):
        self.out = []

    def line(self, x1, y1, x2, y2, cls="w"):
        self.out.append('<line class="%s" x1="%s" y1="%s" x2="%s" y2="%s"/>' % (cls, num(x1), num(y1), num(x2), num(y2)))

    def poly(self, pts, cls="w"):
        self.out.append('<polyline class="%s" points="%s"/>' % (cls, " ".join("%s,%s" % (num(x), num(y)) for x, y in pts)))

    def dot(self, x, y, r=2.4):
        self.out.append('<circle class="d" cx="%s" cy="%s" r="%s"/>' % (num(x), num(y), num(r)))

    def circle(self, x, y, r, cls="sym"):
        self.out.append('<circle class="%s" cx="%s" cy="%s" r="%s"/>' % (cls, num(x), num(y), num(r)))

    def rect(self, x, y, w, h, cls="box", rx=4):
        self.out.append('<rect class="%s" x="%s" y="%s" width="%s" height="%s" rx="%s"/>'
                        % (cls, num(x), num(y), num(w), num(h), rx))

    def path(self, d, cls="sym"):
        self.out.append('<path class="%s" d="%s"/>' % (cls, d))

    def text(self, x, y, s, cls="", anchor="start"):
        self.out.append('<text%s x="%s" y="%s" text-anchor="%s">%s</text>'
                        % (' class="%s"' % cls if cls else "", num(x), num(y), anchor, esc(s)))

    def raw(self, s):
        self.out.append(s)

    def __str__(self):
        return "\n".join(self.out)


def document(w, h, body, title, desc=""):
    w, h = int(w + 0.999), int(h + 0.999)
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img">\n'
            '<title>%s</title>\n<desc>%s</desc>\n<style>%s</style>\n<rect width="%d" height="%d" fill="#ffffff"/>\n%s\n</svg>\n'
            % (w, h, w, h, esc(title), esc(desc), STYLE, w, h, body))


def _wrap(text, width):
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    return lines + ([cur] if cur else [])


# ============================================================================= SPICE annotations
def annotations(sim, config, prefix=None, limit=10, summary=False):
    """Footer lines ``[(text, css-class)]`` from the real simulation report and tech SPICE config."""
    st = sim.get("status", "not_requested")
    head = {"passed": ("SPICE simulation: PASSED", "ok"), "failed": ("SPICE simulation: FAILED", "bad"),
            "unavailable": ("SPICE simulation: UNAVAILABLE (not run)", "na"),
            "not_requested": ("SPICE simulation: not requested", "k")}[st]
    lines = [head]
    if sim.get("reason") and st != "passed":
        lines += [(x, "k") for x in _wrap(sim["reason"], 118)[:3]]
    cond = []
    cond.append("VDD %g V" % config.vdd if config.vdd is not None else "VDD not configured")
    cond.append("%g C" % config.temperature if config.temperature is not None else "T not configured")
    cond.append("corner %s" % config.corner if config.corner else "corner not configured")
    lines.append(("conditions: " + " | ".join(cond), "k"))
    if config.includes:
        lines.append(("models: " + ", ".join(os.path.basename(i["file"].replace("\\", "/")) for i in config.includes), "k"))
    if summary:
        return lines
    shown = 0
    for m in sim.get("measurements", []):
        if prefix is None or m["name"].startswith(prefix):
            lines.append(("%s = %.4g %s" % (m["name"], m["value"], m.get("unit", "")), "k"))
            shown += 1
            if shown >= limit:
                break
    for d in sim.get("delays", []):
        if prefix is None or d["cell"] == prefix.rstrip("."):
            lines.append(("tpd %s %s -> out %s = %.4g s (edge %.3g s, load %.3g F)" % (
                d["input"], d["input_edge"], d["output_edge"], d["delay_s"], d["edge_s"], d["load_f"]), "k"))
    return lines


def _footer(g, x, y, lines, width):
    g.rect(x, y, width, 14 + 13 * len(lines), "box")
    for i, (t, c) in enumerate(lines):
        g.text(x + 10, y + 16 + 13 * i, t, c or "k")
    return 14 + 13 * len(lines)


# ============================================================================= transistor level
class _T(object):
    """Series/parallel tree node: kind d (device) / s (series, top->bottom) / p (parallel)."""

    def __init__(self, kind, kids=(), dev=None, top=None, bot=None):
        self.kind, self.kids, self.dev, self.top, self.bot = kind, list(kids), dev, top, bot
        self.w = self.h = self.ax = 0.0

    def rev(self):
        kids = [k.rev() for k in self.kids]
        if self.kind == "s":
            kids.reverse()
        return _T(self.kind, kids, self.dev, self.bot, self.top)


def _sp_reduce(devs, out, rail, degree):
    """Series/parallel reduction of ``devs`` between nodes out and rail -> (tree or None, leftover devices)."""
    edges = [[d.a, d.b, _T("d", dev=d, top=d.a, bot=d.b)] for d in devs]
    changed = True
    while changed:
        changed = False
        seen = {}
        for e in list(edges):
            if e[0] == e[1]:
                continue
            key = frozenset((e[0], e[1]))
            if key in seen:
                o = seen[key]
                t = e[2] if e[0] == o[0] else e[2].rev()
                kids = (o[2].kids if o[2].kind == "p" else [o[2]]) + (t.kids if t.kind == "p" else [t])
                o[2] = _T("p", kids, top=o[0], bot=o[1])
                edges.remove(e)
                changed = True
            else:
                seen[key] = e
        inc = {}
        for e in edges:
            for n in (e[0], e[1]):
                inc.setdefault(n, []).append(e)
        for n in sorted(inc):
            es = inc[n]
            if n in (out, rail) or len(es) != 2 or es[0] is es[1] or degree.get(n) != 2:
                continue
            e1, e2 = es
            t1 = e1[2] if e1[1] == n else e1[2].rev()
            a = e1[0] if e1[1] == n else e1[1]
            t2 = e2[2] if e2[0] == n else e2[2].rev()
            b = e2[1] if e2[0] == n else e2[0]
            if a == b:
                continue
            kids = (t1.kids if t1.kind == "s" else [t1]) + (t2.kids if t2.kind == "s" else [t2])
            edges.remove(e1)
            edges.remove(e2)
            edges.append([a, b, _T("s", kids, top=a, bot=b)])
            changed = True
            break
    main = [e for e in edges if {e[0], e[1]} == {out, rail}]
    if not main:
        return None, [e[2].dev for e in edges if e[2].kind == "d"]
    e = main[0]
    left = [x[2].dev for x in edges if x is not e and x[2].kind == "d"]
    return (e[2] if e[0] == out else e[2].rev()), left      # oriented out -> rail


def _measure(t):
    if t.kind == "d":
        t.w, t.h, t.ax = DW, DH, AX
    elif t.kind == "s":
        for k in t.kids:
            _measure(k)
        t.ax = max(k.ax for k in t.kids)
        t.w = t.ax + max(k.w - k.ax for k in t.kids)
        t.h = sum(k.h for k in t.kids)
    else:
        for k in t.kids:
            _measure(k)
        t.w = sum(k.w for k in t.kids)
        t.h = max(k.h for k in t.kids) + 2 * PB
        first, last = t.kids[0].ax, t.w - t.kids[-1].w + t.kids[-1].ax
        t.ax = (first + last) / 2.0


def _wl(dev):
    w, l = dev.params.get("w"), dev.params.get("l")
    return "%s/%s" % (w, l) if w and l else (w or l or "")


def _mos(g, x, y, dev, ptype, tags):
    """MOS symbol with its S/D axis at (x, y..y+DH); gate lead to the left carries the net tag."""
    cy = y + DH / 2.0
    g.line(x, y, x, y + DH / 2 - 17)
    g.line(x, y + DH / 2 + 17, x, y + DH)
    g.poly([(x, y + DH / 2 - 17), (x - 12, y + DH / 2 - 17)])
    g.poly([(x, y + DH / 2 + 17), (x - 12, y + DH / 2 + 17)])
    g.line(x - 12, cy - 19, x - 12, cy + 19, "ch")
    g.line(x - 21, cy - 19, x - 21, cy + 19, "w")
    lead = x - 21
    if ptype:
        g.circle(x - 25, cy, 3.5)
        lead = x - 28.5
    g.line(lead, cy, x - 54, cy)
    g.text(x - 58, cy - 6, dev.g, "k", "end")
    tags.append((dev.g, x - 54, cy))
    g.text(x + 10, cy - 4, dev.name, "")
    g.text(x + 10, cy + 8, "PMOS" if ptype else "NMOS", "k")
    g.text(x + 10, cy + 20, _wl(dev), "k")


def _draw_tree(g, t, x0, y, ptype, tags, nets):
    ax = x0 + t.ax
    if t.kind == "d":
        _mos(g, x0 + AX, y, t.dev, ptype, tags)
        return
    if t.kind == "s":
        yy = y
        for i, k in enumerate(t.kids):
            _draw_tree(g, k, x0 + t.ax - k.ax, yy, ptype, tags, nets)
            if i:
                g.dot(ax, yy)
                g.text(ax + 6, yy - 3, k.top, "k")
            yy += k.h
        return
    xs, xx = [], x0
    top_y, bot_y = y + PB, y + t.h - PB
    for k in t.kids:
        _draw_tree(g, k, xx, top_y, ptype, tags, nets)
        cx = xx + k.ax
        xs.append(cx)
        g.dot(cx, top_y)
        if top_y + k.h < bot_y:
            g.line(cx, top_y + k.h, cx, bot_y)
        g.dot(cx, bot_y)
        xx += k.w
    g.line(xs[0], top_y, xs[-1], top_y)
    g.line(xs[0], bot_y, xs[-1], bot_y)
    g.line(ax, y, ax, top_y)
    g.line(ax, bot_y, ax, y + t.h)
    g.dot(ax, top_y)
    g.dot(ax, bot_y)


def _stages(sub, vdd, vss):
    """Channel-connected components of the MOS devices, ordered by gate dependency."""
    sup = {vdd.lower(), vss.lower()}
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for m in sub.mos:
        for n in (m.a, m.b):
            if n.lower() not in sup:
                find(n)
        ends = [n for n in (m.a, m.b) if n.lower() not in sup]
        if len(ends) == 2:
            parent[find(ends[0])] = find(ends[1])
    groups = {}
    for i, m in enumerate(sub.mos):
        ends = [n for n in (m.a, m.b) if n.lower() not in sup]
        key = find(ends[0]) if ends else "~%d" % i
        groups.setdefault(key, []).append(m)
    comps = [{"devs": d, "nodes": sorted({n for m in d for n in (m.a, m.b) if n.lower() not in sup})} for d in groups.values()]
    gate_nets = {m.g for m in sub.mos}
    for c in comps:
        c["deps"] = {m.g for m in c["devs"]}
    order, placed = [], set()
    for _ in range(len(comps)):
        for c in sorted(comps, key=lambda c: (min(sub.mos.index(m) for m in c["devs"]))):
            if id(c) in placed:
                continue
            if all(not (set(c["deps"]) & set(o["nodes"])) or id(o) in placed for o in comps if o is not c):
                order.append(c)
                placed.add(id(c))
    order += [c for c in comps if id(c) not in placed]
    return order, gate_nets


def _out_node(c, sub, vdd, vss, gate_nets):
    sup = {vdd.lower(), vss.lower()}
    best = None
    for n in c["nodes"]:
        rail = any((m.a == n and m.b.lower() in sup) or (m.b == n and m.a.lower() in sup) for m in c["devs"])
        score = 3 * rail + 2 * (n in gate_nets) + (n in sub.pins)
        key = (-score, n)
        if best is None or key < best[0]:
            best = (key, n)
    return best[1]


def cell_body(sub, vdd="VDD", vss="VSS"):
    """``(width, height, body)`` of the transistor-level schematic of ``sub`` (no title/footer)."""
    order, gate_nets = _stages(sub, vdd, vss)
    sup = {vdd.lower(), vss.lower()}
    plans = []
    for c in order:
        out = _out_node(c, sub, vdd, vss, gate_nets)
        degree = {}
        for m in c["devs"]:
            for n in (m.a, m.b):
                degree[n] = degree.get(n, 0) + 1
        pu, left_p = _sp_reduce([m for m in c["devs"] if m.kind == "p"], out, vdd, degree) if any(
            m.kind == "p" for m in c["devs"]) else (None, [])
        pd, left_n = _sp_reduce([m for m in c["devs"] if m.kind == "n"], out, vss, degree) if any(
            m.kind == "n" for m in c["devs"]) else (None, [])
        if pu is not None:
            pu = pu.rev()                         # rail (top) -> out (bottom)
            _measure(pu)
        if pd is not None:
            _measure(pd)
        plans.append({"out": out, "pu": pu, "pd": pd, "side": left_p + left_n, "c": c})
    pu_h = max([p["pu"].h for p in plans if p["pu"]] + [0])
    pd_h = max([p["pd"].h for p in plans if p["pd"]] + [0])
    top, gap = 52, 22
    y_vdd = top
    y_bus = y_vdd + gap + pu_h + gap
    y_vss = y_bus + gap + pd_h + gap
    g, tags = _G(), []
    x = 40
    for p in plans:
        ax = max([t.ax for t in (p["pu"], p["pd"]) if t] + [AX])
        right = max([t.w - t.ax for t in (p["pu"], p["pd"]) if t] + [0])
        side_w = 150 * len(p["side"])
        bus_end = x + ax + max(right, 60) + side_w
        for t, ptype in ((p["pu"], True), (p["pd"], False)):
            if t is None:
                continue
            x0 = x + ax - t.ax
            if ptype:
                y0 = y_bus - gap - t.h
                _draw_tree(g, t, x0, y0, True, tags, None)
                g.line(x + ax, y_vdd, x + ax, y0)
                g.line(x + ax, y0 + t.h, x + ax, y_bus)
                g.dot(x + ax, y_vdd)
            else:
                y0 = y_bus + gap
                _draw_tree(g, t, x0, y0, False, tags, None)
                g.line(x + ax, y_bus, x + ax, y0)
                g.line(x + ax, y0 + t.h, x + ax, y_vss)
                g.dot(x + ax, y_vss)
        g.line(x + ax, y_bus, bus_end, y_bus)
        g.dot(x + ax, y_bus)
        is_pin = p["out"] in sub.pins
        g.text(x + ax + 8, y_bus - 6, p["out"], "k")
        sx = x + ax + max(right, 60)
        for i, dev in enumerate(p["side"]):
            cx = sx + 20 + i * 150
            other = dev.b if dev.a == p["out"] else dev.a
            _side_mos(g, cx, y_bus, dev, tags)
            tgt = other
            g.text(cx + 78, y_bus - 8, tgt, "k")
            tags.append((tgt, cx + 74, y_bus, "pin" if tgt in sub.pins else "net"))
            g.dot(cx, y_bus)
        if is_pin and not p["side"]:
            tags.append((p["out"], bus_end, y_bus, "out"))
        x = bus_end + 70
    width = max(x, 360)
    g.line(20, y_vdd, width - 20, y_vdd, "vdd")
    g.line(20, y_vss, width - 20, y_vss, "vss")
    g.text(22, y_vdd - 6, vdd, "k")
    g.text(22, y_vss + 14, vss, "k")
    flags = _G()
    drawn = set()
    for tg in tags:
        net, tx, ty = tg[0], tg[1], tg[2]
        role = tg[3] if len(tg) > 3 else ("in" if net in sub.pins else "net")
        if (round(tx), round(ty)) in drawn:
            continue
        drawn.add((round(tx), round(ty)))
        cls = {"in": "tag tin", "out": "tag tout", "pin": "tag tin", "net": "tag"}[role]
        flags.circle(tx, ty, 3.2, cls)
    return width, y_vss + 30, str(g) + "\n" + str(flags)


def _side_mos(g, x, y, dev, tags):
    """Horizontal pass transistor: left terminal at (x, y) on the stage bus, right terminal at x+74."""
    g.line(x, y, x + 20, y)
    g.line(x + 20, y, x + 28, y)
    g.line(x + 28, y - 17, x + 28, y + 17, "ch")
    g.line(x + 36, y - 17, x + 36, y + 17, "w")
    g.line(x + 36, y + 17, x + 36, y + 24, "w")
    g.line(x + 28, y, x + 74, y)
    g.line(x + 36, y + 24, x + 36, y + 30)
    g.text(x + 40, y + 42, dev.g, "k", "middle")
    tags.append((dev.g, x + 36, y + 30))
    g.text(x + 28, y + 56, dev.name, "")
    g.text(x + 28, y + 68, _wl(dev), "k")


def cell_svg(sub, vdd="VDD", vss="VSS", title=None, notes=(), function=None):
    w, h, body = cell_body(sub, vdd, vss)
    g = _G()
    g.text(20, 22, title or sub.name, "t")
    sig = [p for p in sub.pins if p.lower() not in (vdd.lower(), vss.lower())]
    g.text(w - 20, 22, "%s | pins: %s | %d MOS%s" % (function or "cell", " ".join(sig), len(sub.mos), ""), "s", "end")
    foot = _G()
    fh = _footer(foot, 12, 0, list(notes), max(w - 24, 300)) if notes else 0
    total_h = h + (fh + 20 if notes else 0)
    foot_body = '<g transform="translate(0,%s)">%s</g>' % (num(h), foot) if notes else ""
    return document(w, total_h, str(g) + "\n" + body + "\n" + foot_body, title or sub.name,
                    "Transistor-level schematic of %s generated from its SPICE netlist" % sub.name), w, total_h


# ============================================================================= gate level
def _symbol(g, x, y, kind, k):
    """Gate symbol in a 56x40 box at (x, y); returns (input_ys, out_x)."""
    ys = [y + 20 + (i - (k - 1) / 2.0) * (10 if k <= 3 else 8) for i in range(k)] or [y + 20]
    bubble = kind in ("inv", "nand2", "nand3", "nand4", "nor2", "nor3", "nor4")
    base = kind.rstrip("0123456789")
    if base == "inv" or base == "buf":
        g.path("M %s %s L %s %s L %s %s Z" % (num(x + 6), num(y + 4), num(x + 6), num(y + 36), num(x + 40), num(y + 20)))
        out_x = x + 40
    elif base in ("nand", "and"):
        g.path("M %s %s H %s A 16 16 0 0 1 %s %s H %s Z" % (num(x + 6), num(y + 4), num(x + 24), num(x + 24), num(y + 36), num(x + 6)))
        out_x = x + 40
    elif base in ("nor", "or"):
        g.path("M %s %s Q %s %s %s %s Q %s %s %s %s Q %s %s %s %s Z" % (
            num(x + 6), num(y + 4), num(x + 20), num(y + 20), num(x + 6), num(y + 36),
            num(x + 30), num(y + 36), num(x + 42), num(y + 20), num(x + 30), num(y + 4), num(x + 6), num(y + 4)))
        out_x = x + 42
    else:
        g.rect(x + 6, y + 4, 38, 32, "sym", 5)
        out_x = x + 44
        bubble = False
    if bubble:
        g.circle(out_x + 4, y + 20, 4)
        out_x += 8
    for yy in ys:
        g.line(x, yy, x + 8, yy)
    return ys, out_x


def _logic_kind(cell, subs, tech, vdd, vss):
    from . import spice_artifacts as SA
    sub = SA.lookup(subs, cell) if subs else None
    if sub is not None:
        info = SA.classify_cell(sub, vdd, vss)
        if info["kind"] not in ("unknown",):
            return info["kind"], info["inputs"], info["output"]
    for role, c in tech.cells.items():
        if c["cell"] == cell:
            ins = [c["pins"][p] for p in c["pins"] if p != "Y"]
            return role, ins, c["pins"].get("Y", "Y")
    return "unknown", None, None


def block_body(blk, subs, collapse_over=60):
    """``(width, height, body, info)`` gate-level diagram of ``blk`` (an :class:`dla.layout.Block`)."""
    t = blk.t
    nodes = {}
    for name, cell, _x, _y, _o in blk.insts:
        pins = {p: n for n, ips in blk.conn.items() for (i, p) in ips if i == name}
        if not pins:
            continue
        kind, ins, out = _logic_kind(cell, subs, t, t.vdd, t.vss)
        sig = {p: n for p, n in pins.items() if p.lower() not in (t.vdd.lower(), t.vss.lower())}
        if ins is None:
            out = "Y" if "Y" in sig else sorted(sig)[-1]
            ins = [p for p in sorted(sig) if p != out]
        nodes[name] = {"cell": cell, "kind": kind, "in": [p for p in ins if p in sig], "out": out, "pins": sig}
    driver = {}
    for name, nd in nodes.items():
        if nd["out"] in nd["pins"]:
            driver[nd["pins"][nd["out"]]] = name
    level = {}

    def lv(n, stack=()):
        if n in level:
            return level[n]
        if n in stack:
            return 1
        v = 1
        for p in nodes[n]["in"]:
            d = driver.get(nodes[n]["pins"][p])
            if d and d != n:
                v = max(v, lv(d, stack + (n,)) + 1)
        level[n] = v
        return v
    for n in sorted(nodes):
        lv(n)
    collapsed = len(nodes) > collapse_over
    units, member = {}, {}
    for n in sorted(nodes):
        key = (level[n], nodes[n]["cell"], re.sub(r"\d+", "#", n)) if collapsed else (n,)
        units.setdefault(key, []).append(n)
        member[n] = key
    ulevel = {k: level[v[0]] for k, v in units.items()}
    nlev = max(ulevel.values() or [1])
    cols = {c: sorted([k for k in units if ulevel[k] == c], key=lambda k: str(k)) for c in range(1, nlev + 1)}
    colw, rowh = 150, 62 if not collapsed else 70
    rows = {}
    for c in range(1, nlev + 1):
        for i, k in enumerate(cols[c]):
            rows[k] = i
    for _ in range(2):                           # barycentre sweeps
        for c in range(2, nlev + 1):
            def bary(k):
                ys = [rows[member[driver[nodes[m]["pins"][p]]]] for m in units[k] for p in nodes[m]["in"]
                      if nodes[m]["pins"][p] in driver and member[driver[nodes[m]["pins"][p]]] in rows]
                return (sum(ys) / len(ys)) if ys else rows[k]
            cols[c].sort(key=lambda k: (bary(k), str(k)))
            for i, k in enumerate(cols[c]):
                rows[k] = i
    in_nets = sorted({n for nm in nodes.values() for p, n in nm["pins"].items() if n not in driver
                      and n not in (t.vdd, t.vss)}, key=_natkey)
    used = {nm["pins"][p] for nm in nodes.values() for p in nm["in"]}
    out_nets = sorted([n for n in driver if re.match(r"^Y(\[|$)", n)] or [n for n in driver if n not in used],
                      key=_natkey)
    left = 150
    x_of = lambda c: left + (c - 1) * colw + 60
    maxrows = max([len(v) for v in cols.values()] + [len(in_nets) if len(in_nets) < 40 else 1, 1])
    height = max(maxrows, 1) * rowh + 80
    g = _G()
    pos_out, pos_in = {}, {}
    for k in units:
        c, r = ulevel[k], rows[k]
        x, y = x_of(c), 50 + r * rowh
        first = units[k][0]
        nd = nodes[first]
        if len(units[k]) == 1:
            ys, out_x = _symbol(g, x, y, nd["kind"], len(nd["in"]))
            g.text(x + 24, y + 48, k[0], "k", "middle")
        else:
            g.rect(x + 8, y + 2, 72, 40, "box")
            g.rect(x + 3, y - 3, 72, 40, "box")
            g.text(x + 39, y + 16, "x%d" % len(units[k]), "", "middle")
            g.text(x + 39, y + 28, nd["cell"], "k", "middle")
            ys = [y + 20 + (i - (len(nd["in"]) - 1) / 2.0) * 8 for i in range(len(nd["in"]))] or [y + 20]
            for yy in ys:
                g.line(x - 2, yy, x + 3, yy)
            out_x = x + 80
            g.line(out_x - 5, y + 20, out_x, y + 20)
        for m in units[k]:
            for i, p in enumerate(nodes[m]["in"]):
                pos_in[(m, p)] = (x - (2 if len(units[k]) > 1 else 0), ys[i])
            pos_out[m] = (out_x, y + 20)
    # ports
    port_y = {}
    show_in = in_nets if len(in_nets) <= 40 else []
    for i, n in enumerate(show_in):
        py = 50 + i * (height - 90) / max(len(show_in), 1)
        port_y[n] = py
        g.circle(48, py, 3.5, "tag tin")
        g.text(42, py + 3, n, "k", "end")
    xr = x_of(nlev + 1) + 40
    show_out = out_nets if len(out_nets) <= 40 else []
    for i, n in enumerate(show_out):
        py = 50 + i * (height - 90) / max(len(show_out), 1)
        port_y[("o", n)] = py
        g.circle(xr, py, 3.5, "tag tout")
        g.text(xr + 8, py + 3, n, "k")
    # wires
    seen_dots = set()
    lane = {}
    for net in sorted(blk.conn, key=_natkey):
        if net in (t.vdd, t.vss):
            continue
        d = driver.get(net)
        sinks = [(m, p) for m in sorted(nodes) for p in nodes[m]["in"] if nodes[m]["pins"][p] == net]
        if d is not None:
            sx, sy = pos_out[d]
        elif net in port_y:
            sx, sy = 48, port_y[net]
        else:
            continue
        pts_done = set()
        for m, p in sinks:
            if d is not None and member[d] == member[m] and collapsed:
                continue
            ex, ey = pos_in[(m, p)]
            key = (round(sx), round(sy), round(ex), round(ey))
            if key in pts_done:
                continue
            pts_done.add(key)
            lane_x = ex - 18 - (hash_idx(net) % 5) * 3
            if d is not None and ulevel[member[m]] > ulevel[member[d]] + 1:
                ylane = 50 + (rows[member[d]] + 0.5) * rowh - 6 + (hash_idx(net) % 5)
                _wire(g, [(sx, sy), (sx + 10 + (hash_idx(net) % 5) * 2, sy), (sx + 10 + (hash_idx(net) % 5) * 2, ylane),
                          (lane_x, ylane), (lane_x, ey), (ex, ey)])
            else:
                _wire(g, [(sx, sy), (lane_x, sy), (lane_x, ey), (ex, ey)])
        if len(sinks) > 1:
            g.dot(sx + 2, sy)
        if ("o", net) in port_y and d is not None:
            _wire(g, [(sx, sy), (xr - 12, sy), (xr - 12, port_y[("o", net)]), (xr, port_y[("o", net)])])
    width = xr + 140
    info = {"gates": len(nodes), "collapsed": collapsed, "levels": nlev, "units": len(units)}
    return width, height, str(g), info


def hash_idx(s):
    return sum(ord(c) for c in str(s)) * 7 % 31


def _natkey(s):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", str(s))]


def _wire(g, pts):
    clean = [pts[0]]
    for p in pts[1:]:
        if p != clean[-1]:
            clean.append(p)
    g.poly(clean)


def block_svg(blk, subs, config, sim):
    w, h, body, info = block_body(blk, subs)
    g = _G()
    g.text(20, 22, blk.name, "t")
    meta = blk.meta
    g.text(w - 20, 22, "%s | %d gates%s | tech %s" % (meta.get("block", "block"), info["gates"],
                                                       " (grouped x%d)" % info["units"] if info["collapsed"] else "", blk.t.name),
           "s", "end")
    notes = annotations(sim, config) + [("supplies: %s / %s on every cell (not drawn per gate)" % (blk.t.vdd, blk.t.vss), "k")]
    if not subs:
        notes.append(("cell functions taken from the tech roles; no SPICE cell_library configured", "k"))
    foot = _G()
    fh = _footer(foot, 12, 0, notes, max(w - 24, 400))
    total = h + fh + 24
    return document(w, total, str(g) + "\n" + body + '\n<g transform="translate(0,%s)">%s</g>' % (num(h), foot),
                    blk.name, "Gate-level block diagram generated from the placed netlist"), w, total


# ============================================================================= SRAM array
def array_svg(name, rows, cols, bitcell, vdd, vss, config, sim, show=4):
    r_show, c_show = min(rows, show), min(cols, show)
    cw, rh, x0, y0 = 120, 70, 110, 90
    g = _G()
    g.text(20, 22, "%s: %d x %d 6T SRAM array" % (name, rows, cols), "t")
    for c in range(c_show):
        cx = x0 + c * cw + cw / 2
        g.line(cx - 8, y0 - 30, cx - 8, y0 + r_show * rh - 10)
        g.line(cx + 8, y0 - 30, cx + 8, y0 + r_show * rh - 10)
        g.text(cx - 10, y0 - 36, "BL[%d]" % c, "k", "end")
        g.text(cx + 10, y0 - 36, "BLB[%d]" % c, "k")
    for r in range(r_show):
        cy = y0 + r * rh + rh / 2 - 10
        g.line(x0 - 70, cy, x0 + c_show * cw - 10, cy)
        g.text(x0 - 74, cy + 4, "WL[%d]" % r, "k", "end")
        for c in range(c_show):
            cx = x0 + c * cw + cw / 2
            g.rect(cx - 36, cy - 22, 72, 44, "box")
            g.text(cx, cy - 4, bitcell, "k", "middle")
            g.text(cx, cy + 10, "Xbc_%d_%d" % (r, c), "k", "middle")
            for dx in (-8, 8):
                g.dot(cx + dx, cy - 22)
            g.dot(cx - 36, cy)
    if rows > r_show or cols > c_show:
        g.text(x0 + c_show * cw / 2, y0 + r_show * rh + 14, "... %d x %d shown of %d x %d" % (r_show, c_show, rows, cols),
               "k", "middle")
    w = x0 + c_show * cw + 60
    h = y0 + r_show * rh + 40
    notes = annotations(sim, config, prefix="hold") + [
        ("all bit-cells share %s / %s; array netlist: %d x %d instances of %s" % (vdd, vss, rows, cols, bitcell), "k")]
    foot = _G()
    fh = _footer(foot, 12, 0, notes, max(w - 24, 400))
    return document(w, h + fh + 20, str(g) + '\n<g transform="translate(0,%s)">%s</g>' % (num(h), foot), name,
                    "SRAM array hierarchy generated from the array SPICE netlist"), w, h + fh + 20


# ============================================================================= writers
def _write(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def _cell_notes(sim, config, name):
    return annotations(sim, config, prefix=name + ".")


def write_library_schematics(outdir, libname, subs, st, config, sim):
    """One transistor-level SVG per logic cell + a contact sheet ``<lib>_schematics.svg``."""
    from . import spice_artifacts as SA
    cells = [s for s in subs.values() if s.mos]
    d = os.path.join(outdir, "schematics")
    out, cards = {}, []
    for sub in cells:
        info = SA.classify_cell(sub, st.vdd, st.vss)
        svg, w, h = cell_svg(sub, st.vdd, st.vss, sub.name, _cell_notes(sim, config, sub.name), info["kind"])
        _write(os.path.join(d, sub.name + ".svg"), svg)
        body = cell_body(sub, st.vdd, st.vss)
        cards.append((sub, info, body))
    cw, chh = 460, 300
    ncol = 3 if len(cards) > 4 else max(len(cards), 1)
    g = _G()
    g.text(20, 26, "%s: standard-cell schematics (%d logic cells)" % (libname, len(cards)), "t")
    st_line = annotations(sim, config)[0]
    g.text(20, 44, "%s | tech %s | symbols derived from the generated SPICE library" % (st_line[0], st.name), "s")
    for i, (sub, info, (w, h, body)) in enumerate(cards):
        cx, cy = 20 + (i % ncol) * (cw + 20), 60 + (i // ncol) * (chh + 20)
        s = min(1.0, (cw - 20) / float(w), (chh - 62) / float(h))
        g.rect(cx, cy, cw, chh, "box")
        g.text(cx + 12, cy + 20, sub.name, "")
        g.text(cx + cw - 12, cy + 20, "%s | %d MOS" % (info["kind"], len(sub.mos)), "k", "end")
        g.raw('<g transform="translate(%s,%s) scale(%s)">%s</g>' % (num(cx + 10), num(cy + 34), num(s), body))
        res = [m for m in sim.get("measurements", []) if m["name"].startswith(sub.name + ".")]
        txt = "; ".join("%s=%.3g%s" % (m["name"].split(".", 1)[1], m["value"], m.get("unit", "")) for m in res[:2]) \
            or ("SPICE: " + sim.get("status", "not_requested"))
        g.text(cx + 12, cy + chh - 10, txt, "k")
    rows_n = (len(cards) + ncol - 1) // ncol
    W, H = 20 + ncol * (cw + 20), 60 + rows_n * (chh + 20)
    H += _footer(g, 20, H, annotations(sim, config, summary=True), W - 40) + 20      # status + conditions only
    out["schematic"] = _write(os.path.join(outdir, libname + "_schematics.svg"),
                              document(W, H, str(g), libname + " schematics", "Library contact sheet"))
    out["schematic_dir"] = d
    return out


def write_block_schematic(outdir, blk, subs, config, sim):
    svg, _w, _h = block_svg(blk, subs, config, sim)
    return {"schematic": _write(os.path.join(outdir, blk.name + "_schematic.svg"), svg)}


def write_sram_schematics(outdir, name, subs, res, config, sim):
    from . import spice_artifacts as SA
    st, cell, arr = res["st"], res["cell"], res["array"]
    out = {}
    svg, _w, _h = array_svg(name, arr.rows, arr.cols, cell.name, st.vdd, st.vss, config, sim)
    out["schematic"] = _write(os.path.join(outdir, name + "_schematic.svg"), svg)
    sub = SA.lookup(subs, cell.name)
    if sub is not None:
        svg, _w, _h = cell_svg(sub, st.vdd, st.vss, cell.name + " (6T bit-cell)",
                               annotations(sim, config, prefix=None, limit=24), "6T SRAM")
        out["schematic_bitcell"] = _write(os.path.join(outdir, name + "_bitcell_schematic.svg"), svg)
    return out
