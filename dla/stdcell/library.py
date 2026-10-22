"""Generate a std-cell library from a tech file's ``stdcell`` section.

    from dla.stdcell import make_library
    lib = make_library("tech/ihp_sg13g2.json", "out/dla_lib")

Writes <lib>.gds (cells + an abutment test cell <lib>_test), <lib>.lef, <lib>.spice,
<lib>.v, <lib>.tech.json (a tech file that makes the block generators use the new
cells) and <lib>.json (summary).
"""
import json
import os
import time

from . import cell as C
from . import writers
from .netlist import CATALOG, FILL_WIDTHS, check_function, make_cell
from .rules import StdTech

ROLE_CELLS = {"inv": "inv_1", "nand2": "nand2_1", "nor2": "nor2_1", "nor3": "nor3_1"}


def default_cells():
    return ["%s_%d" % (b, d) for b, (_, _, _, ds) in CATALOG.items() for d in ds]


def make_library(tech, outdir, name="dla_lib", prefix="dla_", cells=None, log=print):
    st = tech if isinstance(tech, StdTech) else StdTech(tech)
    os.makedirs(outdir, exist_ok=True)
    log("[std ] " + st.summary())
    built, summary = [], {"lib": name, "tech": st.name, "cells": {}}
    for spec in cells or default_cells():
        base, drive = spec.rsplit("_", 1)
        if base not in CATALOG:
            raise KeyError("unknown cell %s (known: %s)" % (base, ", ".join(CATALOG)))
        cn = make_cell(base, int(drive), prefix, st.vdd, st.vss, st.wn, st.wp)
        err = check_function(cn)
        if err:
            raise RuntimeError("%s: transistor network fails its truth table (%d vectors)" % (cn.name, err))
        t0 = time.time()
        cl = C.make_logic(st, cn)
        built.append(cl)
        summary["cells"][cl.name] = {"sites": cl.info["sites"], "width": cl.width,
                                     "devices": len(cn.devices), "gate_order": cl.info["order"]}
        log("[cell] %-14s %d sites (%.2f um), %d devices, gate order %s  (%.1fs)"
            % (cl.name, cl.info["sites"], cl.width, len(cn.devices), " ".join(cl.info["order"]), time.time() - t0))
    fills = []
    for w in FILL_WIDTHS:
        cl = C.make_fill(st, "%sfill_%d" % (prefix, w), w)
        built.append(cl)
        fills.append(cl.name)
    log("[cell] fillers %s" % " ".join(fills))

    p = {k: os.path.join(outdir, name + ext) for k, ext in
         (("gds", ".gds"), ("lef", ".lef"), ("spice", ".spice"), ("verilog", ".v"),
          ("tech", ".tech.json"), ("json", ".json"))}
    logic = [cl.name for cl in built if cl.netlist is not None]
    rows = [logic + fills[:1], list(reversed(logic)) + fills[1:2], logic[::2] + fills[:1] + logic[1::2]]
    writers.write_gds(p["gds"], name, st, built, (name + "_test", rows))
    writers.write_lef(p["lef"], st, built)
    writers.write_spice(p["spice"], st, built, name)
    writers.write_verilog(p["verilog"], built, name)
    by_base = {cl.netlist.base + "_%d" % cl.netlist.drive: cl.name for cl in built if cl.netlist is not None}
    roles = {r: by_base[c] for r, c in ROLE_CELLS.items() if c in by_base}
    if len(roles) == len(ROLE_CELLS):
        writers.write_tech(p["tech"], st, {"lef": p["lef"], "gds": p["gds"], "verilog": p["verilog"]},
                           roles, list(reversed(fills)))
    else:
        p.pop("tech")
    summary["fill"] = fills
    summary["test_cell"] = name + "_test"
    with open(p["json"], "w") as f:
        json.dump(summary, f, indent=2)
    return {"st": st, "cells": built, "paths": p, "name": name}
