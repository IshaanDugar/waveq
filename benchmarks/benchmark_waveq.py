from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
import shutil
import statistics
import time
import uuid
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from waveq.engine import WaveQEngine


def generate_vcd(path: Path, *, signals: int, ticks: int, toggle_stride: int) -> None:
    # One clock plus scalar controls plus 8-bit bus signals. Deterministic by construction.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("$timescale 1 ns $end\n$scope module tb $end\n")
        ids = []
        # VCD id codes: use printable combinations avoiding whitespace.
        alphabet = [chr(i) for i in range(33, 127) if chr(i) not in {' '}]
        for i in range(signals):
            if i < len(alphabet):
                code = alphabet[i]
            else:
                code = alphabet[i % len(alphabet)] + alphabet[i // len(alphabet)]
            ids.append(code)
            width = 1 if (i == 0 or i % 8) else 8
            name = "clk" if i == 0 else f"sig_{i:04d}" if width == 1 else f"bus_{i:04d} [7:0]"
            f.write(f"$var wire {width} {code} {name} $end\n")
        f.write("$upscope $end\n$enddefinitions $end\n$dumpvars\n")
        for i, code in enumerate(ids):
            if i == 0 or i % 8:
                f.write(f"0{code}\n")
            else:
                f.write(f"b00000000 {code}\n")
        f.write("$end\n")
        scalar_values = [0] * signals
        for tick in range(1, ticks + 1):
            f.write(f"#{tick}\n")
            # Clock toggles every tick.
            scalar_values[0] ^= 1
            f.write(f"{scalar_values[0]}{ids[0]}\n")
            for i in range(1, signals):
                if (tick + i) % (toggle_stride + (i % 5)) == 0:
                    if i % 8:
                        scalar_values[i] ^= 1
                        f.write(f"{scalar_values[i]}{ids[i]}\n")
                    else:
                        f.write(f"b{((tick * (i + 3)) & 0xff):08b} {ids[i]}\n")


def pad_vcd_to_size(path: Path, size_mb: int) -> None:
    target = size_mb * 1024 * 1024
    if path.stat().st_size >= target:
        return
    chunk = "$comment " + ("p" * 8170) + " $end\n"
    with path.open("a", encoding="utf-8") as f:
        while path.stat().st_size < target:
            f.write(chunk)


def median_ms(fn: Callable[[], object], loops: int) -> float:
    samples = []
    for _ in range(loops):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(samples)


def run_case(work: Path, name: str, *, signals: int, ticks: int, stride: int, pad_mb: int | None = None) -> dict[str, object]:
    vcd = work / f"{name}.vcd"
    generate_vcd(vcd, signals=signals, ticks=ticks, toggle_stride=stride)
    if pad_mb:
        pad_vcd_to_size(vcd, pad_mb)
    cache = work / "cache"
    eng = WaveQEngine()
    t0 = time.perf_counter()
    configured = eng.configure(str(vcd), cache_dir=str(cache), rebuild=True)
    configure_seconds = time.perf_counter() - t0
    if not configured["ok"]:
        raise RuntimeError(configured)
    status = eng.index_status(str(vcd), str(cache))
    if not status["ok"]:
        raise RuntimeError(status)
    build_changes = eng.build_index(str(vcd), str(cache), level="changes", rebuild=True)
    if not build_changes["ok"]:
        raise RuntimeError(build_changes)
    build_runs = eng.build_index(str(vcd), str(cache), level="runs", rebuild=True)
    if not build_runs["ok"]:
        raise RuntimeError(build_runs)
    build_full = eng.build_index(str(vcd), str(cache), level="full", rebuild=True)
    if not build_full["ok"]:
        raise RuntimeError(build_full)
    info = eng.info()["data"]
    reuse = eng.configure(str(vcd), cache_dir=str(cache), rebuild=False)
    if not reuse["ok"]:
        raise RuntimeError(reuse)
    # Strict functional checks. Any failure raises and aborts the benchmark.
    assert eng.value_at("tb.clk", "1ns")["data"]["value"] == "1"
    assert eng.values_at(["tb.clk", "tb.sig_0001"], "5ns")["ok"] is True
    assert eng.values_at(["tb.clk", "missing_signal"], "5ns")["ok"] is False
    assert eng.edges("tb.clk", kind="rise", start="0ns", end=f"{ticks}ns")["ok"] is True
    assert eng.window_digest(f"{ticks // 2}ns", "20ns", max_signals=20)["ok"] is True
    eng.define_clock("tb.clk", "rise")
    assert eng.time_to_cycle("tb.clk", f"{ticks // 2}ns")["ok"] is True
    find_conds = [{"signal": "tb.sig_0001", "value": "1"}, {"signal": "tb.sig_0002", "value": "1"}]
    assert eng.find_times(find_conds, limit=10000)["ok"] is True
    assert eng.find_times(find_conds, clock="tb.clk", limit=10000)["ok"] is True

    timings = {
        "index_status_median_ms": median_ms(lambda: eng.index_status(str(vcd), str(cache)), 30),
        "value_at_median_ms": median_ms(lambda: eng.value_at("tb.sig_0001", f"{ticks // 2}ns"), 200),
        "values_at_8_median_ms": median_ms(lambda: eng.values_at(["tb.clk", "tb.sig_0001", "tb.sig_0002", "tb.sig_0003", "tb.sig_0004", "tb.sig_0005", "tb.sig_0006", "tb.sig_0007"], f"{ticks // 2}ns"), 100),
        "changes_many_8_median_ms": median_ms(lambda: eng.changes_many(["tb.clk", "tb.sig_0001", "tb.sig_0002", "tb.sig_0003", "tb.sig_0004", "tb.sig_0005", "tb.sig_0006", "tb.sig_0007"], f"{ticks // 2 - 50}ns", f"{ticks // 2 + 50}ns", 200), 50),
        "window_digest_median_ms": median_ms(lambda: eng.window_digest(f"{ticks // 2}ns", "50ns", max_signals=30), 30),
        "find_times_intervals_median_ms": median_ms(lambda: eng.find_times(find_conds, limit=10000), 30),
        "find_times_clock_median_ms": median_ms(lambda: eng.find_times(find_conds, clock="tb.clk", limit=10000), 30),
        "edges_clk_median_ms": median_ms(lambda: eng.edges("tb.clk", kind="rise", start="0ns", end=f"{ticks}ns", limit=10000), 30),
    }
    db_path = Path(build_full["data"]["db_path"])
    result = {
        "case": name,
        "signals_requested": signals,
        "ticks_requested": ticks,
        "vcd_size_bytes": vcd.stat().st_size,
        "db_size_bytes": db_path.stat().st_size,
        "size_class": configured["data"]["size_class"],
        "configure_seconds": configure_seconds,
        "configure_query_ready": configured["data"]["query_ready"],
        "cache_reuse_seconds": reuse["data"]["configure_seconds"],
        "cache_reused": reuse["data"]["cache_reused"],
        "build_changes_seconds": build_changes["data"]["build_seconds"],
        "build_runs_seconds": build_runs["data"]["build_seconds"],
        "build_full_seconds": build_full["data"]["build_seconds"],
        "signal_count": info["signal_count"],
        "change_count": info["change_count"],
        "run_count": info["run_count"],
        "edge_count": info["edge_count"],
        **timings,
    }
    eng.close()
    return result


def write_reports(results: list[dict[str, object]], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "benchmark_results.json").write_text(json.dumps({"results": results}, indent=2), encoding="utf-8")
    lines = [
        "# WaveQ Benchmark Report",
        "",
        "Synthetic deterministic VCDs were generated locally. Each case measures metadata configure, index status, explicit index builds, cache reuse, and strict query checks.",
        "",
        "| Case | Size class | Signals | Ticks | VCD size | DB size | Configure s | Query ready after configure | Reuse s | Changes build s | Runs build s | Full build s | Changes | Runs | Edges | status ms | value_at ms | values_at(8) ms | changes_many(8) ms | digest ms | clk edges ms | find intervals ms | find clock ms |",
        "|---|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| {r['case']} | {r['size_class']} | {r['signal_count']} | {r['ticks_requested']} | {r['vcd_size_bytes']} | {r['db_size_bytes']} | "
            f"{float(r['configure_seconds']):.4f} | {r['configure_query_ready']} | {float(r['cache_reuse_seconds']):.4f} | "
            f"{float(r['build_changes_seconds']):.4f} | {float(r['build_runs_seconds']):.4f} | {float(r['build_full_seconds']):.4f} | "
            f"{r['change_count']} | {r['run_count']} | {r['edge_count']} | {float(r['index_status_median_ms']):.4f} | "
            f"{float(r['value_at_median_ms']):.4f} | {float(r['values_at_8_median_ms']):.4f} | {float(r['changes_many_8_median_ms']):.4f} | "
            f"{float(r['window_digest_median_ms']):.4f} | {float(r['edges_clk_median_ms']):.4f} | "
            f"{float(r['find_times_intervals_median_ms']):.4f} | {float(r['find_times_clock_median_ms']):.4f} |"
        )
    lines.extend([
        "",
        "## Notes",
        "",
        "- Timings are median wall-clock milliseconds in this container, not a hardware-independent guarantee.",
        "- Medium and large cases are metadata-only at configure time unless a full cache already exists.",
        "- `value_at` uses the explicitly built `signal_runs` table.",
        "- Edge and cycle queries use explicitly built `signal_edges` and `clock_edges` tables.",
        "- The benchmark intentionally verifies that a missing signal does not return `ok: true`.",
    ])
    (output / "benchmark_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="reports", help="Directory for benchmark reports")
    ap.add_argument("--keep-work", action="store_true", help="Keep generated VCD/cache work directory")
    args = ap.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    work = output / f"benchmark_work_{uuid.uuid4().hex}"
    work.mkdir(parents=True)
    try:
        results = [
            run_case(work, "small", signals=32, ticks=2000, stride=11),
            run_case(work, "medium", signals=128, ticks=12000, stride=17, pad_mb=12),
            run_case(work, "large", signals=32, ticks=2000, stride=11, pad_mb=55),
        ]
        write_reports(results, output)
        if args.keep_work:
            keep = output / "benchmark_work"
            if keep.exists():
                shutil.rmtree(keep)
            shutil.copytree(work, keep)
    finally:
        if not args.keep_work:
            shutil.rmtree(work, ignore_errors=True)
    print(json.dumps({"ok": True, "results": results, "output": str(output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
