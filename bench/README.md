# Benchmarks

Small synthesizable designs written for this repository (MIT, same as the project).
They exercise the RTL front end, synthesis, and later the physical flow.

| Design | Top | What it covers |
|---|---|---|
| `counter8.v` | `counter8` | 8-bit counter: enable, synchronous clear, load, async active-low reset |
| `gcd.v` | `gcd` | 16-bit GCD by subtraction: FSM, handshake, comparators, subtractors |
| `uart_tx.v` | `uart_tx` | 8N1 transmitter, parameterised baud divider, shift register |
| `uart_rx.v` | `uart_rx` | 8N1 receiver, 2-FF synchroniser, mid-bit sampling, framing error |
| `alu32.v` | `alu32` | 32-bit ALU: add/sub, logic, shifts (logical/arithmetic), signed/unsigned compare, registered output |
