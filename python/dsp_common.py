"""
Shared DSP / fixed-point helpers.

Samples and coefficients are Q1.15: one sign bit, 15 fractional bits, in a
16-bit two's complement word. Range is [-1.0, 1.0 - 2^-15].

Multiplying two Q1.15 numbers gives Q2.30 (32 bits). Summing N of those needs
ceil(log2(N)) more integer bits, which is where the accumulator width in the
RTL comes from.
"""

import numpy as np

Q_FRAC_BITS = 15
Q_SCALE = 1 << Q_FRAC_BITS          # 32768
Q_MIN = -(1 << 15)
Q_MAX = (1 << 15) - 1


def rrc_taps(sps, span_sym, beta):
    """root-raised-cosine impulse response, normalized to unit energy

    sps = samples/symbol, span_sym = filter span in symbols, beta = rolloff.
    returns span_sym*sps + 1 taps (odd length -> symmetric -> linear phase)
    """
    if not 0.0 <= beta <= 1.0:
        raise ValueError("beta must be in [0, 1], got %r" % (beta,))

    n_taps = span_sym * sps + 1
    t = (np.arange(n_taps) - (n_taps - 1) / 2.0) / float(sps)

    num = np.sin(np.pi * t * (1.0 - beta)) + 4.0 * beta * t * np.cos(np.pi * t * (1.0 + beta))
    den = np.pi * t * (1.0 - (4.0 * beta * t) ** 2)
    with np.errstate(divide="ignore", invalid="ignore"):
        h = num / den

    # t = 0 singularity
    h[np.isclose(t, 0.0, atol=1e-12)] = 1.0 + beta * (4.0 / np.pi - 1.0)

    # t = +-1/(4*beta) singularities
    if beta > 0.0:
        sing = np.isclose(np.abs(t), 1.0 / (4.0 * beta), atol=1e-9)
        if sing.any():
            h[sing] = (beta / np.sqrt(2.0)) * (
                (1.0 + 2.0 / np.pi) * np.sin(np.pi / (4.0 * beta))
                + (1.0 - 2.0 / np.pi) * np.cos(np.pi / (4.0 * beta))
            )

    if not np.all(np.isfinite(h)):
        raise FloatingPointError("non-finite RRC tap produced; check sps/beta")

    return h / np.sqrt(np.sum(h ** 2))


def quantize_q15(x):
    """float array -> Q1.15 ints, clamped. returns (codes, num clipped).

    round-half-away-from-zero to match the RTL's round-half-up
    """
    xf = np.asarray(x, dtype=np.float64) * Q_SCALE
    xi = np.floor(xf + 0.5).astype(np.int64)
    n_clipped = int(np.count_nonzero((xi < Q_MIN) | (xi > Q_MAX)))
    return np.clip(xi, Q_MIN, Q_MAX).astype(np.int32), n_clipped


def q15_to_float(xi):
    return np.asarray(xi, dtype=np.float64) / Q_SCALE


def to_hex_words(values, nbits):
    """two's-complement hex strings, zero-padded"""
    if nbits % 4:
        raise ValueError("nbits must be a multiple of 4")
    mask = (1 << nbits) - 1
    width = nbits // 4
    return [format(int(v) & mask, "0%dx" % width) for v in values]


def write_hex(path, values, nbits, header=None):
    """one hex word per line, for $readmemh"""
    lines = []
    if header:
        lines.extend("// " + ln for ln in header.splitlines())
    lines.extend(to_hex_words(values, nbits))
    with open(path, "w", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def read_hex(path, nbits, signed=True):
    """read a $readmemh-style hex file, tolerates comments/@addr/x/z"""
    vals = []
    n_unknown = 0
    sign_bit = 1 << (nbits - 1)
    mod = 1 << nbits
    with open(path, "r") as fh:
        for raw in fh:
            line = raw.split("//")[0].strip()
            if not line:
                continue
            for tok in line.split():
                if tok.startswith("@"):
                    continue
                low = tok.lower()
                if "x" in low or "z" in low:
                    n_unknown += 1
                    vals.append(0)
                    continue
                v = int(low, 16)
                if signed and (v & sign_bit):
                    v -= mod
                vals.append(v)
    return np.array(vals, dtype=np.int64), n_unknown


def round_sat(acc, frac_bits=Q_FRAC_BITS, out_bits=16):
    """round half up then saturate, matches rrc_fir_core.v exactly

    python's >> on a negative int floors, same as verilog's >>> on signed
    """
    acc = np.asarray(acc, dtype=object) if isinstance(acc, np.ndarray) else acc
    lo, hi = -(1 << (out_bits - 1)), (1 << (out_bits - 1)) - 1
    if isinstance(acc, np.ndarray):
        y = (acc + (1 << (frac_bits - 1))) >> frac_bits
        return np.clip(y.astype(np.int64), lo, hi).astype(np.int32)
    y = (int(acc) + (1 << (frac_bits - 1))) >> frac_bits
    return max(lo, min(hi, y))
