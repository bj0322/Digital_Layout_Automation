"""6T SRAM bitcell and R x C four-way mirrored array generator.

    from dla import make_sram
    res = make_sram("tech/ihp_sg13g2.json", "out/sram_4x4", rows=4, cols=4, name="sram_4x4")

Everything PDK specific comes from the tech file (``stdcell`` rules, optional ``sram``
section, M2 / M3 / VIA1 / VIA2 rules).

Bitcell topology (4 gate columns, one rectangular active island per transistor)::

        Col 0 INV_L        Col 1 PG_L        Col 2 PG_R        Col 3 INV_R
    n:  VSS [QB] Q         Q [WL] BL         BLB [WL] QB       QB [Q] VSS
    p:  VDD [QB] Q                                             QB [Q] VDD

The two pass-gates sit next to each other in the middle, so the two WL gates join with a
single poly bar. Q (QB) is a vertical metal-1 bar on the inner diffusion of its inverter;
the gate contact of the *opposite* inverter sits on a poly arm that reaches across that bar,
so the two cross-coupling wires never cross each other.

* :func:`size_6t`           minimum legal quantized widths meeting the CR / PR targets
* :func:`check_sram_cell`   heuristic ratio-model hold / read / write sanity check

The electrical figures (``static_metrics``, ``check_sram_cell``) are fixed-MU_P / fixed-VM
first-order resistive-divider *heuristic ratio models*. They are NOT PDK MOS-model SPICE DC /
transient SNM, write-margin, delay or power results; those need SPICE with the PDK device models.
* :func:`make_sram_cell`    bitcell layout       * :func:`make_sram_array`  array routing
* :func:`make_sram`         everything + views   (``python gen.py sram``)
"""
import copy
import json
import math
import numbers
import os
import time

from .stdcell import cell as C
from .stdcell import writers
from .stdcell.cell import CellLayout, Geo, LayoutError, R, r6
from .stdcell.netlist import Transistor
from .stdcell.rules import StdTech, snap, snap_dn, snap_up
from .tech import EPS

ORIENTS = writers.ORIENTS
PINS = ("BL", "BLB", "WL")


def orient_of(r, c):
    """4-way mirroring of the array: even rows R0 / MY, odd rows MX / R180."""
    return {(0, 0): "R0", (0, 1): "MY", (1, 0): "MX", (1, 1): "R180"}[(r % 2, c % 2)]


# ============================================================================= sizing
class Sizing:
    """Transistor sizes (um) and the static figures of merit of one 6T cell."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    def __repr__(self):
        return ("Sizing(w_pu=%g, w_pg=%g, w_pd=%g, l=%g, cr=%.3f, pr=%.3f)"
                % (self.w_pu, self.w_pg, self.w_pd, self.l, self.cr, self.pr))


VM = 0.5          # fixed inverter switching threshold (fraction of VDD) of the heuristic ratio model
MU_P = 0.4        # fixed PMOS drive strength per W/L relative to NMOS (heuristic, not from the PDK)


def static_metrics(w_pu, w_pg, w_pd, l_pu, l_pg, l_pd, mu_p=MU_P, vm=VM):
    """Dimensionless figures of the *heuristic ratio model* (fixed MU_P / VM resistive dividers).

    Not a PDK MOS-model SPICE DC / transient SNM, write-margin, delay or power result: true
    characterization requires SPICE simulation with the PDK device models.

    cr / pr: cell ratio / pull-up ratio (W/L based). v_read: voltage the '0' node rises to
    while the bit line (precharged high) is connected, v_write: voltage the pulled-down '1'
    node keeps against a bit line at 0. snm_read_factor / write_margin_factor are
    (vm - v) / vm: positive = stable / writable. snm_hold_ratio is the symmetry of the
    inverter trip point (1 = trip point at vm).
    """
    kd, kg, ku = w_pd / l_pd, w_pg / l_pg, mu_p * w_pu / l_pu
    cr = kd / kg
    pr = (w_pu / l_pu) / kg
    v_read = kg / (kg + kd)
    v_write = ku / (ku + kg)
    r = math.sqrt(ku / kd)
    trip = r / (1.0 + r)
    return {"model": "heuristic_ratio", "cr": cr, "pr": pr, "v_read": v_read, "v_write": v_write,
            "snm_hold_ratio": min(trip, 1 - trip) / 0.5,
            "snm_read_factor": (vm - v_read) / vm,
            "write_margin_factor": (vm - v_write) / vm}


def cell_tech(st, sz):
    """StdTech whose vertical template (bands, n-well and implant edges, poly-contact window)
    is derived with the NMOS band as tall as the widest NMOS and the PMOS band W_PU high,
    using the very same inequalities as ``StdTech._derive``."""
    t = copy.copy(st)
    t.wn, t.wp, t.L = r6(max(sz.w_pd, sz.w_pg)), sz.w_pu, sz.l
    t.clear_rail = True
    t._derive()
    return t


def _check_positive(name, v):
    """None (omitted) passes; anything else must be a finite real number > 0."""
    if v is None:
        return
    if isinstance(v, bool) or not isinstance(v, numbers.Real) or not math.isfinite(v) or v <= 0:
        raise ValueError("%s must be a finite number > 0, got %r" % (name, v))


def _check_count(name, v):
    if isinstance(v, bool) or not isinstance(v, numbers.Integral) or v < 1:
        raise ValueError("%s must be an integer >= 1, got %r" % (name, v))


def size_6t(st, cr=None, pr=None, w_pu=None, w_pg=None, w_pd=None, l=None):
    """Find the minimum legal quantized widths (w_pu, w_pg, w_pd) satisfying cell ratio >= cr
    and pull-up ratio <= pr plus the single-contact / minimum-width floors.

    This is a constraint-satisfaction sizing, not a delay or power optimum, and the ratios are
    those of the heuristic ratio model (see :func:`static_metrics`). Widths snap up to 2*mfg and
    never fall below the legal transistor width or the width that holds one contact
    (c + 2 * cont_activ_enc). Every numeric argument that is given must be a finite number > 0
    (bool, nan, inf, non-positive -> ValueError). Raises ValueError if the result does not meet
    the ratios or does not fit the row height.
    """
    st = st if isinstance(st, StdTech) else StdTech(st)
    sr = st.sram
    for k, v in (("cr", cr), ("pr", pr), ("l", l), ("w_pu", w_pu), ("w_pg", w_pg), ("w_pd", w_pd),
                 ("sram.cr", sr.get("cr")), ("sram.pr", sr.get("pr")), ("sram.l", sr.get("l")),
                 ("sram.min_nmos_width", st.min_nmos_width), ("sram.min_pmos_width", st.min_pmos_width)):
        _check_positive(k, v)
    cr = float(cr if cr is not None else sr.get("cr", 1.5))
    pr = float(pr if pr is not None else sr.get("pr", 1.0))
    L = float(l if l is not None else (sr["l"] if "l" in sr and sr["l"] is not None else st.L))
    if L < st.r["poly_width"] - EPS:
        raise ValueError("gate length %.3f below poly_width %.3f" % (L, st.r["poly_width"]))
    q = 2 * st.mfg
    cmin = st.r["cont_size"] + 2 * st.r["cont_activ_enc"]
    lpu = lpg = lpd = L
    wpu = snap_up(max(w_pu or 0.0, st.min_pmos_width, cmin), q)
    wpg = snap_up(max(w_pg or 0.0, st.min_nmos_width, cmin, wpu * lpg / (pr * lpu)), q)
    wpd = snap_up(max(w_pd or 0.0, st.min_nmos_width, cmin, cr * wpg * lpd / lpg), q)
    m = static_metrics(wpu, wpg, wpd, lpu, lpg, lpd)
    if m["cr"] < cr - 1e-6 or m["pr"] > pr + 1e-6:
        raise ValueError("sizing misses the targets: cr %.3f (>= %.3f), pr %.3f (<= %.3f)"
                         % (m["cr"], cr, m["pr"], pr))
    sz = Sizing(w_pu=wpu, w_pg=wpg, w_pd=wpd, l=L, l_pu=lpu, l_pg=lpg, l_pd=lpd,
                cr_target=cr, pr_target=pr, **m)
    cell_tech(st, sz)           # raises ValueError if the row cannot hold these bands
    return sz


# ============================================================================= netlist
SRAM_PINS = ["BL", "BLB", "WL"]


class SramNetlist:
    """Transistor netlist of the 6T cell (devices are :class:`Transistor`: name kind g a b w)."""

    def __init__(self, name, sz, vdd="VDD", vss="VSS"):
        self.name, self.sz, self.vdd, self.vss = name, sz, vdd, vss
        self.pins = SRAM_PINS + [vdd, vss]
        self.inputs, self.outputs = ["WL"], ["BL", "BLB"]
        self.l = sz.l
        self.devices = [
            Transistor("MP_PUL", "p", "QB", vdd, "Q", sz.w_pu),
            Transistor("MN_PDL", "n", "QB", "Q", vss, sz.w_pd),
            Transistor("MP_PUR", "p", "Q", vdd, "QB", sz.w_pu),
            Transistor("MN_PDR", "n", "Q", "QB", vss, sz.w_pd),
            Transistor("MN_PGL", "n", "WL", "BL", "Q", sz.w_pg),
            Transistor("MN_PGR", "n", "WL", "BLB", "QB", sz.w_pg),
        ]

    def spice(self, nmos, pmos, l=None, units="u"):
        l = l or self.l
        fmt = (lambda v: "%gu" % v) if units == "u" else (lambda v: "%ge-6" % v)
        lines = [".SUBCKT %s %s" % (self.name, " ".join(self.pins)),
                 "*.PININFO BL:B BLB:B WL:I %s:B %s:B" % (self.vdd, self.vss)]
        for d in self.devices:
            model, bulk = (nmos, self.vss) if d.kind == "n" else (pmos, self.vdd)
            lines.append("%s %s %s %s %s %s w=%s l=%s" % (d.name, d.a, d.g, d.b, bulk, model, fmt(d.w), fmt(l)))
        lines.append(".ENDS %s" % self.name)
        return "\n".join(lines) + "\n"


def make_sram_netlist(sz, name="sram_bitcell", vdd="VDD", vss="VSS"):
    return SramNetlist(name, sz, vdd, vss)


# ============================================================================= switch level
class _Cell6T:
    """Heuristic ratio model (fixed MU_P / VM resistive dividers, not PDK MOS models) built from the netlist devices (nodes Q / QB)."""

    def __init__(self, cn, mu_p=MU_P, vm=VM):
        self.cn, self.mu_p, self.vm = cn, mu_p, vm

    def _conductances(self, node, lv, wl, bl):
        """[(G, V)] of every device that is on and ties `node` to a known-voltage net."""
        cn, out = self.cn, []
        known = {cn.vdd: 1.0, cn.vss: 0.0}
        known.update(bl)
        for d in cn.devices:
            if node not in (d.a, d.b):
                continue
            other = d.b if d.a == node else d.a
            if other not in known:
                continue
            g = wl if d.g == "WL" else lv.get(d.g)
            if g is None:
                continue
            if (d.kind == "n") != bool(g):
                continue
            out.append(((self.mu_p if d.kind == "p" else 1.0) * d.w / cn.l, known[other]))
        return out

    def settle(self, state, wl, bl):
        """Iterate Q / QB to a fixed point. state = (q, qb); bl = {"BL": v, "BLB": v} (None =
        undriven). Returns (q, qb, volts, floating)."""
        lv = {"Q": state[0], "QB": state[1]}
        volts, floating = {}, set()
        for _ in range(16):
            changed = False
            for node in ("Q", "QB"):
                gs = self._conductances(node, lv, wl, {k: v for k, v in bl.items() if v is not None})
                tot = sum(g for g, _ in gs)
                if tot <= 0:
                    floating.add(node)
                    continue
                floating.discard(node)
                v = sum(g * x for g, x in gs) / tot
                volts[node] = v
                new = 1 if v > self.vm + 1e-9 else 0 if v < self.vm - 1e-9 else lv[node]
                if new != lv[node]:
                    lv[node], changed = new, True
            if not changed:
                break
        else:
            floating.add("oscillates")
        return lv["Q"], lv["QB"], volts, floating


def check_sram_cell(cn, sizing=None, mu_p=MU_P, vm=VM):
    """Heuristic ratio-model check of a 6T netlist (not a SPICE SNM / write-margin proof). Returns a list of failure messages (empty = pass).

    Hold (WL=0): both states stay and both nodes are actively driven.
    Read (WL=1, bit lines precharged high): the '0' node stays below the trip point, state kept.
    Write 0 / write 1 (WL=1, bit lines driven): the cell flips from either state and keeps the
    new value after WL falls.
    """
    errs = []
    m = _Cell6T(cn, mu_p, vm)
    if sizing is None:
        sizing = getattr(cn, "sz", None)
    for s in ((1, 0), (0, 1)):
        tag = "Q=%d" % s[0]
        q, qb, v, fl = m.settle(s, 0, {"BL": None, "BLB": None})
        if (q, qb) != s or fl:
            errs.append("hold %s: state %s floating=%s" % (tag, (q, qb), sorted(fl)))
        q, qb, v, fl = m.settle(s, 1, {"BL": 1.0, "BLB": 1.0})
        node0 = "Q" if s[0] == 0 else "QB"
        if (q, qb) != s or fl:
            errs.append("read %s: upset to %s (floating=%s)" % (tag, (q, qb), sorted(fl)))
        elif v.get(node0, 1.0) >= vm:
            errs.append("read %s: '0' node rises to %.3f >= %.3f" % (tag, v.get(node0, 1.0), vm))
        # bit line of the 0 side must be pulled down by the cell, the other side stays high
        conn = [d for d in cn.devices if d.g == "WL" and node0 in (d.a, d.b)]
        if not conn:
            errs.append("read %s: no pass-gate to the '0' node" % tag)
    for val in (0, 1):
        for s in ((0, 1), (1, 0)):
            bl = {"BL": float(val), "BLB": float(1 - val)}
            q, qb, v, fl = m.settle(s, 1, bl)
            if (q, qb) != (val, 1 - val):
                errs.append("write %d from Q=%d: ends at %s" % (val, s[0], (q, qb)))
                continue
            q, qb, v, fl = m.settle((q, qb), 0, {"BL": None, "BLB": None})
            if (q, qb) != (val, 1 - val) or fl:
                errs.append("write %d from Q=%d: not latched after WL falls (%s)" % (val, s[0], (q, qb)))
    if sizing is not None:
        mm = static_metrics(sizing.w_pu, sizing.w_pg, sizing.w_pd, sizing.l, sizing.l, sizing.l, mu_p, vm)
        if mm["v_read"] >= vm:
            errs.append("cell ratio %.2f leaves v_read %.3f >= %.2f" % (mm["cr"], mm["v_read"], vm))
    return errs


class SramArraySim:
    """R x C array of the switch-level cell: word-line selected write / read with the others held."""

    def __init__(self, rows, cols, cn, mu_p=MU_P, vm=VM):
        self.rows, self.cols = rows, cols
        self.m = _Cell6T(cn, mu_p, vm)
        self.q = [[(0, 1)] * cols for _ in range(rows)]

    def write_row(self, r, bits):
        for c in range(self.cols):
            for rr in range(self.rows):
                wl = 1 if rr == r else 0
                bl = {"BL": float(bits[c]), "BLB": float(1 - bits[c])} if wl else {"BL": None, "BLB": None}
                q, qb, _, _ = self.m.settle(self.q[rr][c], wl, bl)
                self.q[rr][c] = (q, qb)
                if wl:
                    q, qb, _, _ = self.m.settle((q, qb), 0, {"BL": None, "BLB": None})
                    self.q[rr][c] = (q, qb)

    def read_row(self, r):
        out = []
        for c in range(self.cols):
            for rr in range(self.rows):
                wl = 1 if rr == r else 0
                q, qb, _, _ = self.m.settle(self.q[rr][c], wl, {"BL": 1.0, "BLB": 1.0} if wl else {"BL": None, "BLB": None})
                self.q[rr][c] = (q, qb)
            q, qb = self.q[r][c]
            out.append(q if q != qb else None)
        return out


def check_sram_array(rows, cols, cn):
    """Functional check of the array: checkerboard, inverse and walking row writes, read back,
    non-selected rows must keep their data. Returns a list of failure messages."""
    sim, errs = SramArraySim(rows, cols, cn), []
    for k, pat in enumerate((lambda r, c: (r + c) % 2, lambda r, c: 1 - (r + c) % 2,
                             lambda r, c: 1, lambda r, c: 0)):
        for r in range(rows):
            sim.write_row(r, [pat(r, c) for c in range(cols)])
        for r in range(rows):
            got = sim.read_row(r)
            if got != [pat(r, c) for c in range(cols)]:
                errs.append("pattern %d row %d reads %s" % (k, r, got))
    return errs


# ============================================================================= layout helpers
def _pad(via, enc, layer, gx=None):
    """Metal pad (w, h) around a via; enclosure oriented so neighbouring tracks still fit (as dla.tech)."""
    s, e = sorted(enc)
    opts = [(via + 2 * s, via + 2 * e), (via + 2 * e, via + 2 * s)]
    opts = [(max(w, layer.width), max(h, layer.width)) for w, h in opts]
    if gx is not None:
        ok = [o for o in opts if o[0] + layer.space <= gx + EPS]
        opts = ok or opts
    return min(opts, key=lambda o: o[1])


def _grid(lo, hi, step):
    n = int(math.floor((hi - lo) / step + EPS))
    return [r6(lo + i * step) for i in range(n + 1)]


class _Build:
    """One attempt at the bitcell for a given width (sites) and active gaps."""

    def __init__(self, st, sz, name, ns, ga, gc):
        self.st, self.sz, self.name, self.ns = st, sz, name, ns
        self.ga, self.gc = ga, gc
        r = st.r
        self.c = r["cont_size"]
        self.es, self.ee = sorted(r["m1_cont_enc"])
        self.w, self.sp = st.m1.width, st.m1.space
        self.g = st.mfg
        self.sw = max(self.c + 2 * self.es, self.w)                    # metal strap over a contact
        self.cpad = (self.sw, max(self.c + 2 * self.ee, self.w))        # metal over a poly contact
        s, e = sorted(st.v1.enc_below)
        g2 = 2 * st.mfg
        aw, ah = max(st.v1.size + 2 * s, self.w) + g2, max(st.v1.size + 2 * e, self.w) + g2
        if st.m1.min_area and aw * ah < st.m1.min_area:
            ah = snap_up(st.m1.min_area / aw, g2)
        self.apad = (aw, ah)                                            # VIA1 access pad
        self.K = st.v1_cont_space
        self.geo = Geo(st)
        self.vias = []
        self.polys = []                                                 # (key, rect)
        self.actives, self.act_conts = [], []

    # ---- legality
    def m1_legal(self, rects, net):
        st, cl = self.st, self.cl
        lo, hi = st.m1_edge, cl.width - st.m1_edge
        shapes = cl.m1()
        mine = [q for q, n in shapes if n == net] + list(rects)
        for a in rects:
            if a[0] < lo - EPS or a[2] > hi + EPS:
                return False
            for b, n in shapes:
                if self.geo.near(a, b) and not self.geo.ok(a, b, n == net, mine if n == net else None):
                    return False
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                if self.geo.near(a, b) and not self.geo.ok(a, b, True, mine):
                    return False
        return True

    def via_ok(self, x, y, extra=()):
        if not self.K:
            return True
        v = self.st.v1.size / 2
        via = R(x - v, y - v, x + v, y + v)
        conts = [q for l, q, n in self.cl.rects if l == "cont"] + list(extra)
        return all(C._dist(via, q) >= self.K - EPS for q in conts)

    def cont_ok(self, x, y):
        c = self.c / 2
        if self.K:
            q = R(x - c, y - c, x + c, y + c)
            v = self.st.v1.size / 2
            for vx, vy in self.vias:
                if C._dist(q, R(vx - v, vy - v, vx + v, vy + v)) < self.K - EPS:
                    return False
        return True

    def poly_legal(self, rect, own=(), cont=None):
        """New field poly (bar / arm): spacing to other poly, active, and to contacts on active."""
        r = self.st.r
        for k, q in self.polys:
            if k not in own and not C.poly_ok(self.st, rect, q):
                return False
        if any(C._dist(rect, a) < r["poly_activ_space"] - EPS for a in self.actives):
            return False
        if any(C._dist(rect, q) < r["cont_gate_space"] - EPS for q in self.act_conts):
            return False
        if cont is not None and any(C._dist(cont, a) < r["polycont_activ_space"] - EPS for a in self.actives):
            return False
        return True

    def snapshot(self):
        return (len(self.cl.rects), len(self.vias), len(self.polys), dict(self.cl.access), len(self.cl.texts))

    def restore(self, s):
        del self.cl.rects[s[0]:]
        del self.vias[s[1]:]
        del self.polys[s[2]:]
        self.cl.access = dict(s[3])
        del self.cl.texts[s[4]:]

    # ---- construction
    def run(self, ntrack=3):
        st, sz, r = self.st, self.sz, self.st.r
        c, w, g, L = self.c, self.w, self.g, st.L
        xce, xcg = st.x_cont_edge, st.x_cont_gate
        W = r6(self.ns * st.site_w)
        # ---- x positions (before centring)
        x = {}
        xl = st.amargin
        x["c0"] = r6(xl + xce)
        x["g0"] = r6(x["c0"] + xcg)
        x["q0"] = r6(x["g0"] + xcg)
        e0 = r6(x["q0"] + xce)
        s1 = r6(e0 + self.ga)
        x["q1"] = r6(s1 + xce)
        x["g1"] = r6(x["q1"] + xcg)
        x["bl"] = r6(x["g1"] + xcg)
        e1 = r6(x["bl"] + xce)
        s2 = r6(e1 + self.gc)
        x["blb"] = r6(s2 + xce)
        x["g2"] = r6(x["blb"] + xcg)
        x["qb2"] = r6(x["g2"] + xcg)
        e2 = r6(x["qb2"] + xce)
        s3 = r6(e2 + self.ga)
        x["qb3"] = r6(s3 + xce)
        x["g3"] = r6(x["qb3"] + xcg)
        x["c3"] = r6(x["g3"] + xcg)
        e3 = r6(x["c3"] + xce)
        right = r6(e3 + st.amargin)
        if right > W + EPS:
            raise LayoutError("%s: %d sites too narrow (%.3f needed)" % (self.name, self.ns, right))
        dx = snap_dn((W - right) / 2, g)
        for k in x:
            x[k] = r6(x[k] + dx)
        edges = [r6(v + dx) for v in (xl, e0, s1, e1, s2, e2, s3, e3)]
        self.x = x
        cl = self.cl = C.template(st, self.name, self.ns)
        cl.netlist = make_sram_netlist(sz, self.name, st.vdd, st.vss)
        VDD, VSS = st.vdd, st.vss
        nb, pb, pt = st.nb, st.pb, st.pt
        ypd, ypg = r6(nb + sz.w_pd), r6(nb + sz.w_pg)
        # ---- active islands (one rectangle per transistor)
        for x0, x1, top in ((edges[0], edges[1], ypd), (edges[2], edges[3], ypg),
                            (edges[4], edges[5], ypg), (edges[6], edges[7], ypd)):
            cl.add("activ", R(x0, nb, x1, top))
            self.actives.append(R(x0, nb, x1, top))
        for x0, x1 in ((edges[0], edges[1]), (edges[6], edges[7])):
            cl.add("activ", R(x0, pb, x1, pt))
            self.actives.append(R(x0, pb, x1, pt))
        # ---- gate columns of the inverters
        for k in ("g0", "g3"):
            q = R(x[k] - L / 2, st.poly_bot, x[k] + L / 2, st.poly_top)
            cl.add("poly", q, "QB" if k == "g0" else "Q")
            self.polys.append((k, q))

        def slot(key, y0, y1, net, rail=None):
            ys = st.band_contacts(y0, y1)
            xc = x[key]
            for y in ys:
                q = R(xc - c / 2, y - c / 2, xc + c / 2, y + c / 2)
                cl.add("cont", q, net)
                self.act_conts.append(q)
            rect = R(xc - self.sw / 2, ys[0] - c / 2 - self.ee, xc + self.sw / 2, ys[-1] + c / 2 + self.ee)
            if rail == "n":
                rect = R(rect[0], 0.0, rect[2], rect[3])
            elif rail == "p":
                rect = R(rect[0], rect[1], rect[2], st.row_h)
            cl.add("m1", rect, net)
            return ys, rect

        slot("c0", nb, ypd, VSS, "n")
        slot("c0", pb, pt, VDD, "p")
        slot("c3", nb, ypd, VSS, "n")
        slot("c3", pb, pt, VDD, "p")
        # Q: inner diffusion of INV_L (n and p) + the PG_L source, joined by a bar and a jog
        _, qn = slot("q0", nb, ypd, "Q")
        _, qp = slot("q0", pb, pt, "Q")
        cl.add("m1", R(qn[0], qn[1], qn[2], qp[3]), "Q")
        ys1, q1 = slot("q1", nb, ypg, "Q")
        cl.add("m1", R(x["q0"], ys1[0] - self.cpad[1] / 2, x["q1"], ys1[0] + self.cpad[1] / 2), "Q")
        _, qn = slot("qb3", nb, ypd, "QB")
        _, qp = slot("qb3", pb, pt, "QB")
        cl.add("m1", R(qn[0], qn[1], qn[2], qp[3]), "QB")
        ys2, q2 = slot("qb2", nb, ypg, "QB")
        cl.add("m1", R(x["qb2"], ys2[0] - self.cpad[1] / 2, x["qb3"], ys2[0] + self.cpad[1] / 2), "QB")
        ysb, rbl = slot("bl", nb, ypg, "BL")
        ysbb, rblb = slot("blb", nb, ypg, "BLB")
        self.ypg_top = ypg
        # ---- search tracks + levels
        aw = self.apad[0]
        tr = [r6(st.x_off + st.gx * n) for n in range(int(W / st.gx) + 2)]
        tr = [t for t in tr if st.m1_edge + aw / 2 - EPS <= t <= W - st.m1_edge - aw / 2 + EPS]
        center = (x["g1"] + x["g2"]) / 2
        for tb in sorted(tr, key=lambda t: abs(t - x["bl"]))[:ntrack]:
            s0 = self.snapshot()
            if not self.place_bitline("BL", "bl", rbl, ysb[0], tb):
                self.restore(s0)
                continue
            for tbb in sorted(tr, key=lambda t: abs(t - x["blb"]))[:ntrack]:
                if tbb == tb:
                    continue
                s1_ = self.snapshot()
                if not self.place_bitline("BLB", "blb", rblb, ysbb[0], tbb):
                    self.restore(s1_)
                    continue
                for tw in sorted(tr, key=lambda t: abs(t - center))[:ntrack + 1]:
                    if tw in (tb, tbb):
                        continue
                    s2_ = self.snapshot()
                    if (self.m2_ok(tb, tbb, tw) and self.place_wl(tw) and self.place_arm("QB")
                            and self.place_arm("Q")):
                        return self.finish(W)
                    self.restore(s2_)
                self.restore(s1_)
            self.restore(s0)
        raise LayoutError("%s: no legal pin access for %d sites (gaps %.3f / %.3f)" % (self.name, self.ns, self.ga, self.gc))

    # ---- M2 tracks of the array: columns mirror, so the outermost lines need half a pitch
    def m2_ok(self, *ts):
        st = self.st
        pw = max(_pad(st.v1.size, st.v1.enc_above, st.m2, st.gx)[0], st.m2.width)
        need = pw + st.m2.space
        W = self.cl.width
        return 2 * min(ts) >= need - EPS and 2 * (W - max(ts)) >= need - EPS

    def place_bitline(self, net, key, rstrap, yc, t):
        st = self.st
        aw, ah = self.apad
        xc, w, sw = self.x[key], self.w, self.sw
        for y in _grid(yc, st.pad_y[1], self.g):
            vert = R(xc - sw / 2, rstrap[1], xc + sw / 2, max(y + w / 2, rstrap[3]))
            pad = R(t - aw / 2, y - ah / 2, t + aw / 2, y + ah / 2)
            shapes = [vert, pad]
            if abs(t - xc) > EPS:
                shapes.append(R(min(xc, t), y - w / 2, max(xc, t), y + w / 2))
            if not self.via_ok(t, y) or not self.m1_legal(shapes, net):
                continue
            for s in shapes:
                self.cl.add("m1", s, net)
            self.vias.append((t, y))
            self.cl.access[net] = (t, y)
            self.cl.texts.append(("m1_text", net, t, y))
            return True
        return False

    def place_wl(self, t):
        st, r = self.st, self.st.r
        c, L, pc = self.c, st.L, st.pc
        x, w = self.x, self.w
        aw, ah = self.apad
        cw, ch = self.cpad
        d = 0.0 if not self.K else snap_up(c / 2 + self.K + st.v1.size / 2, self.g)
        y_floor = max(st.pad_y[0], r6(self.ypg_top + r["cont_gate_space"] + pc / 2))
        for xw in ([t] if not d else [r6(t - d), r6(t + d)]):
            for y in _grid(y_floor, st.pad_y[1], self.g):
                cont = R(xw - c / 2, y - c / 2, xw + c / 2, y + c / 2)
                lo, hi = min(x["g1"] - L / 2, xw - pc / 2), max(x["g2"] + L / 2, xw + pc / 2)
                bar = R(lo, y - pc / 2, hi, y + pc / 2)
                g1 = R(x["g1"] - L / 2, st.poly_bot, x["g1"] + L / 2, y + pc / 2)
                g2 = R(x["g2"] - L / 2, st.poly_bot, x["g2"] + L / 2, y + pc / 2)
                if not (self.poly_legal(bar, ("g1", "g2"), cont) and self.cont_ok(xw, y)):
                    continue
                if d:
                    shapes = [R(xw - cw / 2, y - ch / 2, xw + cw / 2, y + ch / 2),
                              R(min(xw, t), y - w / 2, max(xw, t), y + w / 2),
                              R(t - aw / 2, y - ah / 2, t + aw / 2, y + ah / 2)]
                else:
                    sx, sy = max(aw, cw), max(ah, ch)
                    shapes = [R(t - sx / 2, y - sy / 2, t + sx / 2, y + sy / 2)]
                if not self.via_ok(t, y, [cont]) or not self.m1_legal(shapes, "WL"):
                    continue
                for q in (g1, g2, bar):
                    self.cl.add("poly", q, "WL")
                self.cl.add("cont", cont, "WL")
                for s in shapes:
                    self.cl.add("m1", s, "WL")
                self.polys += [("g1", g1), ("g1", g2), ("g1", bar)]
                self.vias.append((t, y))
                self.cl.access["WL"] = (t, y)
                self.cl.texts.append(("m1_text", "WL", t, y))
                self.y_wl = y
                return True
        return False

    def place_arm(self, net):
        """Gate contact of INV_L (net QB) / INV_R (net Q) on a poly arm that reaches across the
        opposite inverter's bar; metal wire from the bar of `net` to the contact."""
        st = self.st
        c, L, pc, x, w = self.c, st.L, st.pc, self.x, self.w
        cw, ch = self.cpad
        left = net == "QB"
        gk, bar_x = ("g0", x["qb3"]) if left else ("g3", x["q0"])      # gate column, bar of `net`
        other_bar = x["q0"] if left else x["qb3"]                      # bar the arm reaches across
        base = other_bar + (self.sw / 2 + self.sp + cw / 2) * (1 if left else -1)
        for k in range(0, 60):
            xc = snap_up(base, self.g) + k * self.g if left else snap_dn(base, self.g) - k * self.g
            xc = r6(xc)
            for y in _grid(st.pad_y[0], st.pad_y[1], self.g):
                cont = R(xc - c / 2, y - c / 2, xc + c / 2, y + c / 2)
                lo, hi = (x[gk] - L / 2, xc + pc / 2) if left else (xc - pc / 2, x[gk] + L / 2)
                arm = R(lo, y - pc / 2, hi, y + pc / 2)
                if not (self.poly_legal(arm, (gk,), cont) and self.cont_ok(xc, y)):
                    continue
                pad = R(xc - cw / 2, y - ch / 2, xc + cw / 2, y + ch / 2)
                wire = R(min(xc, bar_x), y - w / 2, max(xc, bar_x), y + w / 2)
                if not self.m1_legal([pad, wire], net):
                    continue
                self.cl.add("poly", arm, net)
                self.cl.add("cont", cont, net)
                self.cl.add("m1", pad, net)
                self.cl.add("m1", wire, net)
                self.polys.append((gk, arm))
                self.cl.texts.append(("m1_text", net, bar_x, y))
                return True
        return False

    def finish(self, W):
        st, cl = self.st, self.cl
        C._final_check(st, cl)
        cl.pins = list(PINS) + [st.vdd, st.vss]
        cl.info = {"kind": "sram", "sites": self.ns, "ga": self.ga, "gc": self.gc,
                   "x": dict(self.x), "y_wl": self.y_wl}
        return cl


def make_sram_cell(tech, name="sram_bitcell", sizing=None, **kw):
    """Layout of the 6T bitcell (:class:`dla.stdcell.cell.CellLayout`; pins BL BLB WL VDD VSS).

    ``cl.access[net]`` is the centre of the VIA1 access pad of BL / BLB / WL; ``cl.st`` is
    the cell template (bands sized for this transistor sizing), ``cl.sizing`` the sizes.
    """
    st0 = tech if isinstance(tech, StdTech) else StdTech(tech)
    sz = sizing or size_6t(st0, **kw)
    st = cell_tech(st0, sz)
    g = st.mfg
    last = None
    ns0 = max(1, int(math.ceil((6 * st.x_cont_gate + 4 * st.x_cont_edge + 2 * st.amargin) / st.site_w)))
    act = st.r["activ_space"]
    for extra in (0, 1, 2, 3, 4, 5, 6):
        for ns in range(ns0, ns0 + 8):
            for ga in (act, act + st.x_cont_edge, act + 2 * st.x_cont_edge):
                try:
                    cl = _Build(st, sz, name, ns, snap_up(ga, g), snap_up(act, g) + extra * st.m1.space).run()
                except LayoutError as e:
                    last = e
                    continue
                cl.sizing, cl.st = sz, st
                return cl
    raise LayoutError("%s: no legal bitcell layout (%s)" % (name, last))


# ============================================================================= array
def _pads(st):
    g2 = 2 * st.mfg
    up = lambda p: (snap_up(p[0], g2), snap_up(p[1], g2))
    return {"v1m2": up(_pad(st.v1.size, st.v1.enc_above, st.m2, st.gx)),
            "v2m2": up(_pad(st.v2.size, st.v2.enc_below, st.m2, st.gx)),
            "v2m3": up(_pad(st.v2.size, st.v2.enc_above, st.m3))}


def _m3_level(st, y1, pads, H):
    """Row-local y of the word-line VIA2 / M3 wire, for the VIA1 at y1.

    Rows alternate with their mirror image, so every distance to the neighbour row is
    2 * (distance to the row edge): spacing, minimum area and via rules are checked that way."""
    v1, v2 = st.v1.size, st.v2.size
    dvia = 0.0 if (st.via_stack and not st.via12_space) else (v1 + v2) / 2 + st.via12_space
    h3 = max(st.m3.width, pads["v2m3"][1])
    ph1, ph2 = pads["v1m2"][1], pads["v2m2"][1]
    best = None
    for ym in _grid(r6((h3 + st.m3.space) / 2), r6(H - (h3 + st.m3.space) / 2), 2 * st.mfg):
        if abs(ym - y1) < max(dvia, 0.0) - EPS:
            continue
        lo, hi = min(y1 - ph1 / 2, ym - ph2 / 2), max(y1 + ph1 / 2, ym + ph2 / 2)
        if lo < st.m2.space / 2 - EPS or H - hi < st.m2.space / 2 - EPS:
            continue
        if st.m2.min_area and (hi - lo) * st.m2.width < st.m2.min_area - EPS:
            continue
        if 2 * (H - ym) < v2 + st.v2.space - EPS or 2 * y1 < v1 + st.v1.space - EPS:
            continue
        if dvia and (abs(2 * H - y1 - ym) < dvia - EPS or abs(y1 + ym) < dvia - EPS):
            continue
        key = abs(ym - y1)
        if best is None or key < best[0]:
            best = (key, ym)
    if best is None:
        raise LayoutError("no legal word-line via2 / M3 level for via1 at y=%.3f in a %.3f row" % (y1, H))
    return best[1]


class SramArray:
    """Placement + routing of a rows x cols array of one bitcell (global coordinates, um)."""

    def __init__(self, name, cell, rows, cols):
        self.name, self.cell, self.rows, self.cols = name, cell, rows, cols
        self.st = cell.st
        self.W, self.H = cell.width, cell.st.row_h
        self.insts = []                          # (name, cell, x, yb, orient)
        self.shapes = {"m1": [], "m2": [], "m3": []}      # (rect, net)
        self.vias = []                           # (layer "via1"/"via2", x, y, net)
        self.pins = []                           # {"name", "x", "y", "layer"}
        self.pinpos = []                         # (inst, pin, x, y, net)

    def xmap(self, c, u):
        return r6(self.x_arr + c * self.W + (u if c % 2 == 0 else self.W - u))

    def ymap(self, r, v):
        return r6(r * self.H + (v if r % 2 == 0 else self.H - v))


def make_sram_array(cell, rows, cols, name):
    """Place rows x cols bitcells with 4-way mirroring (R0 / MY on even rows, MX / R180 on odd rows),
    bit lines on M2, word lines on M3, power rails tied by M2 straps on both sides."""
    _check_count("rows", rows)
    _check_count("cols", cols)
    st = cell.st
    if st.m2 is None or st.m3 is None or st.v2 is None:
        raise KeyError("tech %s: layers.vertical / horizontal / via2 are needed for the array" % st.name)
    a = SramArray(name, cell, rows, cols)
    W, H, g = a.W, a.H, st.mfg
    pads = _pads(st)
    (pw1, ph1), (pw2, ph2), (pw3, ph3) = pads["v1m2"], pads["v2m2"], pads["v2m3"]
    uBL, vBL = cell.access["BL"]
    uBLB, vBLB = cell.access["BLB"]
    uWL, vWL = cell.access["WL"]
    ym = _m3_level(st, vWL, pads, H)
    # power strap geometry
    ap = max(st.v1.size + 2 * min(st.v1.enc_below), st.m1.width) + 2 * g
    ap = max(ap, st.v1.size + 2 * max(st.v1.enc_below) + 2 * g)
    pws = max(pw1, st.m2.width)
    xs_l = snap_up(max(ap, pws) / 2 + st.m1_edge, g)
    us = (uBL, uBLB, uWL)
    near = min(min(us), W - max(us))
    x_min = max(xs_l + ap / 2, xs_l + pws / 2 + st.m2.space + max(pw1, pw2) / 2 - near)
    a.x_arr = snap_up(x_min, st.gx)
    a.xs_l = xs_l
    a.width = r6(2 * a.x_arr + cols * W)
    a.height = r6(rows * H)
    a.xs_r = r6(a.width - xs_l)
    a.ym = ym
    M1, M2, M3 = a.shapes["m1"], a.shapes["m2"], a.shapes["m3"]
    a.uBL, a.uBLB, a.uWL = uBL, uBLB, uWL
    # instances
    for r in range(rows):
        for c in range(cols):
            iname = "bc_%d_%d" % (r, c)
            o = orient_of(r, c)
            a.insts.append((iname, cell.name, r6(a.x_arr + c * W), r6(r * H), o))
            for pin, (u, v), net in (("BL", (uBL, vBL), "BL[%d]" % c), ("BLB", (uBLB, vBLB), "BLB[%d]" % c),
                                     ("WL", (uWL, vWL), "WL[%d]" % r)):
                a.pinpos.append((iname, pin, a.xmap(c, u), a.ymap(r, v), net))
    # bit lines: continuous M2 over all rows, a pad + VIA1 on every cell's access pad
    hw = st.m2.width / 2
    for c in range(cols):
        for pin, u, v in (("BL", uBL, vBL), ("BLB", uBLB, vBLB)):
            net = "%s[%d]" % (pin, c)
            x = a.xmap(c, u)
            ys = [a.ymap(r, v) for r in range(rows)]
            M2.append((R(x - hw, min(0.0, min(ys) - ph1 / 2), x + hw, max(a.height, max(ys) + ph1 / 2)), net))
            for y in ys:
                M2.append((R(x - pw1 / 2, y - ph1 / 2, x + pw1 / 2, y + ph1 / 2), net))
                a.vias.append(("via1", x, y, net))
            a.pins.append({"name": net, "x": x, "y": r6(H / 2), "layer": "m2"})
    # word lines: VIA1 -> M2 stub -> VIA2 -> M3 wire along the row
    h3 = max(st.m3.width, ph3)
    for r in range(rows):
        net = "WL[%d]" % r
        y3 = a.ymap(r, ym)
        for c in range(cols):
            x = a.xmap(c, uWL)
            y1 = a.ymap(r, vWL)
            M2.append((R(x - hw, min(y1, y3), x + hw, max(y1, y3)), net))
            M2.append((R(x - pw1 / 2, y1 - ph1 / 2, x + pw1 / 2, y1 + ph1 / 2), net))
            M2.append((R(x - pw2 / 2, y3 - ph2 / 2, x + pw2 / 2, y3 + ph2 / 2), net))
            M3.append((R(x - pw3 / 2, y3 - h3 / 2, x + pw3 / 2, y3 + h3 / 2), net))
            a.vias.append(("via1", x, y1, net))
            a.vias.append(("via2", x, y3, net))
        M3.append((R(a.x_arr, y3 - st.m3.width / 2, a.x_arr + cols * W, y3 + st.m3.width / 2), net))
        a.pins.append({"name": net, "x": a.xmap(0, uWL), "y": y3, "layer": "m3"})
    # power: rails continue to a VSS strap on the left (even rail index) and a VDD strap on the right
    for k in range(rows + 1):
        y = r6(k * H)
        net, xs = (st.vss, xs_l) if k % 2 == 0 else (st.vdd, a.xs_r)
        if k % 2 == 0:
            M1.append((R(xs - ap / 2, y - st.rail_w / 2, a.x_arr, y + st.rail_w / 2), net))
        else:
            M1.append((R(a.x_arr + cols * W, y - st.rail_w / 2, xs + ap / 2, y + st.rail_w / 2), net))
        a.vias.append(("via1", xs, y, net))
    for net, xs in ((st.vss, xs_l), (st.vdd, a.xs_r)):
        M2.append((R(xs - pws / 2, -ph1 / 2, xs + pws / 2, a.height + ph1 / 2), net))
        a.pins.append({"name": net, "x": xs, "y": r6(H / 2), "layer": "m2"})
    return a


def make_sram_2x2(cell, name):
    """2 x 2 abutment test: all four orientations touching (R0 / MY below, MX / R180 above)."""
    a = SramArray(name, cell, 2, 2)
    W, H = a.W, a.H
    a.x_arr, a.width, a.height = 0.0, 2 * W, 2 * H
    for r in range(2):
        for c in range(2):
            a.insts.append(("bc_%d_%d" % (r, c), cell.name, r6(c * W), r6(r * H), orient_of(r, c)))
    return a


# ============================================================================= views
def write_sram_gds(path, st, cell, arrays):
    """GDS: the bitcell, then each array (placement + routing) as its own cell."""
    gw = writers.GdsWriter(os.path.splitext(os.path.basename(path))[0])
    writers.write_cell_gds(gw, st, cell)
    lay = {"m1": st.lay["m1"], "m2": st.m2.gds, "m3": st.m3.gds}
    for a in arrays:
        gw.begin(a.name)
        for iname, cname, x, yb, o in a.insts:
            gw.sref(cname, x, yb, orient=o, size=(a.W, a.H))
        for k in ("m1", "m2", "m3"):
            for rect, net in a.shapes[k]:
                gw.box(lay[k], rect)
        for layer, x, y, net in a.vias:
            s = (st.v1 if layer == "via1" else st.v2).size / 2
            gw.box((st.v1 if layer == "via1" else st.v2).gds, (x - s, y - s, x + s, y + s))
        for p in a.pins:
            lt = {"m2": st.m2.text, "m3": st.m3.text}[p["layer"]]
            if lt:
                gw.text(lt, p["name"], p["x"], p["y"])
        if st.lay.get("boundary"):
            gw.box(st.lay["boundary"], (0.0, 0.0, a.width, a.height))
        gw.end()
    gw.write(path)


def sram_json(st, cell, a, sz, metrics):
    """Block-style JSON (see dla.export.block_json) for gds.verify_conn / drc / render_png."""
    return {
        "top": a.name, "tech": st.name, "row_h": st.row_h, "insts": a.insts,
        "shapes": [[k, r[0], r[1], r[2], r[3], n] for k in ("m1", "m2", "m3") for r, n in a.shapes[k]],
        "vias": [list(v) for v in a.vias],
        "via_size": {"via1": st.v1.size, "via2": st.v2.size},
        "pins": a.pins,
        "boundary": [0.0, 0.0, a.width, a.height],
        "rows": sorted([r6(r * a.H), orient_of(r, 0)] for r in range(a.rows)),
        "pinpos": [list(p) for p in a.pinpos],
        "cells": {cell.name: {"w": cell.width}},
        "rails": {st.vdd: st.row_h, st.vss: 0.0},
        "layers": {"m1": list(st.m1.gds), "v1": list(st.v1.gds), "m2": list(st.m2.gds),
                   "v2": list(st.v2.gds), "m3": list(st.m3.gds),
                   "m2_text": list(st.m2.text) if st.m2.text else None,
                   "m3_text": list(st.m3.text) if st.m3.text else None,
                   "boundary": list(st.lay["boundary"]) if st.lay.get("boundary") else None},
        "zoom": {},
        "meta": {"block": "sram", "rows": a.rows, "cols": a.cols, "bitcell": cell.name,
                 "bitcell_size": [cell.width, st.row_h]},
        "sram_metrics": metrics,
    }


def write_sram_lef(path, st, cell, a):
    pr = lambda r: "RECT %.3f %.3f %.3f %.3f ;" % r
    L = ["VERSION 5.8 ;", 'BUSBITCHARS "[]" ;', 'DIVIDERCHAR "/" ;', ""]
    L += ["MACRO %s" % cell.name, "  CLASS CORE ;", "  ORIGIN 0 0 ;", "  SIZE %.3f BY %.3f ;" % (cell.width, st.row_h),
          "  SYMMETRY X Y ;"]
    m1 = cell.m1()
    for p in cell.pins:
        power = p in (st.vdd, st.vss)
        L += ["  PIN %s" % p, "    DIRECTION INOUT ;",
              "    USE %s ;" % ("POWER" if p == st.vdd else "GROUND" if p == st.vss else "SIGNAL")]
        if power:
            L.append("    SHAPE ABUTMENT ;")
        L += ["    PORT", "      LAYER %s ;" % st.m1.lef] + ["        " + pr(r) for r, n in m1 if n == p]
        L += ["    END", "  END %s" % p]
    obs = [r for r, n in m1 if n not in cell.pins]
    conts = [r for l, r, n in cell.rects if l == "cont"] if st.v1_cont_space else []
    if obs or conts:
        L.append("  OBS")
        if obs:
            L += ["    LAYER %s ;" % st.m1.lef] + ["      " + pr(r) for r in obs]
        if conts:
            L += ["    LAYER %s SPACING %g ;" % (st.v1.lef, st.v1_cont_space)] + ["      " + pr(r) for r in conts]
        L.append("  END")
    L += ["END %s" % cell.name, ""]
    L += ["MACRO %s" % a.name, "  CLASS BLOCK ;", "  ORIGIN 0 0 ;", "  SIZE %.3f BY %.3f ;" % (a.width, a.height)]
    seen = {}
    for k, lay in (("m2", st.m2.lef), ("m3", st.m3.lef)):
        for rect, net in a.shapes[k]:
            seen.setdefault(net, []).append((lay, rect))
    for p in a.pins:
        power = p["name"] in (st.vdd, st.vss)
        L += ["  PIN %s" % p["name"], "    DIRECTION INOUT ;",
              "    USE %s ;" % ("POWER" if p["name"] == st.vdd else "GROUND" if p["name"] == st.vss else "SIGNAL"),
              "    PORT"]
        want = st.m3.lef if p["layer"] == "m3" else st.m2.lef
        # the long wire of the net (pads and stubs are enclosed by it or stay inside the macro)
        wires = [(lay, r) for lay, r in seen.get(p["name"], []) if lay == want]
        wires.sort(key=lambda t: -(max(t[1][2] - t[1][0], t[1][3] - t[1][1])))
        L += ["      LAYER %s ;" % want, "        " + pr(wires[0][1]), "    END", "  END %s" % p["name"]]
    L += ["  OBS", "    LAYER %s ;" % st.m1.lef, "      " + pr((0.0, 0.0, a.width, a.height)), "  END", "END %s" % a.name, "",
          "END LIBRARY"]
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")


def write_sram_spice(path, st, cell, a):
    cn = cell.netlist
    with open(path, "w") as f:
        f.write("* %s: 6T SRAM, W/L from the sizing optimiser (tech %s)\n\n" % (a.name, st.name))
        f.write(cn.spice(st.nmos, st.pmos, st.L, st.spice_units) + "\n")
        pins = (["BL[%d]" % c for c in range(a.cols)] + ["BLB[%d]" % c for c in range(a.cols)]
                + ["WL[%d]" % r for r in range(a.rows)] + [st.vdd, st.vss])
        f.write(".SUBCKT %s %s\n" % (a.name, " ".join(pins)))
        for r in range(a.rows):
            for c in range(a.cols):
                f.write("Xbc_%d_%d BL[%d] BLB[%d] WL[%d] %s %s %s\n" % (r, c, c, c, r, st.vdd, st.vss, cn.name))
        f.write(".ENDS %s\n" % a.name)


def write_sram_verilog(outdir, name, cell, a):
    R_, C_ = a.rows, a.cols
    bc = cell.name
    paths = {k: os.path.join(outdir, name + s) for k, s in (("layout_v", "_layout.v"), ("rtl", "_rtl.v"), ("tb", "_tb.v"))}
    with open(paths["layout_v"], "w") as f:
        f.write("// %s: structural netlist (transistor switches) - generated by Digital_Layout_Auto\n" % name)
        f.write("module %s (BL, BLB, WL, VDD, VSS);\n  inout BL, BLB, VDD, VSS;\n  input WL;\n  wire Q, QB;\n" % bc)
        for d in cell.netlist.devices:
            f.write("  %s %s (%s, %s, %s);\n" % ("pmos" if d.kind == "p" else "nmos", d.name, d.a, d.b, d.g))
        f.write("endmodule\n\n")
        f.write("module %s (BL, BLB, WL, VDD, VSS);\n  inout [%d:0] BL, BLB;\n  input [%d:0] WL;\n  inout VDD, VSS;\n"
                % (name, C_ - 1, R_ - 1))
        for r in range(R_):
            for c in range(C_):
                f.write("  %s bc_%d_%d (.BL(BL[%d]), .BLB(BLB[%d]), .WL(WL[%d]), .VDD(VDD), .VSS(VSS));\n"
                        % (bc, r, c, c, c, r))
        f.write("endmodule\n")
    with open(paths["rtl"], "w") as f:
        f.write("""// %(n)s: behavioural model (bit lines are pulled high, a cell storing 0 pulls BL low, a cell storing 1
// pulls BLB low; a write drives the bit lines harder than the cell)
module %(n)s_rtl (WL, BL, BLB);
  parameter ROWS = %(r)d, COLS = %(c)d;
  input  [ROWS-1:0] WL;
  inout  [COLS-1:0] BL, BLB;
  tri1   [COLS-1:0] BL, BLB;
  reg    [COLS-1:0] mem [0:ROWS-1];
  genvar gr, gc;
  generate
    for (gr = 0; gr < ROWS; gr = gr + 1) begin : row
      for (gc = 0; gc < COLS; gc = gc + 1) begin : col
        assign (strong0, highz1) BL[gc]  = (WL[gr] && mem[gr][gc] === 1'b0) ? 1'b0 : 1'bz;
        assign (strong0, highz1) BLB[gc] = (WL[gr] && mem[gr][gc] === 1'b1) ? 1'b0 : 1'bz;
        always @(WL[gr] or BL[gc] or BLB[gc])
          if (WL[gr]) begin
            if (BL[gc] === 1'b1 && BLB[gc] === 1'b0) mem[gr][gc] = 1'b1;
            else if (BL[gc] === 1'b0 && BLB[gc] === 1'b1) mem[gr][gc] = 1'b0;
          end
      end
    end
  endgenerate
endmodule
""" % {"n": name, "r": R_, "c": C_})
    with open(paths["tb"], "w") as f:
        f.write("""`timescale 1ns/1ps
module %(n)s_tb;
  parameter ROWS = %(r)d, COLS = %(c)d;
  reg  [ROWS-1:0] wl;
  reg  [COLS-1:0] wd, p;
  reg             we;
  tri1 [COLS-1:0] bl, blb;
  assign (supply0, supply1) bl  = we ? wd  : {COLS{1'bz}};
  assign (supply0, supply1) blb = we ? ~wd : {COLS{1'bz}};
  %(n)s_rtl dut (.WL(wl), .BL(bl), .BLB(blb));
  integer r, c, k, errors;
  function [COLS-1:0] pat;
    input integer row;
    integer k;
    begin for (k = 0; k < COLS; k = k + 1) pat[k] = (row + k) %% 2; end
  endfunction
  initial begin
    errors = 0; wl = 0; we = 0; wd = 0;
    for (r = 0; r < ROWS; r = r + 1) begin       // write the checkerboard
      wd = pat(r); we = 1; #1 wl = (1 << r); #1 wl = 0; #1 we = 0;
    end
    for (r = 0; r < ROWS; r = r + 1) begin       // read it back
      p = pat(r);
      #1 wl = (1 << r); #1;
      for (c = 0; c < COLS; c = c + 1)
        if (bl[c] !== p[c] || blb[c] !== ~p[c]) begin
          errors = errors + 1;
          $display("FAIL row %%0d col %%0d: bl=%%b blb=%%b", r, c, bl[c], blb[c]);
        end
      wl = 0;
    end
    for (r = 0; r < ROWS; r = r + 1) begin       // overwrite with the inverse, other rows must keep their data
      wd = ~pat(r); we = 1; #1 wl = (1 << r); #1 wl = 0; #1 we = 0;
      if (r + 1 < ROWS && dut.mem[r + 1] !== pat(r + 1)) begin errors = errors + 1; $display("FAIL disturb row %%0d", r + 1); end
    end
    for (r = 0; r < ROWS; r = r + 1) begin
      p = ~pat(r);
      #1 wl = (1 << r); #1;
      for (c = 0; c < COLS; c = c + 1)
        if (bl[c] !== p[c]) begin errors = errors + 1; $display("FAIL inverse row %%0d col %%0d", r, c); end
      wl = 0;
    end
    if (errors == 0) $display("PASS %(n)s"); else $display("FAIL %%0d errors", errors);
    $finish;
  end
endmodule
""" % {"n": name, "r": R_, "c": C_})
    return paths


# ============================================================================= top level
def _log(*a):
    print(*a)


def make_sram(tech, outdir, rows=4, cols=4, name=None, cr=None, pr=None, l=None,
              w_pu=None, w_pg=None, w_pd=None, log=_log):
    """Size, lay out, route and write a rows x cols 6T SRAM. Returns a dict with the cell, the
    arrays, the file paths and the switch-level results (``errors``: empty list = pass)."""
    _check_count("rows", rows)
    _check_count("cols", cols)
    t0 = time.time()
    st0 = tech if isinstance(tech, StdTech) else StdTech(tech)
    name = name or "sram_%dx%d" % (rows, cols)
    os.makedirs(outdir, exist_ok=True)
    sz = size_6t(st0, cr=cr, pr=pr, w_pu=w_pu, w_pg=w_pg, w_pd=w_pd, l=l)
    log("[sram] %s: minimum legal widths satisfying CR/PR targets: W_PD %.3f  W_PG %.3f  W_PU %.3f  L %.3f  "
        "CR %.3f  PR %.3f  (heuristic ratio model: read factor %.2f, write factor %.2f; "
        "SNM / write margin / delay / power need SPICE with PDK models)"
        % (st0.name, sz.w_pd, sz.w_pg, sz.w_pu, sz.l, sz.cr, sz.pr, sz.snm_read_factor, sz.write_margin_factor))
    cell = make_sram_cell(st0, name + "_bitcell", sz)
    st = cell.st
    log("[std ] " + st.summary())
    arr = make_sram_array(cell, rows, cols, name)
    two = make_sram_2x2(cell, name + "_2x2")
    errors = check_sram_cell(cell.netlist, sz) + check_sram_array(min(rows, 4), min(cols, 4), cell.netlist)
    metrics = dict(sz.as_dict(), cell_width_um=cell.width, cell_height_um=st.row_h,
                   cell_area_um2=round(cell.width * st.row_h, 6), rows=rows, cols=cols,
                   array_area_um2=round(arr.width * arr.height, 6))
    p = {k: os.path.join(outdir, name + ext) for k, ext in
         (("gds", ".gds"), ("lef", ".lef"), ("spice", ".spice"), ("json", ".json"))}
    write_sram_gds(p["gds"], st, cell, [two, arr])
    write_sram_lef(p["lef"], st, cell, arr)
    write_sram_spice(p["spice"], st, cell, arr)
    p.update(write_sram_verilog(outdir, name, cell, arr))
    with open(p["json"], "w") as f:
        json.dump(sram_json(st, cell, arr, sz, metrics), f, indent=1)
    log("[gen ] %s: %dx%d bits, bitcell %.3f x %.3f um (%d sites), array %.3f x %.3f um  (%.1fs)"
        % (name, rows, cols, cell.width, st.row_h, cell.info["sites"], arr.width, arr.height, time.time() - t0))
    return {"st": st, "cell": cell, "array": arr, "array2x2": two, "sizing": sz, "paths": p, "name": name,
            "errors": errors, "metrics": metrics}
