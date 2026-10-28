"""Digital_Layout_Auto: PDK-agnostic std-cell layout generators for digital blocks."""
from .blocks import BLOCKS, make_decoder
from .export import write_all
from .sram import make_sram
from .spice import SpiceDeck, SpiceError, SpiceRunner
from .spice_artifacts import SpiceConfig, SpiceOptions

__version__ = "0.2.0"
__all__ = ["make_decoder", "BLOCKS", "write_all", "make_sram", "SpiceDeck", "SpiceRunner", "SpiceError",
           "SpiceConfig", "SpiceOptions"]
