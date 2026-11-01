#!/usr/bin/env python3
"""
wide RTL regression: the AXI4-Stream testbench over a matrix of filter
sizes, channels, seeds and stimulus patterns, in parallel.

  qpsk     33 and 21 taps x Es/N0 {0,4,8,12,20,40} x 3 noise seeds, each
           with its own back-pressure seed; bit-exact vs golden + BER check
  corner   rails, fastest swing, impulses, +/-1 LSB
  impulse  output must be the negated coefficient ROM, word for word
  step     output must be the running tap sum, clamped
  random   full-scale random words, saturation in both directions
  case ROM the case-statement coefficient ROM on both tap counts

every run is bit-exact against the golden model; impulse/step/random also
get closed-form checks (python/check_pattern.py) that don't use it.

run from repo root:  python sim/run_regress.py [-j 8] [--quick]
writes build/regress/<run>/ and build/regress/summary.json
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

RTL = ["rtl/rrc_fir_axis.v", "rtl/rrc_fir_core.v", "rtl/rrc_coeff_rom.v",
       "rtl/pipelined_adder_tree.v", "rtl/booth_mult_r4.v"]
SPANS = {33: 8, 21: 5}


def find_tools():
    iverilog = os.environ.get("IVERILOG") or shutil.which("iverilog")
    if not iverilog and os.path.exists("C:/iverilog/bin/iverilog.exe"):
        iverilog = "C:/iverilog/bin/iverilog.exe"
    if not iverilog:
        raise SystemExit("iverilog not found, set IVERILOG")
    vvp = os.path.join(os.path.dirname(iverilog), "vvp")
    if not shutil.which(vvp):
        vvp = shutil.which("vvp") or vvp
    return iverilog, vvp


def matrix(quick):
    runs = []
    esn0s = (0, 4, 8, 12, 20, 40) if not quick else (4, 12)
    seeds = (1, 2, 3) if not quick else (1,)
    for taps, span in SPANS.items():
        for es in esn0s:
            for sd in seeds:
                runs.append(dict(name="qpsk_t%d_es%d_s%d" % (taps, es, sd), pattern="qpsk",
                                 span=span, esn0=es, seed=16 * es + sd,
                                 stall=1000 * taps + 10 * es + sd))
        for pat in ("corner", "impulse", "step", "random"):
            runs.append(dict(name="%s_t%d" % (pat, taps), pattern=pat, span=span,
                             esn0=8, seed=7, stall=11))
        runs.append(dict(name="caserom_t%d" % taps, pattern="qpsk", span=span, esn0=8,
                         seed=9, stall=13, case=True))
    return runs


def run_one(job, iverilog, vvp, root, n_sym):
    d = os.path.join(root, job["name"]).replace(os.sep, "/")
    os.makedirs(d, exist_ok=True)
    py = sys.executable
    log = []

    def sh(cmd):
        p = subprocess.run(cmd, capture_output=True, text=True)
        log.append("$ " + " ".join(cmd) + "\n" + p.stdout + p.stderr)
        return p.returncode, p.stdout

    t0 = time.time()
    res = dict(job, ok=False)
    steps = [
        [py, "python/gen_stimulus.py", "--outdir", d, "--pattern", job["pattern"],
         "--span", str(job["span"]), "--esn0-db", str(job["esn0"]),
         "--seed", str(job["seed"]), "--num-symbols", str(n_sym)],
        [py, "python/golden_model.py", "--datadir", d],
        [iverilog, "-g2005", "-Wall", "-I", "rtl", "-I", d]
        + (["-DRRC_COEFF_CASE"] if job.get("case") else [])
        + ["-o", d + "/tb.vvp", "sim/tb_rrc_fir_axis.v"] + RTL,
        [vvp, d + "/tb.vvp", "+seed=%d" % job["stall"]],
    ]
    if job["pattern"] == "qpsk":
        steps.append([py, "python/analyze_results.py", "--datadir", d, "--no-plots"])
    elif job["pattern"] != "corner":
        steps.append([py, "python/check_pattern.py", "--datadir", d])

    for cmd in steps:
        rc, out = sh(cmd)
        m = re.search(r"beats in / out\s*:\s*(\d+) / (\d+)", out)
        if m:
            res["beats"] = int(m.group(1))
        m = re.search(r"TREADY stall cycles\s*:\s*(\d+)", out)
        if m:
            res["stalls"] = int(m.group(1))
        m = re.search(r"output saturations\s*:\s*(\d+)", out)
        if m:
            res["saturations"] = int(m.group(1))
        m = re.search(r"\((\d+) bit errors in (\d+) bits\)", out)
        if m:
            res["bit_errors"], res["bits"] = int(m.group(1)), int(m.group(2))
        m = re.search(r"responses checked\s*:\s*(\d+)", out)
        if m:
            res["closed_form_checks"] = int(m.group(1))
        if rc != 0:
            res["failed_at"] = os.path.basename(cmd[1] if cmd[0] == py else cmd[0])
            break
    else:
        res["ok"] = True
    res["seconds"] = round(time.time() - t0, 1)
    with open(os.path.join(d, "run.log"), "w") as fh:
        fh.write("\n".join(log))
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="Parallel RTL regression matrix.")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--quick", action="store_true", help="a handful of runs")
    ap.add_argument("--num-symbols", type=int, default=2048,
                    help="symbols per QPSK run (x4 samples)")
    ap.add_argument("--root", default="build/regress")
    args = ap.parse_args(argv)

    os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    iverilog, vvp = find_tools()
    jobs = matrix(args.quick)
    print("running %d RTL simulations on %d workers" % (len(jobs), args.jobs))

    t0 = time.time()
    results = []
    with ThreadPoolExecutor(args.jobs) as ex:
        futs = [ex.submit(run_one, j, iverilog, vvp, args.root, args.num_symbols) for j in jobs]
        for f in futs:
            r = f.result()
            results.append(r)
            extra = ""
            if "bit_errors" in r:
                extra = "BER %d/%d" % (r["bit_errors"], r["bits"])
            if "closed_form_checks" in r:
                extra = "%d closed-form responses" % r["closed_form_checks"]
            print("  %-22s %-4s %6s beats %6s stalls %5s sat  %s"
                  % (r["name"], "PASS" if r["ok"] else "FAIL",
                     r.get("beats", "-"), r.get("stalls", "-"),
                     r.get("saturations", "-"), extra or r.get("failed_at", "")))

    n_fail = sum(not r["ok"] for r in results)
    tot = dict(
        runs=len(results), failed=n_fail,
        beats=sum(r.get("beats", 0) for r in results),
        stalls=sum(r.get("stalls", 0) for r in results),
        saturations=sum(r.get("saturations", 0) for r in results),
        closed_form_checks=sum(r.get("closed_form_checks", 0) for r in results),
        seconds=round(time.time() - t0, 1),
    )
    with open(os.path.join(args.root, "summary.json"), "w") as fh:
        json.dump(dict(total=tot, runs=results), fh, indent=1)
    print("")
    print("%d runs, %d failed: %d beats through the RTL, %d stall cycles, "
          "%d saturating outputs, %d closed-form responses (%.0f s)"
          % (tot["runs"], n_fail, tot["beats"], tot["stalls"], tot["saturations"],
             tot["closed_form_checks"], tot["seconds"]))
    if n_fail:
        print("logs: %s/<run>/run.log" % args.root)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
