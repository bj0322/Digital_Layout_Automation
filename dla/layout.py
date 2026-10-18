"""Block canvas shared by every block generator: row placement, fixed wiring, A* maze router.

All geometry comes from the :class:`dla.tech.Tech`: cells sit on sites, the vertical
routing layer runs on tracks x = x_off + gx * n, the horizontal layer on grid lines
y = y_min + gy * j. The pitches are derived from the design rules so that any two
different nets on neighbouring grid nodes are DRC clean; the router therefore only
tracks node ownership.

A block generator (see ``dla/blocks``) typically
  1. creates a :class:`Block` with its width (sites) and y range,
  2. adds rows and places cells by *role* with :meth:`Block.place`,
  3. draws its regular wiring itself (:meth:`m2`, :meth:`m3`, :meth:`via1`, ...),
     claiming grid nodes with :meth:`take` / :meth:`block_rect`,
  4. calls :meth:`power_straps`, :meth:`maze_route` and :meth:`fill_rows`,
  5. hands the block to :mod:`dla.export`.
"""
import heapq
import math

from .tech import EPS, load

BLOCKED = -1
V, H = 0, 1                     # router layer index: vertical / horizontal routing layer


class RoutingError(RuntimeError):
    pass


def r6(v):
    return round(v, 6)


class Block:
    def __init__(self, name, nsites, y_min, y_max, tech=None):
        self.t = tech or load()
        t = self.t
        self.name = name
        self.nsites = nsites
        self.width = r6(nsites * t.site_w)
        self.y_min, self.y_max = r6(y_min), r6(y_max)
        # tracks whose via pad stays inside the block
        half = max(t.pad_v1_m2[0], t.pad_v2_m2[0]) / 2
        self.n_lo = int(math.ceil((half - t.x_off) / t.gx - EPS))
        self.ntrk = int(math.floor((self.width - half - t.x_off) / t.gx + EPS)) + 1
        self.nj = int(math.floor((self.y_max - self.y_min) / t.gy + EPS)) + 1
        self.insts = []                          # (name, cell, x, ybase, orient)
        self.roles = {}                          # inst -> role
        self.conn = {}                           # net -> [(inst, cell pin)]
        self.pinpos = {}                         # (inst, pin) -> (x, y) VIA1 position
        self.patch = {}                          # (inst, pin) -> (w, h) of an M1 access patch
        self.rows = {}                           # ybase -> orient
        self.used = {}                           # ybase -> [(s0, s1)]
        self.pins, self.ports, self.meta = [], [], {}
        self.own = [[[None] * self.nj for _ in range(self.ntrk)] for _ in range(2)]
        for n in range(min(self.n_lo, self.ntrk)):
            for l in (V, H):
                self.own[l][n] = [BLOCKED] * self.nj
        self.novia = set()                       # nodes where a VIA2 would violate via1 spacing
        self.shapes = []                         # (layer, x1, y1, x2, y2, net), layer in m1/m2/m3
        self.vias = []                           # (via1|via2, x, y, net)

    # ------------------------------------------------------------------ coordinates
    def tx(self, n):
        return r6(self.t.x_off + self.t.gx * n)

    def tn(self, x):
        n = (x - self.t.x_off) / self.t.gx
        assert abs(n - round(n)) < 1e-4, x
        return int(round(n))

    def yj(self, y):
        j = (y - self.y_min) / self.t.gy
        assert abs(j - round(j)) < 1e-4, y
        return int(round(j))

    def jy(self, j):
        return r6(self.y_min + self.t.gy * j)

    def nearest_j(self, y):
        return int(round((y - self.y_min) / self.t.gy))

    def pin_nodes(self, y):
        """Grid nodes a pin owns on its track: every node closer than one pitch, widened so the
        pin stub meets the vertical layer's minimum area and stays clear of other nets' pads."""
        t = self.t
        reach = max(t.gy - EPS, t.m2_min_len / 2 + t.m2.space + max(t.pad_v1_m2[1], t.pad_v2_m2[1]) / 2 - EPS)
        js = [j for j in range(int((y - reach - self.y_min) / t.gy) - 1, int((y + reach - self.y_min) / t.gy) + 2)
              if 0 <= j < self.nj and abs(self.jy(j) - y) < reach]
        return js or [self.nearest_j(y)]

    # ------------------------------------------------------------------ placement
    def add_row(self, ybase, orient):
        assert orient in ("R0", "MX")
        self.rows[r6(ybase)] = orient

    def cell_of(self, role):
        return self.t.cells[role]["cell"] if role in self.t.cells else role

    def place(self, name, role, site, ybase, pins=None):
        """Place the cell for `role` (or a cell name) at `site` in the row at `ybase`.

        pins maps logical pin (A, B, C, Y) -> net.
        """
        t = self.t
        ybase = r6(ybase)
        orient = self.rows[ybase]
        cell = self.cell_of(role)
        w = t.width_sites(cell)
        assert 0 <= site and site + w <= self.nsites, (name, site, w, self.nsites)
        x = r6(site * t.site_w)
        self.insts.append((name, cell, x, ybase, orient))
        self.roles[name] = role
        self.used.setdefault(ybase, []).append((site, site + w))
        if pins:
            acc = t.access(cell)
            pmap = t.cells[role]["pins"]
            for lp, net in pins.items():
                p = pmap[lp]
                ax, ay, patch = acc[p]
                py = ybase + (ay if orient == "R0" else t.row_h - ay)
                self.pinpos[(name, p)] = (r6(x + ax), r6(py))
                if patch:
                    self.patch[(name, p)] = patch
                self.conn.setdefault(net, []).append((name, p))
        return x

    def _row_of(self, inst):
        for name, cell, x, yb, o in self.insts:
            if name == inst:
                return yb
        raise KeyError(inst)

    def pin(self, inst, lpin):
        """Absolute VIA1 position of logical pin `lpin` of `inst`."""
        return self.pinpos[(inst, self.t.cells[self.roles[inst]]["pins"][lpin])]

    def pin_track(self, inst, lpin):
        return self.tn(self.pin(inst, lpin)[0])

    def track_offset(self, role, lpin):
        """Track index of a pin relative to the cell's first site (same for every placement)."""
        cell = self.cell_of(role)
        x = self.t.access(cell)[self.t.cells[role]["pins"][lpin]][0]
        return int(round((x - self.t.x_off) / self.t.gx))

    def place_fill(self, prefix, ybase, s0, s1):
        s = s0
        k = 0
        while s < s1:
            for c in self.t.fill:
                w = self.t.width_sites(c)
                if w <= s1 - s:
                    break
            else:
                raise RuntimeError("no filler fits %d sites" % (s1 - s))
            self.place("%s_%d" % (prefix, k), c, s, ybase)
            k += 1
            s += w

    def straps(self, cols, width=4):
        """Filler columns (sites cols[i] .. +width) on every row; power straps run over them."""
        self.strap_cols, self.strap_w = list(cols), width
        for yb in sorted(self.rows):
            for s in self.strap_cols:
                self.place_fill("strap_%d_%d" % (s, int(round(yb * 1000))), yb, s, s + width)

    def strap_tracks(self, s0):
        """Two tracks strictly inside the strap column starting at site s0 (VDD, VSS)."""
        x0, x1 = s0 * self.t.site_w, (s0 + self.strap_w) * self.t.site_w
        inner = [n for n in range(self.ntrk) if x0 + EPS < self.tx(n) < x1 - EPS and self.tx(n) >= self.tx(self.n_lo) - EPS]
        if len(inner) < 3:
            raise RuntimeError("strap column too narrow for two power straps")
        return inner[0], inner[2]

    def fill_rows(self):
        """Fill every free site range so the rails run edge to edge."""
        for yb in sorted(self.rows):
            occ = [False] * self.nsites
            for s0, s1 in self.used.get(yb, []):
                for s in range(s0, s1):
                    assert not occ[s], ("overlap", yb, s)
                    occ[s] = True
            s, k = 0, 0
            while s < self.nsites:
                if occ[s]:
                    s += 1
                    continue
                e = s
                while e < self.nsites and not occ[e]:
                    e += 1
                self.place_fill("fill_%d_%d" % (int(round(yb * 1000)), k), yb, s, e)
                k += 1
                s = e

    # ------------------------------------------------------------------ grid ownership
    def take(self, l, n, j, net):
        cur = self.own[l][n][j]
        if cur is not None and cur != net:
            raise RoutingError("grid conflict on %s at (%.3f, %.3f): %s vs %s"
                               % ("V" if l == V else "H", self.tx(n), self.jy(j), cur, net))
        self.own[l][n][j] = net

    def take_span(self, l, n0, n1, j0, j1, net):
        for n in range(min(n0, n1), max(n0, n1) + 1):
            for j in range(min(j0, j1), max(j0, j1) + 1):
                self.take(l, n, j, net)

    def block_rect(self, l, n0, n1, j0, j1):
        for n in range(max(0, n0), min(self.ntrk, n1 + 1)):
            for j in range(max(0, j0), min(self.nj, j1 + 1)):
                if self.own[l][n][j] is None:
                    self.own[l][n][j] = BLOCKED

    # ------------------------------------------------------------------ drawing
    def rect(self, layer, x1, y1, x2, y2, net):
        self.shapes.append((layer, r6(min(x1, x2)), r6(min(y1, y2)), r6(max(x1, x2)), r6(max(y1, y2)), net))

    def m2(self, x, y1, y2, net, w=None):
        w = w or self.t.m2.width
        y1, y2 = min(y1, y2), max(y1, y2)
        self.rect("m2", x - w / 2, y1 - w / 2, x + w / 2, y2 + w / 2, net)

    def m3(self, y, x1, x2, net, w=None):
        w = w or self.t.m3.width
        x1, x2 = min(x1, x2), max(x1, x2)
        self.rect("m3", x1 - w / 2, y - w / 2, x2 + w / 2, y + w / 2, net)

    def pad(self, layer, x, y, wh, net):
        self.rect(layer, x - wh[0] / 2, y - wh[1] / 2, x + wh[0] / 2, y + wh[1] / 2, net)

    def via1(self, x, y, net, m1pad=False):
        self.vias.append(("via1", r6(x), r6(y), net))
        self.pad("m2", x, y, self.t.pad_v1_m2, net)
        if m1pad:
            self.pad("m1", x, y, self.t.pad_v1_m1, net)

    def via2(self, x, y, net):
        self.vias.append(("via2", r6(x), r6(y), net))
        self.pad("m2", x, y, self.t.pad_v2_m2, net)
        self.pad("m3", x, y, self.t.pad_v2_m3, net)

    def add_pin(self, name, x, y):
        self.pins.append({"name": name, "x": r6(x), "y": r6(y)})

    # ------------------------------------------------------------------ pins
    def _mark_novia(self, x, y):
        """Forbid VIA2 near a VIA1 when the tech does not allow stacked / close vias."""
        t = self.t
        if t.via_stack and not t.via12_space:
            return
        lim = t.v1.size / 2 + t.v2.size / 2 + t.via12_space
        n = self.tn(x)
        for j in self.pin_nodes(y):
            if abs(self.jy(j) - y) < lim - EPS:
                self.novia.add((n, j))

    def pin_stub(self, inst, pin, net):
        """VIA1 on a cell pin plus a vertical-layer stub over the pin's grid nodes."""
        x, y = self.pinpos[(inst, pin)]
        js = self.pin_nodes(y)
        self.via1(x, y, net)
        if (inst, pin) in self.patch:
            dx0, dy0, dx1, dy1 = self.patch[(inst, pin)]
            if self.rows[self._row_of(inst)] == "MX":
                dy0, dy1 = -dy1, -dy0
            self.rect("m1", x + dx0, y + dy0, x + dx1, y + dy1, net)
        y0, y1 = min(self.jy(js[0]), y), max(self.jy(js[-1]), y)
        if self.t.m2_min_len and y1 - y0 + self.t.m2.width < self.t.m2_min_len:
            ext = (self.t.m2_min_len - (y1 - y0 + self.t.m2.width)) / 2
            y0, y1 = y0 - ext, y1 + ext
        self.m2(x, y0, y1, net)
        self._mark_novia(x, y)
        return self.tn(x), js

    def pin_drop(self, inst, lpin, y_to, net):
        """pin_stub + vertical wire to y_to; claims the nodes. Returns (x, y)."""
        pin = self.t.cells[self.roles[inst]]["pins"][lpin]
        x, y = self.pinpos[(inst, pin)]
        n, js = self.pin_stub(inst, pin, net)
        self.m2(x, y, y_to, net)
        j_to = self.yj(y_to)
        self.take_span(V, n, n, min(js[0], j_to), max(js[-1], j_to), net)
        return x, y

    # ------------------------------------------------------------------ power
    def power_straps(self):
        """Vertical-layer straps over the strap columns, VIA1 onto every row rail."""
        t = self.t
        rails = {}
        for yb, o in self.rows.items():
            for net in (t.vdd, t.vss):
                yc, _ = t.rail[net]
                y = r6(yb + (yc if o == "R0" else t.row_h - yc))
                assert rails.get(y, net) == net, "rows at y=%.3f disagree on the rail" % y
                rails[y] = net
        ylo, yhi = self.y_min, max(rails)
        w = max(t.pad_v1_m2[0], t.m2.width)
        self.power_tracks = []
        for s in self.strap_cols:
            for n, net in zip(self.strap_tracks(s), (t.vdd, t.vss)):
                x = self.tx(n)
                for j in range(self.nj):
                    self.take(V, n, j, net)
                self.m2(x, ylo, yhi, net, w=w)
                for y, rnet in rails.items():
                    if rnet == net:
                        self.via1(x, y, net, m1pad=True)
                self.power_tracks.append((n, net))
        for n, net in self.power_tracks[:2]:
            self.add_pin(net, self.tx(n), ylo + t.gy)

    # ------------------------------------------------------------------ maze router
    def maze_route(self, skip, io=None, attempts=20):
        """Route every net not skipped (set of names or predicate) with A*.

        io: {net: track} - an extra terminal on the bottom edge (j = 0) for block inputs.
        """
        io = io or {}
        is_skipped = skip if callable(skip) else (lambda n: n in skip)
        terms = {}
        for net, ips in self.conn.items():
            if is_skipped(net):
                continue
            terms[net] = []
            for ip in ips:
                x, y = self.pinpos[ip]
                terms[net].append(("pin", ip, self.tn(x), self.pin_nodes(y)))
                self._mark_novia(x, y)
        for net, tl in terms.items():
            for _, _, n, js in tl:
                for j in js:
                    self.take(V, n, j, net)
        bottom = self.pin_nodes(self.y_min)
        for net, n in io.items():
            for j in bottom:
                self.take(V, n, j, net)
            terms.setdefault(net, []).append(("io", net, n, bottom))

        order = sorted(terms, key=lambda nt: -len(terms[nt]))
        failed = None
        for attempt in range(attempts):
            snapshot = [[col[:] for col in lay] for lay in self.own]
            paths, failed = {}, None
            for net in order:
                p = self._route_net(net, terms[net])
                if p is None:
                    failed = net
                    break
                paths[net] = p
            if failed is None:
                break
            self.own = snapshot
            order.remove(failed)
            order.insert(0, failed)
        else:
            raise RoutingError("maze routing failed (last: %s)" % failed)
        self.route_attempts = attempt + 1

        for net, (edges, vias, tl) in paths.items():
            for (l, n1, j1), (_, n2, j2) in edges:
                if l == V:
                    self.m2(self.tx(n1), self.jy(j1), self.jy(j2), net)
                else:
                    self.m3(self.jy(j1), self.tx(n1), self.tx(n2), net)
            for n, j in vias:
                self.via2(self.tx(n), self.jy(j), net)
            for kind, ip, n, js in tl:
                if kind == "pin":
                    self.pin_stub(ip[0], ip[1], net)
                else:
                    x = self.tx(n)
                    self.m2(x, self.jy(js[0]), self.jy(js[-1]), net)
                    self.add_pin(net, x, self.jy(js[0]))

    def _route_net(self, net, tl):
        """Grow a tree from the first terminal, A* to the nearest remaining terminal each time."""
        own, NT, NJ, novia = self.own, self.ntrk, self.nj, self.novia
        hc = 1.07 * self.t.gx / self.t.gy           # cost of one horizontal step
        tree = set((V, tl[0][2], j) for j in tl[0][3])
        edges, vias = [], set()
        todo = list(tl[1:])
        while todo:
            targets = {}
            for t in todo:
                for j in t[3]:
                    targets[(V, t[2], j)] = t
            tg = list(set((k[1], k[2]) for k in targets))

            def h(n, j):
                return min(abs(n - a) * hc + abs(j - b) for a, b in tg)

            dist, prev, pq = {}, {}, []
            for s in tree:
                dist[s] = 0.0
                heapq.heappush(pq, (h(s[1], s[2]), 0.0, s))
            hit = None
            while pq:
                f, gc, u = heapq.heappop(pq)
                if gc > dist.get(u, 1e18):
                    continue
                if u in targets:
                    hit = u
                    break
                l, n, j = u
                if l == V:
                    nbrs = [((V, n, j + 1), 1.0), ((V, n, j - 1), 1.0)]
                else:
                    nbrs = [((H, n + 1, j), hc), ((H, n - 1, j), hc)]
                if (n, j) not in novia:
                    nbrs.append(((1 - l, n, j), 4.0))
                for v, c in nbrs:
                    vl, vn, vj = v
                    if not (0 <= vn < NT and 0 <= vj < NJ):
                        continue
                    o = own[vl][vn][vj]
                    if o is not None and o != net:
                        continue
                    if vl != l and (own[V][vn][vj] not in (None, net) or own[H][vn][vj] not in (None, net)):
                        continue        # a via needs both layers free for this net
                    ng = gc + c
                    if ng < dist.get(v, 1e18):
                        dist[v] = ng
                        prev[v] = u
                        heapq.heappush(pq, (ng + h(vn, vj), ng, v))
            if hit is None:
                return None
            t = targets[hit]
            todo.remove(t)
            u = hit
            while u not in tree:
                p = prev[u]
                if p[0] != u[0]:
                    vias.add((u[1], u[2]))
                else:
                    edges.append((p, u))
                tree.add(u)
                own[u[0]][u[1]][u[2]] = net
                u = p
            for j in t[3]:
                tree.add((V, t[2], j))
        return edges, sorted(vias), tl

    # ------------------------------------------------------------------ summary
    def stats(self):
        logic = [i for i in self.insts if self.roles.get(i[0]) in self.t.cells]
        return {"cells": len(logic), "fill": len(self.insts) - len(logic),
                "shapes": len(self.shapes), "vias": len(self.vias),
                "width_um": r6(self.boundary[2] - self.boundary[0]),
                "height_um": r6(self.boundary[3] - self.boundary[1])}
