// Greatest common divisor by repeated subtraction (Euclid), start/done handshake.
module gcd #(parameter W = 16) (
  input              clk,
  input              rst_n,
  input              start,
  input      [W-1:0] a,
  input      [W-1:0] b,
  output reg [W-1:0] result,
  output reg         done,
  output             busy
);
  localparam IDLE = 2'd0, RUN = 2'd1, FIN = 2'd2;
  reg [1:0]   state;
  reg [W-1:0] x, y;

  assign busy = (state == RUN);

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state  <= IDLE;
      x      <= {W{1'b0}};
      y      <= {W{1'b0}};
      result <= {W{1'b0}};
      done   <= 1'b0;
    end else begin
      done <= 1'b0;
      case (state)
        IDLE: if (start) begin
                x     <= a;
                y     <= b;
                state <= RUN;
              end
        RUN:  if (x == {W{1'b0}} || y == {W{1'b0}}) begin
                result <= x | y;
                state  <= FIN;
              end else if (x > y)
                x <= x - y;
              else if (y > x)
                y <= y - x;
              else begin
                result <= x;
                state  <= FIN;
              end
        default: begin
                done  <= 1'b1;
                state <= IDLE;
              end
      endcase
    end
  end
endmodule
