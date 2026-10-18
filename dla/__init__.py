"""Digital_Layout_Auto: PDK-agnostic std-cell layout generators for digital blocks."""
from .blocks import BLOCKS, make_decoder
from .export import write_all

__version__ = "0.1.0"
__all__ = ["make_decoder", "BLOCKS", "write_all"]
