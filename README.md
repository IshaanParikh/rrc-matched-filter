# QPSK Receiver: RRC Matched Filter in Verilog

A 33-tap root-raised-cosine matched filter for a QPSK downlink. It is built
from custom pipelined radix-4 Booth multipliers, wrapped in AXI4-Stream with
back-pressure, and checked bit-for-bit against a Python model on real modulated
data. A 21-tap version runs on a Cyclone IV FPGA board, and every figure
and number below the line comes from samples sent through that board over USB.

![QPSK constellation before and after the filter, from the board](docs/board_constellation.png)

Left is what goes into the board: RRC-shaped QPSK at 4 samples/symbol on an
Es/N0 = 8 dB channel, sampled once per symbol. Right is what comes back out of
the FPGA. The four clusters come back.

## Results on the board

Measured on an EPT DueProLogic (Cyclone IV EP4CE6) running the 21-tap filter.
All 16,384 samples of the stimulus went through the board on each rail, one
at a time over USB, and came back for checking. See
[Running it on the DueProLogic](#running-it-on-the-dueprologic).

| Check | Result |
|---|---|
| Board output vs golden model | 0 mismatches in 16,384 I + 16,384 Q samples |
| Link performance | BER 0.98x theoretical QPSK (48 bit errors in 8,180 bits at Es/N0 = 8 dB) |
| Matched-filter gain | +5.12 dB (EVM 72.7% to 40.3%) |
| Resources | 5,439 / 6,272 logic elements (87%), no DSP blocks |
| Timing | Fmax 156.5 MHz against the board's 66 MHz clock |
| Throughput over USB | about 990 samples/s (lockstep UART link, not the filter) |

The BER number is the important one. A filter can match its own model exactly
and still be the wrong filter. Matching the theoretical QPSK bound, to within
the +/-14% one-sigma spread you get from only 48 errors, says the response
really is matched to the transmit pulse. The output SNR of 7.90 dB is within
0.1 dB of the 8 dB channel, so the 21-tap truncation and fixed-point maths
cost almost nothing.

| | |
|---|---|
| ![RRC taps on the board](docs/board_taps.png) | ![Eye diagram of the board output](docs/board_eye_diagram.png) |

## Results in simulation

Some properties can't be seen from a board on the end of a USB cable, so the
full 33-tap AXI4-Stream design is also checked by `./sim/run_sim.sh` on
Icarus Verilog 12.0.

| Check | Result |
|---|---|
| Booth multiplier vs Verilog `*` | 0 errors in 81,986 comparisons |
| Filter output vs golden model | 0 mismatches in 16,384 samples |
| Data loss under back-pressure | 16,412 beats in, 16,412 out (11,653 stall + 8,265 gap cycles) |
| Pipeline latency | 12 cycles, measured, matches the parameter |
| X-propagation | X at power-up, cleared by the synchronous reset |
| Saturation coverage | 237 saturating outputs, all bit-exact |
| Zero-ISI | eye open at the sampling instant on a clean channel |

## How it works

```
                    s_axis_tdata {Q[31:16], I[15:0]}
                                 |
        +------------------------+------------------------+
        |              rrc_fir_axis  (AXI4-Stream)        |
        |   pipeline_enable = s_tvalid & m_tready         |
        |   pipe_advance    = m_tready                    |
        |   valid/tlast shift register, LATENCY deep      |
        +-------+---------------------------------+-------+
                | I                               | Q
        +-------v-------+                 +-------v-------+
        | rrc_fir_core  |                 | rrc_fir_core  |
        +-------+-------+                 +---------------+
                |
   +------------v-------------+
   | 33-stage delay line      |  gated by sample_en, synchronous reset
   +------------+-------------+
                | 33 taps
   +------------v-------------+   +------------------+
   | 33 x booth_mult_r4       |<--| rrc_coeff_rom    |
   | radix-4 Booth, 4 stages  |   | 33 x Q1.15       |
   +------------+-------------+   +------------------+
                | 33 x 32-bit products
   +------------v-------------+
   | pipelined_adder_tree     |  33->17->9->5->3->2->1
   +------------+-------------+
                | 38-bit accumulator
   +------------v-------------+
   | round half up, saturate  |  (acc + 2^14) >>> 15, clamped to Q1.15
   +------------+-------------+
                v  m_axis_tdata
```

### Bit growth

| Stage | Format | Width |
|---|---|---|
| Input sample, coefficient | Q1.15 | 16 |
| Booth product | Q2.30 | 32 |
| Accumulator (33 products) | Q8.30 | 32 + ceil(log2 33) = 38 |
| Output | Q1.15 | 16 |

The worst case accumulator value is 33 * 2^15 * 2^15 = 2^35.04, which takes 37
signed bits. The design carries 38, so it provably cannot wrap.
`golden_model.py` asserts this on every run.

### Latency

| Stage | Cycles |
|---|---|
| Delay line | 1 |
| Booth multiplier | 4 |
| Adder tree (ceil(log2 33)) | 6 |
| Output round/saturate | 1 |
| Total | 12 |

The testbench measures the real end-to-end delay and fails if it disagrees with
the parameter, so the two cannot drift apart.

## Two decisions worth explaining

**The Booth multiplier pre-computes its multiples.** Radix-4 recoding turns 16
partial products into 8, each one of {0, +/-A, +/-2A}. The naive version
computes `-A` inside every partial-product stage, which puts a negation and an
adder on the critical path. Here +/-A and +/-2A are computed once in S1 and
shared by all eight multiplexers, so each stage is one 5:1 mux plus one 32-bit
add.

**The AXI4-Stream wrapper uses two enables, not one.** The obvious
`pipeline_enable = tvalid & tready` for everything is subtly wrong. When the
source goes idle the whole pipeline freezes, so a beat already sitting on the
master port stays valid while the sink keeps TREADY high, and the same sample
transfers twice. This design advances the arithmetic pipeline on TREADY alone,
so an idle source injects an invalid bubble that pushes the finished beat out
exactly once, while the delay line still moves only on real samples so the
filter history is never corrupted. The testbench proves it: 16,412 beats in,
16,412 out, under random stalls and gaps.

## Layout

```
python/   gen_stimulus.py     QPSK -> RRC -> AWGN -> Q1.15 -> .hex
          golden_model.py     bit-exact integer model of the RTL
          analyze_results.py  verify output.hex, measure EVM/BER, plot
          dsp_common.py       RRC design + fixed-point helpers
          viz_style.py        figure styling

rtl/      rrc_fir_axis.v          AXI4-Stream top level
          rrc_fir_core.v          delay line + MACs + tree + round/saturate
          booth_mult_r4.v         pipelined radix-4 Booth multiplier
          pipelined_adder_tree.v  generic balanced adder tree
          rrc_coeff_rom.v         coefficient ROM ($readmemh or case)
          rrc_defs.vh             shared constants

sim/      tb_booth_mult.v     multiplier unit test
          tb_rrc_fir_axis.v   data-in-the-loop testbench
          run_sim.sh          full regression

syn/      rrc_fir.sdc         250 MHz constraints
          build.tcl           Quartus project + compile
          report_timing.tcl   Fmax + critical path extraction
```

## Running it

```bash
./sim/run_sim.sh                 # everything: stimulus, golden, RTL, figures
DUMP=1 ./sim/run_sim.sh          # also write data/tb_rrc_fir_axis.vcd
./sim/run_sim.sh --esn0-db 3     # harsher channel
```

Needs Python with numpy/scipy/matplotlib, and Icarus Verilog (`winget install
Icarus.Verilog`). Run from the repository root, since the paths the testbench
uses are repo-relative and generated into `data/stimulus_meta.vh`.

Generated data is not tracked. It rebuilds deterministically from a fixed seed.

### What the regression covers

1. Booth multiplier unit test: corners, sweeps, 50k random, latency, stall
2. Full filter with the `$readmemh` coefficient ROM
3. Full filter with the `case`-statement ROM, so neither path rots
4. Fixed-point corner cases: both rails, fastest swing, impulses, +/-1 LSB
5. Clean channel, for the zero-ISI check
6. Python analysis: bit-exactness, EVM, BER vs theory, figures

## Synthesis of the full design: scripts written, not run

The 21-tap board build has been compiled and measured (see
[Results on the board](#results-on-the-board)). The full 33-tap I/Q design
targets a bigger part, the DE1-SoC's Cyclone V, and has not been through
Quartus yet, so there are no Fmax or resource numbers for it here. Publishing
estimates as if they were measured would defeat the point. The scripts are
ready:

```bash
quartus_sh  -t syn/build.tcl            # default device 5CSEMA5F31C6 (DE1-SoC)
quartus_sta -t syn/report_timing.tcl    # -> syn/output/fmax.rpt, critical_path.rpt
```

Fill the table in from `syn/output/`:

| Metric | Where |
|---|---|
| Fmax | `fmax.rpt`, restricted to register-to-register paths |
| ALMs / registers | `rrc_fir_axis.fit.rpt`, "Fitter Resource Usage Summary" |
| Worst-case slack | printed by `report_timing.tcl` |

`AUTO_DSP_RECOGNITION` is deliberately off. The point is a Booth multiplier
built from logic elements, and letting the synthesiser substitute hard DSP
blocks would delete the thing being measured.

If setup fails at 250 MHz, `critical_path.rpt` names the stage. The design can
absorb that: both the Booth compression tree and the adder tree already carry
one adder per stage, so the next move is to split the partial-product select
(S2) into its own register stage. That costs one cycle of latency and changes
nothing functionally, and the testbench's latency assertion will complain
immediately if `BOOTH_R4_LATENCY` was not updated to match.

Because the coefficients are hardwired, synthesis will constant-fold the Booth
recoder and every partial-product mux, collapsing each multiplier into a fixed
shift/add network. Expect utilisation well below a general 16x16 multiplier
array. Driving `b` from a writable RAM instead keeps the taps reloadable at
runtime, at the cost of that folding.

## Running it on the DueProLogic

`hw_demo/dpl/` is a full board build for the EPT DueProLogic (EPT-4CE6-AF-D2,
USB-C rev, EP4CE6E22C8N): one 21-tap channel of the filter, EPT's Active
Transfer library for the USB link, real pin assignments, and a `.pof` for the
on-board configuration flash. The full 33-tap I/Q design does not fit the
EP4CE6, which is why this uses the same scoped-down core as `hw_demo/`.

```
hw_demo/dpl/  dpl_top.v        board top: USB bytes -> filter -> USB bytes, status LEDs
              dpl_pins.tcl     pin assignments, each one traced to EPT's datasheet/schematic/.qsf
              rrc_bist.v       at-speed self-test: LFSR into the filter every clock, CRC out
              dpl.sdc          66 MHz clock
              build.tcl        Quartus project + compile, writes .sof and .pof
              ept/             drop EPT's Active Transfer library here (not committed)
              sim/             end-to-end testbench through EPT's real library
python/       dpl_host.py      streams samples over the COM port, checks them bit for bit
              board_eye.py     eye diagram, EVM and BER of a dpl_host.py capture
              live_demo.py     live plots: constellations, eye, bit-exact check
              dpl_bist.py      runs the at-speed self-test and checks its CRC
```

### Pins

| Signal | FPGA pin | Board net | Source |
|---|---|---|---|
| `CLK_66MHZ` | 23 | 66 MHz oscillator, GCLK1 | datasheet oscillator table, EPT .qsf |
| `RST` | 89 | CLK5, 2.74k pull-up to 3V3 | EPT .qsf, schematic |
| `UART_IN` | 43 | FT2232H BDBUS0 (TXD) | EPT .qsf, schematic |
| `UART_OUT` | 38 | FT2232H BDBUS1 (RXD) | EPT .qsf, schematic |
| `LED[0..3]` | 115, 114, 113, 112 | LED0..3 of the 6x6 array (XIO_6[0..3]) | datasheet LED table, EPT .qsf |

All I/O is 3.3 V LVTTL. The LEDs are sunk by the FPGA, so low is on, and
each block of four has a power jumper that has to be fitted.

On the USB-C board the FT2232H runs as a UART at 115200 baud, not the older
FT245 parallel bus the user manual describes. EPT's `active_serial_library`
wraps the same endterms behind it, so the top level uses that.

| LED | Meaning |
|---|---|
| LED0 | ~1 Hz heartbeat: clock running, out of reset |
| LED1 | toggles on every sample received |
| LED2 | toggles on every result sent back |
| LED3 | a result was dropped (host didn't wait for the reply) |

### Build

1. Copy EPT's library into `hw_demo/dpl/ept/src/`, as in
   [hw_demo/dpl/ept/README.md](hw_demo/dpl/ept/README.md).
2. Generate the 21-tap coefficients and compile:

```bash
python python/gen_stimulus.py --span 5 --outdir hw_demo/data
quartus_sh -t hw_demo/dpl/build.tcl
```

That writes `hw_demo/dpl/output/rrc_fir_dpl.sof` and `rrc_fir_dpl.pof`.

### Program the flash: EPT-Blaster, Active Serial, .pof

The board configures itself from its SPI flash at power-up. EPT-Blaster is
EPT's Quartus programmer driver that reaches that flash through the second
FT2232H channel, so no USB-Blaster is needed.

1. Install the driver once: copy `jtag_hw_mbftdi_blaster64.dll` from the DPL
   package's `Drivers/EPT_JTAG_Blaster/x64/` into Quartus's `bin64` folder
   (EPT's `EptInstaller.exe` does the same thing), then restart Quartus. If EPT-Blaster doesn't show up, open Device Manager,
   untick "Load VCP" on both FT2232H COM ports, and replug the board.
2. Open Tools > Programmer, click Hardware Setup and double-click
   EPT-Blaster.
3. Set Mode to **Active Serial Programming**.
4. Add File > `hw_demo/dpl/output/rrc_fir_dpl.pof`, tick Program/Configure,
   and click Start.
5. When it finishes the FPGA reconfigures from flash, and LED0 starts
   blinking.

The `.pof` targets an EPCS4, because the datasheet lists the flash as an
M25P40, which is the same part. EPT's own demo projects target EPCS1. If the
Programmer rejects the silicon ID, rebuild with
`quartus_sh -t hw_demo/dpl/build.tcl -config-device EPCS1`.

The USB-C port can't do JTAG. To load the `.sof` straight into the FPGA
without touching the flash, use a USB-Blaster on the 10-pin JTAG header in
JTAG mode.

### Talk to it

```bash
pip install pyserial
python python/dpl_host.py --port COM14
python python/dpl_host.py --port COM14 --count 16384               # whole stimulus, I rail
python python/dpl_host.py --port COM14 --count 16384 --channel q   # and the Q rail
python python/board_eye.py                                         # figures + BER/EVM
```

`dpl_host.py` clears the filter, sends I samples from
`hw_demo/data/qpsk_noisy_iq.hex` one at a time, and compares every result
against the golden model. Use whichever FT2232H COM port is the UART; the
other one belongs to EPT-Blaster. Each sample is a round trip, so set the
port's latency timer to 1 ms (Device Manager > port > Port Settings >
Advanced), or it crawls along at about 60 samples/s. With it set, a 1024-sample
run on the board takes about a second (969 samples/s) and matches the golden
model on every sample.

The board has one filter channel, so I and Q are two runs. Each run saves
what came back to `hw_demo/data/board_output_<channel>.npz`. `board_eye.py`
turns the two into the figures at the top of this README
(`docs/board_*.png`) and prints the EVM, BER and matched-filter gain. With
only the I capture it still makes the eye diagram and per-axis numbers.

The protocol is lockstep on purpose. Inside EPT's library the UART receiver
and the transmit path share one tri-state byte bus, and a byte arriving
while a reply is going out corrupts it. The testbench caught this; sending
one sample and waiting for its result never overlaps the two directions.

### Watch it live

![Live demo](docs/live_demo.gif)

```bash
pip install numpy matplotlib pyserial
python python/live_demo.py --port COM14      # the board
python python/live_demo.py                   # no board, golden model stands in
python python/live_demo.py --port COM14 --record docs/live_demo.gif --frames 300 --sweep 16:5
```

Streams noisy QPSK through the board and plots the constellation before and
after the filter, the eye diagram, and the bit-exact check against the golden
model. I and Q share the one filter channel: before each turn the host
replays that channel's last 20 samples, which restores the FIR state exactly.
The GIF above is from the simulation.

### At-speed self-test

Everything above feeds the filter one sample per USB round trip, about one
every millisecond, so the board never has to produce outputs back to back.
Host trigger bit 1 runs a self-test that does: the filter is cleared, then
takes a sample from a 32-bit LFSR on every 66 MHz clock for 2^20 clocks
(16 ms). Every output goes into a CRC-32, and a 9-word report comes back
over the link.

```bash
python python/dpl_bist.py --port COM14              # one run
python python/dpl_bist.py --port COM14 --runs 100   # 100M samples, ~10 s
python python/dpl_bist.py --model-only              # what the board should say
```

`dpl_bist.py` computes the same LFSR, filter and CRC from the bit-exact
model. A pass means all 1,048,576 outputs were right with the filter running
flat out. The report also carries the cycle count, from the first input to
the last output, which must be exactly 2^20 + 11 (no gaps). It also counts
outputs on a rail: the LFSR is uniform over the full 16-bit range, so about
8% of outputs saturate.

`./hw_demo/dpl/sim/run_bist_sim.sh` checks the self-test against the model
in simulation without EPT's library. `run_dpl_sim.sh` runs it through the
whole board top level as well.

### Simulate it

```bash
./hw_demo/dpl/sim/run_dpl_sim.sh
```

This runs the real board top level and EPT's real library from the UART pins
inward. A host model speaks EPT's framing, sends an impulse, random samples
with both rails, then a clear and a second impulse, and checks every result
against a reference FIR. It then runs a short self-test and checks the
report. The library's baud divider is sped up 4.5x so the
run is short; nothing else changes.
