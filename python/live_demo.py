#!/usr/bin/env python3
"""
live demo: stream noisy QPSK through the filter on the DueProLogic and plot
the results as they come back. constellation before and after the filter,
eye diagram of the I output, and a status line with the bit-exact check
against golden_model.py and the BER.

the board has one filter channel, so I and Q take turns. the core is a plain
FIR whose whole state is the last N_TAPS-1 inputs, so before each turn the
host replays that channel's previous 20 samples and drops their outputs.

    pip install numpy matplotlib pyserial
    py python/live_demo.py --port COM14          # real board
    py python/live_demo.py                       # no board, golden model stands in
    py python/live_demo.py --record docs/live_demo.gif --frames 300 --sweep 16:5

run from repo root.
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
    KEEP_EYE = 150          # eye traces

    def __init__(self, plt, link, filt, n_taps):
        self.plt = plt
        self.link = link
        self.filt = filt
        self.sps = link.sps
        self.tx_off = (n_taps - 1) // 2     # TX filter group delay
        self.rx_off = n_taps - 1            # TX + RX
        self.pending_esn0 = None
        self.sym_in = deque(maxlen=self.KEEP_SYM)
        self.sym_out = deque(maxlen=self.KEEP_SYM)
        self.eye = deque(maxlen=self.KEEP_EYE)
        self.tx_sym = {}
        self.yi = np.zeros(0)               # rolling output I
        self.buf_start = 0                  # absolute sample index of yi[0]
        self.checked = 0
        self.mismatches = 0
        self.reset_ber()
        self._build()

    def reset_ber(self):
        self.ber_bits = 0
        self.ber_err = 0

    def ingest(self, r):
        sps = self.sps
        norm = Q_SCALE * r["scale"]
        for k, s in enumerate(r["sym"]):
            self.tx_sym[r["first_sym"] + k] = s
        n0 = r["first_sym"] * sps
        x = (r["in"]["i"] + 1j * r["in"]["q"]) / norm
        y = (r["out"]["i"] + 1j * r["out"]["q"]) / norm
        n = np.arange(n0, n0 + x.size)

        # symbol instants: input peaks at k*sps + tx_off, output at k*sps + rx_off
        for j in np.flatnonzero((n - self.tx_off) % sps == 0):
            k = (n[j] - self.tx_off) // sps
            if k in self.tx_sym:
                self.sym_in.append(x[j])
        for j in np.flatnonzero((n - self.rx_off) % sps == 0):
            k = (n[j] - self.rx_off) // sps
            s = self.tx_sym.pop(k, None)
            if s is None:
                continue
            self.sym_out.append(y[j])
            self.ber_bits += 2
            self.ber_err += int((y[j].real < 0) != (s.real < 0)) + int((y[j].imag < 0) != (s.imag < 0))

        # eye: one trace per output symbol, +-1 symbol around the peak
        self.yi = np.concatenate([self.yi, y.real])
        if self.yi.size > 1024:
            cut = self.yi.size - 1024
            self.yi = self.yi[cut:]
            self.buf_start += cut
        idx = np.arange(self.buf_start, self.buf_start + self.yi.size)
        new = np.flatnonzero(((idx - self.rx_off) % sps == 0) & (idx >= n0 - sps) & (idx < n0 + x.size - sps))
        for j in new:
            if j - sps >= 0 and j + sps < self.yi.size:
                self.eye.append(self.yi[j - sps: j + sps + 1].copy())

        self.checked += r["out"]["i"].size * 2
        self.mismatches += r["bad"]

    def _build(self):
        plt = self.plt
        from matplotlib.widgets import Slider

        self.fig, (self.ax_in, self.ax_out, self.ax_eye) = plt.subplots(1, 3, figsize=(13, 5))
        self.fig.subplots_adjust(bottom=0.25, top=0.88)
        for ax, title in ((self.ax_in, "filter input"), (self.ax_out, "filter output")):
            ax.set_title(title)
            ax.set_xlim(-1.6, 1.6)
            ax.set_ylim(-1.6, 1.6)
            ax.set_aspect("equal")
            ax.set_xlabel("I")
            ax.set_ylabel("Q")
        self.sc_in = self.ax_in.scatter([], [], s=4)
        self.sc_out = self.ax_out.scatter([], [], s=4)
        self.ax_eye.set_title("eye diagram (output I)")
        self.ax_eye.set_xlim(-1, 1)
        self.ax_eye.set_ylim(-1.6, 1.6)
        self.ax_eye.set_xlabel("time (symbols)")
        self.eye_lines = []
        self.fig.suptitle(self.filt.name)
        self.status = self.fig.text(0.5, 0.13, "", ha="center")

        ax_sl = self.fig.add_axes([0.25, 0.04, 0.5, 0.03])
        self.slider = Slider(ax_sl, "Es/N0 (dB)", 0.0, 20.0, valinit=self.link.esn0_db, valstep=0.5)
        self.slider.on_changed(self._on_slider)

    def _gain(self, pts):
        """scale so the ideal points land on +-0.707"""
        z = np.array(pts)
        return np.mean(np.abs(z.real) + np.abs(z.imag)) / math.sqrt(2.0) if z.size else 1.0

    def redraw(self):
        g_out = self._gain(self.sym_out)
        for sc, pts in ((self.sc_in, self.sym_in), (self.sc_out, self.sym_out)):
            if pts:
                z = np.array(pts) / self._gain(pts)
                sc.set_offsets(np.c_[z.real, z.imag])
        if self.eye:
            for ln in self.eye_lines:
                ln.remove()
            t = np.linspace(-1, 1, 2 * self.sps + 1)
            self.eye_lines = self.ax_eye.plot(t, np.array(self.eye).T / g_out, color="C0", lw=0.5, alpha=0.3)
        ber = self.ber_err / self.ber_bits if self.ber_bits else 0.0
        self.status.set_text("mismatches vs golden model: %d / %d    BER: %.2e (theory %.2e)"
                             % (self.mismatches, self.checked, ber, qpsk_ber_theory(self.link.esn0_db)))

    def _on_slider(self, val):
        self.pending_esn0 = float(val)

    def apply_pending(self):
        """called from whoever owns the link, between blocks"""
        val, self.pending_esn0 = self.pending_esn0, None
        if val is not None and val != self.link.esn0_db:
            self.link.set_esn0(val)
            return True
        return False


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
    dash = Dashboard(plt, link, filt, coeff.size)

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
                dash.fig.text(0.5, 0.5, "link error: %s" % item, color="red", ha="center")
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
            writer.grab_frame()
    print("wrote %s (%d frames, %d mismatches in %s checked)"
          % (args.record, args.frames, dash.mismatches, format(dash.checked, ",")))
    return 0 if dash.mismatches == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
