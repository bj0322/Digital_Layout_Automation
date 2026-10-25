// 32-bit ALU with registered result and flags.
module alu32 (
  input             clk,
  input             rst_n,
  input      [3:0]  op,
  input      [31:0] a,
  input      [31:0] b,
  output reg [31:0] y,
  output reg        zero,
  output reg        carry
);
  localparam ADD = 4'd0, SUB = 4'd1, AND = 4'd2, OR = 4'd3, XOR = 4'd4, NOR = 4'd5,
             SLL = 4'd6, SRL = 4'd7, SRA = 4'd8, SLT = 4'd9, SLTU = 4'd10, PASSB = 4'd11;
  reg [32:0] r;
  wire [4:0] sh = b[4:0];

  always @* begin
    r = 33'd0;
    case (op)
      ADD:   r = {1'b0, a} + {1'b0, b};
      SUB:   r = {1'b0, a} - {1'b0, b};
      AND:   r = {1'b0, a & b};
      OR:    r = {1'b0, a | b};
      XOR:   r = {1'b0, a ^ b};
      NOR:   r = {1'b0, ~(a | b)};
      SLL:   r = {1'b0, a << sh};
      SRL:   r = {1'b0, a >> sh};
      SRA:   r = {1'b0, $signed(a) >>> sh};
      SLT:   r = {32'd0, $signed(a) < $signed(b)};
      SLTU:  r = {32'd0, a < b};
      PASSB: r = {1'b0, b};
      default: r = 33'd0;
    endcase
  end

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      y <= 32'd0; zero <= 1'b0; carry <= 1'b0;
    end else begin
      y     <= r[31:0];
      zero  <= (r[31:0] == 32'd0);
      carry <= r[32];
    end
  end
endmodule
