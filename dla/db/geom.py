"""Geometry helpers shared by the design database: rounding, orientations, transforms.

Coordinates are um floats rounded to 6 decimals (``r6``), as everywhere else in dla.
Orientations use the DEF names; the placement point of an instance is the lower-left
corner of the master's bounding box *after* the orientation is applied (DEF convention).

    >>> xform_rect((0.1, 0.2, 0.3, 0.4), "FS", (10.0, 5.0), 1.0, 3.0)
    (10.1, 7.6, 10.3, 7.8)
"""
import math

ORIENTS = ("N", "S", "E", "W", "FN", "FS", "FE", "FW")
# Block row orientations (dla.layout) -> DEF
BLOCK_ORIENT = {"R0": "N", "MX": "FS"}

# linear part of each orientation: (x, y) -> (a*x + b*y, c*x + d*y)
_LIN = {
    "N": (1, 0, 0, 1),        # R0
    "W": (0, -1, 1, 0),       # R90
    "S": (-1, 0, 0, -1),      # R180
    "E": (0, 1, -1, 0),       # R270
    "FN": (-1, 0, 0, 1),      # MY
    "FS": (1, 0, 0, -1),      # MX
    "FW": (0, 1, 1, 0),       # MX then R90
    "FE": (0, -1, -1, 0),     # MY then R90
}


def r6(v):
    return round(v, 6) + 0.0           # + 0.0: no -0.0


def norm_rect(r):
    x1, y1, x2, y2 = r
    return (r6(min(x1, x2)), r6(min(y1, y2)), r6(max(x1, x2)), r6(max(y1, y2)))


def swaps_xy(orient):
    """True if the orientation exchanges width and height (90 / 270 degrees)."""
    return _LIN[orient][0] == 0


def placed_size(orient, w, h):
    return (h, w) if swaps_xy(orient) else (w, h)


def xform_point(p, orient, at, w, h):
    """Master-local point (origin at the master's lower-left) -> design coordinates."""
    a, b, c, d = _LIN[orient]
    # offset so the transformed bounding box [0,w]x[0,h] starts at `at`
    xs = [a * x + b * y for x, y in ((0, 0), (w, 0), (0, h), (w, h))]
    ys = [c * x + d * y for x, y in ((0, 0), (w, 0), (0, h), (w, h))]
    x, y = p
    return (r6(a * x + b * y - min(xs) + at[0]), r6(c * x + d * y - min(ys) + at[1]))


def xform_rect(r, orient, at, w, h):
    p = xform_point((r[0], r[1]), orient, at, w, h)
    q = xform_point((r[2], r[3]), orient, at, w, h)
    return norm_rect((p[0], p[1], q[0], q[1]))


def to_dbu(v, dbu, what="value"):
    """um -> integer database units; values off the DBU grid are an error, not snapped."""
    n = v * dbu
    k = int(math.floor(n + 0.5))
    if abs(n - k) > 1e-6:
        raise ValueError("%s %.9g um is not on the %d/um database grid" % (what, v, dbu))
    return k


def from_dbu(k, dbu):
    return r6(int(k) / dbu)
