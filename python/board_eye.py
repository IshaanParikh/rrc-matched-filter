#!/usr/bin/env python3
"""
eye diagram, EVM and BER of the in-phase samples that came back from the
DueProLogic, from the capture dpl_host.py saves.

the board only filters the I channel, so the figures here are per-axis:
EVM is the I error against the ideal +-1/sqrt(2) levels and BER counts I
bits only.

    python python/dpl_host.py --port COM14 --count 16384
    python python/board_eye.py

run from repo root. writes docs/board_eye_diagram.png.
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

from dsp_common import Q_SCALE         # noqa: E402
from viz_style import AXIS, INK_2, SERIES_1, panel_title, use_style  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="Eye diagram of the DueProLogic filter output.")
    ap.add_argument("--datadir", default="hw_demo/data")
    ap.add_argument("--capture", default=None,
                    help="dpl_host.py capture (default: DATADIR/board_output.npz)")
    ap.add_argument("--out", default="docs/board_eye_diagram.png")
    args = ap.parse_args(argv)

    with open(os.path.join(args.datadir, "stimulus_meta.json")) as fh:
        meta = json.load(fh)
    ref = np.load(os.path.join(args.datadir, "reference.npz"))
    cap = np.load(args.capture or os.path.join(args.datadir, "board_output.npz"))

    i_out = cap["i_out"].astype(np.float64)
    n_bad = int(np.count_nonzero(cap["i_out"] != cap["expected"]))
    sps = meta["sps"]
    rx_offset = meta["sym_offset"]          # TX + RX group delay, in samples

    n_sym = min((i_out.size - rx_offset - 1) // sps, ref["symbols"].size)
    if n_sym < 8:
        raise SystemExit("only %d samples in the capture, need a longer run" % i_out.size)

    k = np.arange(n_sym)
    out_i = i_out[k * sps + rx_offset] / Q_SCALE
    tx_i = ref["symbols"][:n_sym].real
    alpha = np.dot(tx_i, out_i) / np.dot(tx_i, tx_i)
    out_n = out_i / alpha

    evm = 100.0 * np.sqrt(np.mean((out_n - tx_i) ** 2) / np.mean(tx_i ** 2))
    n_err = int(np.sum((out_n < 0).astype(np.uint8) != ref["bits"][:n_sym, 0]))

    print("board capture")
    print("  samples          : %d (%d differ from the golden model)" % (i_out.size, n_bad))
    print("  symbols          : %d" % n_sym)
    print("  I-channel EVM    : %.2f %%" % evm)
    print("  I-channel BER    : %.4f (%d bit errors in %d bits)" % (n_err / n_sym, n_err, n_sym))

    # 8x interpolation for display only, as in analyze_results.py
    from scipy.signal import resample_poly

    use_style()
    up = 8
    i_up = resample_poly(i_out / Q_SCALE / alpha, up, 1)
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
    panel_title(ax, "Eye diagram, in-phase channel from the DueProLogic",
                "Es/N0 = %.0f dB, %d overlaid traces of 2 symbol periods, 8x "
                "interpolated for display; + marks the ideal decision level"
                % (meta["esn0_db"], n_trace))
    ax.set_xlabel("time (symbol periods, 0 = sampling instant)")
    ax.set_ylabel("normalised amplitude")
    ax.set_xlim(-1, 1)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    fig.savefig(args.out)
    plt.close(fig)
    print("  figure           : %s" % args.out)
    return 0 if n_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
