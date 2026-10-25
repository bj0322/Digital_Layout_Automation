"""Oracle: synthesized IHP netlists simulated by Icarus with the PDK's cell models must match
the RTL, and their area must stay within 1.3x of Yosys + ABC on the same Liberty file.

Needs DLA_TECH (with files.liberty and files.verilog), iverilog and yosys; skips otherwise."""
import os
import re
import shutil
import subprocess

import pytest

from dla.db import write_verilog
from dla.rtl.elab import elaborate

from .simcheck import functional_models, have_iverilog, run_icarus, stimulus

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH = ["counter8", "gcd", "uart_tx", "uart_rx", "alu32"]

if not os.environ.get("DLA_TECH"):
    pytest.skip("set DLA_TECH to run the synthesis oracle tests", allow_module_level=True)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from dla.db import Library
    from dla.tech import load
    tech = load()
    if not tech.liberty or not tech.verilog:
        pytest.skip("tech file needs files.liberty and files.verilog")
    corner = list(tech.liberty)[0]
    lib = Library.from_tech(tech, corners=[corner])
    return tech, lib.liberty[corner], functional_models(tech.verilog, str(tmp_path_factory.mktemp("cells")))


def _synth(top, liberty):
    from dla.synth.flow import synthesize
    return synthesize(os.path.join(ROOT, "bench", top + ".v"), top, liberty)


@pytest.mark.parametrize("top", BENCH)
def test_gate_level_simulation(top, env, tmp_path):
    if not have_iverilog():
        pytest.skip("iverilog not installed")
    tech, liberty, models = env
    src = os.path.join(ROOT, "bench", top + ".v")
    r = _synth(top, liberty)
    assert r.report["equivalence"] == "PASS"
    net = str(tmp_path / "synth.v")
    write_verilog(r.design, net)
    e = elaborate(src, top=top)
    clocks = {f.clk for f in e.aig.ffs.values()}
    vecs = stimulus(e.ports, clocks, {"rst_n": 0}, 400)
    if top == "uart_rx":
        for k, v in enumerate(vecs):
            v["rx"] = 1 if (k // 3) % 7 else 0
    ref = run_icarus([src], top, e.ports, clocks, vecs)
    gate = run_icarus([net], top, e.ports, clocks, vecs, extra=models)
    compared = 0
    for c, (a, b) in enumerate(zip(ref, gate)):
        for port in a:
            for x, y in zip(a[port], b[port]):
                if x in "01":
                    assert y == x, (c, port, a[port], b[port])
                    compared += 1
    assert compared > 0


def _yosys_area(top, lib_path):
    script = ("read_verilog %s; synth -top %s; dfflibmap -liberty %s; abc -liberty %s; opt_clean; stat -liberty %s"
              % (os.path.join(ROOT, "bench", top + ".v"), top, lib_path, lib_path, lib_path))
    out = subprocess.run(["yosys", "-p", script], capture_output=True, text=True, check=True).stdout
    m = re.findall(r"Chip area for module .*?: ([0-9.]+)", out)
    return float(m[-1])


@pytest.mark.parametrize("top", BENCH)
def test_area_against_yosys(top, env):
    if shutil.which("yosys") is None:
        pytest.skip("yosys not installed")
    tech, liberty, _ = env
    ours = _synth(top, liberty).report["area"]
    theirs = _yosys_area(top, liberty.path)
    assert ours <= 1.3 * theirs, (top, ours, theirs, ours / theirs)
