# Project status and next steps

Last updated: 2026-10-29 (branch `feat/stdcell-and-pin-access`).

## Done so far

- Decoder generator, transistor-level standard-cell generator and 6T SRAM bit-cell / 4-way mirrored array generator.
- Generic, PDK-independent SPICE support: `dla/spice.py` (`SpiceDeck`, `SpiceRunner`, `SpiceError` hierarchy).
- Optional PDK-aware SPICE artifacts for all three outputs (stdcell library, decoder/block, SRAM):
  - `dla/spice_artifacts.py`: tech-file `spice` schema (`SpiceConfig`), structural block netlists built from the real cell subcircuits, per-artifact `*.spice.json` metadata/results, DC checks and the `off|auto|require` simulation policy.
  - `dla/schematic.py`: dependency-light SVG schematics (transistor level for cells, gate level for blocks, array and bit-cell for SRAM), annotated with real simulation status only.
  - `gen.py`: `--simulate {off,auto,require}`, `--spice-cmd`, `--spice-timeout`, `--no-schematic`. The SRAM heuristic `[sim ]` result is reported independently of the SPICE policy; only the exit status combines them.
  - API: `make_library(..., spice=)`, `make_sram(..., spice=)`, `write_all(blk, outdir, spice=)`. With `spice=None` output is identical to before.
  - Docs: README ("SPICE artifacts, schematics and opt-in simulation") and `tech/TECH_FILE.md` ("SPICE section").
- Verification: `116 passed, 8 skipped` (skips are the external-tech / real-simulator gates). With a real ngspice engine and the NSPL 0.5 um BSIM3v3 TT model: stdcell 4/4 truth tables + 16 delay arcs, decoder 4-to-16 all vectors, SRAM hold/read/write all passed.

## Known limitations

- Functional checks are DC only: no SNM, write margin, transient SRAM timing or power; not a sign-off characterization.
- Pre-layout netlists have no parasitics. Generated INV delay is about 48% below Liberty; use the supplied PEX netlist for Liberty / sign-off correlation.
- Stdcell delay arcs are produced only when the tech file sets both `testbench.edge` and `testbench.load`.
- Very large decoder schematics (more than 60 gates) are collapsed into counted groups; only the 4-bit drawing was reviewed visually.
- ngspice is not bundled. Local validation used a ctypes shim over KiCad's `ngspice.dll` (kept in the ignored `local/` directory).

## Next steps

1. Decide whether timing / sign-off workflows must require a PEX netlist explicitly.
2. Optional transient SRAM checks (read/write timing, SNM, write margin) as a separate, clearly labelled mode.
3. Review the 8-10 bit decoder schematics visually and tune the grouping if needed.
4. Run the full suite plus the real-PDK checks in CI or on a machine with a native ngspice.
5. Squash / split the commit as desired and open the PR for `feat/stdcell-and-pin-access`.
