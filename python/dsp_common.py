"""
Shared DSP and fixed-point helpers for the lunar-orbit QPSK RRC receiver.

Fixed-point convention used throughout the project
--------------------------------------------------
Samples and coefficients are Q1.15: one sign bit, 15 fractional bits, stored
in a 16-bit two's-complement word.  The representable range is

    [-1.0,  1.0 - 2^-15]  ==  [-32768, 32767] / 2^15

A Q1.15 * Q1.15 product is Q2.30 in 32 bits.  Summing N of those needs
ceil(log2(N)) extra integer bits, giving the accumulator width used by the RTL.
"""

import numpy as np

# --- Q1.15 format constants -------------------------------------------------
Q_FRAC_BITS = 15
Q_SCALE = 1 << Q_FRAC_BITS          # 32768
Q_MIN = -(1 << 15)                  # -32768  == -1.0
Q_MAX = (1 << 15) - 1               #  32767  ==  1.0 - 2^-15


# ---------------------------------------------------------------------------
# Root-raised-cosine pulse shape
# ---------------------------------------------------------------------------
def rrc_taps(sps, span_sym, beta):
    """Root-raised-cosine impulse response, normalised to unit energy.

    Args:
        sps:      samples per symbol (oversampling factor)
        span_sym: filter span in symbol periods
        beta:     roll-off factor in [0, 1]

    Returns:
        float64 array of ``span_sym * sps + 1`` taps.  The length is odd so the
        response is exactly symmetric about its centre, i.e. linear phase.

    Unit-energy normalisation is what makes the TX/RX pair a matched filter:
    conv(h, h) then has a peak of sum(h^2) == 1, so a unit-amplitude symbol
    arrives at the slicer with unit amplitude and no gain bookkeeping.
    """
    if not 0.0 <= beta <= 1.0:
        raise ValueError("beta must be in [0, 1], got %r" % (beta,))

    n_taps = span_sym * sps + 1
    # Time axis in symbol periods, centred on zero.
    t = (np.arange(n_taps) - (n_taps - 1) / 2.0) / float(sps)

    # General closed form (the 1/sqrt(T) scale is dropped -- we normalise below).
    num = np.sin(np.pi * t * (1.0 - beta)) + 4.0 * beta * t * np.cos(np.pi * t * (1.0 + beta))
    den = np.pi * t * (1.0 - (4.0 * beta * t) ** 2)
    with np.errstate(divide="ignore", invalid="ignore"):
        h = num / den

    # Removable singularity at t = 0.
    h[np.isclose(t, 0.0, atol=1e-12)] = 1.0 + beta * (4.0 / np.pi - 1.0)

    # Removable singularities at t = +/- 1/(4*beta).  Only actually land on a
    # sample instant for certain beta (e.g. beta=0.25 with any sps), but the
    # guard keeps the function correct for every roll-off.
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


# ---------------------------------------------------------------------------
# Fixed-point conversion
# ---------------------------------------------------------------------------
def quantize_q15(x):
    """Convert float array to Q1.15 integers, clamping to [-1.0, 1.0).

    Returns (int32 array of Q1.15 codes, number of samples that clipped).
    Rounding is round-half-away-from-zero (numpy's ``rint`` is banker's
    rounding, so ``floor(x + 0.5)`` is used for positive-half-up behaviour that
    matches the RTL's round-half-up on the output stage).
    """
    xf = np.asarray(x, dtype=np.float64) * Q_SCALE
    xi = np.floor(xf + 0.5).astype(np.int64)
    n_clipped = int(np.count_nonzero((xi < Q_MIN) | (xi > Q_MAX)))
    return np.clip(xi, Q_MIN, Q_MAX).astype(np.int32), n_clipped


def q15_to_float(xi):
    """Q1.15 integer codes back to float."""
    return np.asarray(xi, dtype=np.float64) / Q_SCALE


# ---------------------------------------------------------------------------
# Hex file I/O for $readmemh / $writememh
# ---------------------------------------------------------------------------
def to_hex_words(values, nbits):
    """Two's-complement hex strings, zero-padded to nbits/4 digits."""
    if nbits % 4:
        raise ValueError("nbits must be a multiple of 4")
    mask = (1 << nbits) - 1
    width = nbits // 4
    return [format(int(v) & mask, "0%dx" % width) for v in values]


def write_hex(path, values, nbits, header=None):
    """Write one hex word per line, in a form $readmemh accepts.

    $readmemh ignores // comments, so an optional provenance header is safe.
    """
    lines = []
    if header:
        lines.extend("// " + ln for ln in header.splitlines())
    lines.extend(to_hex_words(values, nbits))
    with open(path, "w", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def read_hex(path, nbits, signed=True):
    """Read a $writememh/$readmemh style hex file into an int64 array.

    Tolerates comments, blank lines, ``@address`` records and x/z words (which
    become None-free sentinels -- x words are reported separately so a caller
    can assert on them).
    """
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
                if tok.startswith("@"):          # address record
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


# ---------------------------------------------------------------------------
# Bit-exact model of the RTL output stage
# ---------------------------------------------------------------------------
def round_sat(acc, frac_bits=Q_FRAC_BITS, out_bits=16):
    """Round-half-up then saturate -- exactly what rrc_fir_core.v does.

    Python's ``>>`` on a negative int floors, which is what Verilog's ``>>>``
    on a signed value does, so the two agree bit for bit.
    """
    acc = np.asarray(acc, dtype=object) if isinstance(acc, np.ndarray) else acc
    lo, hi = -(1 << (out_bits - 1)), (1 << (out_bits - 1)) - 1
    if isinstance(acc, np.ndarray):
        y = (acc + (1 << (frac_bits - 1))) >> frac_bits
        return np.clip(y.astype(np.int64), lo, hi).astype(np.int32)
    y = (int(acc) + (1 << (frac_bits - 1))) >> frac_bits
    return max(lo, min(hi, y))
