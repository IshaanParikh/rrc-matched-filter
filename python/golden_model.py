#!/usr/bin/env python3
"""
bit-exact model of rtl/rrc_fir_core.v, integer arithmetic only:
  prod[j] = x[n-j] * h[j]     16x16 -> 32 (Q2.30)
  acc     = sum_j prod[j]     38-bit (Q8.30)
  y[n]    = sat16((acc + 2^14) >>> 15)

no floating point, so any mismatch against the RTL is a real bug

run from repo root: python python/golden_model.py
"""

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dsp_common import Q_FRAC_BITS, read_hex, round_sat, write_hex  # noqa: E402


def acc_width(n_taps, prod_w=32):
    return prod_w + int(np.ceil(np.log2(n_taps)))


def golden_fir(x_q15, h_q15):
    """streaming integer FIR, delay line starts at zero. returns the
    full-precision accumulator (int64) so headroom can be checked too."""
    x = np.asarray(x_q15, dtype=np.int64)
    h = np.asarray(h_q15, dtype=np.int64)
    return np.convolve(x, h)[: x.size]


def golden_fir_scalar(x_q15, h_q15):
    """literal shift-register version, used to sanity-check the vectorised
    one above. O(N*taps), only run on a short slice."""
    x = [int(v) for v in x_q15]
    h = [int(v) for v in h_q15]
    n_taps = len(h)
    dl = [0] * n_taps
    out = []
    for xv in x:
        dl = [xv] + dl[:-1]
        out.append(sum(dl[j] * h[j] for j in range(n_taps)))
    return np.array(out, dtype=np.int64)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bit-exact golden model of the RRC FIR RTL.")
    ap.add_argument("--datadir", default="data")
    args = ap.parse_args(argv)

    with open(os.path.join(args.datadir, "stimulus_meta.json")) as fh:
        meta = json.load(fh)

    coeff, _ = read_hex(meta["coeff_file"], 16, signed=True)
    packed, n_unknown = read_hex(meta["iq_file"], 32, signed=False)
    if n_unknown:
        raise RuntimeError("stimulus contains x/z words")
    if coeff.size != meta["n_taps"]:
        raise RuntimeError("coefficient count %d != n_taps %d" % (coeff.size, meta["n_taps"]))
    if packed.size != meta["num_samples"]:
        raise RuntimeError("sample count %d != num_samples %d" % (packed.size, meta["num_samples"]))

    def unpack(field):
        v = (packed >> field) & 0xFFFF
        return np.where(v & 0x8000, v - 0x10000, v).astype(np.int64)

    i_in, q_in = unpack(0), unpack(16)

    acc_i = golden_fir(i_in, coeff)
    acc_q = golden_fir(q_in, coeff)

    n_chk = min(256, i_in.size)
    ref = golden_fir_scalar(i_in[:n_chk], coeff)
    if not np.array_equal(ref, acc_i[:n_chk]):
        raise AssertionError("vectorised and scalar golden models disagree")

    aw = acc_width(meta["n_taps"])
    acc_max = int(max(np.abs(acc_i).max(), np.abs(acc_q).max()))
    theo_max = meta["n_taps"] * (1 << 15) * (1 << 15)
    if theo_max >= (1 << (aw - 1)):
        raise AssertionError("accumulator width %d too narrow for %d taps" % (aw, meta["n_taps"]))

    y_i = round_sat(acc_i, Q_FRAC_BITS, 16)
    y_q = round_sat(acc_q, Q_FRAC_BITS, 16)
    n_sat = int(np.count_nonzero((acc_i + (1 << 14) >> 15) != y_i)
                + np.count_nonzero((acc_q + (1 << 14) >> 15) != y_q))

    packed_out = ((y_q.astype(np.int64) & 0xFFFF) << 16) | (y_i.astype(np.int64) & 0xFFFF)
    out_path = os.path.join(args.datadir, "golden_output.hex")
    write_hex(out_path, packed_out, 32,
              header="Bit-exact golden model output, packed {Q,I} Q1.15")

    print("Golden model complete")
    print("  samples             : %d" % packed.size)
    print("  accumulator width   : %d bits (worst case needs %d)"
          % (aw, int(np.ceil(np.log2(theo_max))) + 1))
    print("  peak |acc| observed : %d  (%.1f%% of theoretical %d)"
          % (acc_max, 100.0 * acc_max / theo_max, theo_max))
    print("  output saturations  : %d" % n_sat)
    print("  scalar/vector cross-check on first %d samples: MATCH" % n_chk)
    print("  wrote %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
