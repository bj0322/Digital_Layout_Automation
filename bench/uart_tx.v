// UART transmitter, 8 data bits, no parity, 1 stop bit.
module uart_tx #(parameter CLKS_PER_BIT = 8) (
  input        clk,
  input        rst_n,
  input        valid,     // load `data` when idle
  input  [7:0] data,
  output reg   tx,
  output       ready
);
  localparam CW = $clog2(CLKS_PER_BIT);
  reg [CW-1:0] cnt;
  reg [3:0]    bitn;      // 0: start, 1..8: data, 9: stop
  reg [7:0]    sh;
  reg          active;

  assign ready = !active;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      tx <= 1'b1; cnt <= 0; bitn <= 4'd0; sh <= 8'd0; active <= 1'b0;
    end else if (!active) begin
      tx <= 1'b1;
      if (valid) begin
        sh <= data; active <= 1'b1; bitn <= 4'd0; cnt <= 0;
        tx <= 1'b0;                                   // start bit
      end
    end else if (cnt != CLKS_PER_BIT - 1) begin
      cnt <= cnt + 1'b1;
    end else begin
      cnt  <= 0;
      bitn <= bitn + 4'd1;
      if (bitn == 4'd8) begin
        tx <= 1'b1;                                   // stop bit
      end else if (bitn == 4'd9) begin
        active <= 1'b0;
      end else begin
        tx <= sh[0];
        sh <= {1'b0, sh[7:1]};
      end
    end
  end
endmodule
