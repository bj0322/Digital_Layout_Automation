"""Block generators: one function per block type.

Each function returns a placed-and-routed :class:`dla.layout.Block`.
"""
from .decoder import make_decoder

BLOCKS = {
    "decoder": make_decoder,
}

__all__ = ["make_decoder", "BLOCKS"]
