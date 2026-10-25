"""Oracle: the elaborated AIG must behave like the RTL simulated by Icarus Verilog."""
import os

import pytest

from dla.rtl.elab import elaborate

from .simcheck import compare_aig, have_iverilog, run_icarus, stimulus

if not have_iverilog():
    pytest.skip("iverilog not installed", allow_module_level=True)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH = ["counter8", "gcd", "uart_tx", "uart_rx", "alu32"]


def _check(path, top, resets, cycles=400, tweak=None):
    r = elaborate(path, top=top)
    clocks = {f.clk for f in r.aig.ffs.values()}
    vecs = stimulus(r.ports, clocks, resets, cycles)
    if tweak:
        tweak(vecs)
    ref = run_icarus([path], top, r.ports, clocks, vecs)
    assert len(ref) == len(vecs)
    assert compare_aig(r.aig, r.ports, ref, vecs) == []


def test_features():
    _check(os.path.join(ROOT, "tests", "data", "rtl", "features.v"), "features", {"rst_n": 0, "set_n": 0}, 600)


def _uart_rx_line(vecs):
    for k, v in enumerate(vecs):
        v["rx"] = 1 if (k // 3) % 7 else 0


def _gcd_small(vecs):
    for v in vecs:
        v["a"] &= 0xFF
        v["b"] &= 0xFF


@pytest.mark.parametrize("top", BENCH)
def test_bench(top):
    tweak = {"uart_rx": _uart_rx_line, "gcd": _gcd_small}.get(top)
    _check(os.path.join(ROOT, "bench", top + ".v"), top, {"rst_n": 0}, 500, tweak)
