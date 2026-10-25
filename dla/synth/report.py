"""Synthesis outputs: netlist, JSON side car and the text report.

    paths = write_outputs(result, liberty, "out/gcd")
    # out/gcd/synth.v, out/gcd/synth.json, out/gcd/reports/synth.rpt
"""
import json
import os

from ..db import write_verilog


def format_report(r, liberty):
    rep = r.report
    lines = ["Synthesis report: %s" % r.design.name, "=" * 60]
    for k in ("mode", "instances", "area", "flops", "aig_ands", "opt_ands", "depth_ns", "equivalence"):
        if k in rep:
            lines.append("%-14s %s" % (k, rep[k]))
    lines.append("%-14s %s" % ("time_s", " ".join("%s=%.2f" % kv for kv in rep["time_s"].items())))
    lines += ["", "%-24s %6s %10s %10s" % ("cell", "count", "area", "total")]
    rows = sorted(rep["cells"].items(), key=lambda kv: -kv[1] * liberty.cells[kv[0]].area)
    for c, n in rows:
        a = liberty.cells[c].area
        lines.append("%-24s %6d %10.4f %10.4f" % (c, n, a, n * a))
    if r.warnings:
        lines += ["", "Warnings:"] + ["  " + w for w in r.warnings]
    return "\n".join(lines) + "\n"


def write_outputs(r, liberty, outdir, extra=None):
    os.makedirs(os.path.join(outdir, "reports"), exist_ok=True)
    paths = {"verilog": os.path.join(outdir, "synth.v"), "json": os.path.join(outdir, "synth.json"),
             "report": os.path.join(outdir, "reports", "synth.rpt")}
    write_verilog(r.design, paths["verilog"])
    side = {"top": r.design.name, "liberty": liberty.path, "report": r.report,
            "ffmap": {k: list(v) for k, v in sorted(r.ffmap.items())}, "warnings": r.warnings}
    side.update(extra or {})
    with open(paths["json"], "w") as f:
        json.dump(side, f, indent=1, sort_keys=True)
    with open(paths["report"], "w") as f:
        f.write(format_report(r, liberty))
    return paths
