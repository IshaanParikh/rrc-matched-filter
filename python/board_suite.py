#!/usr/bin/env python3
"""
hardware test suite for the DueProLogic build: pushes several stimulus sets
through the board with dpl_host.py and checks every one bit for bit.

  qpsk_esN   link stimulus at Es/N0 = 0, 4, 8, 12 dB, each with its own
             noise seed; I-channel BER and EVM against theory
  corner     both rails, fastest swing, impulses, +/-1 LSB
  impulse    -1.0 impulses, output must be the negated coefficient ROM
  step       full-scale steps, output settles at the DC gain
  random     full-scale random words, saturating in both directions
  repeat     qpsk_es8 again, must match the first pass exactly (the
             trigger-bit reset really clears state between runs)
  soak       optional long random run (--soak N samples)

every set uses the 21-tap coefficients the board was built with, and the
suite refuses to run if they differ from hw_demo/data/rrc_coeffs.hex.

    python python/board_suite.py --port COM14
    python python/board_suite.py --port COM14 --soak 100000

run from repo root. writes hw_demo/suite/<set>/ and
hw_demo/suite/board_summary.json, and prints a table to paste back.
"""

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dpl_host                            # noqa: E402
import gen_stimulus                        # noqa: E402
from dsp_common import Q_MAX, Q_MIN, Q_SCALE, read_hex  # noqa: E402
from perf_analysis import qpsk_ber_theory  # noqa: E402

BOARD_SPAN = 5          # 21 taps, what hw_demo/dpl/dpl_top.v is built with


def make_set(outdir, pattern, esn0=8.0, seed=0xB0071, n_sym=2048):
    gen_stimulus.main(["--outdir", outdir, "--pattern", pattern, "--span", str(BOARD_SPAN),
                       "--esn0-db", str(esn0), "--seed", str(seed),
                       "--num-symbols", str(n_sym)])


def i_channel_stats(d, cap):
    """I-channel BER/EVM, as in board_eye.py"""
    with open(os.path.join(d, "stimulus_meta.json")) as fh:
        meta = json.load(fh)
    ref = np.load(os.path.join(d, "reference.npz"))
    y = cap["i_out"].astype(np.float64)
    sps, off = meta["sps"], meta["sym_offset"]
    n_sym = min((y.size - off - 1) // sps, ref["symbols"].size)
    k = np.arange(n_sym)
    out = y[k * sps + off] / Q_SCALE
    tx = ref["symbols"][:n_sym].real
    out = out / (np.dot(tx, out) / np.dot(tx, tx))
    evm = 100.0 * np.sqrt(np.mean((out - tx) ** 2) / np.mean(tx ** 2))
    n_err = int(np.sum((out < 0).astype(np.uint8) != ref["bits"][:n_sym, 0]))
    return dict(symbols=int(n_sym), bit_errors=n_err, ber=n_err / n_sym,
                ber_theory=float(qpsk_ber_theory(meta["esn0_db"])), evm_pct=float(evm))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Hardware test suite for the DueProLogic RRC filter.")
    ap.add_argument("--port", required=True, help="FT2232H UART port, e.g. COM14")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--root", default="hw_demo/suite")
    ap.add_argument("--rom", default="hw_demo/data/rrc_coeffs.hex",
                    help="the coefficient file the flashed build was compiled from")
    ap.add_argument("--qpsk-symbols", type=int, default=2048,
                    help="symbols per QPSK set (4 samples each, ~1000 samples/s)")
    ap.add_argument("--soak", type=int, default=0, help="extra random samples to run at the end")
    ap.add_argument("--only", default=None, help="comma-separated subset of set names")
    args = ap.parse_args(argv)

    if not os.path.exists(args.rom):
        raise SystemExit("%s not found. run python python/gen_stimulus.py --span 5 "
                         "--outdir hw_demo/data first (same as the board build)" % args.rom)
    rom, _ = read_hex(args.rom, 16, signed=True)

    sets = [("qpsk_es%d" % es, "qpsk", es, 0x100 + es, args.qpsk_symbols)
            for es in (0, 4, 8, 12)]
    sets += [("corner", "corner", 8, 1, 64), ("impulse", "impulse", 8, 1, 64),
             ("step", "step", 8, 1, 64), ("random", "random", 8, 2, 1024)]
    if args.soak:
        sets.append(("soak", "random", 8, 3, (args.soak + 3) // 4))
    if args.only:
        keep = {s.strip() for s in args.only.split(",")}
        sets = [s for s in sets if s[0] in keep or (s[0] == "qpsk_es8" and "repeat" in keep)]

    os.makedirs(args.root, exist_ok=True)
    rows = []
    first_es8 = None
    plan = [(s, False) for s in sets]
    if any(s[0] == "qpsk_es8" for s in sets) and (not args.only or "repeat" in args.only):
        plan.append((next(s for s in sets if s[0] == "qpsk_es8"), True))

    for (name, pattern, es, seed, n_sym), is_repeat in plan:
        d = os.path.join(args.root, name).replace(os.sep, "/")
        label = "repeat" if is_repeat else name
        print("\n=== %s ===" % label)
        if not is_repeat:
            make_set(d, pattern, es, seed, n_sym)
            coeff, _ = read_hex(os.path.join(d, "rrc_coeffs.hex"), 16, signed=True)
            if not np.array_equal(coeff, rom):
                raise SystemExit("coefficients for %s differ from %s, the board would "
                                 "not match" % (name, args.rom))
        n = read_hex(os.path.join(d, "qpsk_noisy_iq.hex"), 32, signed=False)[0].size
        cap_path = os.path.join(d, "board_output%s.npz" % ("_repeat" if is_repeat else ""))
        t0 = time.time()
        rc = dpl_host.main(["--port", args.port, "--baud", str(args.baud), "--datadir", d,
                            "--count", str(n), "--save", cap_path])
        dt = time.time() - t0
        cap = np.load(cap_path)
        got = cap["i_out"]
        row = dict(set=label, pattern=pattern, samples=int(got.size), requested=int(n),
                   mismatches=int(np.count_nonzero(got != cap["expected"])),
                   samples_per_s=round(got.size / dt, 1),
                   saturated=int(np.count_nonzero((got == Q_MAX) | (got == Q_MIN))),
                   ok=bool(rc == 0 and got.size == n))
        if pattern == "qpsk" and got.size > 64:
            row.update(i_channel_stats(d, cap))
        if pattern == "impulse":
            # independent of the golden model: each impulse returns -h exactly
            x = (read_hex(os.path.join(d, "qpsk_noisy_iq.hex"), 32, signed=False)[0] & 0xFFFF)
            hits = [k for k in np.flatnonzero(x == 0x8000) if k + rom.size <= got.size]
            bad = [k for k in hits if not np.array_equal(got[k:k + rom.size], -rom)]
            row["impulses_checked"] = len(hits)
            row["ok"] = row["ok"] and not bad
        if name == "qpsk_es8":
            if is_repeat:
                row["matches_first_pass"] = bool(np.array_equal(got, first_es8))
                row["ok"] = row["ok"] and row["matches_first_pass"]
            else:
                first_es8 = got.copy()
        rows.append(row)

    total = sum(r["samples"] for r in rows)
    n_fail = sum(not r["ok"] for r in rows)
    with open(os.path.join(args.root, "board_summary.json"), "w", newline="\n") as fh:
        json.dump(dict(rows=rows, total_samples=total, failed=n_fail), fh, indent=1)
        fh.write("\n")

    print("\n| set | samples | mismatches | saturated | samples/s | I BER (theory) | I EVM |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        ber = ("%.4f (%.4f)" % (r["ber"], r["ber_theory"])) if "ber" in r else ""
        evm = ("%.1f%%" % r["evm_pct"]) if "evm_pct" in r else ""
        print("| %s%s | %d | %d | %d | %.0f | %s | %s |"
              % (r["set"], "" if r["ok"] else " FAIL", r["samples"], r["mismatches"],
                 r["saturated"], r["samples_per_s"], ber, evm))
    print("\n%d samples through the board, %d of %d sets failed -> %s"
          % (total, n_fail, len(rows), "PASS" if n_fail == 0 else "FAIL"))
    print("summary: %s/board_summary.json" % args.root)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
