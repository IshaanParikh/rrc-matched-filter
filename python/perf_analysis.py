#!/usr/bin/env python3
"""
link-level performance of the matched filter, using the bit-exact golden
model (the RTL matches it word for word in sim, and so does the board):

  1. BER vs Es/N0 against theoretical QPSK, 33-tap and 21-tap builds
  2. MER vs Es/N0 out to 50 dB, fixed point against a float64 filter fed the
     same samples (the gap is the implementation loss)
  3. clean-channel MER, which is the ISI + quantisation floor
  4. dynamic range: MER vs input level, from "too quiet" to clipping
  5. frequency response of the float and Q1.15 taps

writes docs/perf_*.png and docs/perf_results.json.

run from repo root: python python/perf_analysis.py   (--quick for a fast pass)
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

from dsp_common import Q_FRAC_BITS, Q_SCALE, quantize_q15, round_sat, rrc_taps  # noqa: E402
from golden_model import golden_fir    # noqa: E402
from viz_style import (                # noqa: E402
    AXIS, INK, INK_2, MUTED, SERIES_1, SERIES_2, panel_title, use_style,
)

SPS = 4
BETA = 0.35
# the two builds: the full RTL that's simulated, and the one on the board
CONFIGS = [
    ("33-tap (simulated RTL)", 8, SERIES_1),
    ("21-tap (DueProLogic)", 5, SERIES_2),
]


def qpsk_ber_theory(esn0_db):
    from scipy.special import erfc
    esn0 = 10.0 ** (np.asarray(esn0_db, dtype=float) / 10.0)
    return 0.5 * erfc(np.sqrt(esn0 / 2.0))


def make_block(rng, n_sym, h, esn0_db):
    """same chain as gen_stimulus.py: bits -> QPSK -> TX RRC -> AWGN"""
    bits = rng.integers(0, 2, size=(n_sym, 2), dtype=np.uint8)
    sym = ((1 - 2.0 * bits[:, 0]) + 1j * (1 - 2.0 * bits[:, 1])) / np.sqrt(2.0)
    up = np.zeros(n_sym * SPS, dtype=np.complex128)
    up[::SPS] = sym
    shaped = np.convolve(up, h)[: up.size]
    if esn0_db is None:
        return bits, sym, shaped
    sigma = np.sqrt(10.0 ** (-esn0_db / 10.0) / 2.0)
    noise = sigma * (rng.standard_normal(up.size) + 1j * rng.standard_normal(up.size))
    return bits, sym, shaped + noise


def fit_scale(rx, h, headroom=0.92, out_headroom=0.95):
    """gen_stimulus.py's input scaling rule"""
    peak = np.percentile(np.maximum(np.abs(rx.real), np.abs(rx.imag)), 99.99)
    scale = headroom / peak
    mf = np.convolve(rx * scale, h)[: rx.size]
    out_peak = np.percentile(np.maximum(np.abs(mf.real), np.abs(mf.imag)), 99.99)
    if out_peak > out_headroom:
        scale *= out_headroom / out_peak
    return scale


def run_filters(rx, scale, h, coeff):
    """returns (fixed-point output, float output), both complex in input units"""
    xi, _ = quantize_q15(rx.real * scale)
    xq, _ = quantize_q15(rx.imag * scale)
    yi = round_sat(golden_fir(xi, coeff), Q_FRAC_BITS, 16)
    yq = round_sat(golden_fir(xq, coeff), Q_FRAC_BITS, 16)
    fixed = (yi + 1j * yq.astype(np.float64)) / Q_SCALE / scale
    flt = np.convolve(rx, h)[: rx.size]
    return fixed, flt


def symbol_stats(y, sym, bits, n_taps, skip):
    """slice once per symbol at the TX+RX group delay. returns (err bits,
    total bits, error power, signal power) with the LSQ gain removed"""
    off = n_taps - 1
    n = min((y.size - off - 1) // SPS, sym.size)
    k = np.arange(skip, n)
    ys = y[k * SPS + off]
    ref = sym[k]
    alpha = np.vdot(ref, ys) / np.vdot(ref, ref)
    ys = ys / alpha
    hard = np.stack([(ys.real < 0), (ys.imag < 0)], axis=1).astype(np.uint8)
    n_err = int(np.sum(hard != bits[k]))
    return n_err, 2 * k.size, float(np.sum(np.abs(ys - ref) ** 2)), float(np.sum(np.abs(ref) ** 2))


def mer_db(e, s):
    return 10.0 * np.log10(s / e) if e > 0 else float("inf")


def ber_sweep(rng, h, coeff, esn0_list, min_err, max_sym, block):
    n_taps = h.size
    skip = 2 * (n_taps // SPS + 1)       # drop the start-up transient
    rows = []
    for es in esn0_list:
        acc = dict(err_fx=0, err_fl=0, bits=0, e_fx=0.0, e_fl=0.0, s=0.0, n_sym=0)
        scale = None
        while acc["n_sym"] < max_sym and (acc["err_fx"] < min_err or acc["n_sym"] == 0):
            bits, sym, rx = make_block(rng, block, h, es)
            if scale is None:
                scale = fit_scale(rx, h)
            fx, fl = run_filters(rx, scale, h, coeff)
            ne, nb, e1, s1 = symbol_stats(fx, sym, bits, n_taps, skip)
            nf, _, e2, _ = symbol_stats(fl, sym, bits, n_taps, skip)
            acc["err_fx"] += ne
            acc["err_fl"] += nf
            acc["bits"] += nb
            acc["e_fx"] += e1
            acc["e_fl"] += e2
            acc["s"] += s1
            acc["n_sym"] += block
        ber_fx = acc["err_fx"] / acc["bits"]
        th = float(qpsk_ber_theory(es))
        rows.append(dict(
            esn0_db=float(es), bits=acc["bits"],
            err_fixed=acc["err_fx"], err_float=acc["err_fl"],
            ber_fixed=ber_fx, ber_float=acc["err_fl"] / acc["bits"], ber_theory=th,
            mer_fixed_db=mer_db(acc["e_fx"], acc["s"]),
            mer_float_db=mer_db(acc["e_fl"], acc["s"]),
            scale=float(scale),
        ))
        r = rows[-1]
        print("  Es/N0 %5.1f dB  BER %.3e (%5d err / %8d bits, theory %.3e, x%.2f)"
              "  MER fixed %.2f / float %.2f dB"
              % (es, ber_fx, r["err_fixed"], r["bits"], th, ber_fx / th,
                 r["mer_fixed_db"], r["mer_float_db"]))
    return rows


def clean_floor(rng, h, coeff, n_sym):
    """noise-free channel: what's left is ISI from tap truncation plus
    fixed-point rounding"""
    bits, sym, rx = make_block(rng, n_sym, h, None)
    scale = fit_scale(rx, h)
    fx, fl = run_filters(rx, scale, h, coeff)
    skip = 2 * (h.size // SPS + 1)
    _, _, e1, s = symbol_stats(fx, sym, bits, h.size, skip)
    _, _, e2, _ = symbol_stats(fl, sym, bits, h.size, skip)
    # float filter with unquantised taps against the matched pair: pure ISI
    return dict(mer_fixed_db=mer_db(e1, s), mer_float_db=mer_db(e2, s))


def mer_sweep(rng, h, coeff, esn0_list, n_sym):
    """MER only, out to high Es/N0 where the filter's own floor shows"""
    skip = 2 * (h.size // SPS + 1)
    out = []
    for es in esn0_list:
        bits, sym, rx = make_block(rng, n_sym, h, es)
        fx, fl = run_filters(rx, fit_scale(rx, h), h, coeff)
        _, _, e1, s = symbol_stats(fx, sym, bits, h.size, skip)
        _, _, e2, _ = symbol_stats(fl, sym, bits, h.size, skip)
        out.append((mer_db(e1, s), mer_db(e2, s)))
    return np.array(out)


def dynamic_range(rng, h, coeff, levels, esn0_db, n_sym):
    """level = 99.99th-percentile input magnitude as a fraction of Q1.15 full
    scale. too low and rounding noise shows, too high and the input clips
    and the output saturates. returns rows of (MER fixed, MER float, BER
    fixed, % output samples saturated).

    MER on its own is fooled by saturation: a railed output slices to a
    perfect +-1 point. so the BER and the saturation count go with it."""
    bits, sym, rx = make_block(rng, n_sym, h, esn0_db)
    peak = np.percentile(np.maximum(np.abs(rx.real), np.abs(rx.imag)), 99.99)
    skip = 2 * (h.size // SPS + 1)
    out = []
    for lv in levels:
        scale = lv / peak
        fx, fl = run_filters(rx, scale, h, coeff)
        n1, nb, e1, s = symbol_stats(fx, sym, bits, h.size, skip)
        _, _, e2, _ = symbol_stats(fl, sym, bits, h.size, skip)
        codes = np.concatenate([fx.real, fx.imag]) * scale * Q_SCALE
        sat = 100.0 * np.mean(np.abs(np.rint(codes)) >= 32767)
        out.append((mer_db(e1, s), mer_db(e2, s), n1 / nb, sat))
    return np.array(out)


def freq_response(h, coeff, n_fft=8192):
    def db(x):
        H = np.abs(np.fft.rfft(x, n_fft))
        return 20.0 * np.log10(np.maximum(H / H.max(), 1e-12))
    f = np.fft.rfftfreq(n_fft) * SPS        # in units of the symbol rate
    return f, db(h), db(coeff.astype(np.float64) / Q_SCALE)


def stopband_atten(f, hdb):
    """worst sidelobe past the RRC band edge (1+beta)/2 x symbol rate"""
    return float(-hdb[f >= (1.0 + BETA) / 2.0 + 0.05].max())


def main(argv=None):
    ap = argparse.ArgumentParser(description="BER/MER/dynamic-range analysis of the RRC filter.")
    ap.add_argument("--docsdir", default="docs")
    ap.add_argument("--seed", type=int, default=0x5EED)
    ap.add_argument("--min-errors", type=int, default=500,
                    help="keep adding symbols at each Es/N0 until this many bit errors")
    ap.add_argument("--max-symbols", type=int, default=4_000_000)
    ap.add_argument("--quick", action="store_true", help="fewer errors, fewer points")
    args = ap.parse_args(argv)

    if args.quick:
        args.min_errors, args.max_symbols = 50, 400_000
    esn0_list = np.arange(0.0, 11.5, 1.0) if not args.quick else np.arange(0.0, 11.0, 2.0)
    block = 1 << 16

    use_style()
    os.makedirs(args.docsdir, exist_ok=True)
    rng = np.random.default_rng(args.seed)
    results = {"sps": SPS, "beta": BETA, "configs": []}
    levels = np.geomspace(1e-4, 2.0, 44)

    for name, span, _ in CONFIGS:
        h = rrc_taps(SPS, span, BETA)
        coeff, _ = quantize_q15(h)
        print("%s: %d taps, span %d" % (name, h.size, span))
        rows = ber_sweep(rng, h, coeff, esn0_list, args.min_errors, args.max_symbols, block)
        floor = clean_floor(rng, h, coeff, 1 << 16)
        print("  clean-channel MER  fixed %.2f dB   float %.2f dB"
              % (floor["mer_fixed_db"], floor["mer_float_db"]))
        mer_es = np.arange(0.0, 51.0, 2.5)
        ms = mer_sweep(rng, h, coeff, mer_es, 1 << 16)
        dr8 = dynamic_range(rng, h, coeff, levels, 8.0, 1 << 17)
        dr40 = dynamic_range(rng, h, coeff, levels, 40.0, 1 << 15)
        f, hf, hq = freq_response(h, coeff)
        sb_f, sb_q = stopband_atten(f, hf), stopband_atten(f, hq)
        print("  stopband attenuation  float %.1f dB   Q1.15 %.1f dB" % (sb_f, sb_q))
        results["configs"].append(dict(
            name=name, n_taps=int(h.size), span=span, sweep=rows, clean=floor,
            stopband_db_float=sb_f, stopband_db_q15=sb_q,
            mer_esn0_db=mer_es.tolist(), mer_fixed_db=ms[:, 0].tolist(),
            mer_float_db=ms[:, 1].tolist(),
            dyn_levels=levels.tolist(),
            dyn_ber_fixed_8db=dr8[:, 2].tolist(), dyn_sat_pct_8db=dr8[:, 3].tolist(),
            dyn_mer_fixed_40db=dr40[:, 0].tolist(), dyn_mer_float_40db=dr40[:, 1].tolist(),
            dyn_sat_pct_40db=dr40[:, 3].tolist(),
            _f=f, _hf=hf, _hq=hq,
        ))

    figs = []

    def save(fig, name):
        p = os.path.join(args.docsdir, name)
        fig.savefig(p)
        plt.close(fig)
        figs.append(p)

    # 1. BER waterfall
    fig, ax = plt.subplots(figsize=(8.6, 5.2))
    fig.subplots_adjust(top=0.84)
    es_fine = np.linspace(0, 11.5, 200)
    ax.semilogy(es_fine, qpsk_ber_theory(es_fine), color=INK_2, lw=1.4,
                label="theory, QPSK over AWGN")
    for cfg, (name, _, color) in zip(results["configs"], CONFIGS):
        es = [r["esn0_db"] for r in cfg["sweep"]]
        ber = np.array([r["ber_fixed"] for r in cfg["sweep"]])
        err = np.array([r["err_fixed"] for r in cfg["sweep"]])
        nb = np.array([r["bits"] for r in cfg["sweep"]])
        # 95% interval from the error count (Poisson, normal approx)
        lo = np.maximum(err - 1.96 * np.sqrt(err), 0.5) / nb
        hi = (err + 1.96 * np.sqrt(err)) / nb
        ax.errorbar(es, ber, yerr=[ber - lo, hi - ber], ls="none", marker="o", ms=5,
                    color=color, capsize=2.5, lw=1.2, label=name + ", fixed point")
    ax.set_xlabel("Es/N0 (dB)")
    ax.set_ylabel("bit error rate")
    ax.set_xlim(-0.5, 11.5)
    ax.legend(loc="lower left")
    panel_title(ax, "BER vs Es/N0 through the bit-exact filter model",
                "each point runs until %d bit errors; bars are the 95%% interval "
                "from the error count" % args.min_errors)
    save(fig, "perf_ber.png")

    # 2. MER vs Es/N0 out to where the filter's own floor takes over
    fig, ax = plt.subplots(figsize=(8.6, 4.8))
    fig.subplots_adjust(top=0.84)
    es_ax = np.array(results["configs"][0]["mer_esn0_db"])
    ax.plot(es_ax, es_ax, color=INK_2, lw=1.0, ls=":", label="ideal (MER = Es/N0)")
    for cfg, (name, _, color) in zip(results["configs"], CONFIGS):
        ax.plot(cfg["mer_esn0_db"], cfg["mer_fixed_db"], marker="o", ms=4, color=color,
                label="%s, Q1.15 (floor %.1f dB)" % (name, cfg["clean"]["mer_fixed_db"]))
        ax.plot(cfg["mer_esn0_db"], cfg["mer_float_db"], color=color, ls="--", lw=1.2,
                label=name + ", float64")
    ax.set_xlabel("Es/N0 (dB)")
    ax.set_ylabel("MER at the slicer (dB)")
    ax.legend(loc="upper left", fontsize=8.5)
    panel_title(ax, "MER vs Es/N0: where the filter itself starts to limit",
                "the floor is ISI from truncating the RRC to the tap count; "
                "Q1.15 and float64 lie on top of each other")
    save(fig, "perf_mer.png")

    # 3. dynamic range
    fig, axes = plt.subplots(1, 2, figsize=(10.4, 4.6))
    fig.subplots_adjust(top=0.76, wspace=0.22)
    ax = axes[0]
    for cfg, (name, _, color) in zip(results["configs"], CONFIGS):
        lv = np.array(cfg["dyn_levels"])
        ok = np.array(cfg["dyn_sat_pct_40db"]) < 0.01
        ax.semilogx(lv[ok], np.array(cfg["dyn_mer_fixed_40db"])[ok], color=color, label=name)
        ax.semilogx(lv, cfg["dyn_mer_float_40db"], color=color, ls="--", lw=1.2)
    ax.set_ylim(0, 42)
    ax.set_ylabel("MER (dB)")
    ax.legend(loc="lower right", fontsize=8.5)
    panel_title(ax, "Near-clean channel, Es/N0 = 40 dB",
                "solid = Q1.15 (cut where outputs saturate), dashed = float64")
    ax = axes[1]
    ax.axhline(float(qpsk_ber_theory(8.0)), color=INK_2, lw=1.0, ls=":")
    ax.text(1.2e-4, float(qpsk_ber_theory(8.0)) * 1.15, "theory", color=INK_2, fontsize=8)
    for cfg, (name, _, color) in zip(results["configs"], CONFIGS):
        ax.loglog(cfg["dyn_levels"], np.maximum(cfg["dyn_ber_fixed_8db"], 1e-5),
                  color=color, label=name)
    ax.set_ylim(4e-3, 4e-2)
    ax.set_ylabel("bit error rate")
    panel_title(ax, "Operating point, Es/N0 = 8 dB",
                "BER is not fooled by saturation the way MER is")
    for ax in axes:
        ax.axvline(1.0, color=AXIS, lw=0.8)
        ax.set_xlabel("input peak (99.99th pct) / full scale")
    fig.text(0.07, 0.985, "Dynamic range: performance vs input level", ha="left",
             va="top", fontsize=13, weight="600", color=INK)
    fig.text(0.07, 0.925, "left: the matched filter's gain railing the output sets the top, "
             "rounding noise the bottom; right: hard decisions barely care about either",
             ha="left", va="top", fontsize=9, color=MUTED)
    save(fig, "perf_dynamic_range.png")

    # 4. frequency response
    fig, ax = plt.subplots(figsize=(8.6, 4.8))
    fig.subplots_adjust(top=0.84)
    for cfg, (name, _, color) in zip(results["configs"], CONFIGS):
        ax.plot(cfg["_f"], cfg["_hq"], color=color, lw=1.6,
                label="%s, Q1.15 (stopband %.0f dB)" % (name, cfg["stopband_db_q15"]))
    ax.plot(results["configs"][0]["_f"], results["configs"][0]["_hf"], color=INK_2,
            lw=1.0, ls="--", label="33-tap float64")
    ax.axvline((1 + BETA) / 2, color=AXIS, lw=0.8)
    ax.text((1 + BETA) / 2 + 0.03, -8, "band edge (1+beta)/2", color=MUTED, fontsize=8)
    ax.set_xlim(0, 2)
    ax.set_ylim(-80, 5)
    ax.set_xlabel("frequency (x symbol rate)")
    ax.set_ylabel("magnitude (dB)")
    ax.legend(loc="lower left", fontsize=8.5)
    panel_title(ax, "Frequency response of the coefficient ROM",
                "sps = %d, beta = %.2f; stopband = worst sidelobe past band edge + 0.05"
                % (SPS, BETA))
    save(fig, "perf_freq_response.png")

    for cfg in results["configs"]:
        for k in ("_f", "_hf", "_hq"):
            cfg.pop(k)
    out = os.path.join(args.docsdir, "perf_results.json")
    with open(out, "w", newline="\n") as fh:
        json.dump(results, fh, indent=1)
        fh.write("\n")
    print("wrote %s" % ", ".join(figs + [out]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
