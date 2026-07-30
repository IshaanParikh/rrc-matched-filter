#!/usr/bin/env python3
"""
Phase 1 -- stimulus generation for the lunar-orbit QPSK RRC receiver.

Builds the ground-truth dataset the RTL is verified against:

    random bits -> QPSK symbols -> upsample x SPS -> TX root-raised-cosine
                -> AWGN (extreme, lunar-link budget) -> Q1.15 quantisation
                -> .hex for $readmemh

Outputs (all under --outdir, default ``data/``):

    rrc_coeffs.hex      N_TAPS x 16-bit Q1.15 matched-filter coefficients
    qpsk_noisy_iq.hex   NUM_SAMPLES x 32-bit words, {Q[15:0], I[15:0]}
    stimulus_meta.vh    `define's so the testbench tracks these parameters
    stimulus_meta.json  same, for the Python-side checker/plotter
    reference.npz       float reference arrays (symbols, clean/noisy signal)

Run from the repository root:  python python/gen_stimulus.py
"""

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dsp_common import (  # noqa: E402
    Q_MAX,
    Q_MIN,
    Q_SCALE,
    quantize_q15,
    rrc_taps,
    write_hex,
)


def corner_stream():
    """A deterministic fixed-point corner-case stream.

    The autoscaled QPSK stimulus deliberately never reaches the Q1.15 rails, so
    on its own it leaves the RTL's round-and-saturate stage untested.  This
    pattern drives the accumulator hard into both rails and back, so the
    bit-exact comparison against the golden model actually covers saturation,
    the sign boundary, and the -32768 asymmetry of two's complement.

    Returns (I, Q) as int32 arrays of Q1.15 codes.
    """
    hi, lo = Q_MAX, Q_MIN            # +32767, -32768

    def block(vals, n):
        return list(vals) * n

    i = []
    i += block([hi], 40)             # hold at +full scale -> positive rail
    i += block([lo], 40)             # hold at -full scale -> negative rail
    i += block([hi, lo], 20)         # fastest possible swing
    i += block([0], 20)              # settle
    i += block([hi, 0], 20)          # half-rate square
    i += block([lo, 0], 20)
    i += block([0], 8) + [hi] + block([0], 39)   # positive impulse
    i += block([0], 8) + [lo] + block([0], 39)   # negative impulse
    i += block([1, -1], 20)          # +/-1 LSB, exercises the rounding bit
    i += block([0], 40)

    # Q gets the same stress in a different order, so a bug that happens to be
    # symmetric in the two channels cannot hide.
    q = i[len(i) // 2:] + i[: len(i) // 2]

    return (np.asarray(i, dtype=np.int32), np.asarray(q, dtype=np.int32))


def build_parser():
    p = argparse.ArgumentParser(
        description="Generate QPSK + RRC + AWGN stimulus for the Verilog RRC FIR.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--num-symbols", type=int, default=4096, help="QPSK symbols to generate")
    p.add_argument("--sps", type=int, default=4, help="samples per symbol (oversampling factor)")
    p.add_argument("--span", type=int, default=8, help="RRC span in symbols")
    p.add_argument("--beta", type=float, default=0.35, help="RRC roll-off factor")
    # 8 dB puts the recovered clusters 2.5 sigma apart per axis -- four clearly
    # separated lobes -- while the pre-filter sample SNR is still about +2 dB,
    # i.e. an unusable cloud on the wire.  Drop it toward 3 dB for a genuinely
    # marginal link; the filter still tracks theory, the picture just gets ugly.
    p.add_argument("--esn0-db", type=float, default=8.0,
                   help="Es/N0 in dB after matched filtering (lower = harsher channel)")
    p.add_argument("--headroom", type=float, default=0.92,
                   help="target for the 99.99th-percentile INPUT sample magnitude, in Q1.15 full scale")
    p.add_argument("--out-headroom", type=float, default=0.95,
                   help="ceiling for the 99.99th-percentile matched-filter OUTPUT magnitude")
    p.add_argument("--seed", type=int, default=0xB0071, help="PRNG seed (reproducible stimulus)")
    p.add_argument("--outdir", default="data", help="output directory")
    p.add_argument("--pattern", choices=("qpsk", "corner"), default="qpsk",
                   help="qpsk = the link stimulus; corner = fixed-point rail/saturation stress")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    rng = np.random.default_rng(args.seed)
    os.makedirs(args.outdir, exist_ok=True)

    sps, n_sym = args.sps, args.num_symbols

    # -- 1. Random QPSK symbols -------------------------------------------
    # Independent I/Q bits give Gray mapping for free: a symbol error between
    # adjacent constellation points flips exactly one bit.
    bits = rng.integers(0, 2, size=(n_sym, 2), dtype=np.uint8)
    symbols = ((1 - 2.0 * bits[:, 0]) + 1j * (1 - 2.0 * bits[:, 1])) / np.sqrt(2.0)

    # -- 2. Upsample by SPS (zero stuffing) --------------------------------
    upsampled = np.zeros(n_sym * sps, dtype=np.complex128)
    upsampled[::sps] = symbols

    # -- 3. TX pulse shaping ------------------------------------------------
    h = rrc_taps(sps, args.span, args.beta)
    n_taps = h.size
    # Streaming convolution: y[n] = sum_j h[j] x[n-j], truncated to the input
    # length -- exactly the sample-for-sample mapping the RTL implements.
    shaped = np.convolve(upsampled, h)[: upsampled.size]

    # -- 4. AWGN ------------------------------------------------------------
    # With unit-energy RRC at both ends and unit-power symbols, the post-matched
    # -filter SNR equals |a|^2 / sigma^2, so sigma^2 = 10^(-EsN0/10) gives
    # exactly the requested Es/N0 at the slicer.
    esn0 = 10.0 ** (args.esn0_db / 10.0)
    sigma2 = 1.0 / esn0
    sigma_per_dim = np.sqrt(sigma2 / 2.0)
    noise = sigma_per_dim * (rng.standard_normal(shaped.size) + 1j * rng.standard_normal(shaped.size))
    received = shaped + noise

    # -- 5. Fixed-point scaling and quantisation ----------------------------
    # Pick a scale that uses most of the Q1.15 range without meaningful
    # clipping.  The noise, not the signal, sets the peak on a link this harsh.
    peak = np.percentile(np.maximum(np.abs(received.real), np.abs(received.imag)), 99.99)
    scale = args.headroom / peak

    # Sizing the input peak alone is not enough.  The matched filter
    # concentrates the pulse energy, so its OUTPUT peak can exceed its input
    # peak -- on a quiet channel it is the signal, not the noise, that sets the
    # maximum, and a symbol lands at scale*1.0 complex, i.e. scale*0.707 per
    # axis.  Predict the output peak and pull the scale back if the filter
    # would run into the Q1.15 rail.  (Without this the RTL still behaves
    # correctly -- it saturates rather than wrapping -- but the captured
    # waveform is clipped, which quietly flatters an eye diagram.)
    mf_out = np.convolve(received * scale, h)[: received.size]
    out_peak = np.percentile(
        np.maximum(np.abs(mf_out.real), np.abs(mf_out.imag)), 99.99)
    if out_peak > args.out_headroom:
        scale *= args.out_headroom / out_peak
    i_q15, clip_i = quantize_q15(received.real * scale)
    q_q15, clip_q = quantize_q15(received.imag * scale)
    n_samples = i_q15.size
    clip_pct = 100.0 * (clip_i + clip_q) / (2 * n_samples)

    # The corner pattern replaces the link stimulus outright; everything from
    # here on (packing, hex export, metadata) is shared.
    if args.pattern == "corner":
        i_q15, q_q15 = corner_stream()
        n_samples = i_q15.size
        clip_pct = 0.0
        n_sym = 0                 # not a symbol stream; nothing to demodulate

    coeff_q15, clip_h = quantize_q15(h)
    if clip_h:
        raise RuntimeError("RRC coefficients clipped in Q1.15 -- unexpected for unit-energy taps")

    # -- 6. Hex export ------------------------------------------------------
    # Samples are packed {Q[31:16], I[15:0]} into one 32-bit AXI4-Stream word.
    packed = ((q_q15.astype(np.int64) & 0xFFFF) << 16) | (i_q15.astype(np.int64) & 0xFFFF)

    coeff_path = os.path.join(args.outdir, "rrc_coeffs.hex")
    iq_path = os.path.join(args.outdir, "qpsk_noisy_iq.hex")
    # No comment header on the coefficient file: the same file feeds both the
    # simulator and Quartus ROM inference, and not every synthesis tool's
    # $readmemh accepts comments.  Provenance lives in stimulus_meta.json.
    write_hex(coeff_path, coeff_q15, 16)
    write_hex(iq_path, packed, 32,
              header="QPSK + RRC + AWGN, Q1.15 per component, packed {Q,I}\n"
                     "Es/N0=%.2f dB seed=0x%X n_samples=%d"
                     % (args.esn0_db, args.seed, n_samples))

    # Drop-in `case`-statement ROM for tools that will not infer a ROM from
    # $readmemh.  Included by rrc_coeff_rom.v when RRC_COEFF_CASE is defined.
    addr_w = max(1, int(np.ceil(np.log2(n_taps))))
    case_lines = [
        "// Generated by python/gen_stimulus.py -- do not edit by hand.",
        "// RRC taps, Q1.15: sps=%d span=%d beta=%.3f n_taps=%d (unit energy)"
        % (sps, args.span, args.beta, n_taps),
        "case (addr)",
    ]
    case_lines += [
        "    %d'd%-3d: coeff = %d'h%04x;" % (addr_w, k, 16, int(c) & 0xFFFF)
        for k, c in enumerate(coeff_q15)
    ]
    case_lines += ["    default: coeff = 16'h0000;", "endcase"]
    with open(os.path.join(args.outdir, "rrc_coeffs_case.vh"), "w", newline="\n") as fh:
        fh.write("\n".join(case_lines) + "\n")

    # -- 7. Metadata for the testbench and the checker ----------------------
    # A symbol's matched-filter peak lands at output index k*sps + (n_taps-1):
    # (n_taps-1)/2 samples of group delay from the TX filter already baked into
    # the stimulus, plus (n_taps-1)/2 more from the RX filter in the RTL.
    sym_offset = n_taps - 1
    meta = {
        "num_samples": int(n_samples),
        "num_symbols": int(n_sym),
        "n_taps": int(n_taps),
        "sps": int(sps),
        "span": int(args.span),
        "beta": float(args.beta),
        "esn0_db": float(args.esn0_db),
        "seed": int(args.seed),
        "scale": float(scale),
        "sym_offset": int(sym_offset),
        "data_width": 16,
        "axis_width": 32,
        "clip_percent": float(clip_pct),
        "iq_file": iq_path.replace(os.sep, "/"),
        "coeff_file": coeff_path.replace(os.sep, "/"),
        "out_file": os.path.join(args.outdir, "output.hex").replace(os.sep, "/"),
        "golden_file": os.path.join(args.outdir, "golden_output.hex").replace(os.sep, "/"),
    }
    with open(os.path.join(args.outdir, "stimulus_meta.json"), "w", newline="\n") as fh:
        json.dump(meta, fh, indent=2)
        fh.write("\n")

    vh = [
        "// Generated by python/gen_stimulus.py -- do not edit by hand.",
        "`ifndef STIMULUS_META_VH",
        "`define STIMULUS_META_VH",
        "`define STIM_NUM_SAMPLES %d" % n_samples,
        "`define STIM_NUM_SYMBOLS %d" % n_sym,
        "`define STIM_N_TAPS      %d" % n_taps,
        "`define STIM_SPS         %d" % sps,
        "`define STIM_SYM_OFFSET  %d" % sym_offset,
        '`define STIM_IQ_FILE     "%s"' % meta["iq_file"],
        '`define STIM_COEFF_FILE  "%s"' % meta["coeff_file"],
        '`define STIM_OUT_FILE    "%s"' % meta["out_file"],
        '`define STIM_GOLDEN_FILE "%s"' % meta["golden_file"],
        "`endif",
    ]
    with open(os.path.join(args.outdir, "stimulus_meta.vh"), "w", newline="\n") as fh:
        fh.write("\n".join(vh) + "\n")

    # Float reference for the plotter / checker.
    np.savez_compressed(
        os.path.join(args.outdir, "reference.npz"),
        bits=bits, symbols=symbols, shaped=shaped, received=received,
        taps=h, coeff_q15=coeff_q15, i_q15=i_q15, q_q15=q_q15, scale=scale,
    )

    # -- 8. Report ----------------------------------------------------------
    if args.pattern == "corner":
        print("Fixed-point corner-case stimulus generated")
        print("  samples            : %d  (rails, fastest swing, impulses, +/-1 LSB)"
              % n_samples)
    else:
        print("QPSK/RRC/AWGN stimulus generated")
        print("  symbols            : %d  (%d samples @ %d sps)" % (n_sym, n_samples, sps))
    print("  RRC                : %d taps, span %d sym, beta %.2f, unit energy"
          % (n_taps, args.span, args.beta))
    print("  peak |tap|         : %.6f  (Q1.15 code %d)"
          % (np.max(np.abs(h)), int(np.max(np.abs(coeff_q15)))))
    print("  Es/N0              : %.2f dB  (sigma^2 = %.4f per complex sample)"
          % (args.esn0_db, sigma2))
    print("  fixed-point scale  : %.6f  -> %.2f%% of input components clipped"
          % (scale, clip_pct))
    print("  predicted MF output peak (99.99%%) : %.3f of full scale"
          % min(out_peak, args.out_headroom))
    print("  input sample SNR   : %.2f dB (pre-matched-filter, why the raw scatter is a cloud)"
          % (10.0 * np.log10(np.mean(np.abs(shaped) ** 2) / sigma2)))
    print("  MF peak at output  : index k*%d + %d" % (sps, sym_offset))
    print("  wrote %s, %s, stimulus_meta.{vh,json}, reference.npz"
          % (os.path.basename(coeff_path), os.path.basename(iq_path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
