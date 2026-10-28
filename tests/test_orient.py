"""pytest: cell orientations (R0 / MX / MY / R180) agree between the GDS writer and the KLayout path.

No PDK needed. ``orient_origin`` / ``orient_point`` (writers) place a cell [0,W]x[0,H] by its
lower-left corner; ``orient_trans`` (gds) must produce the same placement, and a GDS written
with ``GdsWriter.sref(orient=...)`` must read back at the expected position.
"""
import pytest

from dla.stdcell.writers import GdsWriter, ORIENTS, orient_origin, orient_point

db = pytest.importorskip("klayout.db")

W, H, X, YB = 2.0, 3.0, 10.0, 20.0
# an asymmetric box inside the cell so every orientation gives a different result
BOX = (0.25, 0.5, 0.75, 2.0)


def _expected(orient):
    pts = [orient_point(orient, u, v, X, YB, W, H) for u, v in ((BOX[0], BOX[1]), (BOX[2], BOX[3]))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox(b):
    return tuple(round(v, 6) for v in (b.left, b.bottom, b.right, b.top))


@pytest.mark.parametrize("orient", ORIENTS)
def test_orient_trans_matches_writers(orient):
    from dla.gds import orient_trans
    t = orient_trans(orient, X, YB, W, H)
    for u, v in ((0, 0), (W, 0), (0, H), (0.25, 0.5), (1.5, 2.75)):
        p = t * db.DPoint(u, v)
        assert (round(p.x, 6), round(p.y, 6)) == pytest.approx(orient_point(orient, u, v, X, YB, W, H))


@pytest.mark.parametrize("orient", ORIENTS)
def test_sref_roundtrip(orient, tmp_path):
    gw = GdsWriter("t")
    gw.begin("c")
    gw.box((1, 0), BOX)
    gw.end()
    gw.begin("top")
    gw.sref("c", X, YB, orient=orient, size=(W, H))
    gw.end()
    path = str(tmp_path / "o.gds")
    gw.write(path)
    ly = db.Layout()
    ly.read(path)
    top = ly.cell("top")
    got = top.dbbox_per_layer(ly.layer(1, 0))
    assert _bbox(got) == pytest.approx(_expected(orient))


def test_bad_orientation():
    with pytest.raises(ValueError):
        orient_origin("R90", 0, 0, W, H)
    from dla.gds import orient_trans
    with pytest.raises(ValueError):
        orient_trans("R90", 0, 0, W, H)
