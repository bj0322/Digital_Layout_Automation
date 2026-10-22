"""Std-cell generator: transistor-level layouts of static CMOS cells from front-end rules.

See :func:`dla.stdcell.library.make_library` and ``python gen.py stdcell``.
"""
from .library import make_library
from .netlist import CATALOG

__all__ = ["make_library", "CATALOG"]
