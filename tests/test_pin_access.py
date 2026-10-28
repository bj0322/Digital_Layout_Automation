"""pytest: placement-aware pin access (no PDK needed).

A synthetic library with the pin geometry of the generated dla_nand2_1: its Y via
sits 0.61 um above the VSS rail, so an MX row and the R0 row above it put two Y vias
1.22 um apart on one track. Each pin owns the grid nodes within ~0.715 um, so for some
grid phases both claim the node next to the shared rail (the dec8..dec10 failure with
the generated library). The block must then move one via along its own track.
"""
import json
import os

import pytest

from dla.layout import V, Block, FixedGeometryError
from dla.tech import EPS, Tech

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LEF = """VERSION 5.8 ;
SITE CoreSite
  SIZE 0.480 BY 3.780 ;
END CoreSite
MACRO t_nand2
  SIZE 1.920 BY 3.780 ;
  PIN A
    USE SIGNAL ;
    PORT
      LAYER Metal1 ;
        RECT 0.370 1.390 0.590 1.800 ;
    END
  END A
  PIN B
    USE SIGNAL ;
    PORT
      LAYER Metal1 ;
        RECT 1.330 1.390 1.550 1.800 ;
    END
  END B
  PIN Y
    USE SIGNAL ;
    PORT
      LAYER Metal1 ;
        RECT 0.370 0.550 0.530 1.150 ;
        RECT 0.880 2.270 1.040 3.210 ;
        RECT 0.370 0.525 1.040 0.685 ;
        RECT 0.850 0.455 1.070 0.755 ;
        RECT 0.370 1.035 1.040 1.195 ;
        RECT 0.880 1.035 1.040 2.385 ;
    END
  END Y
  PIN VDD
    USE POWER ;
    PORT
      LAYER Metal1 ;
        RECT 0.000 3.560 1.920 4.000 ;
        RECT 0.370 2.270 0.530 3.780 ;
        RECT 1.390 2.270 1.550 3.780 ;
    END
  END VDD
  PIN VSS
    USE GROUND ;
    PORT
      LAYER Metal1 ;
        RECT 0.000 -0.220 1.920 0.220 ;
        RECT 1.390 0.000 1.550 1.150 ;
    END
  END VSS
END t_nand2
MACRO t_fill
  SIZE 0.480 BY 3.780 ;
  PIN VDD
    USE POWER ;
    PORT
      LAYER Metal1 ;
        RECT 0.000 3.560 0.480 4.000 ;
    END
  END VDD
  PIN VSS
    USE GROUND ;
    PORT
      LAYER Metal1 ;
        RECT 0.000 -0.220 0.480 0.220 ;
    END
  END VSS
END t_fill
END LIBRARY
"""


@pytest.fixture(scope="module")
def tech(tmp_path_factory):
    d = tmp_path_factory.mktemp("tech")
    (d / "t.lef").write_text(LEF)
    (d / "t.gds").write_bytes(b"")
    base = json.load(open(os.path.join(ROOT, "tech", "ihp_sg13g2.json")))
    tj = {"name": "t_pin_access", "files": {"lef": "t.lef", "gds": "t.gds"},
          "manufacturing_grid": base["manufacturing_grid"], "site": "CoreSite",
          "layers": base["layers"], "rules": base.get("rules", {}),
          "cells": {"inv": {"cell": "t_nand2", "pins": {"A": "A", "Y": "Y"}}, "nand2": "t_nand2",
                    "nor2": "t_nand2", "nor3": {"cell": "t_nand2", "pins": {"A": "A", "B": "B", "C": "A", "Y": "Y"}},
                    "fill": ["t_fill"]}}
    (d / "t.json").write_text(json.dumps(tj))
    return Tech(str(d / "t.json"))


def _facing_rows(tech):
    """MX row at 0, R0 row at H, grid phase with a node exactly on the shared rail."""
    H = tech.row_h
    y_min = H - 8 * tech.gy
    blk = Block("t", 8, y_min, 2 * H, tech)
    blk.add_row(0.0, "MX")
    blk.add_row(H, "R0")
    return blk


def test_options_keep_the_track(tech):
    opts, acc = tech.access_options("t_nand2"), tech.access("t_nand2")
    for p, pts in opts.items():
        assert pts[0] == acc[p]                          # default first: unchanged placements
        assert len(set(pts)) == len(pts)
        assert all(abs(pt[0] - acc[p][0]) < EPS for pt in pts)
    assert len(opts["Y"]) > 1


def test_facing_rows_get_disjoint_pin_nodes(tech):
    blk = _facing_rows(tech)
    H = tech.row_h
    y_def = tech.access("t_nand2")["Y"][1]
    # without reselection the two default vias would share a node
    lo, hi = blk.pin_nodes(H - y_def), blk.pin_nodes(H + y_def)
    assert set(lo) & set(hi)

    blk.place("lo", "nand2", 0, 0.0, {"A": "a0", "B": "b0", "Y": "y0"})
    blk.place("hi", "nand2", 0, H, {"A": "a1", "B": "b1", "Y": "y1"})
    (x0, ylo), (x1, yhi) = blk.pin("lo", "Y"), blk.pin("hi", "Y")
    assert abs(x0 - x1) < EPS                            # same track: alignment is kept
    assert abs(ylo - (H - y_def)) < EPS                  # first placed keeps its default
    assert yhi - ylo >= tech.v1.size + tech.v1.space - EPS
    # fixed wiring claiming both stubs no longer collides
    for inst, net in (("lo", "y0"), ("hi", "y1")):
        n, js = blk.pin_stub(inst, "Y", net)
        blk.take_span(V, n, n, js[0], js[-1], net)


def test_no_access_is_a_fixed_geometry_error(tech):
    blk = _facing_rows(tech)
    n = blk.tn(tech.access("t_nand2")["Y"][0])
    blk.own[V][n] = ["other"] * blk.nj                   # a foreign wire over the whole track
    with pytest.raises(FixedGeometryError):
        blk.place("lo", "nand2", 0, 0.0, {"A": "a0", "B": "b0", "Y": "y0"})
