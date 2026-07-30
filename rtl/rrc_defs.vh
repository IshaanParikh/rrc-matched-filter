//============================================================================
// rrc_defs.vh -- shared constants for the lunar-orbit QPSK RRC receiver.
//
// Included at file scope (outside the module) by every RTL and testbench file
// that needs to reason about pipeline depth.
//============================================================================
`ifndef RRC_DEFS_VH
`define RRC_DEFS_VH

// Register stages inside booth_mult_r4:
//   S1 input regs + multiple pre-computation
//   S2 partial-product select + first compression level (8 -> 4)
//   S3 second compression level (4 -> 2)
//   S4 final compression (2 -> 1), product register
// The testbench measures the real latency and asserts it equals this value, so
// the two can never silently drift apart.
`define BOOTH_R4_LATENCY 4

// Q1.15: one sign bit, 15 fractional bits.
`define RRC_FRAC_BITS 15

// ceil(log2(x)) as a preprocessor macro rather than a constant function, so it
// can be used in parameter port lists (a Verilog-2001 function cannot be).
`define RRC_CLOG2(x) ( (x) <=   1 ?  0 : (x) <=   2 ?  1 : (x) <=   4 ?  2 : \
                       (x) <=   8 ?  3 : (x) <=  16 ?  4 : (x) <=  32 ?  5 : \
                       (x) <=  64 ?  6 : (x) <= 128 ?  7 : (x) <= 256 ?  8 : \
                       (x) <= 512 ?  9 : 10 )

`endif
