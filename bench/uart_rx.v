// UART receiver, 8N1, samples in the middle of each bit.
module uart_rx #(parameter CLKS_PER_BIT = 8) (
  input            clk,
  input            rst_n,
  input            rx,
  output reg [7:0] data,
  output reg       valid,     // one cycle pulse
  output reg       frame_err
);
  localparam CW = $clog2(CLKS_PER_BIT);
  localparam S_IDLE = 2'd0, S_START = 2'd1, S_DATA = 2'd2, S_STOP = 2'd3;
  reg       rx_m, rx_s;                               // synchroniser
  reg [1:0] state;
  reg [CW-1:0] cnt;
  reg [2:0] bitn;
  reg [7:0] sh;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      rx_m <= 1'b1; rx_s <= 1'b1;
    end else begin
      rx_m <= rx; rx_s <= rx_m;
    end
  end

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= S_IDLE; cnt <= 0; bitn <= 3'd0; sh <= 8'd0;
      data <= 8'd0; valid <= 1'b0; frame_err <= 1'b0;
    end else begin
      valid <= 1'b0;
      case (state)
        S_IDLE: begin
          cnt <= 0;
          if (!rx_s) state <= S_START;
        end
        S_START: begin
          if (cnt == CLKS_PER_BIT / 2 - 1) begin
            cnt <= 0;
            if (!rx_s) begin state <= S_DATA; bitn <= 3'd0; end
            else state <= S_IDLE;                        // glitch
          end else cnt <= cnt + 1'b1;
        end
        S_DATA: begin
          if (cnt == CLKS_PER_BIT - 1) begin
            cnt <= 0;
            sh <= {rx_s, sh[7:1]};
            bitn <= bitn + 3'd1;
            if (bitn == 3'd7) state <= S_STOP;
          end else cnt <= cnt + 1'b1;
        end
        S_STOP: begin
          if (cnt == CLKS_PER_BIT - 1) begin
            cnt <= 0;
            state <= S_IDLE;
            data <= sh;
            valid <= rx_s;
            frame_err <= !rx_s;
          end else cnt <= cnt + 1'b1;
        end
      endcase
    end
  end
endmodule
