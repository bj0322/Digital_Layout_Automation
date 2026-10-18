"""pytest: decoders for every supported size route, simulate and are clean.

Needs a tech file: set DLA_TECH (e.g. tech/ihp_sg13g2.json with PDK_ROOT set).
"""
import json
import os
import tempfile

import pytest

from dla import make_decoder
from dla.export import block_json
from dla.sim import check_decoder

if not os.environ.get("DLA_TECH"):
    pytest.skip("set DLA_TECH to a tech file to run the tests", allow_module_level=True)

from dla.tech import load  # noqa: E402

CASES = [(n, en) for n in range(4, 11) for en in (False, True)]


@pytest.mark.parametrize("n,en", CASES)
def test_decoder_logic(n, en):
    blk = make_decoder(n, enable=en)
    assert check_decoder(blk) == 0


@pytest.mark.parametrize("n,en", [(4, False), (7, True), (10, True)])
def test_decoder_gds(n, en):
    gds = pytest.importorskip("dla.gds")
    tech = load()
    blk = make_decoder(n, enable=en)
    d = json.loads(json.dumps(block_json(blk)))
    with tempfile.TemporaryDirectory() as t:
        out = os.path.join(t, blk.name + ".gds")
        gds.build_gds(d, tech.gds_file, out)
        assert gds.verify_conn(d, out)["ok"]
        assert gds.drc(out, blk.name, tech) == {}


def test_checkers_catch_faults():
    """A removed via must show up as an open, a squeezed wire as a DRC error."""
    gds = pytest.importorskip("dla.gds")
    tech = load()
    blk = make_decoder(4)
    d = json.loads(json.dumps(block_json(blk)))
    i = [k for k, v in enumerate(d["vias"]) if v[3] == "pb_n_2"][1]
    d["vias"].pop(i)
    x1, y1, x2, y2 = [s for s in d["shapes"] if s[0] == "m2"][10][1:5]
    gap = tech.m2.space / 2
    d["shapes"].append(["m2", x2 + gap, y1, x2 + gap + tech.m2.width, y2, "bad"])
    with tempfile.TemporaryDirectory() as t:
        out = os.path.join(t, "bad.gds")
        gds.build_gds(d, tech.gds_file, out)
        assert not gds.verify_conn(d, out)["ok"]
        assert gds.drc(out, blk.name, tech)
