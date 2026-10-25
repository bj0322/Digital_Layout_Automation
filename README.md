# Digital_Layout_Auto

**PDK-agnostic generators for placed-and-routed digital block layouts built from
standard cells.** Describe your PDK once in a small JSON *tech file*; each block
type is a single Python function that returns a block that is already placed,
routed and ready to stream out to GDS.

| Block | Function | Status |
|---|---|---|
| N → 2^N decoder (optional enable) | `dla.blocks.make_decoder(n_bits, enable=False)` | ✅ 4–10 bits |
| **Std-cell library** (inv, nand2/3, nor2/3, and2, aoi21, fillers) | `dla.stdcell.make_library(tech, outdir)` | ✅ transistor level |
| MUX, register file, shift register, … | `make_...` | planned |

No PDK data is included. A ready-made tech file is provided for the open
[IHP SG13G2](https://github.com/IHP-GmbH/IHP-Open-PDK) PDK; for any other library
write your own (see [`tech/TECH_FILE.md`](tech/TECH_FILE.md)).

---

## Quick start (IHP SG13G2 example)

```bash
pip install klayout          # GDS / connectivity / DRC / PNG steps (or use --no-gds)
git clone --depth 1 https://github.com/IHP-GmbH/IHP-Open-PDK.git
export PDK_ROOT=$PWD/IHP-Open-PDK

python gen.py --tech tech/ihp_sg13g2.json decoder --bits 8 --enable   # -> out/dec8to256en/
export DLA_TECH=tech/ihp_sg13g2.json
python gen.py decoder --bits 10 --name row_dec
python gen.py --tech tech/ihp_sg13g2.json stdcell                     # own std cells -> out/dla_lib/
python gen.py --tech out/dla_lib/dla_lib.tech.json decoder --bits 6   # decoder from those cells
python gen.py --tech tech/ihp_sg13g2.json synth --verilog bench/gcd.v --top gcd   # RTL -> out/gcd/synth.v
python -m pytest -q tests                                             # 118 tests (oracle tests need yosys / iverilog)
```

From Python:

```python
from dla import make_decoder, write_all
from dla.tech import load

tech = load("tech/ihp_sg13g2.json")
blk = make_decoder(6, enable=True, tech=tech)   # placed + routed Block
print(blk.stats())                              # cells, fill, shapes, vias, size (um)
write_all(blk, "out/dec6to64en")                # JSON, layout netlist, RTL, testbench
```

Example run:

```
[tech] ihp_sg13g2: site 0.480 x 3.780, grid 0.480 x 0.500 (x offset 0.000), cells inv=sg13g2_inv_1, ...
[gen ] dec8to256en: 313 cells (+118 fill), 42.7 x 83.9 um, 6151 shapes, 1419 vias  (0.2s)
[sim ] exhaustive gate-level check: PASS
[gds ] out/dec8to256en/dec8to256en.gds (43.2 x 83.9 um)
[conn] 2063 probes: missing 0, opens 0, shorts 0 -> PASS
[drc ] metal/via rules: 0 violations
RESULT: PASS
```

### Outputs (`out/<block>/`)

| File | Contents |
|---|---|
| `<top>.json` | Block JSON: instances, routing shapes, vias, pins, layer map |
| `<top>_layout.v` | Structural netlist with exactly the connectivity drawn in the layout |
| `<top>_rtl.v`, `<top>_tb.v` | Behavioural reference model and an exhaustive testbench |
| `<top>.gds` | Final GDS (std cells instantiated by name from the library GDS) |
| `overview.png`, `predecoder.png`, `array_corner.png` | Layout snapshots |

### Built-in checks

1. **Gate-level simulation** in pure Python: every address, with EN = 0 and 1.
2. **Metal-level connectivity**: pin-layer/via1/vertical/via2/horizontal nets are
   extracted from the GDS and compared with the intended nets (opens, shorts,
   missing metal at pins, rails and labels).
3. **Metal/via DRC** driven by the tech file: width, spacing, wide-metal spacing with
   parallel run length, minimum area, via size and spacing, rectangular via
   enclosure, via keep-outs and stacking.

These are fast sanity checks, not sign-off. Always run your PDK's own DRC/LVS deck
on the GDS (the generated `_tb.v` runs with your library's Verilog models).

---

## Using another PDK

1. Copy `tech/ihp_sg13g2.json`, fill in your LEF/GDS paths, GDS layer numbers, the
   rules of the three metals and two vias, and the cell names for the roles
   `inv`, `nand2`, `nor2`, `nor3` and `fill`.
   Keep tech files for proprietary PDKs outside the repository (or under `local/`,
   which git ignores).
2. `python gen.py --tech local/my_pdk.json decoder --bits 4` — the `[tech]` line shows
   the derived site, routing grid and track offset; the run fails early with a clear
   message if a pin cannot be reached on the track grid.
3. Run your PDK's sign-off DRC on the produced GDS.

Requirements on the library and the full schema are in
[`tech/TECH_FILE.md`](tech/TECH_FILE.md).

### Validation

| PDK | What was checked | Result |
|---|---|---|
| IHP SG13G2 (open) | synthesis of all `bench/` designs: SAT equivalence with the RTL, Icarus gate-level simulation with the IHP cell models, area ≤ 1.3× Yosys + ABC | PASS (0.94–1.07×) |
| IHP SG13G2 (open) | all 14 decoders (4–10 bits, EN on/off): built-in checks, **official IHP KLayout DRC deck** (FEOL + BEOL, density excluded), Icarus Verilog with the IHP cell models | all clean / PASS |

---

## Synthesis (RTL → gates)

`python gen.py --tech <tech.json> synth --verilog <files> --top <top> [--mode area|delay]`
turns synthesizable Verilog-2005 into a netlist of the PDK's Liberty cells and proves it
equivalent to the RTL before writing it.

```
[tech] ihp_sg13g2: Liberty corner typ, 84 cells
[elab] gcd: 35 input bits, 18 output bits, 51 flops, 1057 AIG nodes
[opt ] AIG 1057 -> 688 nodes
[map ] 440 cells, area 6159.28 um^2, depth 4.519 ns (area mode)
[cec ] netlist vs RTL (SAT, FF boundaries): PASS
[out ] out/gcd/synth.v, out/gcd/reports/synth.rpt  (2.9s)
RESULT: PASS
```

1. **Front end** (`dla/rtl`): preprocessor, parser and elaborator for the synthesizable
   subset (parameters, generate, functions, hierarchy, `always` blocks with if/case/for,
   async reset/set). Unsupported constructs, latches, loops and multiple drivers stop
   with `file:line` errors.
2. **AIG** (`dla/synth`): bit-blasting into an and-inverter graph with flip-flops,
   SAT sweeping (FRAIG), ISOP-based refactoring and balancing.
3. **Mapping**: priority cuts (k ≤ 6), Boolean matching against every input permutation
   / inversion of every Liberty gate, area flow + exact area recovery; delay mode keeps
   required times. Flip-flops map to the library's `ff` cells (inverted storage for
   async set, clock inverter for the other edge); constants get tie cells.
4. **Proof**: the netlist is converted back to an AIG from the Liberty functions and
   checked against the elaborated RTL with a pure-Python CDCL SAT solver at the FF
   boundaries. A netlist that fails is never written.

Outputs in `out/<top>/`: `synth.v`, `synth.json` (report, FF instance → RTL register map),
`reports/synth.rpt`.

| Bench (IHP, typ) | Flops | Cells | Area µm² | Yosys+ABC µm² | Ratio | Depth area / delay mode (ns) | Equivalence |
|---|---|---|---|---|---|---|---|
| counter8 | 8 | 56 | 838.2 | 830.8 | 1.01 | 1.17 / 1.04 | PASS |
| gcd | 51 | 440 | 6159.3 | 5731.3 | 1.07 | 4.52 / 4.26 | PASS |
| uart_tx | 17 | 94 | 1564.0 | 1480.5 | 1.06 | 0.88 / 0.81 | PASS |
| uart_rx | 28 | 104 | 2153.6 | 2148.2 | 1.00 | 0.76 / 0.66 | PASS |
| alu32 | 34 | 1278 | 13022.6 | 13849.8 | 0.94 | 8.46 / 7.18 | PASS |

Yosys 0.33: `synth; dfflibmap; abc -liberty; opt_clean`. Depth is the sum of nominal
Liberty delays along the longest path (STA comes in M3). Every netlist above also
matches the RTL in Icarus Verilog with the IHP cell models (`tests/oracle`).

## Std-cell generator

`python gen.py --tech <tech.json> stdcell` builds a std-cell library at transistor level
from the front-end rules in the tech file's `stdcell` section (see
[`tech/TECH_FILE.md`](tech/TECH_FILE.md)). No std-cell library is needed, so a PDK that
ships none still gets the decoder (and future blocks).

```
[cell] dla_inv_1      3 sites (1.44 um), 2 devices, gate order A
[cell] dla_nand2_1    4 sites (1.92 um), 4 devices, gate order A B
[cell] dla_nor3_1     6 sites (2.88 um), 6 devices, gate order C B A
...
[sim ] switch-level truth tables: PASS
[drc ] front-end + metal-1 rules, 12 cells + abutment test: 0 violations
[lvs ] device extraction vs SPICE: PASS
[tech] out/dla_lib/dla_lib.tech.json  (use with --tech to build blocks from these cells)
```

1. **Netlist**: each cell is a list of static CMOS stages, a series/parallel pull-down
   expression per stage; the pull-up is its dual. An exhaustive switch-level simulation
   checks every network against its truth table.
2. **Gate order**: dynamic programming over common Euler trails of the NMOS and PMOS
   diffusion graphs (same gate order in both rows, fewest diffusion breaks).
3. **Template** (derived from the rules): p+ tap / VSS rail, NMOS band, channel with the
   poly-contact window, PMOS band in the n-well, n+ tap / VDD rail. Cells abut in
   R0/MX rows like any library.
4. **Placement + routing**: vertical poly columns at contacted / uncontacted pitch;
   metal-1 straps over contacted diffusion; an A\* router with geometric legality
   (spacing, wide spacing, notches, min area) wires the rest on metal 1 and gives every
   pin an access pad around a VIA1 on the block router's track grid.
5. **Views**: `<lib>.gds` (pure-Python writer, plus an abutment test cell), `.lef`,
   `.spice`, `.v` and `<lib>.tech.json` for the block generators.
6. **Checks** (KLayout): front-end + metal-1 DRC from the tech-file rules, and LVS
   (MOS extraction, compared with the SPICE netlist including W/L and pin names).

| Cell (IHP SG13G2 rules) | Sites | | Cell | Sites |
|---|---|---|---|---|
| inv_1 / inv_2 | 3 / 5 | | nor2_1 / nor3_1 | 4 / 6 |
| nand2_1 / nand3_1 | 4 / 6 | | and2_1 / aoi21_1 | 6 / 6 |

inv_1 and nand2_1 match the widths of the IHP library cells. The generator has also been
run on a second, proprietary bulk-CMOS PDK (not included) that forbids VIA1 near contacts
and stacked vias: the library and a decoder built from it were DRC clean there too. A dec6to64en built from
the generated cells passes the gate-level, connectivity, metal and front-end checks.

## Decoder architecture

The `n` address bits are split into `na = n // 2` row bits (`A[na-1:0]`) and
`nb = n - na` column bits (`A[n-1:na]`), giving `R = 2^na` rows × `C = 2^nb` columns.

- **Predecoders**: a k-bit group is split into `lo = k // 2` and `hi = k - lo` bits;
  each sub-group is decoded with nor2/nor3 of true/inverted literals (a 1-bit
  sub-group needs no gate) and combined by nand2 into the active-low `pa_n[R]`
  (rows) and `pb_n[C]` (columns).
- **Array**: `Y[c·R + r] = nor2(pa_n[r], pb_n[c])`.
- **Enable**: `ENn` becomes one more input of the column group's low sub-predecoder
  (nor2 → nor3); EN = 0 forces every `pb_n` high, so every Y is 0.
- Only nor2/nor3 are needed, so a group has at most 5 bits → **n = 4 … 10**.

### Floorplan

`H` = row height, `y = 0` is the bottom of the array.

```
 y = R*H   +-------+---------+--------------------------------+-------+
           | strap | channel | row nand2 | nor2 x C           | strap |  <- R rows, R0/MX alternating
           |  VDD  |         |           | one horizontal wire|  VDD  |     (rails shared)
 y = 0     |  VSS  |         +--------------------------------+  VSS  |
 y = -H    |       |         |  column nand2 x C              |       |  <- one vertical wire / column
 y = -3H   |       |  column predecoder row (inv, nor, [EN])  |       |
 y = -5H   |       |  row predecoder row                      |       |
 y = -6H   +-------+---- inputs A[*], EN, VDD, VSS (bottom edge) -----+
```

- **Regular wiring is drawn directly**: one horizontal-layer wire per row joins the
  row nand2 output to all nor2 A pins; each column nand2 is shifted so its output
  lands on the B-pin track of its array column, so one vertical wire feeds the
  whole column.
- **Everything else** (literals, predecoded signals, inputs) is routed by an
  **A\* maze router** on the derived grid (vertical layer on tracks, horizontal layer
  on grid lines). The grid guarantees that different nets on neighbouring nodes are
  DRC clean, so the router only tracks node ownership.
- **Power**: vertical-layer straps over filler columns at both ends, VIA1 onto every
  row rail.
- **Auto-retry**: if routing fails, the left channel is widened and the block is
  regenerated.
- **Outputs** `Y[*]` are in-block pins on the vertical layer at each nor2 output — the
  block is a macro meant to abut a memory array or word-line driver column.

### Sizes on IHP SG13G2

| Block | Logic cells | W × H (µm) |
|---|---|---|
| dec4to16 | 28 | 17.8 × 38.5 |
| dec6to64 | 94 | 26.4 × 53.6 |
| dec8to256 / en | 312 / 313 | 42.7 × 83.9 |
| dec10to1024 / en | 1122 / 1123 | 75.4 × 144.3 |

---

## Repository layout

```
dla/
  tech.py         tech-file loader: LEF parser, derived routing grid, automatic pin access
  layout.py       Block: rows and placement by logic role, wiring helpers, power straps,
                  A* maze router, filler insertion
  blocks/
    decoder.py    make_decoder()            <- one file / function per block type
  stdcell/        std-cell generator: rules (template), netlist (networks, switch sim),
                  euler (gate order), cell (placement + metal-1 router), writers
                  (GDS/LEF/SPICE/Verilog/tech), check (KLayout DRC + LVS), library
  rtl/            Verilog front end: lexer + preprocessor, parser, elaborator (-> AIG)
  synth/          aig, words (bit-blasting), sat, fraig (SAT sweeping / CEC), rewrite
                  (refactor, balance), cells (Liberty target), mapper, netgen, verify,
                  flow (synthesize), report
  db/             design database for the netlist flow: Library (LEF + Liberty timing),
                  Design (netlist, placement, routing), DEF read/write, structural Verilog
                  read/write, Liberty read/write with NLDM lookup
  export.py       block JSON, structural Verilog, RTL, testbench
  sim.py          pure-Python gate-level simulator (exhaustive checks)
  gds.py          KLayout back end: GDS build, connectivity, tech-driven DRC, PNG
gen.py            command-line interface
tech/             tech-file reference and the IHP SG13G2 example
docs/             ARCHITECTURE.md (modules, data flow, units), plan/ (milestone plans)
klayout_scripts/  wrappers for the KLayout application (klayout -b -r ...)
bench/            benchmark RTL (counter8, gcd, uart_tx, uart_rx, alu32)
tests/            pytest suite (PDK tests need DLA_TECH; tests/oracle need yosys / iverilog)
```

### Adding a block type

1. Create `dla/blocks/<block>.py` with `make_<block>(..., tech=None) -> Block`:
   create a `Block` → `add_row` / `place(name, role, site, row, pins)` → draw fixed
   wiring → `power_straps` → `maze_route` → `fill_rows`, then set `ports`,
   `boundary` and `meta["block"]`. Use logic roles, never cell names.
2. If it needs a new logic role (e.g. `dff`), add it to `Tech.ROLES`, `sim.GATES`
   and the tech files.
3. Register it in `BLOCKS` (`dla/blocks/__init__.py`), add a `gen.py` sub-command,
   a functional check and tests.

---

## Limitations

- Std cells: no Liberty (.lib) timing characterization of the generated cells yet
  (PDK Liberty files are read by `dla.db`); only static CMOS cells
  (no flip-flops, transmission gates); every transistor of a row has the same width.
  The built-in front-end DRC covers the rules in the tech file, not antenna / density /
  latch-up: run the PDK sign-off decks on the library.

- No timing analysis. In large decoders one nand2 drives a whole row of nor2s
  through a long horizontal wire; add buffering if row delay matters.
- Built-in DRC covers the three metals and two vias only; front-end, density and
  antenna rules need the PDK deck.
- No transistor-level LVS: connectivity is compared at metal level; std-cell
  internals are assumed correct.
- Libraries that need explicit well-tap cells are not supported yet.

## Requirements

- Python ≥ 3.8
- `klayout` Python package ≥ 0.28 for the GDS steps (optional with `--no-gds`)
- `pytest` for the tests

## License

MIT License, Copyright (c) 2026 Beomjoon Kim. See [LICENSE](LICENSE).
The example tech file `tech/ihp_sg13g2.json` only references the IHP SG13G2 PDK,
which is distributed separately by IHP under the Apache License 2.0.
