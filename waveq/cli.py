from __future__ import annotations

import argparse
import json
from typing import Any

from . import __version__
from .engine import WaveQEngine


def print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=False))


def configured_engine(args: argparse.Namespace) -> tuple[WaveQEngine, dict[str, Any]]:
    eng = WaveQEngine()
    result = eng.configure(args.vcd, getattr(args, "cache_dir", None), getattr(args, "rebuild", False))
    return eng, result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="waveq", description="Query VCD waveforms through a SQLite cache")
    p.add_argument("--version", action="version", version=f"waveq {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("vcd", help="Path to .vcd or .vcd.gz")
        sp.add_argument("--cache-dir", default=None, help="Optional cache directory")
        sp.add_argument("--rebuild", action="store_true", help="Force cache rebuild")

    sp = sub.add_parser("configure", help="Configure metadata/cache for a VCD"); add_common(sp)
    sp = sub.add_parser("index-status", help="Check cache/index readiness without rebuilding"); sp.add_argument("vcd", help="Path to .vcd or .vcd.gz"); sp.add_argument("--cache-dir", default=None, help="Optional cache directory")
    sp = sub.add_parser("build-index", help="Build explicit indexes"); add_common(sp); sp.add_argument("--level", choices=["changes", "runs", "edges", "full"], default="full"); sp.add_argument("--signals", nargs="*")
    sp = sub.add_parser("info", help="Load a VCD and print metadata"); add_common(sp)
    sp = sub.add_parser("resolve", help="Resolve signal names"); add_common(sp); sp.add_argument("query"); sp.add_argument("--scope-hint"); sp.add_argument("--limit", type=int, default=10)
    sp = sub.add_parser("value", help="Read one signal value at a time"); add_common(sp); sp.add_argument("signal"); sp.add_argument("time")
    sp = sub.add_parser("values", help="Read multiple signal values at a time"); add_common(sp); sp.add_argument("time"); sp.add_argument("signals", nargs="+")
    sp = sub.add_parser("changes", help="List changes for a signal in a time window"); add_common(sp); sp.add_argument("signal"); sp.add_argument("start"); sp.add_argument("end"); sp.add_argument("--limit", type=int, default=200)
    sp = sub.add_parser("edges", help="List precomputed edges"); add_common(sp); sp.add_argument("signal"); sp.add_argument("--kind"); sp.add_argument("--start"); sp.add_argument("--end"); sp.add_argument("--limit", type=int, default=200)
    sp = sub.add_parser("digest", help="Summarize a window around a time"); add_common(sp); sp.add_argument("center_time"); sp.add_argument("radius"); sp.add_argument("--signals", nargs="*"); sp.add_argument("--scope"); sp.add_argument("--max-signals", type=int, default=30)
    sub.add_parser("mcp", help="Run the MCP stdio server")

    args = p.parse_args(argv)
    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        return mcp_main([])

    if args.cmd == "index-status":
        eng = WaveQEngine()
        result = eng.index_status(args.vcd, args.cache_dir)
        print_json(result)
        return 0 if result.get("ok") else 1

    if args.cmd == "build-index":
        eng = WaveQEngine()
        result = eng.build_index(args.vcd, args.cache_dir, args.level, args.signals, args.rebuild)
        print_json(result)
        return 0 if result.get("ok") else 1

    eng, load_result = configured_engine(args)
    if args.cmd == "configure":
        print_json(load_result)
        return 0 if load_result.get("ok") else 1
    if not load_result.get("ok"):
        print_json(load_result)
        return 1

    if args.cmd == "info": result = eng.info()
    elif args.cmd == "resolve": result = eng.resolve_signals(args.query, args.scope_hint, args.limit)
    elif args.cmd == "value": result = eng.value_at(args.signal, args.time)
    elif args.cmd == "values": result = eng.values_at(args.signals, args.time)
    elif args.cmd == "changes": result = eng.changes(args.signal, args.start, args.end, args.limit)
    elif args.cmd == "edges": result = eng.edges(args.signal, args.kind, args.start, args.end, args.limit)
    elif args.cmd == "digest": result = eng.window_digest(args.center_time, args.radius, args.signals, args.scope, args.max_signals)
    else: raise AssertionError(args.cmd)
    print_json(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
