"""Technology description: everything PDK-specific lives in a JSON tech file.

No PDK data ships with this package. A tech file names the std-cell LEF/GDS, the
three metal layers the generators use (pin layer, vertical routing layer,
horizontal routing layer) and the two vias between them with their design rules,
and maps the logic roles the generators need (inv, nand2, nor2, nor3, fill) to
library cells. See ``tech/TECH_FILE.md`` and ``tech/ihp_sg13g2.json``.

From the rules the routing grid is derived so that any two different nets on
neighbouring grid nodes are DRC clean; the router then only tracks node ownership.
"""
import json
import math
import os
import re

EPS = 1e-6


def _expand(path, base):
    path = re.sub(r"\$\{(\w+)(?::-([^}]*))?\}",
                  lambda m: os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else m.group(0)), path)
    if "${" in path:
        raise EnvironmentError("tech file path needs an environment variable: %s" % path)
    path = os.path.expanduser(path)
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


def snap(v, grid):
    return round(round(v / grid) * grid, 6)


def ceil_to(v, grid):
    return round(math.ceil(v / grid - EPS) * grid, 6)


# ----------------------------------------------------------------------------- LEF
def parse_lef(paths, pin_layer, via1_layer):
    """Macros: size, signal/power pin rects on the pin layer, keep-outs on the via1 layer."""
    macros, sites = {}, {}
    for path in paths:
        cur = pin = layer = site = None
        skip = False
        for line in open(path, errors="replace"):
            t = line.replace(";", " ;").split()
            if not t:
                continue
            k = t[0]
            if k in ("PROPERTYDEFINITIONS", "UNITS"):
                skip = True
            if skip:
                skip = not (k == "END" and len(t) > 1 and t[1] in ("PROPERTYDEFINITIONS", "UNITS"))
                continue
            if k == "SITE" and cur is None and len(t) > 1:
                site = sites.setdefault(t[1], {})
            elif k == "SIZE" and site is not None and cur is None:
                site["w"], site["h"] = float(t[1]), float(t[3])
            elif k == "END" and site is not None and cur is None:
                site = None
            elif k == "MACRO" and len(t) == 2:
                cur = macros.setdefault(t[1], {"pins": {}, "use": {}, "obs": [], "obs_pin": []})
                pin = None
            elif cur is None:
                continue
            elif k == "SIZE":
                cur["w"], cur["h"] = float(t[1]), float(t[3])
            elif k == "PIN":
                pin = t[1]
                cur["pins"].setdefault(pin, [])
            elif k == "USE" and pin:
                cur["use"][pin] = t[1]
            elif k == "OBS":
                pin = "__OBS__"
            elif k == "LAYER":
                layer = t[1]
            elif k == "RECT":
                vals = [v for v in t[1:] if v not in (";", "MASK")]
                r = tuple(float(v) for v in vals[-4:])
                if pin == "__OBS__":
                    if layer == via1_layer:
                        cur["obs"].append(r)
                    elif layer == pin_layer:
                        cur["obs_pin"].append(r)
                elif pin is not None and layer == pin_layer:
                    cur["pins"][pin].append(r)
            elif k == "END" and len(t) > 1:
                if pin is not None and t[1] == pin:
                    pin = None
                elif t[1] == "MACRO" or (cur is not None and t[1] in macros and macros[t[1]] is cur):
                    cur = pin = None
    return macros, sites


def rect_dist(a, b):
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def inside_union(box, rects):
    """True if box lies inside the union of rects (exact, by coordinate compression)."""
    xs = sorted(set([box[0], box[2]] + [v for r in rects for v in (r[0], r[2]) if box[0] < v < box[2]]))
    ys = sorted(set([box[1], box[3]] + [v for r in rects for v in (r[1], r[3]) if box[1] < v < box[3]]))
    for i in range(len(xs) - 1):
        cx = (xs[i] + xs[i + 1]) / 2
        for k in range(len(ys) - 1):
            cy = (ys[k] + ys[k + 1]) / 2
            if not any(r[0] - EPS <= cx <= r[2] + EPS and r[1] - EPS <= cy <= r[3] + EPS for r in rects):
                return False
    return True


def _snap_patch(box, rects, sp):
    """Grow an M1 patch to nearby pin edges so the merged shape has no notch below `sp`."""
    b = list(box)
    for _ in range(4):
        changed = False
        for r in rects:
            for i, sgn in ((0, -1), (1, -1), (2, 1), (3, 1)):
                if i in (0, 2):
                    if min(b[3], r[3]) - max(b[1], r[1]) <= EPS:
                        continue
                    cands = (r[0], r[2])
                else:
                    if min(b[2], r[2]) - max(b[0], r[0]) <= EPS:
                        continue
                    cands = (r[1], r[3])
                for c in cands:
                    d = (c - b[i]) * sgn
                    if EPS < d < sp - EPS:
                        b[i] = c
                        changed = True
        if not changed:
            break
    return tuple(round(v, 6) for v in b)


class Layer:
    def __init__(self, name, d):
        self.name = name
        self.lef = d.get("lef", name)
        self.gds = tuple(d["gds"])
        self.text = tuple(d["text"]) if d.get("text") else None
        self.width = float(d.get("width", 0.0))
        self.space = float(d.get("space", 0.0))
        self.min_area = float(d.get("min_area", 0.0))
        # [(width >=, parallel run length >=, space)] ; a 2-element entry means prl 0
        self.wide = [(w[0], 0.0, w[1]) if len(w) == 2 else tuple(w) for w in d.get("wide_space", [])]
        # vias
        self.size = float(d.get("size", 0.0))
        self.enc_below = tuple(d.get("enc_below", (0.0, 0.0)))     # (side, end), either orientation
        self.enc_above = tuple(d.get("enc_above", (0.0, 0.0)))


class Tech:
    ROLES = ("inv", "nand2", "nor2", "nor3")

    def __init__(self, path):
        self.path = os.path.abspath(path)
        base = os.path.dirname(self.path)
        d = json.load(open(self.path))
        self.raw = d
        self.name = d.get("name", os.path.splitext(os.path.basename(path))[0])
        f = d["files"]
        self.lef = [_expand(p, base) for p in (f["lef"] if isinstance(f["lef"], list) else [f["lef"]])]
        self.gds_file = _expand(f["gds"], base)
        self.verilog = _expand(f["verilog"], base) if f.get("verilog") else None
        for p in self.lef + [self.gds_file]:
            if not os.path.isfile(p):
                raise FileNotFoundError("tech %s: file not found: %s" % (self.name, p))
        self.mfg = float(d.get("manufacturing_grid", 0.005))
        L = d["layers"]
        self.m1, self.m2, self.m3 = (Layer(k, L[k]) for k in ("pin", "vertical", "horizontal"))
        self.v1, self.v2 = Layer("via1", L["via1"]), Layer("via2", L["via2"])
        self.boundary = tuple(L["boundary"]) if L.get("boundary") else None
        self.keepout = {k: (tuple(v["gds"]), float(v["space"])) for k, v in L.get("via1_keepout", {}).items()}
        self.via_stack = bool(d.get("rules", {}).get("via_stack", True))
        self.via12_space = float(d.get("rules", {}).get("via1_via2_space", 0.0))
        self.obs_space = float(d.get("rules", {}).get("via1_obs_space", self.v1.space))
        pw = d.get("power", {})
        self.vdd, self.vss = pw.get("vdd", "VDD"), pw.get("vss", "VSS")

        self.macros, sites = parse_lef(self.lef, self.m1.lef, self.v1.lef)
        C = d["cells"]
        self.cells = {}
        for role in self.ROLES:
            c = C[role]
            c = {"cell": c} if isinstance(c, str) else c
            self.cells[role] = {"cell": c["cell"], "pins": c.get("pins") or {p: p for p in self._logic_pins(role)}}
            if c["cell"] not in self.macros:
                raise KeyError("tech %s: cell %s (%s) not in LEF" % (self.name, c["cell"], role))
        self.fill = sorted(C["fill"], key=lambda c: -self.macros[c]["w"])

        # site
        if d.get("site"):
            s = d["site"]
            self.site_w, self.row_h = (float(s[0]), float(s[1])) if isinstance(s, list) else (sites[s]["w"], sites[s]["h"])
        else:
            smallest = self.macros[self.fill[-1]]
            self.site_w, self.row_h = smallest["w"], smallest["h"]
        for c in self.fill:
            assert abs(self.macros[c]["w"] / self.site_w - round(self.macros[c]["w"] / self.site_w)) < EPS, c

        self._derive_grid(d.get("grid", {}))
        self._rails()
        self._access = {}
        for role in self.ROLES:
            self.access(self.cells[role]["cell"])        # fail early

    @staticmethod
    def _logic_pins(role):
        return {"inv": "AY", "nand2": "ABY", "nor2": "ABY", "nor3": "ABCY"}[role]

    # ------------------------------------------------------------------ grid
    def _pad(self, via, enc, layer, vertical, gx=None):
        """Metal pad (w, h) around a via on `layer`, enclosure oriented to suit the grid."""
        s, e = sorted(enc)
        opts = [(via + 2 * s, via + 2 * e), (via + 2 * e, via + 2 * s)]
        opts = [(max(w, layer.width), max(h, layer.width)) for w, h in opts]
        if gx is not None:
            ok = [o for o in opts if o[0] + layer.space <= gx + EPS]
            if ok:
                opts = ok
        return min(opts, key=lambda o: o[1])     # smallest height -> smallest y pitch

    def _derive_grid(self, g):
        self.gx = float(g.get("x_pitch", self.site_w))
        m2, m3, v1, v2 = self.m2, self.m3, self.v1, self.v2
        self.pad_v1_m2 = self._pad(v1.size, v1.enc_above, m2, True, self.gx)
        self.pad_v2_m2 = self._pad(v2.size, v2.enc_below, m2, True, self.gx)
        self.pad_v2_m3 = self._pad(v2.size, v2.enc_above, m3, False, self.gx)
        self.pad_v1_m1 = self._pad(v1.size, v1.enc_below, self.m1, True)
        need_x = max(max(self.pad_v1_m2[0], self.pad_v2_m2[0]) + m2.space,
                     self.pad_v2_m3[0] + m3.space, v2.size + v2.space)
        if need_x > self.gx + EPS:
            raise ValueError("tech %s: x pitch %.3f too small for the via pads (need %.3f)"
                             % (self.name, self.gx, need_x))
        need_y = max(max(self.pad_v1_m2[1], self.pad_v2_m2[1]) + m2.space,
                     max(self.pad_v2_m3[1], m3.width) + m3.space, v2.size + v2.space)
        self.gy = float(g["y_pitch"]) if g.get("y_pitch") else ceil_to(need_y, 2 * self.mfg)
        if self.gy + EPS < need_y:
            raise ValueError("tech %s: y_pitch %.3f < required %.3f" % (self.name, self.gy, need_y))
        # pin stubs must be long enough for the vertical layer's minimum area
        self.m2_min_len = m2.min_area / m2.width if m2.min_area else 0.0
        self.m3_min_len = m3.min_area / m3.width if m3.min_area else 0.0
        offs = [float(g["x_offset"])] if "x_offset" in g else [round(self.gx / 2, 6), 0.0]
        for off in offs:
            self.x_off = off
            try:
                self._access = {}
                for role in self.ROLES:
                    self.access(self.cells[role]["cell"])
                break
            except RuntimeError as e:
                err = e
        else:
            raise RuntimeError("tech %s: no track offset gives pin access: %s" % (self.name, err))

    def _rails(self):
        """Rail centre y (cell coordinates, R0) of VDD/VSS from the smallest filler."""
        m = self.macros[self.fill[-1]]
        self.rail = {}
        for net in (self.vdd, self.vss):
            rs = [r for r in m["pins"].get(net, []) if r[2] - r[0] >= m["w"] - EPS]
            if not rs:
                raise RuntimeError("tech %s: no %s rail in %s" % (self.name, net, self.fill[-1]))
            r = max(rs, key=lambda r: r[2] - r[0])
            self.rail[net] = ((r[1] + r[3]) / 2, r[3] - r[1])

    # ------------------------------------------------------------------ cells
    def width_sites(self, cell):
        return int(round(self.macros[cell]["w"] / self.site_w))

    def signal_pins(self, cell):
        m = self.macros[cell]
        return [p for p in m["pins"] if m["use"].get(p, "SIGNAL") not in ("POWER", "GROUND")
                and p not in (self.vdd, self.vss)]

    def access(self, cell):
        """{pin: (x, y, patch)} VIA1 position (cell coordinates) of every signal pin.

        x is on a vertical-layer track and the via sits on the pin metal, clear of
        via1-layer obstructions. If the pin metal itself does not give the via1 'below'
        enclosure, an M1 patch (w, h) is added, provided it keeps the pin-layer spacing
        to every other shape of the cell. Pins of one cell use different tracks.
        Preference: no patch, then y closest to the middle of the cell.
        """
        if cell in self._access:
            return self._access[cell]
        m = self.macros[cell]
        v, (s, e) = self.v1.size, sorted(self.v1.enc_below)
        sp = self.m1.space
        step = self.mfg * 2
        cand = {}
        for p in self.signal_pins(cell):
            rects = m["pins"][p]
            others = [r for q, rs in m["pins"].items() if q != p for r in rs] + m["obs_pin"]
            out = []
            n0 = int(math.floor(-self.x_off / self.gx)) - 1
            for n in range(n0, n0 + self.width_sites(cell) + 3):
                x = round(self.x_off + self.gx * n, 6)
                if not (EPS < x < m["w"] - EPS):
                    continue
                ylo = min(r[1] for r in rects)
                yhi = max(r[3] for r in rects)
                k0, k1 = int(math.ceil(ylo / step)), int(math.floor(yhi / step))
                best = None
                for k in range(k0, k1 + 1):
                    y = round(k * step, 6)
                    via = (x - v / 2, y - v / 2, x + v / 2, y + v / 2)
                    on_pin = inside_union(via, rects)
                    if not on_pin and not any(min(via[2], r[2]) - max(via[0], r[0]) >= v / 2 - EPS and
                                              min(via[3], r[3]) - max(via[1], r[1]) >= v / 2 - EPS for r in rects):
                        continue       # a patch can still join the via to the pin
                    if any(rect_dist(via, o) < self.obs_space - EPS for o in m["obs"]):
                        continue
                    patch, cost = None, None
                    for ex, ey in ((s, e), (e, s)):
                        box = (via[0] - ex, via[1] - ey, via[2] + ex, via[3] + ey)
                        if on_pin and inside_union(box, rects):
                            cost = 0
                            break
                        w, h = max(box[2] - box[0], self.m1.width), max(box[3] - box[1], self.m1.width)
                        box = _snap_patch((x - w / 2, y - h / 2, x + w / 2, y + h / 2), rects, sp)
                        if box[0] < sp / 2 - EPS or box[2] > m["w"] - sp / 2 + EPS:
                            continue
                        if all(rect_dist(box, o) >= sp - EPS for o in others) and cost is None:
                            patch = tuple(round(v - c, 6) for v, c in zip(box, (x, y, x, y)))
                            cost = 1
                    if cost is None:
                        continue
                    key = (cost, abs(y - m["h"] / 2))
                    if best is None or key < best[0]:
                        best = (key, (x, y, patch))
                if best is not None:
                    out.append(best)
            if not out:
                raise RuntimeError("%s.%s: no legal VIA1 position on the track grid (offset %.3f)"
                                   % (cell, p, self.x_off))
            cand[p] = [pt for _, pt in sorted(out)]
        chosen = {}

        def solve(order):
            if not order:
                return True
            p = order[0]
            for pt in cand[p]:
                if all(abs(pt[0] - q[0]) > EPS for q in chosen.values()):
                    chosen[p] = pt
                    if solve(order[1:]):
                        return True
                    del chosen[p]
            return False

        if not solve(sorted(cand, key=lambda p: len(cand[p]))):  # noqa
            raise RuntimeError("%s: pins cannot get distinct tracks" % cell)
        self._access[cell] = chosen
        return chosen

    def summary(self):
        return ("%s: site %.3f x %.3f, grid %.3f x %.3f (x offset %.3f), cells %s"
                % (self.name, self.site_w, self.row_h, self.gx, self.gy, self.x_off,
                   ", ".join("%s=%s" % (r, self.cells[r]["cell"]) for r in self.ROLES)))


_CACHE = {}


def load(path=None):
    """Load a tech file (default: $DLA_TECH)."""
    path = path or os.environ.get("DLA_TECH")
    if not path:
        raise EnvironmentError("no tech file: pass --tech <file.json> or set DLA_TECH")
    path = os.path.abspath(path)
    if path not in _CACHE:
        _CACHE[path] = Tech(path)
    return _CACHE[path]
