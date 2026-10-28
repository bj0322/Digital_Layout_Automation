"""Library views of generated cells: GDS (pure Python), LEF, SPICE, Verilog, tech file.

The GDS writer needs no KLayout, so a library (and a tech file for the block
generators) can be produced on any machine.
"""
import datetime
import json
import math
import os
import struct


# ----------------------------------------------------------------------------- GDS
def _rec(rt, dt, data=b""):
    return struct.pack(">HBB", 4 + len(data), rt, dt) + data


def _i2(*v):
    return struct.pack(">%dh" % len(v), *v)


def _i4(*v):
    return struct.pack(">%di" % len(v), *v)


def _s(t):
    b = t.encode("ascii")
    return b + (b"\0" if len(b) % 2 else b"")


def _r8(v):
    """GDSII 8-byte excess-64 base-16 real."""
    if v == 0:
        return b"\0" * 8
    sign = 0x80 if v < 0 else 0
    v = abs(v)
    e = int(math.floor(math.log(v, 16))) + 1
    m = int(round(v / 16.0 ** e * 2 ** 56))
    if m >= 2 ** 56:
        m >>= 4
        e += 1
    return bytes([sign | (e + 64)]) + m.to_bytes(7, "big")


ORIENTS = ("R0", "MX", "MY", "R180")


def orient_origin(orient, x, yb, W, H):
    """Reference origin of a cell with box [0,W]x[0,H] placed with its lower-left box corner at (x, yb)."""
    if orient == "R0":
        return x, yb
    if orient == "MX":                 # vertical flip   (u, v) -> (x + u, yb + H - v)
        return x, yb + H
    if orient == "MY":                 # horizontal flip (u, v) -> (x + W - u, yb + v)
        return x + W, yb
    if orient == "R180":               # both            (u, v) -> (x + W - u, yb + H - v)
        return x + W, yb + H
    raise ValueError("orientation %r (expected one of %s)" % (orient, ", ".join(ORIENTS)))


def orient_point(orient, u, v, x, yb, W, H):
    """Where the cell point (u, v) lands for ``orient`` placed at lower-left (x, yb)."""
    fx, fy = orient in ("MY", "R180"), orient in ("MX", "R180")
    orient_origin(orient, x, yb, W, H)         # validates
    return (x + (W - u if fx else u), yb + (H - v if fy else v))


class GdsWriter:
    """Boxes, texts and (optionally mirrored) cell references."""

    def __init__(self, libname, dbu=0.001):
        self.dbu = dbu
        now = datetime.datetime(2026, 1, 1)
        t = (now.year, now.month, now.day, now.hour, now.minute, now.second)
        self.out = [_rec(0x00, 0x02, _i2(600)), _rec(0x01, 0x02, _i2(*(t + t))),
                    _rec(0x02, 0x06, _s(libname)), _rec(0x03, 0x05, _r8(dbu) + _r8(dbu * 1e-6))]
        self._t = t

    def _xy(self, pts):
        return _rec(0x10, 0x03, _i4(*[int(round(c / self.dbu)) for p in pts for c in p]))

    def begin(self, name):
        self.out += [_rec(0x05, 0x02, _i2(*(self._t + self._t))), _rec(0x06, 0x06, _s(name))]

    def box(self, ld, r):
        x1, y1, x2, y2 = r
        self.out += [_rec(0x08, 0x00), _rec(0x0D, 0x02, _i2(ld[0])), _rec(0x0E, 0x02, _i2(ld[1])),
                     self._xy([(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]), _rec(0x11, 0x00)]

    def text(self, ld, s, x, y):
        self.out += [_rec(0x0C, 0x00), _rec(0x0D, 0x02, _i2(ld[0])), _rec(0x16, 0x02, _i2(ld[1])),
                     self._xy([(x, y)]), _rec(0x19, 0x06, _s(s)), _rec(0x11, 0x00)]

    def sref(self, name, x, y, mirror_x=False, orient=None, size=None):
        """Cell reference. Legacy form: (x, y) is the reference origin, mirror_x flips about x.

        With ``orient`` in R0 / MX / MY / R180 and ``size=(W, H)`` of the cell's [0,W]x[0,H]
        box, (x, y) is the lower-left corner of the placed box (see :func:`orient_origin`).
        """
        if orient is not None:
            x, y = orient_origin(orient, x, y, *size)
            mirror_x = orient in ("MX", "MY")
            angle = 180.0 if orient in ("MY", "R180") else 0.0
        else:
            angle = 0.0
        self.out += [_rec(0x0A, 0x00), _rec(0x12, 0x06, _s(name))]
        if mirror_x or angle:
            self.out.append(_rec(0x1A, 0x01, struct.pack(">H", 0x8000 if mirror_x else 0)))
        if angle:
            self.out.append(_rec(0x1C, 0x05, _r8(angle)))
        self.out += [self._xy([(x, y)]), _rec(0x11, 0x00)]

    def end(self):
        self.out.append(_rec(0x07, 0x00))

    def write(self, path):
        with open(path, "wb") as f:
            f.write(b"".join(self.out + [_rec(0x04, 0x00)]))


def write_cell_gds(gw, st, cl):
    gw.begin(cl.name)
    for layer, r, net in cl.rects:
        ld = st.lay.get(layer)
        if ld:
            gw.box(ld, r)
        if layer == "m1" and st.lay.get("pin") and net in cl.pins:
            gw.box(st.lay["pin"], r)
    for layer, s, x, y in cl.texts:
        ld = st.lay.get(layer)
        if ld:
            gw.text(ld, s, x, y)
    gw.end()


def write_gds(path, libname, st, cells, test_rows=None):
    """Library GDS. test_rows: optional [(top name, [[cell names] per row]])] abutment test."""
    gw = GdsWriter(libname)
    for cl in cells:
        write_cell_gds(gw, st, cl)
    if test_rows:
        width = {cl.name: cl.width for cl in cells}
        top, rows = test_rows
        gw.begin(top)
        for k, row in enumerate(rows):
            x, mx = 0.0, k % 2 == 1
            for name in row:
                gw.sref(name, x, (k + 1) * st.row_h if mx else k * st.row_h, mirror_x=mx)
                x = round(x + width[name], 6)
        gw.end()
    gw.write(path)


# ----------------------------------------------------------------------------- LEF
def write_lef(path, st, cells):
    m1 = st.m1.lef
    L = ["VERSION 5.8 ;", 'BUSBITCHARS "[]" ;', 'DIVIDERCHAR "/" ;', "",
         "SITE %s" % st.site_name, "  CLASS CORE ;", "  SYMMETRY Y ;",
         "  SIZE %.3f BY %.3f ;" % (st.site_w, st.row_h), "END %s" % st.site_name, ""]
    for cl in cells:
        L += ["MACRO %s" % cl.name, "  CLASS CORE ;", "  ORIGIN 0 0 ;",
              "  SIZE %.3f BY %.3f ;" % (cl.width, st.row_h), "  SYMMETRY X Y ;", "  SITE %s ;" % st.site_name]
        m1 = cl.m1()
        for p in cl.pins:
            if p in (st.vdd, st.vss):
                use, d = ("POWER" if p == st.vdd else "GROUND"), "INOUT"
            else:
                outs = set(getattr(cl.netlist, "outputs", ("Y",))) if cl.netlist else {"Y"}
                use, d = "SIGNAL", "OUTPUT" if p in outs else "INPUT"
            L += ["  PIN %s" % p, "    DIRECTION %s ;" % d, "    USE %s ;" % use]
            if use != "SIGNAL":
                L.append("    SHAPE ABUTMENT ;")
            L += ["    PORT", "      LAYER %s ;" % st.m1.lef]
            L += ["        RECT %.3f %.3f %.3f %.3f ;" % r for r, n in m1 if n == p]
            L += ["    END", "  END %s" % p]
        obs = [r for r, n in m1 if n not in cl.pins]
        conts = [r for l, r, n in cl.rects if l == "cont"] if st.v1_cont_space else []
        if obs or conts:
            L.append("  OBS")
            if obs:
                L += ["    LAYER %s ;" % st.m1.lef] + ["      RECT %.3f %.3f %.3f %.3f ;" % r for r in obs]
            if conts:      # contacts block VIA1 (keep-out rule of the PDK)
                L += ["    LAYER %s SPACING %g ;" % (st.v1.lef, st.v1_cont_space)]
                L += ["      RECT %.3f %.3f %.3f %.3f ;" % r for r in conts]
            L.append("  END")
        L += ["END %s" % cl.name, ""]
    L.append("END LIBRARY")
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")


# ----------------------------------------------------------------------------- netlists
def write_spice(path, st, cells, libname):
    with open(path, "w") as f:
        f.write("* %s: generated by Digital_Layout_Auto (tech %s)\n\n" % (libname, st.name))
        for cl in cells:
            if cl.netlist is not None:
                f.write(cl.netlist.spice(st.nmos, st.pmos, st.L, st.spice_units) + "\n")
            else:
                f.write(".SUBCKT %s %s %s\n.ENDS %s\n\n" % (cl.name, st.vdd, st.vss, cl.name))


def write_verilog(path, cells, libname):
    with open(path, "w") as f:
        f.write("// %s: behavioural models, generated by Digital_Layout_Auto\n" % libname)
        for cl in cells:
            cn = cl.netlist
            f.write("`celldefine\nmodule %s (%s);\n" % (cl.name, ", ".join(cn.inputs + [cn.output]) if cn else ""))
            if cn:
                f.write("  input %s;\n  output %s;\n" % (", ".join(cn.inputs), cn.output))
                f.write("  assign %s = %s;\n" % (cn.output, cn.verilog_expr()))
            f.write("endmodule\n`endcelldefine\n\n")


def write_tech(path, st, files, roles, fills):
    """Copy of the source tech file that points the block generators at the new library."""
    d = json.loads(json.dumps(st.raw))
    d["name"] = d.get("name", st.name) + "_dla"
    d["comment"] = "Generated by Digital_Layout_Auto from %s: blocks built from the generated cells." % st.name
    d["files"] = {k: os.path.basename(v) for k, v in files.items()}
    d["site"] = [st.site_w, st.row_h]
    d["cells"] = dict(roles, fill=fills)
    with open(path, "w") as f:
        json.dump(d, f, indent=2)
