// Hand-written structural netlist for the dla.db tests.
module small (clk, a, b, y, z, \odd.name , k);
  input clk;
  input [3:0] a;
  input b;
  output [1:0] y;
  output z;
  output \odd.name ;
  output [2:0] k;
  wire [3:0] t;
  wire n1, \esc[0] ;
  (* keep *) wire unused;
  /* block
     comment */
  assign z = n1;
  assign \odd.name = a[0];
  assign k = {1'b0, t[3:2]};
  INV u1 (.A(a[3]), .Y(t[3]));
  NAND2 u2 (.A(a[2]), .B(b), .Y(t[2]));
  NAND2 \u[3] (.A(t[3]), .B(t[2]), .Y(n1));
  INV u4 (.A(n1), .Y(\esc[0] ));
  NAND2 u5 (.A(\esc[0] ), .B(1'b1), .Y(y[0]));
  INV u6 (.A(a[1]), .Y(y[1]));
  DFF r0 (.D(n1), .CK(clk), .Q(), .QN(t[0]));
endmodule
