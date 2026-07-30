#!/usr/bin/env python3
"""
Phase 3 -- pull the RTL's output.hex back into Python, verify it, measure it,
and plot it.

Checks the captured stream against the bit-exact golden model, then reports the
link metrics that actually matter (EVM, BER against theory, matched-filter SNR
gain) and writes the constellation / tap / eye figures to docs/.

Run from the repository root:  python python/analyze_results.py
"""

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import matplotlib                      # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt        # noqa: E402

from dsp_common import Q_SCALE, read_hex  # noqa: E402
from viz_style import (                    # noqa: E402
    AXIS, INK, INK_2, MUTED, SERIES_1, SERIES_2, panel_title, use_style,
)


def unpack_iq(packed):
    """{Q[31:16], I[15:0]} -> (I, Q) as signed int arrays."""
    def field(shift):
        v = (packed >> shift) & 0xFFFF
        return np.where(v & 0x8000, v - 0x10000, v).astype(np.float64)
    return field(0), field(16)


def qpsk_ber_theory(esn0_db):
    """Exact QPSK/4-QAM bit error rate over AWGN: Q(sqrt(Es/N0))."""
    from scipy.special import erfc
    esn0 = 10.0 ** (np.asarray(esn0_db, dtype=float) / 10.0)
    return 0.5 * erfc(np.sqrt(esn0 / 2.0))


def lsq_normalise(rx, ref):
    """Scale rx so it best matches ref in the least-squares sense.

    alpha = <ref, rx> / <ref, ref> is the standard EVM/AGC normalisation: it
    removes the arbitrary fixed-point gain without flattering the error.
    """
    alpha = np.vdot(ref, rx) / np.vdot(ref, ref)
    return rx / alpha, alpha


def main(argv=None):
    ap = argparse.ArgumentParser(description="Verify and visualise the RTL output stream.")
    ap.add_argument("--datadir", default="data")
    ap.add_argument("--docsdir", default="docs")
    ap.add_argument("--tag", default="", help="prefix for the output figure filenames")
    ap.add_argument("--figures", default="all",
                    help="comma-separated subset of: constellation, taps, eye, all")
    ap.add_argument("--no-plots", action="store_true")
    args = ap.parse_args(argv)

    want = {f.strip() for f in args.figures.split(",")}
    if "all" in want:
        want = {"constellation", "taps", "eye"}
    made = []

    use_style()
    os.makedirs(args.docsdir, exist_ok=True)

    with open(os.path.join(args.datadir, "stimulus_meta.json")) as fh:
        meta = json.load(fh)
    ref = np.load(os.path.join(args.datadir, "reference.npz"))

    sps = meta["sps"]
    n_taps = meta["n_taps"]
    rx_offset = meta["sym_offset"]          # TX + RX group delay, in samples
    tx_offset = (n_taps - 1) // 2           # TX group delay only

    # ---- load and verify the RTL stream ---------------------------------
    rtl, n_x = read_hex(meta["out_file"], 32, signed=False)
    gold, _ = read_hex(os.path.join(args.datadir, "golden_output.hex"), 32, signed=False)
    if n_x:
        raise SystemExit("FAIL: %d words in %s contain X" % (n_x, meta["out_file"]))
    if rtl.size != gold.size:
        raise SystemExit("FAIL: RTL produced %d words, golden model has %d" % (rtl.size, gold.size))
    n_bad = int(np.count_nonzero(rtl != gold))

    i_out, q_out = unpack_iq(rtl)
    i_in_all = ref["i_q15"].astype(np.float64)
    q_in_all = ref["q_q15"].astype(np.float64)
    symbols = ref["symbols"]
    bits = ref["bits"]

    # ---- decimate to symbol instants -------------------------------------
    n_sym = min(
        (rtl.size - rx_offset - 1) // sps,
        (i_in_all.size - tx_offset - 1) // sps,
        symbols.size,
    )
    k = np.arange(n_sym)
    out_sym = (i_out[k * sps + rx_offset] + 1j * q_out[k * sps + rx_offset]) / Q_SCALE
    in_sym = (i_in_all[k * sps + tx_offset] + 1j * q_in_all[k * sps + tx_offset]) / Q_SCALE
    tx_sym = symbols[:n_sym]

    out_n, _ = lsq_normalise(out_sym, tx_sym)
    in_n, _ = lsq_normalise(in_sym, tx_sym)

    # ---- metrics ----------------------------------------------------------
    def evm_pct(rx):
        return 100.0 * np.sqrt(np.mean(np.abs(rx - tx_sym) ** 2) / np.mean(np.abs(tx_sym) ** 2))

    def hard_bits(rx):                     # Gray-mapped QPSK slicer
        return np.stack([(rx.real < 0).astype(np.uint8), (rx.imag < 0).astype(np.uint8)], axis=1)

    tx_bits = bits[:n_sym]
    ber_out = float(np.mean(hard_bits(out_n) != tx_bits))
    ber_in = float(np.mean(hard_bits(in_n) != tx_bits))
    ber_theory = float(qpsk_ber_theory(meta["esn0_db"]))

    def snr_db(rx):
        err = np.mean(np.abs(rx - tx_sym) ** 2)
        return 10.0 * np.log10(np.mean(np.abs(tx_sym) ** 2) / err)

    snr_out, snr_in = snr_db(out_n), snr_db(in_n)
    n_err_bits = int(np.sum(hard_bits(out_n) != tx_bits))

    print("Phase 3 analysis")
    print("  RTL vs golden model : %s (%d of %d words differ)"
          % ("BIT-EXACT MATCH" if n_bad == 0 else "MISMATCH", n_bad, rtl.size))
    print("  symbols evaluated   : %d" % n_sym)
    print("  Es/N0 (channel)     : %.2f dB" % meta["esn0_db"])
    print("")
    print("                        pre-filter      RTL output")
    print("  EVM                 : %8.2f %%    %8.2f %%" % (evm_pct(in_n), evm_pct(out_n)))
    print("  effective SNR       : %8.2f dB   %8.2f dB" % (snr_in, snr_out))
    print("  BER                 : %8.4f     %8.4f" % (ber_in, ber_out))
    print("")
    print("  matched-filter gain : %+.2f dB" % (snr_out - snr_in))
    print("  theoretical QPSK BER at %.2f dB Es/N0 : %.4f" % (meta["esn0_db"], ber_theory))
    print("  RTL BER / theory    : %.2fx  (%d bit errors in %d bits)"
          % (ber_out / ber_theory if ber_theory > 0 else float("nan"), n_err_bits, 2 * n_sym))

    if n_bad != 0:
        raise SystemExit("FAIL: RTL output does not match the golden model")

    if args.no_plots:
        return 0

    # ---------------------------------------------------------------------
    # Figure 1 -- the deliverable: constellation before vs after.
    # Small multiples on a shared scale, single-series density scatter.  A
    # constellation is read by position, so colour carries no extra meaning
    # here and a second hue would be redundant encoding.
    # ---------------------------------------------------------------------
    # The "before" panel shows the raw received stream -- every sample, no
    # timing recovery -- because that is genuinely what arrives on the wire.
    # It is scaled by the same alpha as the symbol-instant fit so both panels
    # share an amplitude scale.  Each panel is annotated only with statistics
    # of the points it actually plots.
    def fig_path(name):
        p = os.path.join(args.docsdir, args.tag + name)
        made.append(os.path.basename(p))
        return p

    if "constellation" in want:
        _, alpha_in = lsq_normalise(in_sym, tx_sym)
        in_all = (i_in_all + 1j * q_in_all) / Q_SCALE / alpha_in
        snr_samples = 10.0 * np.log10(
            np.mean(np.abs(ref["shaped"]) ** 2) / (10.0 ** (-meta["esn0_db"] / 10.0)))

        lim = 2.6
        fig, axes = plt.subplots(1, 2, figsize=(9.6, 5.3), sharex=True, sharey=True)
        fig.subplots_adjust(top=0.74, bottom=0.12, left=0.075, right=0.975, wspace=0.14)

        panels = [
            (axes[0], in_all, "Channel input, before filtering",
             "every received sample (%d per symbol), no timing recovery" % sps,
             "sample SNR %.1f dB" % snr_samples),
            (axes[1], out_n, "RTL matched-filter output",
             "sampled once per symbol at the filter's peak",
             "EVM %.1f%%\nBER %.4f" % (evm_pct(out_n), ber_out)),
        ]

        for ax, data, title, sub, note in panels:
            ax.axhline(0, color=AXIS, lw=0.8, zorder=1)
            ax.axvline(0, color=AXIS, lw=0.8, zorder=1)
            ax.scatter(data.real, data.imag, s=7, c=SERIES_1, alpha=0.28,
                       linewidths=0, zorder=3, rasterized=True)
            # Ideal QPSK points are a reference overlay drawn in ink, not a
            # series hue -- annotation must not read as a second data series.
            for sx in (-1, 1):
                for sy in (-1, 1):
                    ax.plot(sx / np.sqrt(2), sy / np.sqrt(2), marker="+", ms=11,
                            mew=1.6, color=INK_2, zorder=4)
            panel_title(ax, title, sub)
            ax.set_xlim(-lim, lim)
            ax.set_ylim(-lim, lim)
            ax.set_aspect("equal")
            ax.set_xlabel("In-phase (I)")
            ax.text(0.035, 0.035, note, transform=ax.transAxes, fontsize=9,
                    color=INK_2, va="bottom", ha="left", linespacing=1.5)

        axes[0].set_ylabel("Quadrature (Q)")
        fig.text(0.055, 0.975, "QPSK recovery through the Verilog RRC matched filter",
                 ha="left", va="top", fontsize=14, weight="600", color=INK)
        fig.text(0.055, 0.905,
                 "Es/N0 = %.1f dB   |   %d symbols   |   normalised to the transmitted "
                 "constellation (+ marks ideal)" % (meta["esn0_db"], n_sym),
                 ha="left", va="top", fontsize=9, color=MUTED)
        fig.savefig(fig_path("constellation.png"))
        plt.close(fig)

    # ---------------------------------------------------------------------
    # Figure 2 -- RRC taps, float vs the Q1.15 values burned into the ROM.
    # Two series, so a legend is present.
    # ---------------------------------------------------------------------
    if "taps" in want:
        taps = ref["taps"]
        coeff = ref["coeff_q15"].astype(np.float64) / Q_SCALE
        n = np.arange(taps.size) - (taps.size - 1) // 2

        fig, (ax1, ax2) = plt.subplots(
            2, 1, figsize=(8.6, 6.0), height_ratios=[2.4, 1], sharex=True)
        fig.subplots_adjust(top=0.85, hspace=0.42)

        ax1.axhline(0, color=AXIS, lw=0.8)
        ax1.plot(n, taps, color=SERIES_1, label="ideal RRC (float64)", zorder=3)
        ax1.plot(n, coeff, ls="none", marker="o", ms=4.5, color=SERIES_2,
                 label="Q1.15 coefficient ROM", zorder=4)
        ax1.set_ylabel("amplitude")
        panel_title(ax1, "Root-raised-cosine matched filter, %d taps" % taps.size,
                    "beta = %.2f, span %d symbols, %d samples/symbol, unit energy"
                    % (meta["beta"], meta["span"], sps))
        ax1.legend(loc="upper right")

        err_lsb = (ref["coeff_q15"].astype(np.float64) - taps * Q_SCALE)
        ax2.axhline(0, color=AXIS, lw=0.8)
        ax2.vlines(n, 0, err_lsb, color=SERIES_2, lw=2.0)
        ax2.set_ylabel("error (LSB)")
        ax2.set_xlabel("tap index, relative to centre")
        ax2.set_ylim(-0.75, 0.75)
        panel_title(ax2, "Quantisation error",
                    "within +/-0.5 LSB at every tap, the bound for round-to-nearest",
                    y=1.12, y_sub=1.02)

        fig.savefig(fig_path("rrc_taps.png"))
        plt.close(fig)

    # ---------------------------------------------------------------------
    # Figure 3 -- eye diagram of the RTL's in-phase output.
    # ---------------------------------------------------------------------
    # At 4 samples/symbol the raw stream is far too coarse to draw an eye, so
    # the trace is band-limited-interpolated 8x for display only.  This changes
    # nothing about the data under test -- it is the same thing a scope does.
    if "eye" in want:
        from scipy.signal import resample_poly

        up = 8
        _, alpha_out = lsq_normalise(out_sym, tx_sym)
        i_up = resample_poly(i_out / Q_SCALE / np.real(alpha_out), up, 1)
        sps_up = sps * up
        centre0 = rx_offset * up

        fig, ax = plt.subplots(figsize=(8.6, 4.6))
        fig.subplots_adjust(top=0.82)

        t = np.linspace(-1.0, 1.0, 2 * sps_up)
        n_trace = 0
        for kk in range(n_sym):
            c = centre0 + kk * sps_up
            if c - sps_up < 0 or c + sps_up > i_up.size:
                continue
            ax.plot(t, i_up[c - sps_up:c + sps_up], color=SERIES_1, alpha=0.05, lw=1.0)
            n_trace += 1
            if n_trace >= 800:
                break

        ax.axvline(0, color=AXIS, lw=0.8)
        ax.axhline(0, color=AXIS, lw=0.8)
        for lev in (-1 / np.sqrt(2), 1 / np.sqrt(2)):
            ax.plot(0, lev, marker="+", ms=11, mew=1.6, color=INK_2, zorder=5)
        panel_title(ax, "Eye diagram, in-phase channel at the RTL output",
                    "Es/N0 = %.0f dB, %d overlaid traces of 2 symbol periods, 8x "
                    "interpolated for display; + marks the ideal decision level"
                    % (meta["esn0_db"], n_trace))
        ax.set_xlabel("time (symbol periods, 0 = sampling instant)")
        ax.set_ylabel("normalised amplitude")
        ax.set_xlim(-1, 1)
        fig.savefig(fig_path("eye_diagram.png"))
        plt.close(fig)

    print("")
    print("  figures -> %s/{%s}" % (args.docsdir, ", ".join(made)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
