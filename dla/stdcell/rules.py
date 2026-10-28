"""Front-end description for the std-cell generator (the ``stdcell`` section of a tech file).

Only design rules and layer numbers are needed - no std-cell library. The metal-1
rules (layer ``pin``) and the VIA1 enclosure (layer ``via1``) are shared with the block
generators, so generated cells are pin-accessible from the block router's track grid.

From the rules the vertical cell template (taps, active bands, implant / well edges,
poly-contact window) and the horizontal device pitches are derived here.
"""
import json
import math
import os

from ..tech import EPS, Layer

RULES = {
    # name                  meaning (all in um)
    "activ_width":          "min active width",
    "activ_space":          "min active space",
    "poly_width":           "min poly width (drawn gate length if devices.l is not given)",
    "poly_space":           "min poly space",
    "poly_endcap":          "min poly extension beyond active (gate end cap)",
    "poly_activ_space":     "min poly (field) space to active",
    "cont_size":            "contact size (square)",
    "cont_space":           "min contact space",
    "cont_activ_enc":       "min active enclosure of contact",
    "cont_poly_enc":        "min poly enclosure of contact",
    "cont_gate_space":      "min space contact on active to gate",
    "polycont_activ_space": "min space contact on poly to active",
    "m1_cont_enc":          "[side, end] metal-1 enclosure of contact",
    "nwell_enc_pdiff":      "min n-well enclosure of p+ active (PMOS)",
    "nwell_space_ndiff":    "min n-well space to n+ active (NMOS)",
    "nwell_enc_ntap":       "min n-well enclosure of n+ tap",
    "nwell_space_ptap":     "min n-well space to p+ tap",
    "pimp_enc_pdiff":       "min p-implant enclosure of p+ active",
    "pimp_space_ndiff":     "min p-implant space to n+ active",
    "pimp_enc_ptap":        "min p-implant enclosure of p+ tap",
    "pimp_space_ntap":      "min p-implant space to n+ tap",
    "pimp_enc_gate":        "min p-implant enclosure of a PMOS gate",
    "pimp_space_gate":      "min p-implant space to an NMOS gate",
    "pimp_cont_space":      "min p-implant edge distance to a contact on active (both sides)",
}


def snap(v, g):
    return round(round(v / g) * g, 6)


def snap_up(v, g):
    return round(math.ceil(v / g - 1e-6) * g, 6)


def snap_dn(v, g):
    return round(math.floor(v / g + 1e-6) * g, 6)


class StdTech:
    """Rules + derived template. Loads a tech file without needing any library files."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        d = json.load(open(self.path))
        self.raw = d
        self.name = d.get("name", os.path.splitext(os.path.basename(path))[0])
        if "stdcell" not in d:
            raise KeyError("tech %s has no 'stdcell' section (see tech/TECH_FILE.md)" % self.name)
        s = d["stdcell"]
        self.mfg = float(d.get("manufacturing_grid", 0.005))
        L = d["layers"]
        self.m1 = Layer("pin", L["pin"])
        self.v1 = Layer("via1", L["via1"])
        pw = d.get("power", {})
        self.vdd, self.vss = pw.get("vdd", "VDD"), pw.get("vss", "VSS")
        self.row_h = float(s["row_height"])
        self.site_w = float(s["site_width"])
        self.site_name = s.get("site_name", "CoreSite")
        g = d.get("grid", {})
        self.gx = float(g.get("x_pitch", self.site_w))
        self.x_off = float(g.get("x_offset", 0.0))
        lay = s["layers"]
        self.lay = {k: (tuple(v) if v else None) for k, v in lay.items()}
        for k in ("activ", "poly", "cont", "nwell", "pimp"):
            if not self.lay.get(k):
                raise KeyError("tech %s: stdcell.layers.%s missing" % (self.name, k))
        self.lay["m1"] = self.m1.gds
        self.lay["m1_text"] = self.m1.text
        self.lay.setdefault("nimp", None)
        self.lay.setdefault("pin", None)
        if L.get("boundary") and not self.lay.get("boundary"):
            self.lay["boundary"] = tuple(L["boundary"])
        # VIA1 keep-out from contacts (layers.via1_keepout of the block tech): pin access vias
        # must keep it, and the LEF reports every contact as a VIA1 obstruction
        self.v1_cont_space = max([float(v["space"]) for v in L.get("via1_keepout", {}).values()
                                  if tuple(v["gds"]) == self.lay["cont"]] or [0.0])
        r = s["rules"]
        missing = [k for k in RULES if k not in r]
        if missing:
            raise KeyError("tech %s: stdcell.rules missing %s" % (self.name, ", ".join(missing)))
        self.r = {k: (tuple(float(x) for x in v) if isinstance(v, list) else float(v)) for k, v in r.items()}
        dv = s["devices"]
        self.nmos, self.pmos = dv["nmos"], dv["pmos"]
        self.L = float(dv.get("l", self.r["poly_width"]))
        self.wn, self.wp = float(dv["wn"]), float(dv["wp"])
        self.rail_w = float(s.get("rail_width", 0.0))
        self.spice_units = dv.get("spice", "u")       # "u": w=0.74u, or "m": SI metres
        self._derive()

    # ------------------------------------------------------------------ template
    def _derive(self):
        r, g = self.r, self.mfg
        c, ca = r["cont_size"], r["cont_activ_enc"]
        es, ee = sorted(r["m1_cont_enc"])
        H = self.row_h
        self.tap_h = snap_up(max(c + 2 * ca, r["activ_width"]), 2 * g)
        self.rail_w = max(self.rail_w, c + 2 * ee, self.m1.width)
        pcs = r["pimp_cont_space"]
        ptap_imp = max(self.tap_h / 2 + r["pimp_enc_ptap"], c / 2 + pcs)  # p-implant half height on the VSS tap
        # NMOS band: above the VSS tap (contacts on n+ active sit >= ca inside the band)
        nb = max(self.tap_h / 2 + r["activ_space"],
                 ptap_imp + max(r["pimp_space_ndiff"], r["pimp_space_gate"], pcs - ca),
                 self.tap_h / 2 + r["poly_activ_space"] + r["poly_endcap"])
        self.nb = snap_up(nb, g)
        self.nt = round(self.nb + self.wn, 6)
        # PMOS band: below the VDD tap; the p-implant stops short of the n+ tap and its contacts
        self.pimp_top = round(min(H - self.tap_h / 2 - r["pimp_space_ntap"], H - c / 2 - pcs), 6)
        pt = H - max(self.tap_h / 2 + r["activ_space"],
                     H - self.pimp_top + max(r["pimp_enc_pdiff"], r["pimp_enc_gate"], pcs - ca),
                     self.tap_h / 2 + r["poly_activ_space"] + r["poly_endcap"])
        self.pt = snap_dn(pt, g)
        self.pb = round(self.pt - self.wp, 6)
        # well / implant edge between the bands
        lo_w, hi_w = self.nt + r["nwell_space_ndiff"], self.pb - r["nwell_enc_pdiff"]
        lo_i = self.nt + max(r["pimp_space_ndiff"], r["pimp_space_gate"], pcs - ca)
        hi_i = self.pb - max(r["pimp_enc_pdiff"], r["pimp_enc_gate"], pcs - ca)
        if lo_w > hi_w + EPS or lo_i > hi_i + EPS:
            raise ValueError("tech %s: row height %.3f too small for wn=%.3f, wp=%.3f "
                             "(no room for the n-well / implant edge)" % (self.name, H, self.wn, self.wp))
        self.nwell_bot = snap((max(lo_w, lo_i) + min(hi_w, hi_i)) / 2, g) \
            if max(lo_w, lo_i) <= min(hi_w, hi_i) + EPS else snap((lo_w + hi_w) / 2, g)
        self.pimp_bot = min(max(self.nwell_bot, lo_i), hi_i)
        self.nwell_top = round(H + self.tap_h / 2 + r["nwell_enc_ntap"], 6)
        self.nwell_x = r["nwell_enc_ntap"]
        self.ptap_imp = round(ptap_imp, 6)
        # p-implant must cover p+ active / gates / contacts up to the cell edge (abutting cells merge)
        self.amargin = snap_up(r["activ_space"] / 2, g)               # active to cell edge
        self.pimp_x = snap_up(max(r["pimp_enc_ptap"], r["pimp_enc_pdiff"] - self.amargin,
                                  pcs - ca - self.amargin), g)
        # poly contact window in the channel (contact centre y)
        self.pc = snap_up(c + 2 * r["cont_poly_enc"], 2 * g)          # poly pad (square)
        lo = max(self.nt + r["polycont_activ_space"] + c / 2, self.nt + r["poly_activ_space"] + self.pc / 2)
        hi = min(self.pb - r["polycont_activ_space"] - c / 2, self.pb - r["poly_activ_space"] - self.pc / 2)
        self.pad_y = (snap_up(lo, g), snap_dn(hi, g))
        if self.pad_y[0] > self.pad_y[1] + EPS:
            raise ValueError("tech %s: no room for a poly contact between the n and p active" % self.name)
        # metal-1 usable y range (wire centre), away from the wide rails
        self.rail_space = max([self.m1.space] + [s for w, prl, s in self.m1.wide if w <= self.rail_w + EPS])
        self.m1_ylo = round(self.rail_w / 2 + self.rail_space + self.m1.width / 2, 6)
        self.m1_yhi = round(H - self.m1_ylo, 6)
        # device pitches (x)
        L = self.L
        self.x_cont_gate = round(c / 2 + r["cont_gate_space"] + L / 2, 6)           # contact centre -> gate centre
        self.x_gate_unc = snap_up(max(L + r["poly_space"], L / 2 + r["poly_space"] + self.pc / 2), g)
        self.x_cont_edge = round(c / 2 + ca, 6)                                        # contact centre -> active edge
        self.m1_edge = snap_up(self.m1.space / 2, g)                                   # metal-1 to cell edge
        # contacts on the bands
        self.cont_pitch = round(c + r["cont_space"], 6)
        self.poly_bot = round(self.nb - r["poly_endcap"], 6)
        self.poly_top = round(self.pt + r["poly_endcap"], 6)

    def band_contacts(self, y0, y1, n=None):
        """Centres of contacts stacked in an active band [y0, y1] (all that fit, or n)."""
        c, ca = self.r["cont_size"], self.r["cont_activ_enc"]
        room = (y1 - y0) - 2 * ca - c
        k = int(math.floor(room / self.cont_pitch + EPS)) + 1
        if n:
            k = max(1, min(k, n))
        span = (k - 1) * self.cont_pitch
        yc = snap((y0 + y1) / 2 - span / 2, self.mfg)
        return [round(yc + i * self.cont_pitch, 6) for i in range(k)]

    def summary(self):
        return ("%s: row %.3f, site %.3f, NMOS %.3f..%.3f, PMOS %.3f..%.3f, nwell from %.3f, "
                "poly pad y %.3f..%.3f, gate pitch %.3f / %.3f"
                % (self.name, self.row_h, self.site_w, self.nb, self.nt, self.pb, self.pt, self.nwell_bot,
                   self.pad_y[0], self.pad_y[1], 2 * self.x_cont_gate, self.x_gate_unc))


def load(path=None):
    path = path or os.environ.get("DLA_TECH")
    if not path:
        raise EnvironmentError("no tech file: pass --tech <file.json> or set DLA_TECH")
    return StdTech(path)
