#!/usr/bin/env python3
"""
figures and link numbers from what actually came back from the DueProLogic,
using the captures dpl_host.py saves.

the board has one filter channel, so I and Q are two runs:

    python python/dpl_host.py --port COM14 --count 16384
    python python/dpl_host.py --port COM14 --count 16384 --channel q
    python python/board_eye.py

with only the I capture it still writes the eye diagram and the per-axis
numbers; the constellation needs both.

run from repo root. writes docs/board_{constellation,eye_diagram,taps}.png,
plain grayscale.
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


def load_capture(datadir, ch):
    """returns (outputs, mismatches) or None. board_output.npz is the name
    the I capture had before Q runs existed"""
    names = ["board_output_%s.npz" % ch] + (["board_output.npz"] if ch == "i" else [])
    for name in names:
        p = os.path.join(datadir, name)
        if os.path.exists(p):
            cap = np.load(p)
            out = cap["out"] if "out" in cap else cap["i_out"]
            return out.astype(np.int64), int(np.count_nonzero(out != cap["expected"]))
    return None


def qpsk_ber_theory(esn0_db):
    from scipy.special import erfc
    return 0.5 * erfc(np.sqrt(10.0 ** (esn0_db / 10.0) / 2.0))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Figures and BER/EVM from the DueProLogic captures.")
    ap.add_argument("--datadir", default="hw_demo/data")
    ap.add_argument("--docsdir", default="docs")
    args = ap.parse_args(argv)

    with open(os.path.join(args.datadir, "stimulus_meta.json")) as fh:
        meta = json.load(fh)
    ref = np.load(os.path.join(args.datadir, "reference.npz"))
    sps = meta["sps"]
    rx_offset = meta["sym_offset"]              # TX + RX group delay, in samples
    tx_offset = (meta["n_taps"] - 1) // 2       # TX group delay only

    caps = {ch: load_capture(args.datadir, ch) for ch in ("i", "q")}
    if caps["i"] is None:
        raise SystemExit("no I capture in %s, run dpl_host.py first" % args.datadir)
    chans = [ch for ch in ("i", "q") if caps[ch] is not None]

    n_out = min(caps[ch][0].size for ch in chans)
    n_sym = min((n_out - rx_offset - 1) // sps, ref["symbols"].size)
    if n_sym < 8:
        raise SystemExit("only %d samples in the capture, need a longer run" % n_out)
    k = np.arange(n_sym)
    tx = ref["symbols"][:n_sym]
    bits = ref["bits"][:n_sym]
    rail_in = {"i": ref["i_q15"], "q": ref["q_q15"]}
    rail_tx = {"i": tx.real, "q": tx.imag}

    def stats(rx, ideal, tx_bits):
        alpha = np.dot(ideal, rx) / np.dot(ideal, ideal)    # least-squares gain
        rx = rx / alpha
        err = np.mean((rx - ideal) ** 2)
        return {
            "rx": rx,
            "alpha": alpha,
            "evm": 100.0 * np.sqrt(err / np.mean(ideal ** 2)),
            "snr": 10.0 * np.log10(np.mean(ideal ** 2) / err),
            "errors": int(np.sum((rx < 0).astype(np.uint8) != tx_bits)),
        }

    res = {}
    for col, ch in enumerate(chans):
        out = caps[ch][0][k * sps + rx_offset] / Q_SCALE
        inp = rail_in[ch][k * sps + tx_offset] / Q_SCALE
        res[ch] = (stats(inp, rail_tx[ch], bits[:, col]), stats(out, rail_tx[ch], bits[:, col]))

    n_bits = n_sym * len(chans)
    errs_in = sum(res[ch][0]["errors"] for ch in chans)
    errs_out = sum(res[ch][1]["errors"] for ch in chans)
    snr_in = np.mean([res[ch][0]["snr"] for ch in chans])
    snr_out = np.mean([res[ch][1]["snr"] for ch in chans])
    evm_in = np.sqrt(np.mean([res[ch][0]["evm"] ** 2 for ch in chans]))
    evm_out = np.sqrt(np.mean([res[ch][1]["evm"] ** 2 for ch in chans]))
    ber_theory = float(qpsk_ber_theory(meta["esn0_db"]))
    ber_out = errs_out / n_bits

    print("board captures (%s)" % " + ".join(ch.upper() for ch in chans))
    for ch in chans:
        print("  %s vs golden model  : %d of %d samples differ"
              % (ch.upper(), caps[ch][1], caps[ch][0].size))
    print("  symbols            : %d" % n_sym)
    print("  Es/N0 (channel)    : %.2f dB" % meta["esn0_db"])
    print("")
    print("                       pre-filter    board output")
    print("  EVM                : %7.2f %%    %7.2f %%" % (evm_in, evm_out))
    print("  effective SNR      : %7.2f dB   %7.2f dB" % (snr_in, snr_out))
    print("  BER                : %7.4f      %7.4f" % (errs_in / n_bits, ber_out))
    print("")
    print("  matched-filter gain : %+.2f dB" % (snr_out - snr_in))
    print("  BER / theory        : %.2fx  (%d bit errors in %d bits, theory %.4f)"
          % (ber_out / ber_theory, errs_out, n_bits, ber_theory))

    plt.style.use("grayscale")
    os.makedirs(args.docsdir, exist_ok=True)
    made = []

    def save(fig, name):
        p = os.path.join(args.docsdir, name)
        fig.savefig(p, dpi=120, bbox_inches="tight")
        plt.close(fig)
        made.append(p)

    lev = 1 / np.sqrt(2)

    # constellation, input vs board output at the symbol instants
    if len(chans) == 2:
        fig, axes = plt.subplots(1, 2, figsize=(9, 4.5))
        for ax, idx, title in ((axes[0], 0, "Input to the board"),
                               (axes[1], 1, "Board output")):
            ax.scatter(res["i"][idx]["rx"], res["q"][idx]["rx"], s=3, c="k", alpha=0.3, linewidths=0)
            ax.plot([-lev, -lev, lev, lev], [-lev, lev, -lev, lev], "+", c="0.5", ms=12, mew=2)
            ax.set_title("%s (EVM %.1f%%)" % (title, evm_in if idx == 0 else evm_out))
            ax.set_xlabel("I")
            ax.set_ylabel("Q")
            ax.set_xlim(-2.2, 2.2)
            ax.set_ylim(-2.2, 2.2)
            ax.set_aspect("equal")
            ax.grid(True, c="0.85")
        fig.suptitle("QPSK at Es/N0 = %.0f dB, %d symbols, sampled once per symbol"
                     % (meta["esn0_db"], n_sym))
        save(fig, "board_constellation.png")
    else:
        print("\n  no Q capture, skipping the constellation")

    # eye diagram of the I rail. 8x interpolated for display only
    from scipy.signal import resample_poly

    up = 8
    i_up = resample_poly(caps["i"][0] / Q_SCALE / res["i"][1]["alpha"], up, 1)
    span = sps * up
    t = np.linspace(-1.0, 1.0, 2 * span)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    n_trace = 0
    for kk in range(n_sym):
        c = (rx_offset + kk * sps) * up
        if c - span < 0 or c + span > i_up.size:
            continue
        ax.plot(t, i_up[c - span:c + span], c="k", alpha=0.04, lw=0.8)
        n_trace += 1
        if n_trace >= 800:
            break
    ax.axvline(0, c="0.6", lw=0.8)
    ax.axhline(0, c="0.6", lw=0.8)
    ax.set_title("Eye diagram of the board output, I rail (%d traces, Es/N0 = %.0f dB)"
                 % (n_trace, meta["esn0_db"]))
    ax.set_xlabel("time (symbol periods, 0 = sampling instant)")
    ax.set_ylabel("normalised amplitude")
    ax.set_xlim(-1, 1)
    ax.grid(True, c="0.85")
    save(fig, "board_eye_diagram.png")

    # the 21 coefficients the board runs
    h, _ = read_hex(meta["coeff_file"], 16, signed=True)
    fig, ax = plt.subplots(figsize=(8, 3.5))
    n = np.arange(h.size)
    ax.vlines(n, 0, h, colors="k", lw=1.2)
    ax.plot(n, h, "o", c="k", ms=4)
    ax.axhline(0, c="0.6", lw=0.8)
    ax.set_title("RRC coefficients on the board (%d taps, beta %.2f, Q1.15)" % (h.size, meta["beta"]))
    ax.set_xticks(n[::2])
    ax.set_xlabel("tap")
    ax.set_ylabel("Q1.15 code")
    ax.grid(True, c="0.85")
    save(fig, "board_taps.png")

    print("\n  figures -> %s" % ", ".join(made))
    return 0 if all(caps[ch][1] == 0 for ch in chans) else 1


if __name__ == "__main__":
    sys.exit(main())
