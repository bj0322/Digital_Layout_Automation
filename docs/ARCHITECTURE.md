# Architecture

This document describes how the repository is organised today and how the
RTL-to-GDS flow (see `CLAUDE.md` / `docs/plan/`) is layered on top of it.

## 1. Two flows, one technology description

```
                       tech/<pdk>.json  (TECH_FILE.md)
                               |
                         dla/tech.py  Tech
          (LEF macros, 3-metal grid, pin access, rails, roles)
             /                         |                          \
  generator flow (existing)    std-cell generator (existing)   netlist flow (new, dla/db ...)
  dla/layout.py  Block         dla/stdcell/*                   dla/db  Library + Design
  dla/blocks/decoder.py        -> GDS/LEF/SPICE/V/tech.json    LEF in, DEF in/out, Verilog in/out
  dla/export.py, sim.py, gds.py        |                          |
            |                          v                          v
            v                 a tech file for its own   synth -> sta -> floorplan -> place
   placed + routed GDS        cells (feeds both flows)  -> cts -> route -> extract -> signoff
```

- **Generator flow** (`Block`): a block generator places cells by *logic role* and
  draws its own wiring on a 3-layer grid derived from the rules; an A* router
  finishes the irregular nets. Result: block JSON, structural Verilog, GDS, checks.
- **Std-cell generator** (`dla.stdcell`): builds transistor-level cells from the
  front-end rules in the tech file's `stdcell` section and writes a library plus a
  tech file pointing at it, so every other part can run on generated cells.
- **Netlist flow** (`dla.db` and the stage packages that follow): a general
  netlist → layout flow. Stages exchange files (DEF + structural Verilog + JSON
  side cars); in memory they share a `Design` bound to a `Library`.

## 2. Modules

| Module | Role | Main entry points |
|---|---|---|
| `dla/tech.py` | JSON tech file, std-cell LEF (pin layer + VIA1 OBS only), derived routing grid (`gx`, `gy`, `x_off`), automatic VIA1 pin access per cell, rails | `load()`, `Tech`, `parse_lef` |
| `dla/layout.py` | `Block` canvas: rows (R0/MX), placement by role, fixed wiring (`m2`, `m3`, `via1`, `via2`), grid-node ownership, A* `maze_route`, power straps, fill, notch fix | `Block` |
| `dla/blocks/decoder.py` | N→2^N decoder generator | `make_decoder` |
| `dla/export.py` | block JSON, structural Verilog, RTL model, testbench | `write_all`, `block_json` |
| `dla/sim.py` | role-based gate simulator (exhaustive decoder check) | `check_decoder` |
| `dla/gds.py` | KLayout back end: GDS build, metal connectivity, metal/via DRC, PNG | `build_gds`, `verify_conn`, `drc` |
| `dla/stdcell/` | `rules` (template from rules), `netlist` (stages, switch sim), `euler` (gate order), `cell` (placement + M1 router), `writers` (GDS/LEF/SPICE/V/tech), `check` (KLayout DRC/LVS), `library` | `make_library` |
| `dla/db/` | design database for the netlist flow | see §4 |
| `dla/rtl/` | Verilog front end: `lexer` (+ preprocessor), `parser` (AST), `elab` (→ AIG with FFs) | `elaborate` |
| `dla/synth/` | `aig`, `words` (bit-blasting), `sat` (CDCL), `fraig` (SAT sweeping, `cec`), `rewrite` (refactor, balance), `opt` (sweep), `cells` (Liberty target library), `mapper` (cut mapping), `netgen` (Design), `verify` (Design → AIG), `flow`, `report` | `synthesize` |
| `gen.py` | CLI (`decoder`, `stdcell`; flow stages to come) | |
| `klayout_scripts/` | wrappers to run the GDS steps inside the KLayout application | |

Dependencies point one way: `blocks → layout → tech`; `stdcell → tech (Layer, EPS)`;
`db` depends on nothing in the package except optionally a `Tech` to find its LEFs.
Nothing imports `db` from the existing modules, so the generator flow is unaffected.

## 3. Data flow

**Generator flow**: `Tech` → `make_decoder` → `Block` (insts, conn, shapes, vias) →
`export.block_json` (self-contained dict) → `gds.build_gds` (instances by cell name
from the library GDS + drawn metal) → `verify_conn` / `drc` / `render_png`.

**Std-cell generator**: `StdTech` (rules) → `netlist.make_cell` → `euler.orders` →
`cell.make_logic` (layout) → `writers` → `<lib>.gds/.lef/.spice/.v/.tech.json` →
`check.drc` / `check.lvs`.

**Synthesis** (M2): `rtl/*.v` → `rtl.elab.elaborate` (AST → bit-level AIG + FFs, port
list) → `synth.opt.sweep` → `fraig` → `refactor`/`balance`/`fraig` rounds →
`cells.TargetLib` (from `Library.liberty[corner]`) → `mapper.Mapper` → `netgen.build_design`
(→ `dla.db.Design`) → `verify.design_to_aig` + `fraig.cec` against the elaborated AIG →
`db.write_verilog`. FF instances are named `<register bit>_reg`; `synth.json` maps them to
the RTL bits (and whether the state is stored inverted).

**Netlist flow** (M0 provides the bottom layer):
```
rtl/*.v --synth--> synth.v --floorplan--> floorplan.def --place--> place.def
        --cts--> cts.def --route--> route.def --signoff--> <top>.gds, .spef, reports
```
Each stage reads the previous stage's DEF (+ Verilog for synth/STA), mutates a
`Design`, writes its own DEF and `out/<top>/reports/<stage>.rpt`.

## 4. Design database (`dla/db`)

| File | Contents |
|---|---|
| `geom.py` | `r6`, DEF orientations (`N S E W FN FS FE FW`), `xform_rect`, DBU conversion (`to_dbu` refuses off-grid values) |
| `lex.py` | LEF/DEF tokenizer with line numbers, `ParseError` |
| `library.py` | `Library` (layers bottom-up, vias, via rules, sites, macros with pins/OBS; Liberty per corner, `timing()`, `check_join()`) and the LEF reader |
| `liberty.py` | Liberty: generic tree parser/writer, `LibertyLib` (units → ns/pF, cells, pins, NLDM arcs, `ff`/`latch`), `Table` lookup, Boolean `Expr` |
| `design.py` | `Design` (insts, ports, nets, special nets, rows, tracks, gcell grid, DEF vias, blockages) |
| `def_io.py` | DEF read / write |
| `verilog.py` | structural Verilog read / write |

- **Names** are bit level. Bus bit 3 of `a` is `a[3]`; an escaped Verilog name
  `\a[3] ` is stored as `a\[3\]` (DEF escaping), so the two never collide.
- **Connectivity** lives on nets: `Net.conns = [(inst, pin)]`, with `("PIN", port)`
  for top-level ports (as in DEF). `Design.inst_pins()` gives the instance view.
- **Constants** in a netlist are the pseudo-nets `1'b0`/`1'b1`; DEF write refuses
  them (tie cells come from synthesis).
- **Routing** is kept per DEF statement (`Path`: layer, width for special nets,
  points with extensions, vias, `RECT`, `VIRTUAL`), `*` resolved on read.
- **Equality**: `Design.__eq__` compares a canonical dict (connection order inside a
  net ignored); round-trip tests rely on it.
- Unsupported DEF sections/options and Verilog constructs raise `ParseError` with
  `file:line`; unused LEF statements are kept verbatim (`extra`) or skipped.

## 5. Units and coordinates

- **Time / capacitance**: ns and pF everywhere; Liberty values are converted on read
  from the library's declared units.
- **Length**: µm as Python floats, rounded with `r6` (6 decimals) after arithmetic;
  geometry is snapped to the manufacturing grid where it is created
  (`tech.snap`, `stdcell.rules.snap`). DEF/LEF integer database units appear only in
  files: `to_dbu` raises on values off the DBU grid instead of rounding silently.
- **Origin**: a cell's (0, 0) is the lower-left of its LEF `SIZE` box (LEF `ORIGIN`
  is applied when reading). Block coordinates start at the block's lower-left; the
  decoder places its array at y = 0 with predecoders below (negative y).
- **Rows**: alternate R0 / MX (DEF `N` / `FS`), sharing rails. An `FS` instance at
  (x, y) occupies [x, x+w] × [y, y+h]; its geometry is mirrored about the row's
  horizontal centre line — the DEF convention "placement point = lower-left of the
  oriented bounding box" (`geom.xform_rect`, verified against KLayout's DEF reader).
- **Grid (generator flow)**: vertical-layer tracks at `x = x_off + gx·n`, horizontal
  lines at `y = y_min + gy·j`; `gx` defaults to the site width, `gy` is derived so
  different nets on neighbouring nodes are DRC clean.
- **Layers**: the generator flow knows three metals (`pin`, `vertical`,
  `horizontal`) and two vias by role; the netlist flow reads the full stack from the
  technology LEF (`files.tech_lef`), bottom-up in file order.
- **Determinism**: no randomness in the existing code; dict insertion order defines
  file order in every writer, so equal inputs give byte-equal outputs.

## 6. Testing

`python -m pytest -q tests` with `DLA_TECH` (and `PDK_ROOT`) set runs everything;
without `DLA_TECH` the PDK-dependent tests skip and the pure-Python ones (netlists,
Euler, `dla.db` round trips on `tests/data`) still run. Oracle tools (Yosys,
OpenROAD, OpenSTA, ngspice, Netgen) will only be called from `tests/oracle/`.
