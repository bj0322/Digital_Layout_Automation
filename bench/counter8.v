// 8-bit up counter with enable, synchronous clear and parallel load.
module counter8 (
  input            clk,
  input            rst_n,     // asynchronous, active low
  input            en,
  input            clr,       // synchronous clear (priority over load)
  input            load,
  input      [7:0] d,
  output reg [7:0] q,
  output           wrap       // q == 8'hff while counting
);
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n)
      q <= 8'd0;
    else if (clr)
      q <= 8'd0;
    else if (load)
      q <= d;
    else if (en)
      q <= q + 8'd1;
  end
  assign wrap = en & (&q);
endmodule
