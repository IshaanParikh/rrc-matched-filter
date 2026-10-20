#!/usr/bin/env python3
"""
stream samples through the filter running on a DueProLogic and check the
results bit for bit against the golden model.

talks EPT's Active Transfer framing straight over the FT2232H UART, so no
Active Host DLL is needed. each frame is {2'b10|2'b11, cmd[2:0], addr[2:0]}
followed by one payload byte; hw_demo/dpl/dpl_top.v has the details.

lockstep, one sample in flight: EPT's library garbles a byte if the two
directions overlap. each round trip waits on the FTDI latency timer, so set
it to 1 ms (Device Manager > the COM port > Port Settings > Advanced) or
expect ~60 samples/s instead of several hundred.

    pip install pyserial
    python python/gen_stimulus.py --span 5 --outdir hw_demo/data
    python python/dpl_host.py --port COM14

run from repo root.
"""

import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dsp_common import Q_FRAC_BITS, read_hex, round_sat  # noqa: E402
from golden_model import golden_fir  # noqa: E402

CMD_TRIGGER = 0x1
CMD_TRANSFER = 0x2


def frame(cmd, addr, payload, to_host=False):
    hdr = (0xC0 if to_host else 0x80) | (cmd << 3) | addr
    return bytes([hdr, payload & 0xFF])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bit-exact hardware check of the DueProLogic RRC filter.")
    ap.add_argument("--port", required=True, help="FT2232H UART port, e.g. COM14")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--datadir", default="hw_demo/data")
    ap.add_argument("--addr", type=int, default=1, help="SAMPLE_ADDR in dpl_top.v")
    ap.add_argument("--count", type=int, default=1024, help="samples to send (I channel)")
    ap.add_argument("--timeout", type=float, default=2.0)
    args = ap.parse_args(argv)

    import serial  # pyserial, only needed here

    coeff, _ = read_hex(os.path.join(args.datadir, "rrc_coeffs.hex"), 16, signed=True)
    packed, n_unknown = read_hex(os.path.join(args.datadir, "qpsk_noisy_iq.hex"), 32, signed=False)
    if n_unknown:
        raise RuntimeError("stimulus contains x/z words")
    x = (packed & 0xFFFF).astype(np.int64)
    x = np.where(x & 0x8000, x - 0x10000, x)[: args.count]
    expected = round_sat(golden_fir(x, coeff), Q_FRAC_BITS, 16)

    reply_hdr = frame(CMD_TRANSFER, args.addr, 0, to_host=True)[0]

    with serial.Serial(args.port, args.baud, timeout=args.timeout) as port:
        port.reset_input_buffer()
        # trigger bit 0: clear filter history and byte phase
        port.write(frame(CMD_TRIGGER, 0, 0x01))
        port.flush()
        time.sleep(0.05)
        port.reset_input_buffer()

        got = []
        t0 = time.time()
        for xv in x:
            v = int(xv) & 0xFFFF
            port.write(frame(CMD_TRANSFER, args.addr, v) + frame(CMD_TRANSFER, args.addr, v >> 8))
            raw = port.read(4)
            if len(raw) < 4:
                print("timed out after %d of %d samples" % (len(got), x.size))
                return 1
            if raw[0] != reply_hdr or raw[2] != reply_hdr:
                print("bad frame header at sample %d: %s" % (len(got), raw.hex()))
                return 1
            y = raw[1] | (raw[3] << 8)
            got.append(y - 0x10000 if y & 0x8000 else y)
        dt = time.time() - t0

    got = np.array(got, dtype=np.int64)
    bad = np.flatnonzero(got != expected)
    print("samples     : %d in %.1f s (%.0f samples/s)" % (x.size, dt, x.size / dt))
    print("mismatches  : %d" % bad.size)
    for i in bad[:10]:
        print("  [%d] got %d expected %d" % (i, got[i], expected[i]))
    print("PASS" if bad.size == 0 else "FAIL")
    return 0 if bad.size == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
