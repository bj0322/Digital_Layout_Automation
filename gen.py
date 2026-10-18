#!/usr/bin/env python3
"""Generate a std-cell block layout and check it.

    python gen.py --tech tech/ihp_sg13g2.json decoder --bits 8 --enable   # -> out/dec8to256en/
    python gen.py decoder --bits 10 --name row_dec --no-gds              # tech from $DLA_TECH

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

    a = ap.parse_args(argv)
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


if __name__ == "__main__":
    sys.exit(main())
