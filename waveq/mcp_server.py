from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Any, Callable

from . import __version__
from .engine import WaveQEngine, fail

SERVER_INSTRUCTIONS = (
    "Use WaveQ to inspect VCD waveforms. First call configure_waveform. Large VCDs configure metadata only; call index_status and build_index before indexed queries. "
    "Resolve ambiguous names with resolve_signals. Prefer batch tools values_at, changes_many, and window_digest over repeated single-signal calls. "
    "Use define_clock before cycle-based queries. WaveQ reports observed waveform facts; it does not prove protocol correctness unless a tool explicitly checks a rule."
)

ENGINE = WaveQEngine()


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def _str(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


def _int(desc: str, default: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "integer", "description": desc}
    if default is not None:
        out["default"] = default
    return out


def _bool(desc: str, default: bool | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "boolean", "description": desc}
    if default is not None:
        out["default"] = default
    return out


def _arr(desc: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": desc}


TOOLS: dict[str, tuple[str, dict[str, Any], Callable[[dict[str, Any]], dict[str, Any]]]] = {
    "configure_waveform": (
        "Configure metadata/cache for a .vcd or .vcd.gz waveform. Large files are metadata-only; use build_index for expensive indexing.",
        _schema({"vcd_path": _str("Path to .vcd or .vcd.gz"), "cache_dir": _str("Optional cache directory"), "rebuild": _bool("Force rebuild", False)}, ["vcd_path"]),
        lambda a: ENGINE.configure(a["vcd_path"], a.get("cache_dir"), bool(a.get("rebuild", False))),
    ),
    "index_status": (
        "Check cache/index readiness without rebuilding and without fully parsing the VCD.",
        _schema({"vcd_path": _str("Optional path to .vcd or .vcd.gz"), "cache_dir": _str("Optional cache directory")}),
        lambda a: ENGINE.index_status(a.get("vcd_path"), a.get("cache_dir")),
    ),
    "build_index": (
        "Explicitly build expensive indexes. Levels: changes, runs, edges, full.",
        _schema({
            "vcd_path": _str("Optional path to .vcd or .vcd.gz"),
            "cache_dir": _str("Optional cache directory"),
            "level": _str("changes, runs, edges, or full"),
            "signals": _arr("Optional signal subset; currently unsupported"),
            "rebuild": _bool("Force rebuild", False),
        }),
        lambda a: ENGINE.build_index(a.get("vcd_path"), a.get("cache_dir"), a.get("level", "full"), a.get("signals"), bool(a.get("rebuild", False))),
    ),
    "waveform_info": ("Return loaded waveform metadata including timescale, signal count, change count, and time range.", _schema({}), lambda a: ENGINE.info()),
    "resolve_signals": (
        "Find candidate VCD signals by exact, leaf, substring, or token match. Use before querying ambiguous names.",
        _schema({"query": _str("Signal name or tokens"), "scope_hint": _str("Optional scope substring"), "limit": _int("Maximum matches", 10)}, ["query"]),
        lambda a: ENGINE.resolve_signals(a["query"], a.get("scope_hint"), int(a.get("limit", 10))),
    ),
    "value_at": (
        "Return one signal's exact held value at a time. Time may be a raw tick or unit string like 25ns.",
        _schema({"signal": _str("Exact full path or unique leaf name"), "time": _str("Raw tick or exact unit time")}, ["signal", "time"]),
        lambda a: ENGINE.value_at(a["signal"], a["time"]),
    ),
    "values_at": (
        "Batch value_at for multiple signals at one time. Prefer this over repeated value_at calls.",
        _schema({"signals": _arr("Signal full paths or unique leaf names"), "time": _str("Raw tick or exact unit time")}, ["signals", "time"]),
        lambda a: ENGINE.values_at(list(a["signals"]), a["time"]),
    ),
    "changes": (
        "Return changes for one signal in an inclusive time window.",
        _schema({"signal": _str("Signal full path or unique leaf"), "start": _str("Window start"), "end": _str("Window end"), "limit": _int("Max rows", 200)}, ["signal", "start", "end"]),
        lambda a: ENGINE.changes(a["signal"], a["start"], a["end"], int(a.get("limit", 200))),
    ),
    "changes_many": (
        "Batch changes for multiple signals in one time window. Prefer this for waveform debug context.",
        _schema({"signals": _arr("Signal full paths or unique leaf names"), "start": _str("Window start"), "end": _str("Window end"), "limit_per_signal": _int("Max rows per signal", 200)}, ["signals", "start", "end"]),
        lambda a: ENGINE.changes_many(list(a["signals"]), a["start"], a["end"], int(a.get("limit_per_signal", 200))),
    ),
    "edges": (
        "Return precomputed signal edges. Kinds include rise, fall, x_enter, x_exit, z_enter, z_exit, scalar_change, vector_change.",
        _schema({"signal": _str("Signal full path or unique leaf"), "kind": _str("Optional edge kind"), "start": _str("Optional start"), "end": _str("Optional end"), "limit": _int("Max rows", 200)}, ["signal"]),
        lambda a: ENGINE.edges(a["signal"], a.get("kind"), a.get("start"), a.get("end"), int(a.get("limit", 200))),
    ),
    "first_edge_after": (
        "Return the first edge of a given kind after a time.",
        _schema({"signal": _str("Signal full path or unique leaf"), "kind": _str("Edge kind"), "time": _str("Reference time")}, ["signal", "kind", "time"]),
        lambda a: ENGINE.first_edge_after(a["signal"], a["kind"], a["time"]),
    ),
    "stable_between": (
        "Check whether a signal changed between two times.",
        _schema({"signal": _str("Signal full path or unique leaf"), "start": _str("Start time"), "end": _str("End time")}, ["signal", "start", "end"]),
        lambda a: ENGINE.stable_between(a["signal"], a["start"], a["end"]),
    ),
    "window_digest": (
        "Summarize what changed around a time without dumping raw VCD text. Good first debug query near a failure.",
        _schema({"center_time": _str("Center time"), "radius": _str("Half-window radius"), "signals": _arr("Optional selected signals"), "scope": _str("Optional scope filter"), "max_signals": _int("Maximum signals", 30)}, ["center_time", "radius"]),
        lambda a: ENGINE.window_digest(a["center_time"], a["radius"], a.get("signals"), a.get("scope"), int(a.get("max_signals", 30))),
    ),
    "define_clock": (
        "Build a cycle index from one signal's edge events. Call before cycle-based tools.",
        _schema({"signal": _str("Clock signal"), "edge": _str("rise or fall; default rise")}, ["signal"]),
        lambda a: ENGINE.define_clock(a["signal"], a.get("edge", "rise")),
    ),
    "time_to_cycle": (
        "Map a time to the latest clock cycle at or before that time.",
        _schema({"clock": _str("Clock signal"), "time": _str("Query time"), "edge": _str("rise or fall; default rise")}, ["clock", "time"]),
        lambda a: ENGINE.time_to_cycle(a["clock"], a["time"], a.get("edge", "rise")),
    ),
    "cycle_to_time": (
        "Map a clock cycle index to its VCD time.",
        _schema({"clock": _str("Clock signal"), "cycle_index": _int("Cycle index"), "edge": _str("rise or fall; default rise")}, ["clock", "cycle_index"]),
        lambda a: ENGINE.cycle_to_time(a["clock"], int(a["cycle_index"]), a.get("edge", "rise")),
    ),
    "values_at_cycle": (
        "Batch values_at for several signals at a clock cycle.",
        _schema({"signals": _arr("Signal full paths or unique leaf names"), "clock": _str("Clock signal"), "cycle_index": _int("Cycle index"), "edge": _str("rise or fall; default rise")}, ["signals", "clock", "cycle_index"]),
        lambda a: ENGINE.values_at_cycle(list(a["signals"]), a["clock"], int(a["cycle_index"]), a.get("edge", "rise")),
    ),
    "changes_by_cycle": (
        "Return one signal's changes between two clock cycle indices.",
        _schema({"signal": _str("Signal full path or unique leaf"), "clock": _str("Clock signal"), "start_cycle": _int("Start cycle"), "end_cycle": _int("End cycle"), "edge": _str("rise or fall; default rise"), "limit": _int("Max rows", 200)}, ["signal", "clock", "start_cycle", "end_cycle"]),
        lambda a: ENGINE.changes_by_cycle(a["signal"], a["clock"], int(a["start_cycle"]), int(a["end_cycle"]), a.get("edge", "rise"), int(a.get("limit", 200))),
    ),
}


def tool_descriptions() -> list[dict[str, Any]]:
    result_schema = {"type": "object", "properties": {"ok": {"type": "boolean"}, "status": {"type": "string"}, "summary": {"type": "string"}, "data": {"type": "object"}, "confidence": {"type": "number"}, "limitations": {"type": "array", "items": {"type": "string"}}}, "required": ["ok", "status", "summary"]}
    return [{"name": name, "description": desc, "inputSchema": input_schema, "outputSchema": result_schema} for name, (desc, input_schema, _handler) in TOOLS.items()]


def to_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "structuredContent": result, "isError": not bool(result.get("ok"))}


def handle_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    if "id" not in msg:
        return None
    msg_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    try:
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "waveq", "version": __version__}, "instructions": SERVER_INSTRUCTIONS}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tool_descriptions()}}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name not in TOOLS:
                return {"jsonrpc": "2.0", "id": msg_id, "result": to_tool_result(fail("unknown_tool", f"unknown tool: {name}"))}
            try:
                result = TOOLS[name][2](arguments)
            except Exception as exc:
                result = fail("tool_exception", str(exc), {"traceback": traceback.format_exc(limit=5)})
            return {"jsonrpc": "2.0", "id": msg_id, "result": to_tool_result(result)}
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": str(exc)}}


def serve() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
            response = handle_message(msg)
        except json.JSONDecodeError as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, separators=(",", ":"), ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WaveQ MCP server over stdio")
    parser.add_argument("--version", action="version", version=f"waveq {__version__}")
    parser.parse_args(argv)
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
