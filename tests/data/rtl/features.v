// Front-end feature test: operators, widths, signedness, selects, generate,
// functions, hierarchy, loops, async set/reset. Compared against Icarus.
`define WIDTH 8
module adder #(parameter W = 4, parameter SUB = 0) (input [W-1:0] x, y, output [W:0] z);
  generate
    if (SUB) begin : g_sub
      assign z = {1'b0, x} - {1'b0, y};
    end else begin : g_add
      assign z = x + y;
    end
  endgenerate
endmodule

module features (
  input                     clk, rst_n, set_n,
  input  [`WIDTH-1:0]       a, b,
  input  signed [7:0]       sa, sb,
  input  [2:0]              idx,
  input  [3:0]              sel,
  output [8:0]              sum, dif,
  output [15:0]             prod,
  output [7:0]              shl, shr, ashr, rot,
  output                    lt_u, lt_s, ge_s, eq, ne, red_and, red_or, red_xor, red_nand, lnot,
  output [7:0]              cat_rep,
  output                    bit_var,
  output [3:0]              part_up, part_dn, part_var,
  output reg [7:0]          wr_var,
  output [7:0]              tern, fn_out, popc,
  output signed [9:0]       sext,
  output reg [7:0]          q_rst, q_set,
  output reg [3:0]          q_en,
  output [7:0]              gen_xor,
  output [15:0]             mul_const
);
  function [7:0] swap_nib;
    input [7:0] v;
    begin
      swap_nib = {v[3:0], v[7:4]};
    end
  endfunction

  function integer ones;
    input [7:0] v;
    integer k;
    begin
      ones = 0;
      for (k = 0; k < 8; k = k + 1)
        ones = ones + v[k];
    end
  endfunction

  adder #(.W(8))          u_add (.x(a), .y(b), .z(sum));
  adder #(8, 1)           u_sub (a, b, dif);

  assign prod = a * b;
  assign shl  = a << idx;
  assign shr  = a >> idx;
  assign ashr = sa >>> idx;
  assign rot  = (a << idx) | (a >> (4'd8 - idx));
  assign lt_u = a < b;
  assign lt_s = sa < sb;
  assign ge_s = sa >= sb;
  assign eq   = a == b;
  assign ne   = a != b;
  assign red_and  = &a[3:0];
  assign red_or   = |b;
  assign red_xor  = ^a;
  assign red_nand = ~&b[7:4];
  assign lnot = !(a && b) || (idx == 3'd5);
  assign cat_rep  = {{2{a[1:0]}}, b[7:6], ~a[7], 1'b1};
  assign bit_var  = a[idx];
  assign part_up  = a[2 +: 4];
  assign part_dn  = b[7 -: 4];
  assign part_var = a[idx +: 4];
  assign tern  = sel[0] ? (sel[1] ? a : b) : (sel[2] ? a & b : a ^ b);
  assign fn_out = swap_nib(a) ^ swap_nib(b);
  assign popc  = ones(a);
  assign sext  = sa + sb;                 // signed, extended to 10 bits
  assign mul_const = a * 16'd10 + 3;

  genvar i;
  generate
    for (i = 0; i < 8; i = i + 1) begin : g_bits
      wire t = a[i] ^ b[7 - i];
      assign gen_xor[i] = t;
    end
  endgenerate

  always @* begin
    wr_var = a;
    wr_var[idx] = b[0];
    if (sel == 4'hf) wr_var = 8'h00;
  end

  always @(posedge clk or negedge rst_n)
    if (!rst_n) q_rst <= 8'h5a;
    else        q_rst <= q_rst ^ a;

  always @(posedge clk or negedge set_n)
    if (!set_n) q_set <= 8'hff;
    else if (sel[3]) q_set <= b;

  always @(posedge clk) begin : blk
    reg [3:0] tmp;
    tmp = a[3:0] + 4'd1;
    if (sel[1]) q_en <= tmp;
  end
endmodule
