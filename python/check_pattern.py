#!/usr/bin/env python3
"""
closed-form checks on the RTL output for the impulse and step patterns,
worked out from the coefficient file alone, so they don't lean on the
golden model being right:

  impulse : a -1.0 input (code -32768) returns -h[k] word for word, since
            (-32768*h + 2^14) >> 15 == -h exactly, and 0 between impulses
  step    : every sample of every held level, from the running tap sum
            while the delay line fills to the DC gain sum(h) once it has
  random  : output must rail in both directions (saturation exercised)

run from repo root: python python/check_pattern.py --datadir data_impulse
"""

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dsp_common import Q_MAX, Q_MIN, read_hex, round_sat  # noqa: E402


def unpack(packed):
    def field(shift):
        v = (packed >> shift) & 0xFFFF
        return np.where(v & 0x8000, v - 0x10000, v).astype(np.int64)
    return field(0), field(16)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Closed-form checks for impulse/step/random runs.")
    ap.add_argument("--datadir", required=True)
    args = ap.parse_args(argv)

    with open(os.path.join(args.datadir, "stimulus_meta.json")) as fh:
        meta = json.load(fh)
    pattern = meta.get("pattern", "qpsk")
    h, _ = read_hex(meta["coeff_file"], 16, signed=True)
    x_i, x_q = unpack(read_hex(meta["iq_file"], 32, signed=False)[0])
    y_i, y_q = unpack(read_hex(meta["out_file"], 32, signed=False)[0])
    n = h.size
    errors = 0
    checks = 0

    for name, x, y in (("I", x_i, y_i), ("Q", x_q, y_q)):
        if pattern == "impulse":
            # impulses are spaced wider than the filter, so the whole stream
            # is -h after each one and exactly 0 everywhere else
            exp = np.zeros(y.size, dtype=np.int64)
            for k in np.flatnonzero(x == Q_MIN):
                m = min(n, y.size - k)
                exp[k:k + m] = -h[:m]
                checks += 1
            if np.any(x[(x != 0) & (x != Q_MIN)]):
                raise SystemExit("impulse stimulus has values other than 0 and -1.0")
            for k in np.flatnonzero(y != exp)[:5]:
                print("  %s y[%d] = %d, expected %d" % (name, k, y[k], exp[k]))
            errors += int(np.count_nonzero(y != exp))
        elif pattern == "step":
            # every constant run, preceded by another constant run at least
            # n long (or the reset zeros): the accumulator is lvl*cumsum(h)
            # + prev*(sum(h) - cumsum(h)) until the line fills, then lvl*sum(h)
            cs = np.cumsum(h)
            starts = np.concatenate([[0], np.flatnonzero(np.diff(x) != 0) + 1])
            ends = np.concatenate([starts[1:], [x.size]])
            prev, prev_len = 0, n
            for a, b in zip(starts, ends):
                lvl = int(x[a])
                j = np.minimum(np.arange(b - a), n - 1)
                acc = lvl * cs[j] + prev * (cs[-1] - cs[j])
                exp = round_sat(acc.astype(np.int64), 15, 16)
                if prev_len >= n:
                    checks += 1
                    if not np.array_equal(y[a:b], exp):
                        errors += 1
                        bad = a + int(np.flatnonzero(y[a:b] != exp)[0])
                        print("  %s level %d from %d: y[%d] = %d, expected %d"
                              % (name, lvl, a, bad, y[bad], exp[bad - a]))
                prev, prev_len = lvl, b - a
        elif pattern == "random":
            checks += 1
            if not (np.any(y == Q_MAX) and np.any(y == Q_MIN)):
                errors += 1
                print("  %s: output never reached both rails" % name)
        else:
            raise SystemExit("no closed-form check for pattern %r" % pattern)

    n_sat = int(np.sum((y_i == Q_MAX) | (y_i == Q_MIN)) + np.sum((y_q == Q_MAX) | (y_q == Q_MIN)))
    print("Closed-form check, pattern %s, %d taps" % (pattern, n))
    print("  responses checked   : %d" % checks)
    print("  saturated outputs   : %d" % n_sat)
    print("  errors              : %d" % errors)
    print("  RESULT              : %s" % ("PASS" if errors == 0 and checks else "FAIL"))
    return 0 if errors == 0 and checks else 1


if __name__ == "__main__":
    sys.exit(main())
