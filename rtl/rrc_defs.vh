// shared constants, included by the RTL and testbenches
`ifndef RRC_DEFS_VH
`define RRC_DEFS_VH

// booth_mult_r4 register stages: S1 input regs, S2 select + 8->4, S3 4->2,
// S4 2->1. testbench measures the real latency and asserts on this.
`define BOOTH_R4_LATENCY 4

// Q1.15: 1 sign bit, 15 fractional bits
`define RRC_FRAC_BITS 15

// ceil(log2(x)) as a macro so it works in parameter port lists
`define RRC_CLOG2(x) ( (x) <=   1 ?  0 : (x) <=   2 ?  1 : (x) <=   4 ?  2 : \
                       (x) <=   8 ?  3 : (x) <=  16 ?  4 : (x) <=  32 ?  5 : \
                       (x) <=  64 ?  6 : (x) <= 128 ?  7 : (x) <= 256 ?  8 : \
                       (x) <= 512 ?  9 : 10 )

`endif
