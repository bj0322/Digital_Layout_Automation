"""Transistor-level layout of one static CMOS std cell.

Template (R0, y up): VSS rail + p+ tap at y = 0, NMOS band, channel with the poly
contacts, PMOS band in the n-well, VDD rail + n+ tap at y = row height. Transistors
are placed as vertical poly columns in the order found by :mod:`dla.stdcell.euler`;
neighbouring columns share diffusion. Contacted diffusions get a metal-1 strap over
their contacts; straps on VDD/VSS run into the rail.

Everything else is wired on metal 1 by a small A* router whose legality test is
geometric (rect against rect, with the pin-layer spacing / wide-spacing rules and
notch detection between shapes of the same net). Every input and the output also
get an *access pad* that encloses a VIA1 on a vertical-layer track, so the block
router (:mod:`dla.layout`) can reach each pin on its own track.
"""
import heapq
import itertools
import math

from ..tech import EPS, inside_union
from . import euler
from .rules import snap


class LayoutError(RuntimeError):
    pass


def r6(v):
    return round(v, 6)


def R(x1, y1, x2, y2):
    return (r6(min(x1, x2)), r6(min(y1, y2)), r6(max(x1, x2)), r6(max(y1, y2)))


class CellLayout:
    """Shapes of one cell. rects: (layer, rect, net); texts: (layer, text, x, y)."""

    def __init__(self, name, width, st):
        self.name, self.width, self.st = name, width, st
        self.rects, self.texts = [], []
        self.access = {}             # net -> (x, y) centre of the access pad
        self.pins = []               # pin net names (signal pins, then power)
        self.netlist = None
        self.info = {}

    def add(self, layer, rect, net=None):
        self.rects.append((layer, rect, net))

    def m1(self, net=None):
        return [(r, n) for l, r, n in self.rects if l == "m1" and (net is None or n == net)]


# ----------------------------------------------------------------------------- template
def template(st, name, nsites):
    W = r6(nsites * st.site_w)
    H, th = st.row_h, st.tap_h
    c = st.r["cont_size"]
    cl = CellLayout(name, W, st)
    cl.add("boundary", R(0, 0, W, H))
    cl.add("m1", R(0, -st.rail_w / 2, W, st.rail_w / 2), st.vss)
    cl.add("m1", R(0, H - st.rail_w / 2, W, H + st.rail_w / 2), st.vdd)
    cl.add("activ", R(0, -th / 2, W, th / 2), st.vss)
    cl.add("activ", R(0, H - th / 2, W, H + th / 2), st.vdd)
    # tap contacts halfway between vertical-layer tracks, so power-strap vias on the rails
    # never land on them (matters when the PDK forbids VIA1 near contacts)
    edge = c / 2 + st.r["cont_activ_enc"]
    k = int(math.floor(-st.x_off / st.gx)) - 1
    while True:
        x = r6(st.x_off + st.gx * (k + 0.5))
        k += 1
        if x > W - edge + EPS:
            break
        if x < edge - EPS:
            continue
        for y, net in ((0.0, st.vss), (H, st.vdd)):
            cl.add("cont", R(x - c / 2, y - c / 2, x + c / 2, y + c / 2), net)
    px = st.pimp_x
    cl.add("pimp", R(-px, -st.ptap_imp, W + px, st.ptap_imp))
    cl.add("pimp", R(-px, st.pimp_bot, W + px, st.pimp_top))
    if st.lay.get("nimp"):
        cl.add("nimp", R(-px, st.ptap_imp, W + px, st.pimp_bot))
        cl.add("nimp", R(-px, st.pimp_top, W + px, H + st.ptap_imp))
    cl.add("nwell", R(-st.nwell_x, st.nwell_bot, W + st.nwell_x, st.nwell_top))
    cl.texts.append(("m1_text", st.vss, W / 2, 0.0))
    cl.texts.append(("m1_text", st.vdd, W / 2, H))
    return cl


def make_fill(st, name, nsites):
    cl = template(st, name, nsites)
    cl.pins = [st.vdd, st.vss]
    cl.info = {"kind": "fill", "sites": nsites}
    return cl


# ----------------------------------------------------------------------------- placement
def _columns(st, cn, seq):
    """Diffusion slots and gate columns with x positions (before centring)."""
    elems = []
    for i, (g, (nl, nr), (pl, pr), brk, devs) in enumerate(seq):
        if i == 0 or brk:
            if brk:
                elems.append({"t": "break"})
            elems.append({"t": "slot", "n": nl, "p": pl})
        elems.append({"t": "gate", "g": g, "devs": devs})
        elems.append({"t": "slot", "n": nr, "p": pr})
    occ = {}
    for e in elems:
        if e["t"] == "slot":
            for row in "np":
                occ[e[row]] = occ.get(e[row], 0) + 1
    gate_nets = {e["g"] for e in elems if e["t"] == "gate"}
    must = {st.vdd, st.vss, cn.output} | gate_nets
    for e in elems:
        if e["t"] == "slot":
            e["cn"] = e["n"] in must or occ[e["n"]] > 1
            e["cp"] = e["p"] in must or occ[e["p"]] > 1
            e["c"] = e["cn"] or e["cp"]
    # x positions
    x_edge = st.amargin
    chains, cur = [], None
    prev_gate = None
    for k, e in enumerate(elems):
        if e["t"] == "break":
            cur["x1"] = r6(elems[k - 1]["x"] + st.x_cont_edge)
            x_edge = r6(cur["x1"] + st.r["activ_space"])
            prev_gate = None
            continue
        if e["t"] == "slot":
            if prev_gate is None:                       # chain start
                e["x"] = r6(x_edge + st.x_cont_edge)
                cur = {"x0": x_edge}
                chains.append(cur)
            elif e["c"]:
                e["x"] = r6(prev_gate + st.x_cont_gate)
            else:
                e["x"] = None                           # between two gates, set below
            continue
        before = elems[k - 1]
        if before["x"] is not None and before["c"]:
            e["x"] = r6(before["x"] + st.x_cont_gate)
        else:
            e["x"] = r6(prev_gate + st.x_gate_unc)
        prev_gate = e["x"]
    for k, e in enumerate(elems):
        if e["t"] == "slot" and e["x"] is None:
            e["x"] = r6((elems[k - 1]["x"] + elems[k + 1]["x"]) / 2)
    cur["x1"] = r6(elems[-1]["x"] + st.x_cont_edge)
    right = cur["x1"] + st.amargin
    return elems, chains, right


def _shift(elems, chains, dx):
    for e in elems:
        if "x" in e:
            e["x"] = r6(e["x"] + dx)
    for ch in chains:
        ch["x0"], ch["x1"] = r6(ch["x0"] + dx), r6(ch["x1"] + dx)


# ----------------------------------------------------------------------------- geometry rules
class Geo:
    """Metal-1 legality: spacing / wide spacing to other nets, notches within a net."""

    def __init__(self, st):
        self.w = st.m1.width
        self.s = st.m1.space
        self.wide = st.m1.wide

    def need(self, a, b, ox, oy):
        s = self.s
        if self.wide:
            run = ox if oy < 0 and ox > 0 else oy if ox < 0 and oy > 0 else 0.0
            wa = min(a[2] - a[0], a[3] - a[1])
            wb = min(b[2] - b[0], b[3] - b[1])
            for w, prl, s2 in self.wide:
                if max(wa, wb) >= w - EPS and run >= prl - EPS:
                    s = max(s, s2)
        return s

    def ok(self, a, b, same, fill=None):
        ox = min(a[2], b[2]) - max(a[0], b[0])
        oy = min(a[3], b[3]) - max(a[1], b[1])
        if ox > EPS and oy > EPS:
            return same
        if ox > -EPS and oy > -EPS:
            # touching shapes of one net merge; a real neck would be a width error, which the
            # KLayout DRC (width check on merged metal) reports
            return same
        gap = math.hypot(max(0.0, -ox), max(0.0, -oy))
        if gap >= self.need(a, b, ox, oy) - EPS:
            return True
        if not same or fill is None:
            return False
        if ox < EPS and oy < EPS:
            return True        # corner to corner within one net: concave corner, not a notch
        # same net closer than the spacing: legal only if the gap is filled (no notch)
        gx0, gx1 = (max(a[0], b[0]), min(a[2], b[2])) if ox > 0 else (min(a[2], b[2]), max(a[0], b[0]))
        gy0, gy1 = (max(a[1], b[1]), min(a[3], b[3])) if oy > 0 else (min(a[3], b[3]), max(a[1], b[1]))
        box = (gx0, gy0, gx1, gy1)
        near = [r for r in fill if r[0] < box[2] + EPS and r[2] > box[0] - EPS and r[1] < box[3] + EPS and r[3] > box[1] - EPS]
        return bool(near) and inside_union(box, near)


def _overlap(a, b):
    return min(a[2], b[2]) - max(a[0], b[0]) > EPS and min(a[3], b[3]) - max(a[1], b[1]) > EPS


def _dist(a, b):
    return math.hypot(max(0.0, max(a[0], b[0]) - min(a[2], b[2])), max(0.0, max(a[1], b[1]) - min(a[3], b[3])))


def poly_ok(st, a, b):
    ox = min(a[2], b[2]) - max(a[0], b[0])
    oy = min(a[3], b[3]) - max(a[1], b[1])
    if ox > -EPS and oy > -EPS:
        return False
    return math.hypot(max(0.0, -ox), max(0.0, -oy)) >= st.r["poly_space"] - EPS


# ----------------------------------------------------------------------------- router
class Router:
    def __init__(self, st, cl, xs, ys, tracks, gates, io_nets):
        self.st, self.cl = st, cl
        self.xs, self.ys = xs, ys
        self.ix = {x: i for i, x in enumerate(xs)}
        self.tracks = tracks
        self.gates = gates                   # gate idx -> {"x", "g", "pad": None|y}
        self.geo = Geo(st)
        self.w = st.m1.width
        self.io_nets = io_nets
        self.used_tracks = {}
        c = st.r["cont_size"]
        es, ee = sorted(st.r["m1_cont_enc"])
        self.cpad = (max(c + 2 * es, self.w), max(c + 2 * ee, self.w))      # metal over a poly contact (w, h)
        vs = st.v1.size
        s, e = sorted(st.v1.enc_below)
        g2 = 2 * st.mfg
        self.apad = (max(vs + 2 * s, self.w) + g2, max(vs + 2 * e, self.w) + g2)   # access pad (w, h)
        self.xmin = st.m1_edge
        self.xmax = cl.width - st.m1_edge
        self.unconnected = set()             # same-net straps not yet joined to the tree

    # ---- shapes
    def shapes(self):
        return self.cl.m1()

    def legal(self, rect, net, extra_same=()):
        return self.legal_other(rect, net) and self.legal_same(rect, net, extra_same)

    def legal_other(self, rect, net):
        """Spacing to every shape of another net (and the cell edge)."""
        if rect[0] < self.xmin - EPS or rect[2] > self.xmax + EPS:
            return False
        return all(self.geo.ok(rect, r, False) for r, n in self.shapes() if n != net)

    def legal_same(self, rect, net, extra_same=()):
        """No notch against the net's own shapes; extra_same (the path so far) may fill gaps."""
        mine = [r for r, n in self.shapes() if n == net and r not in self.unconnected]
        fill = mine + list(extra_same) + [rect]
        return all(self.geo.ok(rect, r, True, fill) for r in mine + list(extra_same))

    def edge_rect(self, a, b):
        (x1, y1), (x2, y2) = self.node_xy(a), self.node_xy(b)
        h = self.w / 2
        return R(min(x1, x2) - h, min(y1, y2) - h, max(x1, x2) + h, max(y1, y2) + h)

    def node_xy(self, n):
        return self.xs[n[0]], self.ys[n[1]]

    # ---- terminals
    def stub_nodes(self, rect):
        out = []
        for i, x in enumerate(self.xs):
            if rect[0] - EPS <= x - self.w / 2 and x + self.w / 2 <= rect[2] + EPS:
                for j, y in enumerate(self.ys):
                    if rect[1] - EPS <= y <= rect[3] + EPS:
                        out.append((i, j))
        return out

    def gate_pad(self, gi, y, xc=None, rot=False):
        """Poly pad + metal for a poly contact at (xc, y); xc may sit beside the gate line."""
        st, xg = self.st, self.gates[gi]["x"]
        xc = xg if xc is None else xc
        pc, L = st.pc, st.L
        mw, mh = self.cpad[::-1] if rot else self.cpad
        poly = R(min(xc - pc / 2, xg - L / 2), y - pc / 2, max(xc + pc / 2, xg + L / 2), y + pc / 2)
        m1 = R(xc - mw / 2, y - mh / 2, xc + mw / 2, y + mh / 2)
        return poly, m1

    def gate_candidates(self, gi, net):
        st = self.st
        xg = self.gates[gi]["x"]
        half = st.x_cont_gate
        out = {}
        for xc in (xg, r6(xg - half), r6(xg + half)):
            if xc not in self.ix:
                continue
            for j, y in enumerate(self.ys):
                if not (st.pad_y[0] - EPS <= y <= st.pad_y[1] + EPS):
                    continue
                for rot in (False, True):
                    poly, m1 = self.gate_pad(gi, y, xc, rot)
                    if not all(poly_ok(st, poly, q) for k, q in self.poly_others(gi)):
                        break
                    c = st.r["cont_size"] / 2
                    if self.legal(m1, net) and self.cont_clear(R(xc - c, y - c, xc + c, y + c)):
                        out[(self.ix[xc], j)] = ("gate", gi, y, xc, rot)
                        break
        return out

    def poly_others(self, gi):
        for k, gd in self.gates.items():
            if k == gi:
                continue
            x, L = gd["x"], self.st.L
            yield k, R(x - L / 2, self.st.poly_bot, x + L / 2, self.st.poly_top)
            if gd["pad"] is not None:
                yield k, self.gate_pad(k, *gd["pad"])[0]

    def io_candidates(self, net):
        out = {}
        for x in self.tracks:
            if x not in self.ix or self.used_tracks.get(x, net) != net:
                continue
            for j, y in enumerate(self.ys):
                a = R(x - self.apad[0] / 2, y - self.apad[1] / 2, x + self.apad[0] / 2, y + self.apad[1] / 2)
                if self.legal(a, net) and self.via_clear(x, y):
                    out[(self.ix[x], j)] = ("io", x, y)
        return out

    def _via(self, x, y):
        v = self.st.v1.size / 2
        return R(x - v, y - v, x + v, y + v)

    def via_clear(self, x, y):
        """A VIA1 at (x, y) keeps the via1-contact keep-out."""
        sp = self.st.v1_cont_space
        if not sp:
            return True
        via = self._via(x, y)
        return all(_dist(via, r) >= sp - EPS for l, r, n in self.cl.rects if l == "cont")

    def cont_clear(self, cont):
        """A new contact keeps the keep-out to the access vias placed so far."""
        sp = self.st.v1_cont_space
        return not sp or all(_dist(cont, self._via(x, y)) >= sp - EPS for x, y in self.cl.access.values())

    def commit_terminal(self, net, term):
        st = self.st
        if term[0] == "gate":
            _, gi, y, x, rot = term
            poly, m1 = self.gate_pad(gi, y, x, rot)
            self.gates[gi]["pad"] = (y, x)
            c = st.r["cont_size"]
            self.cl.add("poly", poly, net)
            self.cl.add("cont", R(x - c / 2, y - c / 2, x + c / 2, y + c / 2), net)
            self.cl.add("m1", m1, net)
        elif term[0] == "io":
            _, x, y = term
            self.cl.add("m1", R(x - self.apad[0] / 2, y - self.apad[1] / 2, x + self.apad[0] / 2, y + self.apad[1] / 2), net)
            self.used_tracks[x] = net
            self.cl.access[net] = (x, y)

    # ---- one net
    def route_net(self, net, terms):
        """terms: list of ("stub", rect) | ("gate", gi) | ("io",)."""
        self.is_input = not any(t[0] == "stub" for t in terms)
        fixed = [t for t in terms if t[0] == "stub"]
        virt = [t for t in terms if t[0] != "stub"]
        tree = set()
        pending = []
        for t in fixed:
            nodes = self.stub_nodes(t[1])
            if not nodes:
                raise LayoutError("%s: no grid node on a strap of %s" % (self.cl.name, net))
            pending.append(("stub", nodes, t[1]))
        self.unconnected = {t[2] for t in pending[1:]} if pending else set()
        pending += virt
        first = pending.pop(0)
        if first[0] == "stub":
            tree = set(first[1])
            src_virtual = None
        else:
            src_virtual = self._cands(net, first)
            if not src_virtual:
                return False
        while pending:
            targets = {}
            for k, t in enumerate(pending):
                if t[0] == "stub":
                    for n in t[1]:
                        targets[n] = (k, None)
                else:
                    for n, term in self._cands(net, t).items():
                        targets[n] = (k, term)
            if not targets:
                return False
            sources = tree if src_virtual is None else set(src_virtual)
            accept = None
            if src_virtual is not None and self.st.v1_cont_space:
                # both ends are still free: a poly contact and an access via must keep the keep-out
                accept = lambda root, goal: self._pair_ok(src_virtual[root], targets[goal][1])
            path = self._astar(net, sources, targets, accept)
            if path is None:
                return False
            if src_virtual is not None:
                self.commit_terminal(net, src_virtual[path[0]])
                src_virtual = None
            k, term = targets[path[-1]]
            self._commit_path(net, path)
            if term is not None:
                self.commit_terminal(net, term)
            t = pending.pop(k)
            tree |= set(path)
            if t[0] == "stub":
                tree |= set(t[1])
                self.unconnected.discard(t[2])
        self.unconnected = set()
        if src_virtual is not None:             # single virtual terminal: nothing to connect
            return False
        return True

    def _pair_ok(self, a, b):
        """A poly contact and an access via picked in the same step keep the via1-contact keep-out."""
        sp = self.st.v1_cont_space
        if not sp or a is None or b is None:
            return True
        c = self.st.r["cont_size"] / 2
        conts = [R(t[3] - c, t[2] - c, t[3] + c, t[2] + c) for t in (a, b) if t[0] == "gate"]
        vias = [self._via(t[1], t[2]) for t in (a, b) if t[0] == "io"]
        return all(_dist(cr, v) >= sp - EPS for cr in conts for v in vias)

    def _cands(self, net, t):
        if t[0] == "gate":
            return self.gate_candidates(t[1], net)
        return self.io_candidates(net)

    def _astar(self, net, sources, targets, accept=None):
        NX, NY = len(self.xs), len(self.ys)
        tg = list(targets)

        def h(n):
            x, y = self.node_xy(n)
            return min(abs(x - self.xs[a]) + abs(y - self.ys[b]) for a, b in tg)

        cache = {}

        def edge_ok(a, b):
            key = (a, b) if a < b else (b, a)
            if key not in cache:
                cache[key] = self.legal_other(self.edge_rect(a, b), net)
            if not cache[key]:
                return False
            chain, n = [], a                    # the last edges of the path fill gaps to own shapes
            while n in prev and len(chain) < 6:
                chain.append(self.edge_rect(prev[n], n))
                n = prev[n]
            return self.legal_same(self.edge_rect(a, b), net, chain)

        dist, prev, pq = {}, {}, []
        for s in sources:
            dist[s] = 0.0
            heapq.heappush(pq, (h(s), 0.0, s))
        while pq:
            f, g, u = heapq.heappop(pq)
            if g > dist.get(u, 1e18) + EPS:
                continue
            if u in targets:
                path = [u]
                while path[-1] in prev:
                    path.append(prev[path[-1]])
                path.reverse()
                if accept is None or accept(path[0], u):
                    return path
            i, j = u
            for v in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                if not (0 <= v[0] < NX and 0 <= v[1] < NY):
                    continue
                if not edge_ok(u, v):
                    continue
                (x1, y1), (x2, y2) = self.node_xy(u), self.node_xy(v)
                turn = 0.0
                if u in prev:
                    p = prev[u]
                    if (p[0] == u[0]) != (u[0] == v[0]):
                        turn = 0.05
                cost = abs(x1 - x2) + abs(y1 - y2)
                if y1 == y2 and not self.is_input and self.st.pad_y[0] - EPS <= y1 <= self.st.pad_y[1] + EPS:
                    cost *= 4.0                   # keep the poly-contact window free for the inputs
                ng = g + cost + turn
                if ng < dist.get(v, 1e18) - EPS:
                    dist[v] = ng
                    prev[v] = u
                    heapq.heappush(pq, (ng + h(v), ng, v))
        return None

    def _commit_path(self, net, path):
        if len(path) < 2:
            return
        runs, start = [], path[0]
        for a, b, c in zip(path, path[1:], path[2:]):
            if (a[0] == b[0]) != (b[0] == c[0]):
                runs.append((start, b))
                start = b
        runs.append((start, path[-1]))
        for a, b in runs:
            self.cl.add("m1", self.edge_rect(a, b), net)


# ----------------------------------------------------------------------------- one attempt
def _attempt(st, cn, seq, extra_sites, cont_max, net_order_shift):
    elems, chains, right = _columns(st, cn, seq)
    io = cn.inputs + [cn.output]
    nsites = max(int(math.ceil(right / st.site_w - EPS)), 1) + extra_sites
    while _tracks(st, nsites) < len(io):
        nsites += 1
    W = r6(nsites * st.site_w)
    _shift(elems, chains, snap((W - right) / 2, st.mfg))
    cl = template(st, cn.name, nsites)
    cl.netlist = cn
    H, c = st.row_h, st.r["cont_size"]
    es, ee = sorted(st.r["m1_cont_enc"])
    for ch in chains:
        cl.add("activ", R(ch["x0"], st.nb, ch["x1"], st.nt))
        cl.add("activ", R(ch["x0"], st.pb, ch["x1"], st.pt))
    gates = {}
    for k, e in enumerate(elems):
        if e["t"] == "gate":
            gi = len(gates)
            gates[gi] = {"x": e["x"], "g": e["g"], "pad": None}
            cl.add("poly", R(e["x"] - st.L / 2, st.poly_bot, e["x"] + st.L / 2, st.poly_top), e["g"])
    stubs = {}
    for e in elems:
        if e["t"] != "slot":
            continue
        for row, (y0, y1), rail in (("n", (st.nb, st.nt), st.vss), ("p", (st.pb, st.pt), st.vdd)):
            if not e["c" + row]:
                continue
            net = e[row]
            # cont_max: None = all contacts, k = at most k on signal straps, -k = on every strap
            n = abs(cont_max) if cont_max and (cont_max < 0 or net != rail) else None
            ys = st.band_contacts(y0, y1, n)
            x = e["x"]
            for y in ys:
                cl.add("cont", R(x - c / 2, y - c / 2, x + c / 2, y + c / 2), net)
            sw = max(c + 2 * es, st.m1.width)
            rect = R(x - sw / 2, ys[0] - c / 2 - ee, x + sw / 2, ys[-1] + c / 2 + ee)
            if net == rail:                         # strap runs into the rail
                rect = R(rect[0], 0.0, rect[2], rect[3]) if row == "n" else R(rect[0], rect[1], rect[2], H)
            cl.add("m1", rect, net)
            if net != rail:
                stubs.setdefault(net, []).append(rect)
    # router grid
    w = st.m1.width
    tracks = [r6(st.x_off + st.gx * n) for n in range(int(W / st.gx) + 2)
              if EPS < st.x_off + st.gx * n < W - EPS]
    lo, hi = r6(st.m1_edge + w / 2), r6(W - st.m1_edge - w / 2)
    xs = sorted({e["x"] for e in elems if "x" in e} | set(tracks) | {lo, hi})
    xs = [x for x in xs if lo - EPS <= x <= hi + EPS]
    # extra columns between distant ones (room to pass beside pads near the cell edge)
    xs = sorted(set(xs) | {snap((a + b) / 2, st.mfg) for a, b in zip(xs, xs[1:]) if b - a > w + st.m1.space})
    step = snap((w + st.m1.space) / 4, st.mfg)
    ny = int(math.floor((st.m1_yhi - st.m1_ylo) / step + EPS)) + 1
    ys = [r6(st.m1_ylo + step * j) for j in range(ny)]
    rt = Router(st, cl, xs, ys, tracks, gates, io)
    nets = sorted(set(stubs) | {g["g"] for g in gates.values()})
    terms = {}
    for net in nets:
        t = [("stub", r) for r in stubs.get(net, [])]
        t += [("gate", gi) for gi, g in gates.items() if g["g"] == net]
        if net in io:
            t.append(("io",))
        terms[net] = t
    order = sorted(nets, key=lambda n: (n in stubs, -len(terms[n]), n))
    order = _net_orders(order)[net_order_shift % len(_net_orders(order))]
    for net in order:
        if not rt.route_net(net, terms[net]):
            raise LayoutError("%s: could not route %s" % (cn.name, net))
    _fill_notches(rt)
    _fix_min_area(rt)
    _final_check(st, cl)
    cl.pins = io + [st.vdd, st.vss]
    for net in io:
        x, y = cl.access[net]
        cl.texts.append(("m1_text", net, x, y))
    cl.info = {"kind": "logic", "sites": nsites, "order": [e["g"] for e in elems if e["t"] == "gate"],
               "breaks": sum(1 for e in elems if e["t"] == "break")}
    return cl


def _net_orders(order, k=24):
    """Heuristic order, its reverse, then permutations spread evenly over all of them."""
    perms = list(itertools.islice(itertools.permutations(order), 5040))
    stride = max(1, len(perms) // k)
    out = [list(order), list(order[::-1])]
    for p in perms[::stride]:
        if list(p) not in out:
            out.append(list(p))
    return out[:k]


def _fill_notches(rt):
    """Close gaps narrower than the spacing between shapes of one net (where legal)."""
    geo, cl = rt.geo, rt.cl
    for _ in range(6):
        added = False
        sh = cl.m1()
        by_net = {}
        for r, n in sh:
            by_net.setdefault(n, []).append(r)
        for net, rs in by_net.items():
            for i, a in enumerate(rs):
                for b in rs[i + 1:]:
                    if geo.ok(a, b, True, rs):
                        continue
                    ox = min(a[2], b[2]) - max(a[0], b[0])
                    oy = min(a[3], b[3]) - max(a[1], b[1])
                    if ox > 0:
                        box = R(max(a[0], b[0]), min(a[3], b[3]), min(a[2], b[2]), max(a[1], b[1]))
                    elif oy > 0:
                        box = R(min(a[2], b[2]), max(a[1], b[1]), max(a[0], b[0]), min(a[3], b[3]))
                    else:
                        continue
                    if rt.legal_other(box, net):
                        cl.add("m1", box, net)
                        rs.append(box)
                        added = True
        if not added:
            return


def _fix_min_area(rt):
    """Stretch the tallest shape of a too-small metal-1 island up/down until it meets min area."""
    st, cl = rt.st, rt.cl
    need = st.m1.min_area
    if not need:
        return
    by_net = {}
    for r, n in cl.m1():
        by_net.setdefault(n, []).append(r)
    for net, rs in by_net.items():
        for comp in _components(rs):
            a = _area(comp)
            if a >= need - EPS:
                continue
            base = max(comp, key=lambda r: r[3] - r[1])
            w = base[2] - base[0]
            step = st.mfg * 2
            done = False
            for k in range(1, 200):
                ext = k * step
                for lo, hi in ((ext, 0.0), (0.0, ext), (ext / 2, ext / 2)):
                    r = R(base[0], base[1] - lo, base[2], base[3] + hi)
                    if r[1] < st.m1_ylo - rt.w / 2 - EPS or r[3] > st.m1_yhi + rt.w / 2 + EPS:
                        continue
                    if a + (lo + hi) * w < need - EPS:
                        continue
                    if rt.legal_other(r, net) and _area(comp + [r]) >= need - EPS:
                        cl.add("m1", r, net)
                        done = True
                        break
                if done:
                    break


def _tracks(st, nsites):
    W = nsites * st.site_w
    return sum(1 for n in range(int(W / st.gx) + 2) if EPS < st.x_off + st.gx * n < W - EPS)


def _final_check(st, cl):
    geo = Geo(st)
    sh = cl.m1()
    by_net = {}
    for r, n in sh:
        by_net.setdefault(n, []).append(r)
    for i, (a, na) in enumerate(sh):
        for b, nb in sh[i + 1:]:
            if not geo.ok(a, b, na == nb, by_net[na] if na == nb else None):
                raise LayoutError("%s: metal-1 conflict %s %s / %s %s" % (cl.name, na, a, nb, b))
    if st.m1.min_area:
        for net, rs in by_net.items():
            for comp in _components(rs):
                if _area(comp) < st.m1.min_area - EPS:
                    raise LayoutError("%s: metal-1 of %s below min area" % (cl.name, net))


def _components(rects):
    comps = []
    for r in rects:
        hit = [c for c in comps if any(min(r[2], q[2]) >= max(r[0], q[0]) and min(r[3], q[3]) >= max(r[1], q[1]) for q in c)]
        merged = [r] + [q for c in hit for q in c]
        comps = [c for c in comps if c not in hit] + [merged]
    return comps


def _area(rects):
    xs = sorted({v for r in rects for v in (r[0], r[2])})
    ys = sorted({v for r in rects for v in (r[1], r[3])})
    a = 0.0
    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            cx, cy = (xs[i] + xs[i + 1]) / 2, (ys[j] + ys[j + 1]) / 2
            if any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in rects):
                a += (xs[i + 1] - xs[i]) * (ys[j + 1] - ys[j])
    return a


def make_logic(st, cn, max_orders=8):
    """Place and route cell `cn` (:class:`dla.stdcell.netlist.CellNetlist`)."""
    nbreak, seqs = euler.orders(cn)
    scored = []
    for s in seqs:
        _, _, right = _columns(st, cn, s)
        scored.append((right, len(scored), s))
    scored.sort(key=lambda t: t[:2])
    last = None
    nnets = len(set(cn.internal) | set(cn.inputs) | {cn.output})
    nperm = min(24, math.factorial(nnets))
    for extra in (0, 1, 2):
        for cont_max in (None, 1, -1):
            for _, _, seq in scored[:max_orders]:
                for shift in range(nperm):
                    try:
                        return _attempt(st, cn, seq, extra, cont_max, shift)
                    except LayoutError as e:
                        last = e
    raise LayoutError("%s: no routable layout (%s)" % (cn.name, last))
