"""Oracle helper: simulate RTL with Icarus Verilog and compare cycle by cycle.

    mism = compare_rtl(paths, top, model_step, ports, cycles=200, resets={"rst_n": 0})

The testbench drives random inputs after every falling clock edge, samples the outputs
just before the rising edge and prints them in binary; `x` bits (uninitialised state)
are not compared. Resets (name -> active value) are active for the first two cycles,
then asserted again in about 3 % of the cycles.
"""
import os
import random
import shutil
import subprocess
import tempfile


def have_iverilog():
    return shutil.which("iverilog") is not None and shutil.which("vvp") is not None


def stimulus(ports, clocks, resets, cycles, seed=1):
    rnd = random.Random(seed)
    vecs = []
    for c in range(cycles):
        v = {}
        for name, d, msb, lsb in ports:
            if d != "input" or name in clocks:
                continue
            w = 1 if msb is None else abs(msb - lsb) + 1
            if name in resets:
                act = c < 2 or rnd.random() < 0.03
                v[name] = resets[name] if act else 1 - resets[name]
            else:
                v[name] = rnd.getrandbits(w)
        vecs.append(v)
    return vecs


def run_icarus(paths, top, ports, clocks, vecs, extra=(), workdir=None):
    """Simulate; returns [{output: bit string MSB first}] per cycle."""
    outs = [p for p in ports if p[1] == "output"]
    ins = [p for p in ports if p[1] == "input"]
    decl = []
    for name, d, msb, lsb in ports:
        rng = "" if msb is None else "[%d:%d] " % (msb, lsb)
        decl.append("  %s %s%s;" % ("reg" if d == "input" else "wire", rng, name))
    tb = ["`timescale 1ns/1ps", "module tb;"] + decl
    tb.append("  %s dut (%s);" % (top, ", ".join(".%s(%s)" % (p[0], p[0]) for p in ports)))
    tb.append("  initial begin")
    for c in clocks:
        tb.append("    %s = 0;" % c)
    for k, v in enumerate(vecs):
        for name, val in v.items():
            tb.append("    %s = %d;" % (name, val))
        tb.append("    #4;")
        tb.append('    $display("%s", %s);' % (" ".join("%b" for _ in outs), ", ".join(p[0] for p in outs)))
        tb.append("    #1; %s" % " ".join("%s = 1;" % c for c in clocks))
        tb.append("    #5; %s" % " ".join("%s = 0;" % c for c in clocks))
    tb.append("    $finish;")
    tb.append("  end")
    tb.append("endmodule")
    tmp = workdir or tempfile.mkdtemp(prefix="dla_iv_")
    tbp = os.path.join(tmp, "tb.v")
    with open(tbp, "w") as f:
        f.write("\n".join(tb) + "\n")
    exe = os.path.join(tmp, "sim.vvp")
    subprocess.run(["iverilog", "-g2005", "-o", exe, "-s", "tb", tbp] + list(paths) + list(extra),
                   check=True, capture_output=True, text=True)
    r = subprocess.run(["vvp", "-n", exe], check=True, capture_output=True, text=True)
    lines = [l for l in r.stdout.splitlines() if l and not l.startswith("VCD")]
    res = []
    for l in lines[:len(vecs)]:
        res.append(dict(zip([p[0] for p in outs], l.split())))
    return res


def bits_of(port, value_bits):
    """{bit name: 0/1/None} from an Icarus binary string."""
    name, _, msb, lsb = port
    if msb is None:
        return {name: None if value_bits[0] in "xz" else int(value_bits[0])}
    out = {}
    w = abs(msb - lsb) + 1
    for k in range(w):
        ch = value_bits[k]
        idx = msb - k if msb >= lsb else msb + k
        out["%s[%d]" % (name, idx)] = None if ch in "xz" else int(ch)
    return out


def aig_inputs(ports, v):
    pis = {}
    for name, d, msb, lsb in ports:
        if d != "input" or name not in v:
            continue
        val = v[name]
        if msb is None:
            pis[name] = val & 1
        else:
            w = abs(msb - lsb) + 1
            for p in range(w):
                idx = lsb + p if msb >= lsb else lsb - p
                pis["%s[%d]" % (name, idx)] = (val >> p) & 1
    return pis


def compare_aig(aig, ports, icarus, vecs):
    """Mismatches [(cycle, bit, rtl, aig)] between Icarus outputs and AIG cycle simulation."""
    state = {n: 0 for n in aig.ffs}
    bad = []
    for c, (v, ref) in enumerate(zip(vecs, icarus)):
        outs, state = aig.step(state, aig_inputs(ports, v))
        for p in ports:
            if p[1] != "output":
                continue
            for b, rv in bits_of(p, ref[p[0]]).items():
                if rv is not None and outs[b] != rv:
                    bad.append((c, b, rv, outs[b]))
    return bad
