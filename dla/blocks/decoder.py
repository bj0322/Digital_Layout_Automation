"""N-to-2^N row/column decoder (optional enable) built from any std-cell library.

    from dla.blocks import make_decoder
    blk = make_decoder(8, enable=True)          # 8 -> 256, name "dec8to256en"

Logic (n = n_bits, na = n // 2 row bits, nb = n - na column bits)
  row group    : A[na-1:0] -> literals -> sub-predecoders -> pa_n[2^na]  (nand2, active low)
  column group : A[n-1:na] -> literals -> sub-predecoders -> pb_n[2^nb]  (nand2, active low)
  array        : Y[c * 2^na + r] = nor2(pa_n[r], pb_n[c])
  enable       : EN gates the low sub-predecoder of the column group, so EN = 0
                 forces every pb_n high and every Y low.

  A k-bit group is split into lo = k // 2 and hi = k - lo bits. An m-bit sub-group is
  decoded by nor_m of the inverted/true literals (m = 1 needs no gate: the true literal
  itself is the decoded signal; with EN it becomes nor2(literal, ENn)). Only nor2/nor3
  are required, so k <= 5 and 4 <= n <= 10.

Floorplan (H = row height; y = 0 is the bottom of the array)
  0 .. R*H      array rows (R0/MX alternating). Per row: row nand2 at site PA_S, nor2 at
                ARR_S + P*c. One horizontal-layer line per row joins the nand2 output to
                the A pins of the C nor2s.
  -H            column nand2s, shifted so their Y pin lands on the B-pin track of array
                column c: one vertical-layer line per column feeds all rows.
  -3H, -5H      column / row predecoder rows (empty rows between are routing channels)
  -6H           bottom edge: inputs A[*], EN and the VDD/VSS straps
  left of PA_S  channel where the maze router brings the row predecoder nets up
  first/last strap_w sites: filler columns carrying the power straps
"""
from ..layout import H as LH
from ..layout import V as LV
from ..layout import Block, RoutingError, r6
from ..tech import EPS, load

PRE_START_GAP, PRE_GAP = 2, 2       # sites


def _lit(bit, val):
    """Literal that is 0 when A[bit] == val (input of a NOR predecoder)."""
    return "An%d" % bit if val else "A[%d]" % bit


def _true(bit, val):
    """Literal that is 1 when A[bit] == val."""
    return "A[%d]" % bit if val else "An%d" % bit


def _group(prefix, b0, k, en):
    """Predecoder for A[b0+k-1:b0]. Returns (cells, lo_nets, hi_nets, lo_bits)."""
    lo, hi = k // 2, k - k // 2
    cells = [("%sinv_%d" % (prefix, b), "inv", {"A": "A[%d]" % b, "Y": "An%d" % b})
             for b in range(b0, b0 + k)]
    if en:
        cells.append(("%sinv_en" % prefix, "inv", {"A": "EN", "Y": "ENn"}))

    def sub(tag, bs, m, gated):
        nets = []
        for j in range(1 << m):
            lits = [_lit(bs + t, (j >> t) & 1) for t in range(m)] + (["ENn"] if gated else [])
            if len(lits) == 1:
                nets.append(_true(bs, j))
                continue
            out = "p%s%s_%d" % (tag, prefix, j)
            role = {2: "nor2", 3: "nor3"}[len(lits)]
            cells.append(("%snor%s_%d" % (prefix, tag, j), role, dict(zip("ABC", lits), Y=out)))
            nets.append(out)
        return nets

    lo_nets = sub("lo", b0, lo, en)
    hi_nets = sub("hi", b0 + lo, hi, False)
    return cells, lo_nets, hi_nets, lo


def make_decoder(n_bits, enable=False, name=None, tech=None, channel=None):
    """Generate a placed and routed n_bits -> 2**n_bits decoder.

    n_bits  : 4 .. 10
    enable  : add an active-high EN input (EN = 0 -> all outputs 0)
    name    : top cell / module name (default dec<n>to<2^n>[en])
    tech    : dla.tech.Tech (default: $DLA_TECH)
    channel : extra routing tracks left of the row nand2 column (auto, grown on failure)
    Returns a :class:`dla.layout.Block`.
    """
    if not 4 <= n_bits <= 10:
        raise ValueError("decoder supports 4..10 address bits (nor2/nor3 predecoders)")
    tech = tech or load()
    name = name or "dec%dto%d%s" % (n_bits, 1 << n_bits, "en" if enable else "")
    extra = 4 if channel is None else channel
    last = None
    for _ in range(8):
        try:
            blk = _build(n_bits, enable, name, tech, extra)
            blk.meta.update({"block": "decoder", "n_bits": n_bits, "enable": enable,
                             "channel": extra, "tech": tech.name})
            return blk
        except RoutingError as e:
            last = e
            extra += 4
    raise RoutingError("decoder %s could not be routed: %s" % (name, last))


def _row_line(blk, yb, orient, avoid_y):
    """Grid node for a row's horizontal line: inside the row, as far as possible from pin vias."""
    t = blk.t
    js = [j for j in range(blk.nj) if yb + t.gy / 2 <= blk.jy(j) <= yb + t.row_h - t.gy / 2]
    js = [j for j in js if all(j not in blk.pin_nodes(y) for y in avoid_y)]
    if not js:
        raise RuntimeError("row %.3f: no free grid line for the row wire" % yb)
    return blk.jy(max(js, key=lambda j: min(abs(blk.jy(j) - y) for y in avoid_y)))


def _build(n, en, name, tech, extra):
    t = tech
    H = t.row_h
    na, nb = n // 2, n - n // 2
    R, C = 1 << na, 1 << nb
    cells_a, lo_a, hi_a, lo_bits_a = _group("a", 0, na, False)
    cells_b, lo_b, hi_b, lo_bits_b = _group("b", na, nb, en)
    W = {r: t.width_sites(t.cells[r]["cell"]) for r in t.ROLES}
    SW = 4                                       # strap column width (sites)

    n_up = len(set(lo_a + hi_a))                 # nets the channel carries up to the rows
    trk_per_site = t.site_w / t.gx
    PA_S = SW + int(round((n_up + extra) / trk_per_site))
    P = max(W["nor2"], W["nand2"])               # array column pitch (sites)
    ARR_S = PA_S + W["nand2"]

    # track offsets inside the cells (identical for R0 and MX)
    dummy = Block("_", 1, 0.0, H, t)
    off = lambda role, p: dummy.track_offset(role, p)
    nand_shift = off("nor2", "B") - off("nand2", "Y")          # column nand2 site = nor2 site + shift
    if nand_shift < 0:                           # keep the column nand2s right of the channel
        ARR_S -= nand_shift
    col_sites = [ARR_S + P * c + nand_shift for c in range(C)]
    array_end = max(ARR_S + P * C, col_sites[-1] + W["nand2"])

    def row_len(cells):
        s = SW + PRE_START_GAP
        for _, role, _ in cells:
            s += W[role] + PRE_GAP
        return s - PRE_GAP

    rstrap = max(array_end, row_len(cells_a), row_len(cells_b))
    y_min = -6 * H
    blk = Block(name, rstrap + SW, y_min, H * R, t)
    blk.y_min = r6(y_min)

    for r in range(R):
        blk.add_row(H * r, "R0" if r % 2 == 0 else "MX")
    ROW_COLNAND, ROW_COLPRE, ROW_ROWPRE = -H, -3 * H, -5 * H
    blk.add_row(ROW_COLNAND, "MX")
    blk.add_row(ROW_COLPRE, "R0")
    blk.add_row(ROW_ROWPRE, "R0")
    blk.straps([0, rstrap], SW)

    # ---- array ---------------------------------------------------------------
    for r in range(R):
        yb = H * r
        blk.place("pa_nand_%d" % r, "nand2", PA_S, yb,
                  {"A": lo_a[r & ((1 << lo_bits_a) - 1)], "B": hi_a[r >> lo_bits_a], "Y": "pa_n_%d" % r})
        for c in range(C):
            blk.place("dec_%d_%d" % (r, c), "nor2", ARR_S + P * c, yb,
                      {"A": "pa_n_%d" % r, "B": "pb_n_%d" % c, "Y": "Y[%d]" % (c * R + r)})
    for c in range(C):
        blk.place("pb_nand_%d" % c, "nand2", col_sites[c], ROW_COLNAND,
                  {"A": lo_b[c & ((1 << lo_bits_b) - 1)], "B": hi_b[c >> lo_bits_b], "Y": "pb_n_%d" % c})

    # ---- predecoder rows -----------------------------------------------------------
    for cells, yb in ((cells_b, ROW_COLPRE), (cells_a, ROW_ROWPRE)):
        s = SW + PRE_START_GAP
        for iname, role, pins in cells:
            blk.place(iname, role, s, yb, pins)
            s += W[role] + PRE_GAP

    # ---- power ---------------------------------------------------------------------
    blk.power_straps()

    # ---- regular wiring: row lines (horizontal layer) and column lines (vertical) -----
    for r in range(R):
        net = "pa_n_%d" % r
        yb = H * r
        avoid = [blk.pin("pa_nand_%d" % r, p)[1] for p in ("A", "B", "Y")] + \
                [blk.pin("dec_%d_0" % r, p)[1] for p in ("A", "B", "Y")]
        yline = _row_line(blk, yb, blk.rows[r6(yb)], avoid)
        x0, _ = blk.pin_drop("pa_nand_%d" % r, "Y", yline, net)
        blk.via2(x0, yline, net)
        x_end = blk.pin("dec_%d_%d" % (r, C - 1), "A")[0]
        blk.m3(yline, x0, x_end, net)
        blk.take_span(LH, blk.tn(x0), blk.tn(x_end), blk.yj(yline), blk.yj(yline), net)
        for c in range(C):
            inst = "dec_%d_%d" % (r, c)
            x, _ = blk.pin_drop(inst, "A", yline, net)
            if (blk.tn(x), blk.yj(yline)) in blk.novia:
                raise RoutingError("row line too close to a pin via")
            blk.via2(x, yline, net)
            out = "Y[%d]" % (c * R + r)
            yx, yy = blk.pin(inst, "Y")
            ny, js = blk.pin_stub(inst, t.cells["nor2"]["pins"]["Y"], out)
            blk.take_span(LV, ny, ny, js[0], js[-1], out)
            blk.add_pin(out, yx, yy)
    for c in range(C):
        net = "pb_n_%d" % c
        x, y0 = blk.pin("pb_nand_%d" % c, "Y")
        y1 = blk.pin("dec_%d_%d" % (R - 1, c), "B")[1]
        assert abs(x - blk.pin("dec_0_%d" % c, "B")[0]) < EPS, "column nand2 Y not on the B track"
        js0 = blk.pin_stub("pb_nand_%d" % c, t.cells["nand2"]["pins"]["Y"], net)[1]
        blk.m2(x, y0, y1, net)
        js1 = js0
        for r in range(R):
            js1 = blk.pin_stub("dec_%d_%d" % (r, c), t.cells["nor2"]["pins"]["B"], net)[1]
        blk.take_span(LV, blk.tn(x), blk.tn(x), js0[0], js1[-1], net)
    # keep the router out of the array
    top = blk.nj - 1
    j0 = blk.nearest_j(0.0)
    blk.block_rect(LV, ARR_S, blk.ntrk - 1, j0, top)
    blk.block_rect(LH, ARR_S, blk.ntrk - 1, j0, top)

    # ---- inputs on the bottom edge, below each inverter's A pin ------------------------
    want = [("A[%d]" % b, blk.pin_track("%sinv_%d" % ("a" if b < na else "b", b), "A")) for b in range(n)]
    if en:
        want.append(("EN", blk.pin_track("binv_en", "A")))
    io, bottom = {}, blk.pin_nodes(y_min)
    for net, tk in want:          # nearest free bottom-edge track (row a / row b may share one)
        for d in sorted(range(-tk, blk.ntrk - tk), key=abs):
            if all(blk.own[LV][tk + d][j] is None for j in bottom) and tk + d not in io.values():
                io[net] = tk + d
                break
    fixed = {t.vdd, t.vss} | {"pa_n_%d" % i for i in range(R)} | {"pb_n_%d" % i for i in range(C)}
    blk.maze_route(lambda net: net in fixed or net.startswith("Y["), io)
    blk.fill_rows()

    blk.ports = [("A", "input", n)] + ([("EN", "input", 1)] if en else []) + [("Y", "output", 1 << n)]
    m = max(t.pad_v1_m2[1], t.pad_v2_m2[1]) / 2 + t.m2.space
    blk.boundary = [0.0, r6(y_min - m), blk.width, r6(H * R + m)]
    sw = t.site_w
    blk.zoom = {"predecoder": [0.0, y_min - m, blk.width, H],
                "array_corner": [max(0.0, (PA_S - 4) * sw), -H, min(blk.width, (ARR_S + 8 * P) * sw), 2 * H]}
    return blk
