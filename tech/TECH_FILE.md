# Tech file reference

A tech file is a JSON document that tells Digital_Layout_Auto everything it needs to
know about a PDK. The generators themselves contain no PDK data.
[`ihp_sg13g2.json`](ihp_sg13g2.json) is a complete, working example for the open
IHP SG13G2 PDK.

> Keep tech files for **proprietary PDKs out of version control**. `.gitignore`
> already ignores `local/` and `tech/*.local.json`; put them there.

## What the PDK must provide

- A std-cell **LEF** (macros with `SIZE`, signal pins drawn on one metal layer —
  the *pin layer*) and the matching **GDS** (one cell per macro, same names).
- Three metal layers usable by the generators: the **pin layer** (usually metal 1),
  a **vertical** routing layer above it and a **horizontal** routing layer above that,
  plus the two vias between them.
- Cells for the logic roles `inv`, `nand2`, `nor2`, `nor3` and one or more **fillers**
  whose widths are multiples of the site. Rows abut with alternating R0/MX
  orientation and share their power rails, which must run along the full cell width
  on the pin layer.
- Libraries that need separate well-tap cells are not supported yet.

## Schema

```jsonc
{
  "name": "my_pdk",                       // used in messages and output headers
  "files": {
    "lef": ["${PDK_ROOT}/.../stdcell.lef"], // one or more LEF files (macros)
    "gds": "${PDK_ROOT}/.../stdcell.gds",
    "verilog": "${PDK_ROOT}/.../stdcell.v"  // optional, for your own simulations
  },
  "manufacturing_grid": 0.005,             // um
  "site": "CoreSite",                      // LEF SITE name, or [width, height] in um;
                                           // default: size of the smallest filler
  "power": {"vdd": "VDD", "vss": "VSS"},   // power / ground pin names in the LEF
  "grid": {                                // all optional
    "x_pitch": 0.48,                       // vertical-layer track pitch, default = site width
    "x_offset": 0.0,                       // track offset from the cell origin, default: auto
    "y_pitch": 0.50                        // horizontal grid pitch, default: derived from rules
  },
  "layers": {
    "pin":        {"lef": "Metal1", "gds": [8, 0],  "text": [8, 25],
                   "width": 0.16, "space": 0.18, "min_area": 0.09,
                   "wide_space": [[0.30, 1.0, 0.22]]},  // [width >=, parallel run >=, space]
    "vertical":   {"lef": "Metal2", "gds": [10, 0], "text": [10, 25], "width": 0.20, "space": 0.21, ...},
    "horizontal": {"lef": "Metal3", "gds": [30, 0], "text": [30, 25], "width": 0.20, "space": 0.21, ...},
    "via1": {"lef": "Via1", "gds": [19, 0], "size": 0.19, "space": 0.22,
             "enc_below": [0.01, 0.05],          // [side, end] enclosure, either orientation
             "enc_above": [0.005, 0.05]},
    "via2": {"lef": "Via2", "gds": [29, 0], "size": 0.19, "space": 0.22,
             "enc_below": [0.005, 0.05], "enc_above": [0.005, 0.05]},
    "boundary": [189, 4],                         // optional: placement boundary layer
    "via1_keepout": {"CONT": {"gds": [6, 0], "space": 0.1}}   // optional
  },
  "rules": {
    "via_stack": true,          // false: VIA2 may not sit on VIA1
    "via1_via2_space": 0.0,     // optional minimum VIA1-VIA2 distance
    "via1_obs_space": 0.22      // distance from VIA1-layer OBS in the LEF (default: via1 space)
  },
  "cells": {
    "inv":   "INV_CELL",                         // or {"cell": "...", "pins": {"A": "I", "Y": "ZN"}}
    "nand2": "NAND2_CELL",                       // logical pins A, B, Y
    "nor2":  "NOR2_CELL",
    "nor3":  "NOR3_CELL",                        // logical pins A, B, C, Y
    "fill":  ["FILL1", "FILL2", "FILL4"]
  }
}
```

Paths may use `${VAR}` or `${VAR:-default}`; relative paths are relative to the tech file.
If a library's pin names differ from `A/B/C/Y`, give the mapping under `pins`.

## What is derived automatically

- **Routing grid.** Via pads are sized from the via size and the rectangular
  enclosure, oriented to fit the track pitch. The y pitch is the smallest value for
  which pads/wires of different nets on neighbouring grid nodes keep the layer
  spacing, so the router only has to track node ownership.
- **Pin access.** For every signal pin a VIA1 position on a vertical-layer track is
  searched in the LEF geometry. If the pin metal is too narrow for the via
  enclosure, a small pin-layer patch is added when it keeps the pin-layer spacing
  to everything else in the cell.
- **Minimum area.** Pin stubs on the vertical layer are lengthened to the layer's
  `min_area`, and their grid nodes are reserved accordingly.
- **Rails** are read from the filler's power pins.

Run `python gen.py --tech my_pdk.json decoder --bits 4` to see the derived grid
(`[tech]` line) and check a first block.
