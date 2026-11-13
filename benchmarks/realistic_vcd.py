"""Generate a realistic VCD with known ground truth, then check WaveQ's answers and timings against it.

The design: a clock, an AXI-style read channel (out-of-order across IDs, in order within an ID,
one request that never completes), a 4-state FSM, a register that goes X at a known cycle, and
filler registers that make the file large. Every answer the debug tools give is compared with
what the generator actually wrote.

    python benchmarks/realistic_vcd.py [--cycles 200000] [--filler 64] [--keep DIR]
"""
from __future__ import annotations

import argparse
import random
import shutil
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from waveq.engine import WaveQEngine  # noqa: E402


def generate(path: Path, cycles: int, filler: int, seed: int = 7, flip_at: int | None = None) -> dict:
    rnd = random.Random(seed)
    ids = {"clk": "!", "rst_n": '"', "arvalid": "#", "arid": "$", "araddr": "%", "rvalid": "&", "rid": "'", "rdata": "(", "state": ")", "bad": "*"}
    head = ["$timescale 1 ns $end", "$scope module tb $end", '$var wire 1 ! clk $end', '$var wire 1 " rst_n $end',
            "$scope module axi $end", "$var wire 1 # arvalid $end", "$var wire 4 $ arid [3:0] $end", "$var wire 32 % araddr [31:0] $end",
            "$var wire 1 & rvalid $end", "$var wire 4 ' rid [3:0] $end", "$var wire 64 ( rdata [63:0] $end", "$upscope $end",
            "$scope module fsm $end", "$var wire 2 ) state [1:0] $end", "$upscope $end", "$scope module core $end", "$var wire 32 * bad_reg [31:0] $end"]
    fill_ids = [f"f{i}" for i in range(filler)]
    head += [f"$var wire 32 {fid} reg_{i:03d} [31:0] $end" for i, fid in enumerate(fill_ids)]
    head += ["$upscope $end", "$upscope $end", "$enddefinitions $end"]
    hang_cycle, hang_id, x_cycle = cycles // 2, 7, cycles * 3 // 4
    queues: dict[int, deque] = {i: deque() for i in range(16)}
    pairs, fsm_trans = [], {}
    state, dwell = 0, 3
    out = ["\n".join(head), "#0", "$dumpvars", "0!", '0"', "0#", "b0 $", "b0 %", "0&", "b0 '", "b0 (", "b0 )", "b0 *"]
    out += [f"b0 {fid}" for fid in fill_ids] + ["$end"]
    with path.open("w", encoding="ascii", newline="\n") as fh:
        fh.write("\n".join(out) + "\n")
        prev = {"arvalid": 0, "rvalid": 0}
        for c in range(cycles):
            t = 10 * c
            ch = [f"#{t}", "0!"] if c else []
            if c == 2:
                ch.append('1"')
            # one request per cycle at most, never on the hung ID once it is stuck
            req = None
            if c == hang_cycle or (c > 3 and rnd.random() < 0.3):
                i = hang_id if c == hang_cycle else rnd.choice([k for k in range(16) if k != hang_id])
                req = (i, c, c + rnd.randint(3, 20))
            rsp = None
            ready = [q[0] for k, q in queues.items() if q and q[0][2] <= c and not (k == hang_id and q[0][1] >= hang_cycle)]
            if ready:
                rsp = min(ready, key=lambda r: (r[2], r[0]))
                queues[rsp[0]].popleft()
                pairs.append((rsp[1], c, rsp[0]))
            if req:
                queues[req[0]].append(req)
                ch += ["1#" if not prev["arvalid"] else "", f"b{req[0]:b} $", f"b{rnd.getrandbits(32):b} %"]
            elif prev["arvalid"]:
                ch.append("0#")
            if rsp:
                ch += ["1&" if not prev["rvalid"] else "", f"b{rsp[0]:b} '", f"b{rnd.getrandbits(64):b} ("]
            elif prev["rvalid"]:
                ch.append("0&")
            prev = {"arvalid": int(bool(req)), "rvalid": int(bool(rsp))}
            dwell -= 1
            if dwell == 0:
                nxt = (state + 1) % 4 if rnd.random() < 0.9 else 0
                if nxt != state:
                    fsm_trans[(state, nxt)] = fsm_trans.get((state, nxt), 0) + 1
                    ch.append(f"b{nxt:b} )")
                state, dwell = nxt, rnd.randint(2, 12)
            if c == x_cycle:
                ch.append("bx *")
            elif c < x_cycle and rnd.random() < 0.01:
                ch.append(f"b{rnd.getrandbits(32):b} *")
            for fid in fill_ids:
                if rnd.random() < 0.05:
                    v = rnd.getrandbits(32) ^ (1 if flip_at is not None and c >= flip_at and fid == "f0" else 0)
                    ch.append(f"b{v:b} {fid}")
            ch += [f"#{t + 5}", "1!"]
            fh.write("\n".join(x for x in ch if x) + "\n")
        fh.write(f"#{10 * cycles}\n0!\n")
    return {"pairs": pairs, "unmatched": [(r[1], r[0]) for q in queues.values() for r in q], "fsm": fsm_trans,
            "x_cycle": x_cycle, "hang": (hang_cycle, hang_id)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=200000)
    ap.add_argument("--filler", type=int, default=64)
    ap.add_argument("--keep", default=None)
    a = ap.parse_args()
    work = Path(a.keep or tempfile.mkdtemp(prefix="waveq-real-"))
    work.mkdir(parents=True, exist_ok=True)
    vcd, vcd_b = work / "soc.vcd", work / "soc_b.vcd"
    t0 = time.perf_counter()
    truth = generate(vcd, a.cycles, a.filler)
    flip = a.cycles // 3
    generate(vcd_b, a.cycles, a.filler, flip_at=flip)
    print(f"generated 2 x {vcd.stat().st_size / 2**20:.1f} MB ({a.cycles} cycles, {a.filler + 10} signals) in {time.perf_counter() - t0:.1f}s")
    e = WaveQEngine()
    checks: list[tuple[str, bool, str]] = []

    def timed(label, fn):
        t = time.perf_counter(); r = fn(); ms = (time.perf_counter() - t) * 1000
        print(f"  {label:<34} {ms:9.1f} ms  {r.get('status')}")
        return r

    try:
        r = timed("configure + full index", lambda: e.configure(str(vcd), cache_dir=str(work / "cache"), rebuild=True, index="full"))
        timed("define_clock", lambda: e.define_clock("tb.clk"))
        req = [{"signal": "tb.axi.arvalid", "value": "1"}]
        rsp = [{"signal": "tb.axi.rvalid", "value": "1"}]
        p = timed("pair_events by ID", lambda: e.pair_events("tb.clk", req, rsp, request_id="tb.axi.arid", response_id="tb.axi.rid", limit=10000))
        got = sorted((x["request_cycle"], x["response_cycle"], int(x["id"], 16)) for x in p["data"]["pairs"])
        listed_ok = set(got) <= set(truth["pairs"]) and (len(got) == len(truth["pairs"]) or p["data"]["truncated"])
        checks.append(("every request/response pair exact", listed_ok and p["data"]["pair_count"] == len(truth["pairs"]), f"{p['data']['pair_count']} pairs, {len(got)} listed"))
        un = [(u["cycle_index"], int(u["id"], 16)) for u in p["data"]["unmatched_requests"]]
        checks.append(("hung request found", sorted(un) == sorted(truth["unmatched"]) and truth["hang"] in un, f"unmatched {un[:3]}"))
        x = timed("x_report at end", lambda: e.x_report(scope="core"))
        first = x["data"]["signals"][0] if x["data"]["signals"] else {}
        checks.append(("X origin and time", first.get("signal", "").startswith("tb.core.bad_reg") and first.get("unknown_since_tick") == 10 * truth["x_cycle"], str(first.get("unknown_since_time"))))
        s = timed("state_transitions", lambda: e.state_transitions("tb.fsm.state"))
        got_t = {(int(t["from"], 16), int(t["to"], 16)): t["count"] for t in s["data"]["transitions"] if t["from"] != "x"}
        checks.append(("FSM transition counts", got_t == truth["fsm"], f"{sum(got_t.values())} transitions"))
        hang_tick = 10 * truth["hang"][0]
        act = timed("activity after hang (axi)", lambda: e.activity(start=hang_tick, scope="axi"))
        checks.append(("activity runs", act["ok"], act["summary"][:60]))
        timed("find_times clock + payload", lambda: e.find_times(req, clock="tb.clk", payload=["tb.axi.arid", "tb.axi.araddr"], limit=1000))
        timed("value_at", lambda: e.value_at("tb.axi.rdata", hang_tick))
        timed("window_digest", lambda: e.window_digest(hang_tick, "200ns"))
        d = timed("first_divergence vs run B", lambda: e.first_divergence(str(vcd_b)))
        checks.append(("first divergence on reg_000 at/after flip", d["data"]["signals"][0]["signal"].startswith("tb.core.reg_000") and d["data"]["first_divergence_tick"] >= 10 * flip, str(d["data"]["first_divergence_time"])))
        e.close()
        e2 = WaveQEngine()
        e2.configure(str(vcd), cache_dir=str(work / "sub"), rebuild=True, index="metadata")
        timed("subset index (4 signals)", lambda: e2.build_index(level="full", signals=["tb.clk", "tb.axi.arvalid", "tb.axi.rvalid", "tb.axi.arid"]))
        e2.close()
    finally:
        e.close()
        if not a.keep:
            shutil.rmtree(work, ignore_errors=True)
    print()
    for name, passed, detail in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}  ({detail})")
    return 0 if all(c[1] for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
