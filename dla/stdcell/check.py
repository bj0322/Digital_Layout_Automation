"""KLayout checks of a generated library: front-end + metal-1 DRC and cell-level LVS.

DRC is driven by the ``stdcell.rules`` of the tech file (plus the pin-layer rules) and
runs on every cell and on the abutment test cell (R0 / MX rows of abutted cells).
LVS extracts NMOS/PMOS devices from the layout and compares the netlist (with
W/L) against the generated SPICE netlist. Both are fast sanity checks: run the
PDK's sign-off decks before tape-out.
"""
try:
    import pya as db
except ImportError:
    import klayout.db as db


def _layers(ly, top, st):
    def reg(key):
        ld = st.lay.get(key)
        if not ld:
            return db.Region()
        r = db.Region(top.begin_shapes_rec(ly.layer(ld[0], ld[1])))
        r.merge()
        return r
    return {k: reg(k) for k in ("activ", "poly", "cont", "nwell", "pimp", "m1")}


def drc(gds, topname, st):
    """{rule: count} of violations (empty = clean)."""
    ly = db.Layout()
    ly.read(gds)
    top = ly.cell(topname)
    um = lambda v: int(round(v / ly.dbu))
    L = _layers(ly, top, st)
    r = st.r
    A, P, CO, NW, PI, M1 = (L[k] for k in ("activ", "poly", "cont", "nwell", "pimp", "m1"))
    gate = P & A
    pdiff, ndiff = (A & PI) & NW, (A - PI) - NW           # transistor active
    ptap, ntap = (A & PI) - NW, (A - PI) & NW
    co_a, co_p = CO & A, CO - A
    res = {}

    def add(rule, n):
        if n:
            res[rule] = res.get(rule, 0) + n

    def enc(inner, outer, d):
        """inner must lie inside outer shrunk by d (in x and y)."""
        return (inner.sized(um(d)) - outer).count() if not inner.is_empty() else 0

    add("activ.width", A.width_check(um(r["activ_width"])).count())
    add("activ.space", A.space_check(um(r["activ_space"])).count())
    add("poly.width", P.width_check(um(r["poly_width"])).count())
    add("poly.space", P.space_check(um(r["poly_space"])).count())
    # field poly to active: compare edges, leaving out the shared edges where poly crosses active
    fp = P - A
    add("poly.activ_space", (A.edges() - fp.edges()).separation_check(fp.edges() - A.edges(),
                                                                       um(r["poly_activ_space"])).count())
    # gates are vertical poly lines: the poly must cover the gate extended by the end cap in y
    endcap = sum(1 for p in gate.each() if not (db.Region(p.bbox().enlarged(0, um(r["poly_endcap"]))) - P).is_empty())
    add("poly.endcap", endcap)
    sz = um(r["cont_size"])
    add("cont.size", sum(1 for p in CO.each() if p.bbox().width() != sz or p.bbox().height() != sz or p.num_points() != 4))
    add("cont.space", CO.space_check(um(r["cont_space"])).count())
    add("cont.activ_enc", enc(co_a, A, r["cont_activ_enc"]))
    add("cont.poly_enc", enc(co_p, P, r["cont_poly_enc"]))
    add("cont.on_gate", (CO & gate).count())
    add("cont.gate_space", co_a.separation_check(gate, um(r["cont_gate_space"])).count())
    add("polycont.activ_space", co_p.separation_check(A, um(r["polycont_activ_space"])).count())
    s, e = sorted(r["m1_cont_enc"])
    bad = 0
    for p in CO.each():
        b = p.bbox()
        if not any((db.Region(db.Box(b.left - um(ex), b.bottom - um(ey), b.right + um(ex), b.top + um(ey))) - M1).is_empty()
                   for ex, ey in ((s, e), (e, s))):
            bad += 1
    add("cont.m1_enc", bad)
    add("nwell.enc_pdiff", enc(pdiff, NW, r["nwell_enc_pdiff"]))
    add("nwell.space_ndiff", NW.separation_check(ndiff, um(r["nwell_space_ndiff"])).count() + (NW & ndiff).count())
    add("nwell.enc_ntap", enc(ntap, NW, r["nwell_enc_ntap"]))
    add("nwell.space_ptap", NW.separation_check(ptap, um(r["nwell_space_ptap"])).count() + (NW & ptap).count())
    add("pimp.enc_pdiff", enc(A & PI & NW, PI, r["pimp_enc_pdiff"]))
    add("pimp.space_ndiff", PI.separation_check(ndiff, um(r["pimp_space_ndiff"])).count())
    add("pimp.enc_ptap", enc(ptap, PI, r["pimp_enc_ptap"]))
    add("pimp.space_ntap", PI.separation_check(ntap, um(r["pimp_space_ntap"])).count())
    add("pimp.enc_gate", enc(gate & PI, PI, r["pimp_enc_gate"]))
    add("pimp.space_gate", PI.separation_check(gate - PI, um(r["pimp_space_gate"])).count())
    add("pimp.cont_space", (co_a & PI).sized(um(r["pimp_cont_space"])).not_inside(PI).count()
        + (co_a - PI).separation_check(PI, um(r["pimp_cont_space"])).count())
    m1 = st.m1
    add("m1.width", M1.width_check(um(m1.width)).count())
    add("m1.space", M1.space_check(um(m1.space)).count())
    for w, prl, sp in m1.wide:
        wide = M1.sized(-um(w / 2)).sized(um(w / 2))
        a = (False, db.Region.Projection, None, um(prl) if prl else None, None)
        add("m1.wide_space(w>=%.2f)" % w, wide.separation_check(M1 - wide, um(sp), *a).count()
            + wide.space_check(um(sp), *a).count())
    if m1.min_area:
        add("m1.area", M1.with_area(0, int(m1.min_area / ly.dbu ** 2), False).count())
    return res


# ----------------------------------------------------------------------------- LVS
def lvs(gds, cl, st, spice_text):
    """Extract one cell and compare it with its reference SPICE subcircuit. Returns (ok, message)."""
    ly = db.Layout()
    ly.read(gds)
    top = ly.cell(cl.name)
    l2n = db.LayoutToNetlist(db.RecursiveShapeIterator(ly, top, []))

    def mk(key):
        ld = st.lay[key]
        return l2n.make_layer(ly.layer(ld[0], ld[1]), key)

    A, P, CO, M1, NW, PI = mk("activ"), mk("poly"), mk("cont"), mk("m1"), mk("nwell"), mk("pimp")
    txt = l2n.make_text_layer(ly.layer(*st.lay["m1_text"]), "m1_text")
    bbox = db.Region(top.bbox().enlarged(um_(ly, 1.0), um_(ly, 1.0)))
    sub = l2n.make_polygon_layer(ly.layer(), "sub")
    sub.insert(bbox - NW)
    gate = P & A
    psd = (A & PI) - P
    nsd = (A - PI) - P
    pgate, ngate = gate & PI, gate - PI
    ntap, ptap = nsd & NW, psd - NW
    for name, r in (("psd", psd), ("nsd", nsd), ("pgate", pgate), ("ngate", ngate), ("ntap", ntap), ("ptap", ptap)):
        l2n.register(r, name)
    l2n.extract_devices(db.DeviceExtractorMOS4Transistor(st.pmos.upper()), {"SD": psd, "G": pgate, "P": P, "W": NW})
    l2n.extract_devices(db.DeviceExtractorMOS4Transistor(st.nmos.upper()), {"SD": nsd, "G": ngate, "P": P, "W": sub})
    for lay in (P, CO, M1, psd, nsd, NW, sub, ntap, ptap):
        l2n.connect(lay)
    l2n.connect(P, CO)
    l2n.connect(psd, CO)
    l2n.connect(nsd, CO)
    l2n.connect(CO, M1)
    l2n.connect(M1, txt)
    l2n.connect(NW, ntap)
    l2n.connect(ntap, nsd)
    l2n.connect(sub, ptap)
    l2n.connect(ptap, psd)
    l2n.extract_netlist()
    lay_nl = l2n.netlist()
    lay_nl.combine_devices()
    lay_nl.make_top_level_pins()
    lay_nl.purge()
    ref = db.Netlist()
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".sp")
    with os.fdopen(fd, "w") as f:
        f.write(spice_text)
    try:
        ref.read(path, db.NetlistSpiceReader())
    finally:
        os.remove(path)
    ref.combine_devices()
    ref.purge()
    cmp = db.NetlistComparer()
    xref = db.NetlistCrossReference()
    ok = cmp.compare(lay_nl, ref, xref)
    msg = "%d devices" % sum(1 for c in lay_nl.each_circuit() for _ in c.each_device())
    if not ok:
        return False, msg + ", netlists differ"
    # labelled layout nets must map onto the reference nets of the same name
    named = 0
    for cp in xref.each_circuit_pair():
        for np in xref.each_net_pair(cp):
            a, b = np.first(), np.second()
            if a is not None and b is not None and a.name and b.name:
                if a.name.upper() != b.name.upper():
                    return False, "net %s in the layout matches %s in the netlist" % (a.name, b.name)
                named += 1
    if named < len(cl.pins):
        return False, "only %d of %d pins labelled" % (named, len(cl.pins))
    return True, msg


def um_(ly, v):
    return int(round(v / ly.dbu))
