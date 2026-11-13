# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ishaan Dugar
from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Any, Callable

from . import __version__
from .db import CLOCK_EDGE_KINDS, EDGE_KINDS
from .engine import CONFIGURE_INDEX_MODES, FIND_OPS, FIND_SAMPLES, WaveQEngine, fail

SERVER_INSTRUCTIONS = (
    "Use WaveQ to inspect VCD waveforms. First call configure_waveform. Large VCDs configure metadata only; call index_status and build_index before indexed queries. "
    "Resolve ambiguous names with resolve_signals. Prefer batch tools values_at, changes_many, and window_digest over repeated single-signal calls. "
    "Use find_times to answer when/whether a condition occurred (e.g. valid==1 and ready==1 at clk rising edges) instead of scanning changes. "
    "Add payload=[...] to find_times to get each match's data in the same call. "
    "For debugging: list_scopes to browse the hierarchy, x_report for where an X came from, activity for where a hung design went quiet, "
    "state_transitions for FSM behaviour, pair_events for request/response latency and requests that never completed. "
    "Use first_divergence to compare the configured waveform with a second VCD (passing vs failing run) and find where they first differ. "
    "Use define_clock before cycle-based queries. WaveQ reports observed waveform facts; it does not prove protocol correctness unless a tool explicitly checks a rule."
)

ENGINE = WaveQEngine()


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def _str(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


def _enum(desc: str, values: tuple[str, ...]) -> dict[str, Any]:
    return {"type": "string", "enum": list(values), "description": desc}


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


def _conditions(desc: str) -> dict[str, Any]:
    return {"type": "array", "minItems": 1, "description": desc, "items": _schema({
        "signal": _str("Signal full path or unique leaf"),
        "op": _enum("Comparison; default ==", FIND_OPS),
        "value": _str("x, z, decimal, 0x-hex, or 0b-binary. Numeric matches never include x/z values"),
    }, ["signal", "value"])}


TOOLS: dict[str, tuple[str, dict[str, Any], Callable[[dict[str, Any]], dict[str, Any]]]] = {
    "configure_waveform": (
        "Configure metadata/cache for a .vcd or .vcd.gz waveform. index=auto (default) fully indexes files under 10 MB and is metadata-only above; "
        "index=full makes any file query-ready in this one call; index=metadata never parses the dump body.",
        _schema({
            "vcd_path": _str("Path to .vcd or .vcd.gz"),
            "cache_dir": _str("Optional cache directory"),
            "rebuild": _bool("Force rebuild", False),
            "index": _enum("auto, full, or metadata; default auto", CONFIGURE_INDEX_MODES),
        }, ["vcd_path"]),
        lambda a: ENGINE.configure(a["vcd_path"], a.get("cache_dir"), bool(a.get("rebuild", False)), a.get("index", "auto")),
    ),
    "index_status": (
        "Check cache/index readiness without rebuilding and without fully parsing the VCD.",
        _schema({"vcd_path": _str("Optional path to .vcd or .vcd.gz"), "cache_dir": _str("Optional cache directory")}),
        lambda a: ENGINE.index_status(a.get("vcd_path"), a.get("cache_dir")),
    ),
    "build_index": (
        "Explicitly build expensive indexes. Levels: changes, runs, edges, full. Pass signals to index only those (much faster on big dumps); "
        "queries on other signals then fail with signal_not_indexed, and a later call with more signals grows the subset.",
        _schema({
            "vcd_path": _str("Optional path to .vcd or .vcd.gz"),
            "cache_dir": _str("Optional cache directory"),
            "level": _enum("Index level; default full", ("changes", "runs", "edges", "full")),
            "signals": _arr("Optional signals to index (names resolve like queries); omit for every signal"),
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
        _schema({"signal": _str("Signal full path or unique leaf"), "kind": _enum("Optional edge kind", EDGE_KINDS), "start": _str("Optional start"), "end": _str("Optional end"), "limit": _int("Max rows", 200)}, ["signal"]),
        lambda a: ENGINE.edges(a["signal"], a.get("kind"), a.get("start"), a.get("end"), int(a.get("limit", 200))),
    ),
    "first_edge_after": (
        "Return the first edge of a given kind after a time.",
        _schema({"signal": _str("Signal full path or unique leaf"), "kind": _enum("Edge kind", EDGE_KINDS), "time": _str("Reference time")}, ["signal", "kind", "time"]),
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
    "find_times": (
        "Find when all conditions hold (ANDed). Without clock: returns time intervals. With clock (call define_clock first): returns the "
        "cycles whose edge satisfies the conditions, sampled just before the edge by default (flip-flop semantics). "
        "No match is ok with status no_match. Use limit=1 for 'first time X happened'. "
        "payload signals are sampled at each match (cycle sample tick, or interval start) and returned per match as hex.",
        _schema({
            "conditions": _conditions("Conditions that must all hold"),
            "start": _str("Optional window start"),
            "end": _str("Optional window end"),
            "clock": _str("Optional clock signal; switches to per-cycle matching"),
            "edge": _enum("Clock edge; default rise", CLOCK_EDGE_KINDS),
            "sample": _enum("Sample just before the edge (default) or at the edge tick", FIND_SAMPLES),
            "limit": _int("Max matches", 100),
            "payload": _arr("Optional signals to sample at each match, e.g. address/data of a handshake"),
        }, ["conditions"]),
        lambda a: ENGINE.find_times(list(a["conditions"]), a.get("start"), a.get("end"), a.get("clock"), a.get("edge", "rise"), a.get("sample", "before"), int(a.get("limit", 100)), a.get("payload")),
    ),
    "define_clock": (
        "Build a cycle index from one signal's edge events. Call before cycle-based tools.",
        _schema({"signal": _str("Clock signal"), "edge": _enum("Clock edge; default rise", CLOCK_EDGE_KINDS)}, ["signal"]),
        lambda a: ENGINE.define_clock(a["signal"], a.get("edge", "rise")),
    ),
    "time_to_cycle": (
        "Map a time to the latest clock cycle at or before that time.",
        _schema({"clock": _str("Clock signal"), "time": _str("Query time"), "edge": _enum("Clock edge; default rise", CLOCK_EDGE_KINDS)}, ["clock", "time"]),
        lambda a: ENGINE.time_to_cycle(a["clock"], a["time"], a.get("edge", "rise")),
    ),
    "cycle_to_time": (
        "Map a clock cycle index to its VCD time.",
        _schema({"clock": _str("Clock signal"), "cycle_index": _int("Cycle index"), "edge": _enum("Clock edge; default rise", CLOCK_EDGE_KINDS)}, ["clock", "cycle_index"]),
        lambda a: ENGINE.cycle_to_time(a["clock"], int(a["cycle_index"]), a.get("edge", "rise")),
    ),
    "values_at_cycle": (
        "Batch values_at for several signals at a clock cycle. sample=at (default) reads at the edge tick (post-edge); "
        "sample=before reads just before the edge, matching what find_times sees for that cycle.",
        _schema({"signals": _arr("Signal full paths or unique leaf names"), "clock": _str("Clock signal"), "cycle_index": _int("Cycle index"), "edge": _enum("Clock edge; default rise", CLOCK_EDGE_KINDS), "sample": _enum("at (default) or before the edge", FIND_SAMPLES)}, ["signals", "clock", "cycle_index"]),
        lambda a: ENGINE.values_at_cycle(list(a["signals"]), a["clock"], int(a["cycle_index"]), a.get("edge", "rise"), a.get("sample", "at")),
    ),
    "changes_by_cycle": (
        "Return one signal's changes between two clock cycle indices.",
        _schema({"signal": _str("Signal full path or unique leaf"), "clock": _str("Clock signal"), "start_cycle": _int("Start cycle"), "end_cycle": _int("End cycle"), "edge": _enum("Clock edge; default rise", CLOCK_EDGE_KINDS), "limit": _int("Max rows", 200)}, ["signal", "clock", "start_cycle", "end_cycle"]),
        lambda a: ENGINE.changes_by_cycle(a["signal"], a["clock"], int(a["start_cycle"]), int(a["end_cycle"]), a.get("edge", "rise"), int(a.get("limit", 200))),
    ),
    "list_scopes": (
        "Browse the design hierarchy: scopes with direct and subtree signal counts, plus the signals of a single matched scope. "
        "Works right after configure_waveform, before any index build.",
        _schema({"prefix": _str("Optional scope path or trailing part of one, e.g. u_core or tb.u_core"), "max_depth": _int("Levels below each matched scope", 1), "limit": _int("Max scopes", 200)}),
        lambda a: ENGINE.list_scopes(a.get("prefix"), int(a.get("max_depth", 1)), int(a.get("limit", 200))),
    ),
    "x_report": (
        "List signals holding x/z at a time (default: end of simulation), earliest-to-go-unknown first, which usually points at the X source.",
        _schema({"time": _str("Optional time; default end of simulation"), "scope": _str("Optional scope (segment match, includes sub-scopes)"), "limit": _int("Max signals", 100)}),
        lambda a: ENGINE.x_report(a.get("time"), a.get("scope"), int(a.get("limit", 100))),
    ),
    "activity": (
        "Per-signal change counts and last-change times after start, plus idle signals. Use to find where a hung design went quiet; "
        "pass free-running clocks in ignore.",
        _schema({"start": _str("Optional window start (changes strictly after it count)"), "end": _str("Optional window end"), "scope": _str("Optional scope"), "ignore": _arr("Signals to exclude, e.g. clocks"), "limit": _int("Max signals per list", 50)}),
        lambda a: ENGINE.activity(a.get("start"), a.get("end"), a.get("scope"), a.get("ignore"), int(a.get("limit", 50))),
    ),
    "state_transitions": (
        "FSM-style summary of one signal: from->to transition counts with first/last times, and time spent in each value.",
        _schema({"signal": _str("Signal full path or unique leaf"), "start": _str("Optional start"), "end": _str("Optional end"), "limit": _int("Max rows per list", 50)}, ["signal"]),
        lambda a: ENGINE.state_transitions(a["signal"], a.get("start"), a.get("end"), int(a.get("limit", 50))),
    ),
    "pair_events": (
        "Pair request cycles with later response cycles on a defined clock (in order, or in order per ID) and report latency stats, "
        "unmatched requests (hangs, lost transactions), and orphan responses. Call define_clock first.",
        _schema({
            "clock": _str("Clock signal (define_clock first)"),
            "request": _conditions("Conditions marking a request cycle, e.g. arvalid==1, arready==1"),
            "response": _conditions("Conditions marking a response cycle, e.g. rvalid==1, rready==1, rlast==1"),
            "edge": _enum("Clock edge; default rise", CLOCK_EDGE_KINDS),
            "sample": _enum("Sample before (default) or at the edge", FIND_SAMPLES),
            "start": _str("Optional window start"),
            "end": _str("Optional window end"),
            "request_id": _str("Optional ID signal sampled on requests (e.g. arid)"),
            "response_id": _str("Optional ID signal sampled on responses (e.g. rid)"),
            "allow_same_cycle": _bool("Let a response complete a request from the same cycle", False),
            "limit": _int("Max rows per list", 100),
        }, ["clock", "request", "response"]),
        lambda a: ENGINE.pair_events(
            a["clock"], list(a["request"]), list(a["response"]), a.get("edge", "rise"), a.get("sample", "before"), a.get("start"), a.get("end"),
            a.get("request_id"), a.get("response_id"), bool(a.get("allow_same_cycle", False)), int(a.get("limit", 100)),
        ),
    ),
    "first_divergence": (
        "Compare the configured waveform (run A) with a second VCD (run B) and report the earliest time they differ and on which signals "
        "(e.g. passing vs failing seed, or two RTL revisions). Signals match by exact full path; both runs need the same timescale. "
        "No difference is ok with status no_match.",
        _schema({
            "other_vcd_path": _str("Path to the second .vcd or .vcd.gz"),
            "signals": _arr("Optional signals to compare (full paths or unique leaf names in run A)"),
            "scope": _str("Optional scope filter"),
            "start": _str("Optional window start"),
            "end": _str("Optional window end"),
            "other_cache_dir": _str("Optional cache directory for the second VCD; defaults to the first one's"),
            "limit": _int("Max diverging signals listed", 20),
        }, ["other_vcd_path"]),
        lambda a: ENGINE.first_divergence(a["other_vcd_path"], a.get("signals"), a.get("scope"), a.get("start"), a.get("end"), a.get("other_cache_dir"), int(a.get("limit", 20))),
    ),
    "define_clock": (
        "Build a cycle index from one signal's edge events. Call before cycle-based tools.",
        _schema({"signal": _str("Clock signal"), "edge": _enum("Clock edge; default rise", CLOCK_EDGE_KINDS)}, ["signal"]),
        lambda a: ENGINE.define_clock(a["signal"], a.get("edge", "rise")),
    ),
    "time_to_cycle": (
        "Map a time to the latest clock cycle at or before that time.",
        _schema({"clock": _str("Clock signal"), "time": _str("Query time"), "edge": _enum("Clock edge; default rise", CLOCK_EDGE_KINDS)}, ["clock", "time"]),
        lambda a: ENGINE.time_to_cycle(a["clock"], a["time"], a.get("edge", "rise")),
    ),
    "cycle_to_time": (
        "Map a clock cycle index to its VCD time.",
        _schema({"clock": _str("Clock signal"), "cycle_index": _int("Cycle index"), "edge": _enum("Clock edge; default rise", CLOCK_EDGE_KINDS)}, ["clock", "cycle_index"]),
        lambda a: ENGINE.cycle_to_time(a["clock"], int(a["cycle_index"]), a.get("edge", "rise")),
    ),
    "values_at_cycle": (
        "Batch values_at for several signals at a clock cycle.",
        _schema({"signals": _arr("Signal full paths or unique leaf names"), "clock": _str("Clock signal"), "cycle_index": _int("Cycle index"), "edge": _enum("Clock edge; default rise", CLOCK_EDGE_KINDS)}, ["signals", "clock", "cycle_index"]),
        lambda a: ENGINE.values_at_cycle(list(a["signals"]), a["clock"], int(a["cycle_index"]), a.get("edge", "rise")),
    ),
    "changes_by_cycle": (
        "Return one signal's changes between two clock cycle indices.",
        _schema({"signal": _str("Signal full path or unique leaf"), "clock": _str("Clock signal"), "start_cycle": _int("Start cycle"), "end_cycle": _int("End cycle"), "edge": _enum("Clock edge; default rise", CLOCK_EDGE_KINDS), "limit": _int("Max rows", 200)}, ["signal", "clock", "start_cycle", "end_cycle"]),
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
