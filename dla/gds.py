"""GDS back end (KLayout): build, metal-level connectivity check, rule-driven DRC, PNG.

Works with either
  * the ``klayout`` Python package  (``pip install klayout``) - used by the CLI, or
  * KLayout's embedded Python (``pya``), via the wrappers in ``klayout_scripts/``.
Everything layer-specific comes from the block JSON / tech file.
"""
try:
    import pya as db                    # inside klayout_app
except ImportError:                     # pip install klayout
    import klayout.db as db


def _li(ly, ld):
    return ly.layer(ld[0], ld[1])


def orient_trans(orient, x, yb, W, H):
    """KLayout transformation of a cell [0,W]x[0,H] placed with lower-left corner (x, yb).

    R0 / MX (vertical flip, the only ones block rows need) / MY (horizontal flip) / R180.
    """
    if orient == "R0":
        return db.DTrans(db.DTrans.R0, x, yb)
    if orient == "MX":
        return db.DTrans(db.DTrans.M0, x, yb + H)
    if orient == "MY":
        return db.DTrans(db.DTrans.M90, x + W, yb)
    if orient == "R180":
        return db.DTrans(db.DTrans.R180, x + W, yb + H)
    raise ValueError("orientation %r (expected R0, MX, MY or R180)" % (orient,))


# ----------------------------------------------------------------------------- build
def build_gds(d, stdgds, out):
    """d: block JSON dict. Instantiates library cells from the std-cell GDS by name."""
    ly = db.Layout()
    ly.read(stdgds)
    top = ly.create_cell(d["top"])
    H = d["row_h"]
    for name, cell, x, yb, orient in d["insts"]:
        c = ly.cell(cell)
        if c is None:
            raise KeyError("cell %s not in %s" % (cell, stdgds))
        if orient in ("MY", "R180") and cell not in d.get("cells", {}):
            raise KeyError("cell %s has no width in the block JSON (needed for orientation %s)" % (cell, orient))
        t = orient_trans(orient, x, yb, d["cells"][cell]["w"] if orient in ("MY", "R180") else 0.0, H)
        top.insert(db.DCellInstArray(c.cell_index(), t))
    L = d["layers"]
    li = {k: _li(ly, L[k]) for k in ("m1", "v1", "m2", "v2", "m3")}
    for layer, x1, y1, x2, y2, net in d["shapes"]:
        top.shapes(li[layer]).insert(db.DBox(x1, y1, x2, y2))
    for layer, x, y, net in d["vias"]:
        s = d["via_size"][layer] / 2
        top.shapes(li["v1" if layer == "via1" else "v2"]).insert(db.DBox(x - s, y - s, x + s, y + s))
    for p in d["pins"]:
        tl = L.get(p.get("layer", "m2") + "_text")
        if tl:
            top.shapes(_li(ly, tl)).insert(db.DText(p["name"], p["x"], p["y"]))
    if L.get("boundary"):
        top.shapes(_li(ly, L["boundary"])).insert(db.DBox(*d["boundary"]))
    for k in ("m1", "m2", "m3"):          # merge touching routing shapes
        r = db.Region(top.shapes(li[k]))
        r.merge()
        top.shapes(li[k]).clear()
        top.shapes(li[k]).insert(r)
    for c in list(ly.top_cells()):        # drop unused library cells
        if c.cell_index() != top.cell_index():
            c.prune_cell()
    ly.write(out)
    b = top.dbbox()
    return b.width(), b.height()


# ----------------------------------------------------------------------------- connectivity
def verify_conn(d, gds):
    """Extract m1-v1-m2-v2-m3 nets from the GDS and compare with the intended nets.

    Probes every signal pin, every cell's power rails and every pin label.
    """
    ly = db.Layout()
    ly.read(gds)
    top = ly.cell(d["top"])
    L = d["layers"]
    l2n = db.LayoutToNetlist(db.RecursiveShapeIterator(ly, top, []))
    R = {k: l2n.make_layer(_li(ly, L[k]), k) for k in ("m1", "v1", "m2", "v2", "m3")}
    for k in R:
        l2n.connect(R[k])
    for a, b in (("m1", "v1"), ("v1", "m2"), ("m2", "v2"), ("v2", "m3")):
        l2n.connect(R[a], R[b])
    l2n.extract_netlist()

    def probe(layer, x, y):
        n = l2n.probe_net(R[layer], db.DPoint(x, y))
        return None if n is None else n.cluster_id

    H = d["row_h"]
    probes = []
    for inst, pin, x, y, net in d["pinpos"]:
        probes.append((net, "%s.%s" % (inst, pin), probe("m1", x, y)))
    for name, cell, x, yb, orient in d["insts"]:
        xc = x + d["cells"][cell]["w"] / 2
        for net, yc in d["rails"].items():
            flip = orient in ("MX", "R180")
            probes.append((net, name + ".rail", probe("m1", xc, yb + (H - yc if flip else yc))))
    for p in d["pins"]:
        probes.append((p["name"], "label " + p["name"], probe(p.get("layer", "m2"), p["x"], p["y"])))

    by_net, by_cl, missing = {}, {}, []
    for net, what, cl in probes:
        if cl is None:
            missing.append((net, what))
            continue
        by_net.setdefault(net, set()).add(cl)
        by_cl.setdefault(cl, set()).add(net)
    opens = {n: len(c) for n, c in by_net.items() if len(c) > 1}
    shorts = [sorted(n) for n in by_cl.values() if len(n) > 1]
    return {"probes": len(probes), "missing": missing, "opens": opens, "shorts": shorts,
            "ok": not (missing or opens or shorts)}


# ----------------------------------------------------------------------------- DRC
def drc(gds, topname, tech):
    """Rule-driven DRC of the three metals and two vias, from the tech file.

    Width, spacing, wide-metal spacing, minimum area, via size and spacing, rectangular
    via enclosure (side/end, either orientation), via1 keep-outs, via stacking.
    Front-end layers are not checked: run your PDK's sign-off deck for that.
    """
    ly = db.Layout()
    ly.read(gds)
    top = ly.cell(topname)
    um = lambda v: int(round(v / ly.dbu))

    def reg(ld):
        r = db.Region(top.begin_shapes_rec(_li(ly, ld)))
        r.merge()
        return r

    M = {k: reg(getattr(tech, k).gds) for k in ("m1", "m2", "m3")}
    VI = {k: reg(getattr(tech, k).gds) for k in ("v1", "v2")}
    res = {}

    def add(rule, n):
        if n:
            res[rule] = res.get(rule, 0) + n

    for k in ("m1", "m2", "m3"):
        lay, r = getattr(tech, k), M[k]
        add("%s.width" % lay.lef, r.width_check(um(lay.width)).count())
        add("%s.space" % lay.lef, r.space_check(um(lay.space)).count())
        for w, prl, s in lay.wide:
            wide = r.sized(-um(w / 2)).sized(um(w / 2))
            # positional arguments: older KLayout versions take no keywords here
            a = (False, db.Region.Projection, None, um(prl) if prl else None, None)
            n = wide.separation_check(r - wide, um(s), *a).count() + wide.space_check(um(s), *a).count()
            add("%s.wide_space(w>=%.2f,prl>=%.2f)" % (lay.lef, w, prl), n)
        if lay.min_area:
            add("%s.area" % lay.lef, r.with_area(0, int(lay.min_area / ly.dbu ** 2), False).count())
    for k, below, above in (("v1", "m1", "m2"), ("v2", "m2", "m3")):
        v, r = getattr(tech, k), VI[k]
        sz = um(v.size)
        add("%s.size" % v.lef, sum(1 for p in r.each() if p.bbox().width() != sz or p.bbox().height() != sz))
        add("%s.space" % v.lef, r.space_check(um(v.space)).count())
        for mk, enc in ((below, v.enc_below), (above, v.enc_above)):
            s, e = sorted(enc)
            bad = 0
            for p in r.each():
                b = p.bbox()
                ok = False
                for ex, ey in ((s, e), (e, s)):
                    g = db.Region(db.Box(b.left - um(ex), b.bottom - um(ey), b.right + um(ex), b.top + um(ey)))
                    if (g - M[mk]).is_empty():
                        ok = True
                        break
                bad += not ok
            add("%s.enclosure_by_%s" % (v.lef, getattr(tech, mk).lef), bad)
    for name, (ld, sp) in tech.keepout.items():
        other = reg(ld)
        add("%s-%s.space" % (tech.v1.lef, name), VI["v1"].separation_check(other, um(sp)).count()
            + (VI["v1"] & other).count())
    if not tech.via_stack:
        add("%s-on-%s" % (tech.v2.lef, tech.v1.lef), (VI["v2"] & VI["v1"]).count())
    if tech.via12_space:
        add("%s-%s.space" % (tech.v2.lef, tech.v1.lef), VI["v2"].separation_check(VI["v1"], um(tech.via12_space)).count())
    return res


# ----------------------------------------------------------------------------- render
DEFAULT_COLORS = [0x3070ff, 0xff40ff, 0x00c0c0, 0x202020, 0x606060]


def render_png(gds, out, d, box=None, width=1400, top=None):
    """Rasterize the top cell: base layers of the std cells in grey, routing in colour."""
    try:
        import pya as lay
    except ImportError:
        import klayout.lay as lay
    view = lay.LayoutView()
    view.load_layout(gds, True)
    if top:
        view.cellview(0).cell_name = top
    view.max_hier()
    view.clear_layers()
    L = d["layers"]
    route = [L["m1"], L["m2"], L["m3"], L["v1"], L["v2"]]
    ly = view.cellview(0).layout()
    for li in ly.layer_indexes():
        info = ly.get_info(li)
        if [info.layer, info.datatype] in route:
            continue
        lp = lay.LayerPropertiesNode()
        lp.source = "%d/%d" % (info.layer, info.datatype)
        lp.fill_color = lp.frame_color = 0xc8c8c8
        lp.dither_pattern = 2
        view.insert_layer(view.end_layers(), lp)
    for ld, col in zip(route, DEFAULT_COLORS):
        lp = lay.LayerPropertiesNode()
        lp.source = "%d/%d" % tuple(ld)
        lp.fill_color = lp.frame_color = col
        lp.dither_pattern = 5
        view.insert_layer(view.end_layers(), lp)
    view.set_config("background-color", "#ffffff")
    view.set_config("text-visible", "false")
    if not box:
        b = view.cellview(0).cell.dbbox()
        m = 0.02 * max(b.width(), b.height())
        box = [b.left - m, b.bottom - m, b.right + m, b.top + m]
    view.zoom_box(db.DBox(*box))
    h = int(width * (box[3] - box[1]) / max(1e-9, box[2] - box[0]))
    view.save_image(out, width, max(50, min(h, 4000)))
    return out
