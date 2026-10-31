#!/usr/bin/env python3
"""
live demo: noisy QPSK goes through the filter on the DueProLogic and the
plots fill in as the results come back.

  top    : constellation before and after the filter, at the symbol instants,
           and the eye diagram of the filtered I rail
  bottom : the I rail scrolling past (in vs out), and the scoreboard: every
           FPGA result checked bit for bit against golden_model.py, plus a
           live BER against the QPSK bound

the board only has one filter channel, so I and Q take turns. the core is a
plain FIR, its whole state is the last N_TAPS-1 inputs, so before each turn
the host replays that channel's previous 20 samples and throws away their
outputs. what comes out after that is bit for bit what a dedicated channel
would give, and it's checked against exactly that.

    pip install numpy matplotlib pyserial
    py python/live_demo.py --port COM14          # real board
    py python/live_demo.py                       # no board, golden model stands in
    py python/live_demo.py --record docs/live_demo.gif --frames 300 --sweep 14:4

the Es/N0 slider changes the channel on the fly. space pauses, c clears the
plots, s saves a screenshot. run from repo root.
"""

import argparse
import math
import os
import queue
import sys
import threading
import time
from collections import deque

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dsp_common import Q_FRAC_BITS, Q_SCALE, quantize_q15, read_hex, round_sat, rrc_taps  # noqa: E402

CMD_TRIGGER = 0x1
CMD_TRANSFER = 0x2

# dark palette, same series hues as viz_style but lifted for a dark surface
BG = "#0d1117"
SURFACE = "#11161d"
GRID = "#232a33"
AXIS = "#3a434e"
INK = "#e8eaed"
INK_2 = "#b4bac2"
MUTED = "#7d8590"
BLUE = "#4c9aff"
ORANGE = "#ff8a4c"
GREEN = "#3fb950"
RED = "#f85149"
VIOLET = "#b392f0"
QUAD = np.array([[0x4c, 0x9a, 0xff], [0xff, 0x8a, 0x4c], [0x3f, 0xb9, 0x50], [0xb3, 0x92, 0xf0]]) / 255.0


def qpsk_ber_theory(esn0_db):
    """QPSK BER over AWGN, Q(sqrt(Es/N0))"""
    return 0.5 * math.erfc(math.sqrt(10.0 ** (esn0_db / 10.0) / 2.0))


# ----------------------------------------------------------------------------
# the channel: endless QPSK -> RRC -> AWGN -> Q1.15, same recipe as
# gen_stimulus.py but generated a block at a time so Es/N0 can move
# ----------------------------------------------------------------------------
class Link:
    def __init__(self, sps, span, beta, esn0_db, seed, headroom=0.92, out_headroom=0.95):
        self.sps = sps
        self.h = rrc_taps(sps, span, beta)
        self.n_taps = self.h.size
        self.headroom = headroom
        self.out_headroom = out_headroom
        self.rng = np.random.default_rng(seed)
        self.tx_hist = np.zeros(self.n_taps - 1, dtype=np.complex128)
        self.n_sym = 0                          # symbols generated so far
        self.set_esn0(esn0_db)

    def set_esn0(self, esn0_db):
        self.esn0_db = float(esn0_db)
        self.sigma = math.sqrt(10.0 ** (-self.esn0_db / 10.0) / 2.0)
        # pick the Q1.15 scale the way gen_stimulus.py does, on a throwaway
        # burst with its own seed so the live stream isn't disturbed
        rng = np.random.default_rng(1234)
        sym = self._symbols(rng, 4096)[1]
        up = np.zeros(sym.size * self.sps, dtype=np.complex128)
        up[:: self.sps] = sym
        rx = np.convolve(up, self.h)[: up.size] + self._noise(rng, up.size)
        peak = np.percentile(np.maximum(np.abs(rx.real), np.abs(rx.imag)), 99.99)
        scale = self.headroom / peak
        mf = np.convolve(rx * scale, self.h)[: rx.size]
        out_peak = np.percentile(np.maximum(np.abs(mf.real), np.abs(mf.imag)), 99.99)
        if out_peak > self.out_headroom:
            scale *= self.out_headroom / out_peak
        self.scale = scale

    def _symbols(self, rng, n):
        bits = rng.integers(0, 2, size=(n, 2), dtype=np.uint8)
        return bits, ((1 - 2.0 * bits[:, 0]) + 1j * (1 - 2.0 * bits[:, 1])) / math.sqrt(2.0)

    def _noise(self, rng, n):
        return self.sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))

    def next_block(self, n_sym):
        bits, sym = self._symbols(self.rng, n_sym)
        up = np.zeros(n_sym * self.sps, dtype=np.complex128)
        up[:: self.sps] = sym
        # causal TX filter carried across blocks
        ext = np.concatenate([self.tx_hist, up])
        shaped = np.convolve(ext, self.h)[self.tx_hist.size: ext.size]
        self.tx_hist = ext[-(self.n_taps - 1):]
        rx = (shaped + self._noise(self.rng, up.size)) * self.scale
        i_q15, _ = quantize_q15(rx.real)
        q_q15, _ = quantize_q15(rx.imag)
        first = self.n_sym
        self.n_sym += n_sym
        return first, bits, sym, i_q15.astype(np.int64), q_q15.astype(np.int64)


# ----------------------------------------------------------------------------
# what does the filtering: the board, or the golden model pretending to be one
# ----------------------------------------------------------------------------
class BoardFilter:
    """one lockstep sample at a time, same framing as dpl_host.py"""

    def __init__(self, port, baud, addr, timeout):
        import serial  # pyserial, only needed here
        self.port_name = port
        self.addr = addr
        self.port = serial.Serial(port, baud, timeout=timeout)
        self.reply_hdr = self._frame(CMD_TRANSFER, 0, to_host=True)[0]
        self.name = "DueProLogic EP4CE6 on %s" % port
        self.live = True
        # trigger bit 0: clear filter history and byte phase
        self.port.reset_input_buffer()
        self.port.write(self._frame(CMD_TRIGGER, 0x01, addr=0))
        self.port.flush()
        time.sleep(0.05)
        self.port.reset_input_buffer()

    def _frame(self, cmd, payload, to_host=False, addr=None):
        hdr = (0xC0 if to_host else 0x80) | (cmd << 3) | (self.addr if addr is None else addr)
        return bytes([hdr, payload & 0xFF])

    def run(self, x):
        out = np.empty(len(x), dtype=np.int64)
        for n, xv in enumerate(x):
            v = int(xv) & 0xFFFF
            self.port.write(self._frame(CMD_TRANSFER, v) + self._frame(CMD_TRANSFER, v >> 8))
            raw = self.port.read(4)
            if len(raw) < 4:
                raise IOError("board timed out (no reply to a sample). is %s the UART port?" % self.port_name)
            if raw[0] != self.reply_hdr or raw[2] != self.reply_hdr:
                raise IOError("bad frame header from board: %s" % raw.hex())
            y = raw[1] | (raw[3] << 8)
            out[n] = y - 0x10000 if y & 0x8000 else y
        return out

    def close(self):
        self.port.close()


class SimFilter:
    """golden model with one delay line, fed exactly like the board"""

    def __init__(self, coeff, rate):
        self.coeff = coeff
        self.state = np.zeros(coeff.size - 1, dtype=np.int64)
        self.rate = rate                       # samples/s to pace at, 0 = flat out
        self.name = "simulation, golden model (no board)"
        self.live = False

    def run(self, x):
        ext = np.concatenate([self.state, x])
        y = round_sat(np.convolve(ext, self.coeff)[self.state.size: ext.size], Q_FRAC_BITS, 16)
        self.state = ext[-self.state.size:]
        if self.rate:
            time.sleep(len(x) / float(self.rate))
        return y.astype(np.int64)

    def close(self):
        pass


# ----------------------------------------------------------------------------
# one step: a block of symbols through the filter, both rails, checked
# ----------------------------------------------------------------------------
class Pipeline:
    def __init__(self, link, filt, coeff, block_sym):
        self.link = link
        self.filt = filt
        self.coeff = coeff
        self.block_sym = block_sym
        self.pre = coeff.size - 1
        self.hist = {"i": np.zeros(self.pre, dtype=np.int64), "q": np.zeros(self.pre, dtype=np.int64)}

    def step(self):
        first, bits, sym, xi, xq = self.link.next_block(self.block_sym)
        res = {"first_sym": first, "bits": bits, "sym": sym, "scale": self.link.scale,
               "esn0_db": self.link.esn0_db, "in": {}, "out": {}, "bad": 0, "sent": 0}
        t0 = time.time()
        for ch, x in (("i", xi), ("q", xq)):
            ext = np.concatenate([self.hist[ch], x])
            got = self.filt.run(ext)[self.pre:]
            want = round_sat(np.convolve(ext, self.coeff)[self.pre: ext.size], Q_FRAC_BITS, 16)
            res["bad"] += int(np.count_nonzero(got != want))
            res["sent"] += ext.size
            self.hist[ch] = ext[-self.pre:]
            res["in"][ch], res["out"][ch] = x, got
        res["dt"] = time.time() - t0
        return res


# ----------------------------------------------------------------------------
# the window
# ----------------------------------------------------------------------------
class Dashboard:
    KEEP_SYM = 700          # points per constellation
    KEEP_EYE = 160          # eye traces
    STRIP = 240             # samples in the scrolling trace
    WIN_SYM = 2000          # symbols behind the EVM figures

    def __init__(self, plt, link, filt, n_taps, interactive):
        self.plt = plt
        self.link = link
        self.filt = filt
        self.sps = link.sps
        self.tx_off = (n_taps - 1) // 2     # TX filter group delay
        self.rx_off = n_taps - 1            # TX + RX
        self.paused = False
        self.pending_esn0 = None
        self.reset_counters(full=True)
        self._build(interactive)

    # -- state ------------------------------------------------------------
    def reset_ber(self):
        self.ber_bits = 0
        self.ber_err = 0

    def reset_counters(self, full=False):
        self.sym_in = deque(maxlen=self.KEEP_SYM)
        self.sym_out = deque(maxlen=self.KEEP_SYM)
        self.sym_q = deque(maxlen=self.KEEP_SYM)
        self.eye = deque(maxlen=self.KEEP_EYE)
        self.ber_bits = 0
        self.ber_err = 0
        self.evm_in = deque(maxlen=self.WIN_SYM)
        self.evm_out = deque(maxlen=self.WIN_SYM)
        if full:
            self.checked = 0
            self.mismatches = 0
            self.sent = 0
            self.t_busy = 0.0
            self.t_start = time.time()
            self.tx_sym = {}                 # symbol index -> (bits, complex)
            self.yi = np.zeros(0)            # rolling output I, normalized
            self.xi = np.zeros(0)
            self.buf_start = 0               # absolute sample index of yi[0]
            self.strip_end = None            # absolute index at the strip's right edge
            self.smooth = True

    def ingest(self, r):
        sps = self.sps
        scale = r["scale"]
        for k, (b, s) in enumerate(zip(r["bits"], r["sym"])):
            self.tx_sym[r["first_sym"] + k] = (b, s)
        n0 = r["first_sym"] * sps            # absolute index of this block's first sample
        xi, xq = r["in"]["i"] / (Q_SCALE * scale), r["in"]["q"] / (Q_SCALE * scale)
        yi, yq = r["out"]["i"] / (Q_SCALE * scale), r["out"]["q"] / (Q_SCALE * scale)
        n = np.arange(n0, n0 + xi.size)

        # symbol instants: input peaks at k*sps + tx_off, output at k*sps + rx_off
        for arr_i, arr_q, off, dst, ev in ((xi, xq, self.tx_off, self.sym_in, self.evm_in),
                                           (yi, yq, self.rx_off, self.sym_out, self.evm_out)):
            sel = np.flatnonzero((n - off) % sps == 0)
            for j in sel:
                k = (n[j] - off) // sps
                if k < 0 or k not in self.tx_sym:
                    continue
                b, s = self.tx_sym[k]
                z = arr_i[j] + 1j * arr_q[j]
                dst.append(z)
                ev.append((z, s))
                if dst is self.sym_out:
                    self.sym_q.append(int(b[0]) * 2 + int(b[1]))
                    self.ber_bits += 2
                    self.ber_err += int((z.real < 0) != bool(b[0])) + int((z.imag < 0) != bool(b[1]))
                    self.tx_sym.pop(k, None)       # input side used it a block ago at the latest

        # rolling buffers for the eye and the strip
        self.yi = np.concatenate([self.yi, yi])
        self.xi = np.concatenate([self.xi, xi])
        keep = 4 * self.STRIP
        if self.yi.size > keep:
            cut = self.yi.size - keep
            self.yi, self.xi = self.yi[cut:], self.xi[cut:]
            self.buf_start += cut
        idx = np.arange(self.buf_start, self.buf_start + self.yi.size)
        new = np.flatnonzero(((idx - self.rx_off) % sps == 0) & (idx >= n0 - sps) & (idx < n0 + xi.size - sps))
        for j in new:
            if j - sps >= 0 and j + sps < self.yi.size:
                self.eye.append(self.yi[j - sps: j + sps + 1].copy())

        self.checked += r["out"]["i"].size * 2
        self.mismatches += r["bad"]
        self.sent += r["sent"]
        self.t_busy += r["dt"]

    # -- drawing ----------------------------------------------------------
    def _style_axes(self, ax, title, sub=None):
        ax.set_facecolor(SURFACE)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(AXIS)
        ax.tick_params(colors=MUTED, labelsize=8)
        ax.grid(True, color=GRID, linewidth=0.7)
        ax.set_axisbelow(True)
        ax.text(0, 1.10, title, transform=ax.transAxes, color=INK, fontsize=11.5,
                weight="bold", ha="left", va="bottom")
        return ax.text(0, 1.025, sub or "", transform=ax.transAxes, color=MUTED,
                       fontsize=8.6, ha="left", va="bottom")

    def _build(self, interactive):
        plt = self.plt
        from matplotlib.collections import LineCollection
        from matplotlib.widgets import Button, Slider

        fig = plt.figure(figsize=(14.5, 8.4), facecolor=BG)
        self.fig = fig
        gs = fig.add_gridspec(2, 3, left=0.05, right=0.985, top=0.83, bottom=0.17,
                              wspace=0.24, hspace=0.62, height_ratios=[1.35, 1.0])

        live = self.filt.live
        fig.text(0.05, 0.955, "QPSK matched filter, live on an FPGA", color=INK,
                 fontsize=17, weight="bold", ha="left", va="center")
        fig.text(0.05, 0.915, "21-tap root-raised-cosine (β = 0.35, 4 samples/symbol), "
                 "Q1.15 fixed point, radix-4 Booth multipliers on a Cyclone IV EP4CE6",
                 color=MUTED, fontsize=9.5, ha="left", va="center")
        badge_col = GREEN if live else ORANGE
        fig.text(0.985, 0.955, ("●  LIVE  " if live else "●  SIMULATION  "), color=badge_col,
                 fontsize=11, weight="bold", ha="right", va="center")
        fig.text(0.985, 0.915, self.filt.name, color=INK_2, fontsize=9.5, ha="right", va="center")

        lim = 1.6
        self.ax_in = fig.add_subplot(gs[0, 0])
        self.sub_in = self._style_axes(self.ax_in, "Before: what arrives on the wire")
        self.ax_out = fig.add_subplot(gs[0, 1])
        self.sub_out = self._style_axes(self.ax_out, "After: FPGA matched-filter output")
        for ax in (self.ax_in, self.ax_out):
            ax.set_xlim(-lim, lim)
            ax.set_ylim(-lim, lim)
            ax.set_aspect("equal", adjustable="box")
            ax.axhline(0, color=AXIS, lw=0.8)
            ax.axvline(0, color=AXIS, lw=0.8)
            ideal = np.array([1, 1j, -1, -1j]) * np.exp(1j * np.pi / 4)
            ax.scatter(ideal.real, ideal.imag, marker="+", s=110, color=INK, lw=1.4, zorder=5)
            ax.set_xlabel("I", color=INK_2, fontsize=9)
            ax.set_ylabel("Q", color=INK_2, fontsize=9)
        self.sc_in = self.ax_in.scatter([], [], s=7, lw=0, alpha=0.75)
        self.sc_out = self.ax_out.scatter([], [], s=7, lw=0, alpha=0.75)

        self.ax_eye = fig.add_subplot(gs[0, 2])
        self._style_axes(self.ax_eye, "Eye diagram, filtered I rail",
                         "sample at t = 0: the eye is open where the decision is made")
        self.ax_eye.set_xlim(-1, 1)
        self.ax_eye.set_ylim(-lim, lim)
        self.ax_eye.axvline(0, color=AXIS, lw=0.8, ls="--")
        self.ax_eye.set_xlabel("time (symbols)", color=INK_2, fontsize=9)
        self.eye_lc = LineCollection([], colors=[BLUE], linewidths=0.8, alpha=0.22)
        self.ax_eye.add_collection(self.eye_lc)

        self.ax_strip = fig.add_subplot(gs[1, 0:2])
        self._style_axes(self.ax_strip, "I rail as it streams through",
                         "grey: noisy samples in.  blue: filter output.  dots: symbol decisions")
        self.ax_strip.set_xlim(0, self.STRIP)
        self.ax_strip.set_ylim(-2.4, 2.4)
        self.ax_strip.set_xlabel("sample", color=INK_2, fontsize=9)
        (self.ln_x,) = self.ax_strip.plot([], [], color=MUTED, lw=0.9, alpha=0.8)
        (self.ln_y,) = self.ax_strip.plot([], [], color=BLUE, lw=1.6)
        self.sc_dec = self.ax_strip.scatter([], [], s=16, color=INK, zorder=5)

        # scoreboard
        ax_s = fig.add_subplot(gs[1, 2])
        ax_s.set_axis_off()
        ax_s.text(0, 1.10, "Scoreboard", transform=ax_s.transAxes, color=INK, fontsize=11.5,
                  weight="bold", ha="left", va="bottom")
        self.score = {}
        rows = [("exact", "FPGA vs golden model"), ("thru", "Throughput"),
                ("ber", "Bit error rate"), ("theory", "QPSK theory at this Es/N0"),
                ("gain", "Matched-filter gain")]
        for i, (key, label) in enumerate(rows):
            y = 0.92 - i * 0.215
            ax_s.text(0, y, label, transform=ax_s.transAxes, color=MUTED, fontsize=9, va="center")
            self.score[key] = ax_s.text(1.0, y, "", transform=ax_s.transAxes, color=INK,
                                        fontsize=11, weight="bold", ha="right", va="center",
                                        family="monospace")
        self.ax_s = ax_s

        # controls
        ax_sl = fig.add_axes([0.13, 0.055, 0.42, 0.03], facecolor=SURFACE)
        self.slider = Slider(ax_sl, "Es/N0  ", 0.0, 20.0, valinit=self.link.esn0_db,
                             valstep=0.5, color=BLUE, track_color=GRID)
        self.slider.label.set_color(INK_2)
        self.slider.valtext.set_color(INK)
        self.slider.valtext.set_text("%.1f dB" % self.link.esn0_db)
        self.slider.on_changed(self._on_slider)
        if getattr(self.slider, "vline", None) is not None:
            self.slider.vline.set_visible(False)
        fig.text(0.13, 0.025, "channel quality: lower means more noise", color=MUTED, fontsize=8.5)
        self.buttons = []
        if interactive:
            for x, label, cb in ((0.66, "Pause", self._on_pause), (0.75, "Clear", self._on_clear),
                                 (0.84, "Screenshot", self._on_shot)):
                ax_b = fig.add_axes([x, 0.045, 0.08, 0.05])
                b = Button(ax_b, label, color=SURFACE, hovercolor=GRID)
                b.label.set_color(INK)
                b.on_clicked(cb)
                self.buttons.append(b)
            fig.canvas.mpl_connect("key_press_event", self._on_key)

    def redraw(self):
        if self.sym_out:
            cols = QUAD[np.array(self.sym_q)]
            for sc, pts in ((self.sc_in, self.sym_in), (self.sc_out, self.sym_out)):
                z = np.array(pts)
                g = self._gain(sc)
                z = z / g
                sc.set_offsets(np.c_[z.real, z.imag])
                sc.set_facecolors(cols[-len(z):])
        evm_in, evm_out = self._evm(self.evm_in), self._evm(self.evm_out)
        if evm_in is not None:
            self.sub_in.set_text("noisy samples at the symbol instants · EVM %.0f%%" % evm_in)
            self.sub_out.set_text("same symbols after the filter · EVM %.0f%%" % evm_out)
            gain = 20 * math.log10(evm_in / evm_out)
            self.score["gain"].set_text("%+.1f dB" % gain)

        if self.eye:
            t = np.linspace(-1, 1, 2 * self.sps + 1)
            g = self._gain(self.sc_out)
            self.eye_lc.set_segments([np.c_[t, tr / g] for tr in self.eye])

        latest = self.buf_start + self.yi.size
        n = min(self.STRIP, self.yi.size)
        if n:
            # results land a block at a time; ease the strip toward the newest
            # sample so it scrolls instead of jumping
            if not self.smooth or self.strip_end is None or latest - self.strip_end > 2 * self.STRIP:
                self.strip_end = latest
            else:
                self.strip_end += max(1, (latest - self.strip_end) // 3) if latest > self.strip_end else 0
            hi = self.strip_end - self.buf_start
            lo = max(0, hi - n)
            g = self._gain(self.sc_out)
            gi = self._gain(self.sc_in)
            ys, xs = self.yi[lo:hi] / g, self.xi[lo:hi] / gi
            idx = np.arange(self.buf_start + lo, self.buf_start + hi)
            n = hi - lo
            self.ln_x.set_data(np.arange(n), xs)
            self.ln_y.set_data(np.arange(n), ys)
            d = np.flatnonzero((idx - self.rx_off) % self.sps == 0)
            self.sc_dec.set_offsets(np.c_[d, ys[d]] if d.size else np.zeros((0, 2)))

        ok = self.mismatches == 0
        self.score["exact"].set_text("%s mismatches / %s" % (format(self.mismatches, ","), format(self.checked, ",")))
        self.score["exact"].set_color(GREEN if ok else RED)
        if self.filt.live:
            rate = self.sent / self.t_busy if self.t_busy else 0.0
            self.score["thru"].set_text("%.0f samples/s" % rate)
        elif self.filt.rate:
            self.score["thru"].set_text("%.0f samples/s, paced" % self.filt.rate)
        else:
            self.score["thru"].set_text("offline render")
        if self.ber_bits:
            self.score["ber"].set_text("%.2e  (%d / %s bits)" % (self.ber_err / self.ber_bits, self.ber_err,
                                                                 format(self.ber_bits, ",")))
        self.score["theory"].set_text("%.2e" % qpsk_ber_theory(self.link.esn0_db))
        if self.paused:
            self.score["thru"].set_text("paused")

    def _gain(self, sc):
        """least-squares gain of each panel onto the ideal points, so both
        constellations sit on the same + marks (same idea as analyze_results)"""
        ev = self.evm_in if sc is self.sc_in else self.evm_out
        if not ev:
            return 1.0
        z = np.array([e[0] for e in ev])
        s = np.array([e[1] for e in ev])
        return abs(np.vdot(s, z) / np.vdot(s, s))

    def _evm(self, ev):
        if len(ev) < 20:
            return None
        z = np.array([e[0] for e in ev])
        s = np.array([e[1] for e in ev])
        a = np.vdot(s, z) / np.vdot(s, s)
        return 100.0 * math.sqrt(np.mean(np.abs(z / a - s) ** 2) / np.mean(np.abs(s) ** 2))

    # -- controls ---------------------------------------------------------
    def _on_slider(self, val):
        self.slider.valtext.set_text("%.1f dB" % val)
        self.pending_esn0 = float(val)

    def apply_pending(self):
        """called from whoever owns the link, between blocks"""
        if self.pending_esn0 is not None and self.pending_esn0 != self.link.esn0_db:
            self.link.set_esn0(self.pending_esn0)
            self.pending_esn0 = None
            return True
        self.pending_esn0 = None
        return False

    def _on_pause(self, _evt=None):
        self.paused = not self.paused
        if self.buttons:
            self.buttons[0].label.set_text("Resume" if self.paused else "Pause")

    def _on_clear(self, _evt=None):
        self.reset_counters()

    def _on_shot(self, _evt=None):
        path = time.strftime("live_demo_%Y%m%d_%H%M%S.png")
        self.fig.savefig(path, facecolor=BG, dpi=150)
        print("saved", path)

    def _on_key(self, evt):
        if evt.key == " ":
            self._on_pause()
        elif evt.key == "c":
            self._on_clear()
        elif evt.key == "s":
            self._on_shot()


# ----------------------------------------------------------------------------
def load_coeff(path, sps, span, beta):
    """use the taps the bitstream was built with if they're on disk,
    otherwise regenerate them (same function gen_stimulus.py uses)"""
    fresh, _ = quantize_q15(rrc_taps(sps, span, beta))
    fresh = fresh.astype(np.int64)
    if path and os.path.exists(path):
        coeff, _ = read_hex(path, 16, signed=True)
        if coeff.size != fresh.size or not np.array_equal(coeff, fresh):
            print("note: %s differs from sps=%d span=%d beta=%g, using the file" % (path, sps, span, beta))
        return coeff
    return fresh


def build_parser():
    ap = argparse.ArgumentParser(description="Live GUI for the RRC matched filter on the DueProLogic.",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--port", help="FT2232H UART port, e.g. COM14. leave out to run the simulation")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--addr", type=int, default=1, help="SAMPLE_ADDR in dpl_top.v")
    ap.add_argument("--timeout", type=float, default=2.0)
    ap.add_argument("--coeffs", default="hw_demo/data/rrc_coeffs.hex", help="taps the board was built with")
    ap.add_argument("--span", type=int, default=5, help="RRC span in symbols (21 taps on the board)")
    ap.add_argument("--esn0-db", type=float, default=8.0, help="starting Es/N0")
    ap.add_argument("--block", type=int, default=None,
                    help="symbols per block (default 24 on the board, 64 in simulation, 16 when recording)")
    ap.add_argument("--sim-rate", type=float, default=4000.0,
                    help="samples/s the simulation is paced at, 0 = unpaced. ~970 mimics the board")
    ap.add_argument("--seed", type=int, default=0xB0071)
    ap.add_argument("--record", help="write an animation (.gif or .mp4) instead of opening a window")
    ap.add_argument("--frames", type=int, default=240, help="frames to record")
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--sweep", help="Es/N0 sweep while recording, START:END in dB, e.g. 14:4")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    sps, beta = 4, 0.35

    import matplotlib
    if args.record:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    coeff = load_coeff(args.coeffs, sps, args.span, beta)
    link = Link(sps, args.span, beta, args.esn0_db, args.seed)
    if link.n_taps != coeff.size:
        raise SystemExit("coefficient file has %d taps, --span %d gives %d" % (coeff.size, args.span, link.n_taps))

    if args.port:
        filt = BoardFilter(args.port, args.baud, args.addr, args.timeout)
        block = args.block or (16 if args.record else 24)
    else:
        filt = SimFilter(coeff, 0 if args.record else args.sim_rate)
        block = args.block or (16 if args.record else 64)
    pipe = Pipeline(link, filt, coeff, block)
    dash = Dashboard(plt, link, filt, coeff.size, interactive=not args.record)
    dash.smooth = not args.record

    try:
        if args.record:
            return record(args, plt, dash, pipe)
        return run_live(plt, dash, pipe)
    finally:
        filt.close()


def run_live(plt, dash, pipe):
    from matplotlib.animation import FuncAnimation

    q = queue.Queue(maxsize=8)
    stop = threading.Event()

    def worker():
        try:
            while not stop.is_set():
                if dash.paused:
                    time.sleep(0.05)
                    continue
                if dash.apply_pending():
                    q.put("esn0")
                q.put(pipe.step())
        except Exception as exc:            # surface board errors in the window
            q.put(exc)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    status = {"err": None}

    def tick(_frame):
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, Exception):
                status["err"] = item
                dash.fig.text(0.5, 0.5, "link error: %s" % item, color=RED, fontsize=13,
                              ha="center", va="center", weight="bold",
                              bbox=dict(facecolor=BG, edgecolor=RED, boxstyle="round,pad=0.6"))
                print("link error:", item)
            elif item == "esn0":
                dash.reset_ber()
            else:
                dash.ingest(item)
        dash.redraw()
        return ()

    anim = FuncAnimation(dash.fig, tick, interval=80, cache_frame_data=False)  # noqa: F841
    plt.show()
    stop.set()
    return 1 if status["err"] else 0


def record(args, plt, dash, pipe):
    from matplotlib.animation import FFMpegWriter, PillowWriter

    if args.sweep:
        a, b = (float(v) for v in args.sweep.split(":"))
    writer = (FFMpegWriter(fps=args.fps, bitrate=2000) if args.record.lower().endswith(".mp4")
              else PillowWriter(fps=args.fps))
    out_dir = os.path.dirname(args.record)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    # a short warm-up so the first frame isn't empty
    for _ in range(3):
        dash.ingest(pipe.step())
    dpi = 80 if args.record.lower().endswith(".gif") else 110
    with writer.saving(dash.fig, args.record, dpi=dpi):
        for f in range(args.frames):
            if args.sweep:
                # hold each end for a moment, sweep in the middle
                u = min(1.0, max(0.0, (f / max(1, args.frames - 1) - 0.15) / 0.7))
                target = float(round(a + (b - a) * u))
                if target != dash.link.esn0_db:
                    dash.slider.set_val(target)
                    if dash.apply_pending():
                        dash.reset_ber()
            dash.ingest(pipe.step())
            dash.redraw()
            writer.grab_frame(facecolor=BG)
    print("wrote %s (%d frames, %d mismatches in %s checked)"
          % (args.record, args.frames, dash.mismatches, format(dash.checked, ",")))
    return 0 if dash.mismatches == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
