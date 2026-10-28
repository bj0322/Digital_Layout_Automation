#!/usr/bin/env python3
"""Generate a std-cell block layout and check it.

    python gen.py --tech tech/ihp_sg13g2.json decoder --bits 8 --enable   # -> out/dec8to256en/
    python gen.py decoder --bits 10 --name row_dec --no-gds              # tech from $DLA_TECH
    python gen.py --tech tech/ihp_sg13g2.json stdcell                     # -> out/dla_lib/ (own std cells)
    python gen.py --tech out/dla_lib/dla_lib.tech.json decoder --bits 6   # block from the generated cells
    python gen.py --tech tech/ihp_sg13g2.json sram --rows 4 --cols 4      # -> out/sram_4x4/ (6T bitcell + array)

Steps: place + route -> block JSON / layout netlist / RTL / testbench
       -> exhaustive Python gate sim -> GDS -> metal connectivity -> metal/via DRC -> PNG
The GDS steps need the `klayout` Python package (pip install klayout); without it use
--no-gds and the wrappers in klayout_scripts/ with the KLayout application.
"""
import argparse
import json
import os
import sys
import time

from dla import BLOCKS, write_all
from dla.tech import load


def main(argv=None):
    for f in (sys.stdout, sys.stderr):      # non-ASCII paths (e.g. Korean folders) in logs
        if not f.isatty() and hasattr(f, "reconfigure"):
            f.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tech", help="tech JSON file (default: $DLA_TECH), see tech/TECH_FILE.md")
    ap.add_argument("--out", default="out", help="output root (a sub-folder per block is created)")
    ap.add_argument("--no-gds", action="store_true", help="stop after JSON/netlist/sim")
    sub = ap.add_subparsers(dest="block", required=True)

    d = sub.add_parser("decoder", help="N -> 2^N decoder (NOR2 array + NAND2/NOR predecoders)")
    d.add_argument("--bits", type=int, required=True, help="address bits, 4..10")
    d.add_argument("--enable", action="store_true", help="add active-high EN input")
    d.add_argument("--name", help="top cell / module name")
    d.add_argument("--channel", type=int, help="extra routing tracks in the left channel")

    s = sub.add_parser("stdcell", help="generate a std-cell library from the tech file's front-end rules")
    s.add_argument("--name", default="dla_lib", help="library name (output folder and file names)")
    s.add_argument("--prefix", default="dla_", help="cell name prefix")
    s.add_argument("--cells", nargs="+", help="cells to build, e.g. inv_1 nand2_1 (default: all)")

    r = sub.add_parser("sram", help="6T SRAM bitcell + rows x cols 4-way mirrored array")
    r.add_argument("--rows", type=int, default=4)
    r.add_argument("--cols", type=int, default=4)
    r.add_argument("--cr", type=float, help="minimum cell ratio W_PD/W_PG (default: tech sram.cr or 1.5)")
    r.add_argument("--pr", type=float, help="maximum pull-up ratio W_PU/W_PG (default: tech sram.pr or 1.0)")
    r.add_argument("--name", help="top cell / output folder name (default sram_<rows>x<cols>)")

    a = ap.parse_args(argv)
    if a.block == "stdcell":
        return stdcell(a)
    if a.block == "sram":
        return sram(a)
    tech = load(a.tech)
    print("[tech] " + tech.summary())
    t0 = time.time()

    if a.block == "decoder":
        blk = BLOCKS["decoder"](a.bits, enable=a.enable, name=a.name, tech=tech, channel=a.channel)
    outdir = os.path.join(a.out, blk.name)
    paths = write_all(blk, outdir)
    st = blk.stats()
    print("[gen ] %s: %d cells (+%d fill), %.1f x %.1f um, %d shapes, %d vias  (%.1fs)"
          % (blk.name, st["cells"], st["fill"], st["width_um"], st["height_um"],
             st["shapes"], st["vias"], time.time() - t0))

    ok = True
    if blk.meta.get("block") == "decoder":
        from dla.sim import check_decoder
        err = check_decoder(blk)
        print("[sim ] exhaustive gate-level check: %s" % ("PASS" if err == 0 else "FAIL (%d vectors)" % err))
        ok &= err == 0

    if not a.no_gds:
        try:
            from dla import gds
        except ImportError:
            sys.exit("klayout Python package not found: pip install klayout  (or use --no-gds)")
        dj = json.load(open(paths["json"]))
        out_gds = os.path.join(outdir, blk.name + ".gds")
        w, h = gds.build_gds(dj, tech.gds_file, out_gds)
        print("[gds ] %s (%.1f x %.1f um)" % (out_gds, w, h))
        c = gds.verify_conn(dj, out_gds)
        print("[conn] %d probes: missing %d, opens %d, shorts %d -> %s"
              % (c["probes"], len(c["missing"]), len(c["opens"]), len(c["shorts"]), "PASS" if c["ok"] else "FAIL"))
        ok &= c["ok"]
        v = gds.drc(out_gds, blk.name, tech)
        print("[drc ] metal/via rules: %s" % ("0 violations" if not v else v))
        ok &= not v
        gds.render_png(out_gds, os.path.join(outdir, "overview.png"), dj, width=800)
        for zname, box in dj.get("zoom", {}).items():
            gds.render_png(out_gds, os.path.join(outdir, zname + ".png"), dj, box=box, width=1400)
        print("[png ] %s" % outdir)

    print("RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def sram(a):
    """Size + lay out + route the SRAM, then switch-level, DRC, LVS, connectivity and PNG checks."""
    from dla.sram import make_sram
    name = a.name or "sram_%dx%d" % (a.rows, a.cols)
    outdir = os.path.join(a.out, name)
    res = make_sram(a.tech or os.environ.get("DLA_TECH"), outdir, a.rows, a.cols, name, cr=a.cr, pr=a.pr)
    st, p, cell, sz = res["st"], res["paths"], res["cell"], res["sizing"]
    ok = not res["errors"]
    print("[sim ] heuristic ratio model (fixed MU_P/VM, not SPICE SNM/write-margin/delay/power): "
          "6T hold 0/1, read 0/1, write 0/1 + %dx%d array functional check: %s"
          % (min(a.rows, 4), min(a.cols, 4), "PASS" if ok else "FAIL " + "; ".join(res["errors"])))
    if a.no_gds:
        print("RESULT: %s (DRC / LVS / connectivity skipped)" % ("PASS" if ok else "FAIL"))
        return 0 if ok else 1
    try:
        from dla import gds
        from dla.stdcell import check
    except ImportError:
        sys.exit("klayout Python package not found: pip install klayout  (or use --no-gds)")
    bad = {}
    for top in (name + "_bitcell", name + "_2x2", name):
        v = check.drc(p["gds"], top, st)
        if v:
            bad[top] = v
    print("[drc ] front-end + metal-1 rules on bitcell, 2x2 and array: %s" % ("0 violations" if not bad else bad))
    ok &= not bad
    stats = {}
    good, msg = check.lvs(p["gds"], cell, st, cell.netlist.spice(st.nmos, st.pmos, st.L, st.spice_units), stats)
    print("[lvs ] %s device extraction vs SPICE: %s (raw %s, combined %s)"
          % (cell.name, "PASS" if good and stats.get("raw") == 6 and stats.get("combined") == 6 else "FAIL " + msg,
             stats.get("raw"), stats.get("combined")))
    ok &= good and stats.get("raw") == 6 and stats.get("combined") == 6
    dj = json.load(open(p["json"]))
    c = gds.verify_conn(dj, p["gds"])
    print("[conn] %d probes: missing %d, opens %d, shorts %d -> %s"
          % (c["probes"], len(c["missing"]), len(c["opens"]), len(c["shorts"]), "PASS" if c["ok"] else "FAIL"))
    ok &= c["ok"]
    v = gds.drc(p["gds"], name, st)
    print("[drc ] metal/via rules M1-M3 / VIA1-VIA2 on the array: %s" % ("0 violations" if not v else v))
    ok &= not v
    w, h = cell.width, st.row_h
    gds.render_png(p["gds"], os.path.join(outdir, "overview.png"), dj, width=900, top=name)
    gds.render_png(p["gds"], os.path.join(outdir, "bitcell.png"), dj, box=[-0.1 * w, -0.1 * h, 1.1 * w, 1.1 * h],
                   width=900, top=cell.name)
    arr = res["array"]
    gds.render_png(p["gds"], os.path.join(outdir, "array_corner.png"), dj,
                   box=[0, 0, min(arr.width, arr.x_arr + 2 * w + 0.2 * w), min(arr.height, 2 * h)], width=1200, top=name)
    print("[png ] %s" % outdir)
    print("RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def stdcell(a):
    """Generate the library, then switch-level (inside make_library), DRC and LVS checks."""
    from dla.stdcell import make_library
    t0 = time.time()
    lib = make_library(a.tech or os.environ.get("DLA_TECH"), os.path.join(a.out, a.name), a.name, a.prefix, a.cells)
    p, st = lib["paths"], lib["st"]
    print("[lib ] %s: %d cells -> %s  (%.1fs)" % (a.name, len(lib["cells"]), os.path.dirname(p["gds"]), time.time() - t0))
    print("[sim ] switch-level truth tables: PASS")
    ok = True
    if a.no_gds:
        print("RESULT: PASS (DRC / LVS skipped)")
        return 0
    try:
        from dla.stdcell import check
    except ImportError:
        sys.exit("klayout Python package not found: pip install klayout  (or use --no-gds)")
    bad = {}
    for name in [c.name for c in lib["cells"]] + [a.name + "_test"]:
        v = check.drc(p["gds"], name, st)
        if v:
            bad[name] = v
    print("[drc ] front-end + metal-1 rules, %d cells + abutment test: %s"
          % (len(lib["cells"]), "0 violations" if not bad else bad))
    ok &= not bad
    nfail = 0
    for cl in lib["cells"]:
        if cl.netlist is None:
            continue
        good, msg = check.lvs(p["gds"], cl, st, cl.netlist.spice(st.nmos, st.pmos, st.L, st.spice_units))
        if not good:
            nfail += 1
            print("[lvs ] %s: %s" % (cl.name, msg))
    print("[lvs ] device extraction vs SPICE: %s" % ("PASS" if not nfail else "%d cells FAIL" % nfail))
    ok &= nfail == 0
    if "tech" in p:
        from dla.tech import Tech
        bt = Tech(p["tech"])        # the block router must reach every pin on its own track
        bad = []
        for cl in lib["cells"]:
            if cl.netlist is not None:
                try:
                    bt.access(cl.name)
                except RuntimeError as e:
                    bad.append(str(e))
        print("[pins] block-router access (VIA1 on tracks): %s" % ("PASS" if not bad else "; ".join(bad)))
        ok &= not bad
        print("[tech] %s  (use with --tech to build blocks from these cells)" % p["tech"])
    print("RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
