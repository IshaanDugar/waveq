from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
import sqlite3
import time

from .db import (
    CLOCK_EDGE_KINDS,
    EDGE_KINDS,
    BuildStats,
    build_database,
    build_metadata_database,
    cache_path_for,
    cache_status,
    connect,
    rebuild_clock_edges,
    size_class_for_bytes,
)
from .timeparse import Timescale, parse_radius_to_ticks, parse_time_to_ticks


# Stay well under SQLite's historical 999 bound-parameter limit.
_SQL_CHUNK = 500

FIND_OPS = ("==", "!=")
FIND_SAMPLES = ("before", "at")
CONFIGURE_INDEX_MODES = ("auto", "full", "metadata")
_MAX_FIND_CONDITIONS = 16
_PAIR_MAX_EVENTS = 200000


def compile_condition(op: str, value: Any, width: int) -> Callable[[sqlite3.Row], bool]:
    """Build a predicate over a values_interned row for `signal <op> value`.

    value may be x / z (matches any value containing an x / z bit), decimal, 0x-hex, or
    0b-/b-binary. Numeric comparisons never match values containing x or z.
    """
    if op not in FIND_OPS:
        raise ValueError(f"op {op!r} is not one of: {', '.join(FIND_OPS)}")
    text = str(value).strip().lower().replace("_", "")
    if text in ("x", "z"):
        column = "has_x" if text == "x" else "has_z"

        def test(row: sqlite3.Row) -> bool:
            return bool(row[column])
    else:
        try:
            if text.startswith("0x"):
                target = int(text[2:], 16)
            elif text.startswith("0b"):
                target = int(text[2:], 2)
            elif text.startswith("b"):
                target = int(text[1:], 2)
            else:
                target = int(text, 10)
        except ValueError:
            raise ValueError(f"value {value!r} must be x, z, decimal, 0x-hex, or 0b/b-binary") from None
        if target < 0 or target >= (1 << width):
            raise ValueError(f"value {value!r} does not fit in {width} bit(s), so the condition can never match")

        def test(row: sqlite3.Row) -> bool:
            return row["int_value"] is not None and int(row["int_value"]) == target
    if op == "!=":
        return lambda row: not test(row)
    return test


def hex_of(int_value: Any, width: int) -> str | None:
    """0x-hex for a fully known value, zero-padded to the signal width; None when x/z/non-numeric."""
    if int_value is None:
        return None
    return f"0x{int(int_value):0{max(1, (width + 3) // 4)}x}"


def display_value(row: sqlite3.Row, width: int) -> str:
    """Compact exact value: scalars as 0/1/x/z, known vectors as 0x-hex, otherwise the raw VCD value."""
    if width > 1:
        h = hex_of(row["int_value"], width)
        if h is not None:
            return h
    return str(row["raw_value"])


def ok(status: str, summary: str, data: Any = None, *, confidence: float = 1.0, limitations: list[str] | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "status": status,
        "summary": summary,
        "data": data if data is not None else {},
        "confidence": confidence,
        "limitations": limitations or [],
    }


def fail(status: str, summary: str, data: Any = None, *, limitations: list[str] | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "status": status,
        "summary": summary,
        "data": data if data is not None else {},
        "confidence": 0.0,
        "limitations": limitations or [],
    }


@dataclass(frozen=True)
class SignalRef:
    signal_id: int
    full_path: str
    scope: str
    leaf: str
    kind: str
    width: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SignalRef":
        return cls(
            signal_id=int(row["signal_id"]),
            full_path=row["full_path"],
            scope=row["scope"],
            leaf=row["leaf"],
            kind=row["kind"],
            width=int(row["width"]),
        )


class WaveQEngine:
    def __init__(self) -> None:
        self.con: sqlite3.Connection | None = None
        self.db_path: Path | None = None
        self.vcd_path: Path | None = None
        self.cache_dir: str | None = None
        self.timescale: Timescale | None = None
        self.meta: dict[str, str] = {}
        # Per-cache memo: resolved names and defined clocks. Reset whenever the cache is (re)opened.
        self._sig_cache: dict[str, SignalRef] = {}
        self._clock_ok: set[tuple[int, str]] = set()

    def close(self) -> None:
        if self.con is not None:
            try:
                self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.con.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.Error:
                pass
            self.con.close()
        self.con = None
        self.db_path = None
        self.vcd_path = None
        self.cache_dir = None
        self.timescale = None
        self.meta = {}
        self._sig_cache = {}
        self._clock_ok = set()

    def _open_cache(self, db_path: Path, vcd_path: str, cache_dir: str | None = None) -> None:
        self.close()
        self.con = connect(db_path, readonly=False)
        self.db_path = db_path
        self.vcd_path = Path(vcd_path).expanduser().resolve()
        self.cache_dir = cache_dir
        self.meta = {r["key"]: r["value"] for r in self.con.execute("SELECT key, value FROM meta")}
        if self.meta.get("ready_metadata") == "1":
            self.timescale = Timescale(int(self.meta["timescale_number"]), self.meta["timescale_unit"])

    def _ready_levels(self) -> list[str]:
        return [level for level in ("metadata", "changes", "runs", "edges") if self.meta.get(f"ready_{level}") == "1"]

    def _query_ready(self) -> bool:
        levels = self._ready_levels()
        return "changes" in levels or "runs" in levels or "edges" in levels

    def _index_required(self, level: str) -> dict[str, Any] | None:
        self.require()
        if self.meta.get(f"ready_{level}") == "1":
            return None
        return fail(
            "index_required",
            f"{level} index is not ready; call build_index with level='{level}' or 'full'",
            {
                "required_level": level,
                "ready_levels": self._ready_levels(),
                "recommended_next_tool": "build_index",
                "recommended_build_level": level,
            },
        )

    def _size_data(self, path: Path | None = None) -> dict[str, Any]:
        p = path or self.vcd_path
        size = p.stat().st_size if p and p.exists() else 0
        return {"vcd_size_mb": round(size / (1024 * 1024), 3), "size_class": size_class_for_bytes(size)}

    def configure(self, vcd_path: str, cache_dir: str | None = None, rebuild: bool = False, index: str = "auto") -> dict[str, Any]:
        """Open (or build) the cache for a VCD.

        index="auto" fully indexes small files and does metadata only for larger ones; "full" always
        leaves the cache query-ready in one call; "metadata" never parses the dump body.
        """
        started = time.perf_counter()
        try:
            if index not in CONFIGURE_INDEX_MODES:
                return fail("bad_index_mode", f"index must be one of: {', '.join(CONFIGURE_INDEX_MODES)}", {"allowed": list(CONFIGURE_INDEX_MODES)})
            source = Path(vcd_path).expanduser().resolve()
            size_class = size_class_for_bytes(source.stat().st_size)
            prior = cache_status(source, cache_dir)
            want_full = index == "full" or (index == "auto" and size_class == "small")
            prior_ready = prior.get("ready_levels", [])
            if not rebuild and prior.get("cache_valid") and "metadata" in prior_ready and (
                not want_full or all(level in prior_ready for level in ("changes", "runs", "edges"))
            ):
                stats = BuildStats(
                    db_path=cache_path_for(source, cache_dir),
                    signal_count=int(prior.get("metadata", {}).get("signal_count", 0)),
                    change_count=int(prior.get("metadata", {}).get("change_count", 0)),
                    run_count=int(prior.get("metadata", {}).get("run_count", 0)),
                    edge_count=int(prior.get("metadata", {}).get("edge_count", 0)),
                    min_tick=int(prior.get("metadata", {}).get("min_tick", 0)),
                    max_tick=int(prior.get("metadata", {}).get("max_tick", 0)),
                    build_seconds=0.0,
                    source_size_bytes=source.stat().st_size,
                    size_class=size_class,
                    built_level="metadata",
                    reused=True,
                )
            elif want_full:
                self.close()
                stats = build_database(vcd_path, cache_dir, rebuild=rebuild, level="full")
            else:
                self.close()
                stats = build_metadata_database(vcd_path, cache_dir, rebuild=True)
            self._open_cache(stats.db_path, vcd_path, cache_dir)
            elapsed = time.perf_counter() - started
            data = self._info_data()
            data["db_path"] = str(stats.db_path)
            data["build_seconds"] = stats.build_seconds
            data["configure_seconds"] = elapsed
            data["status"] = self.meta.get("build_state", "unknown")
            data["size_class"] = stats.size_class
            data["vcd_size_mb"] = round(stats.source_size_bytes / (1024 * 1024), 3)
            data["query_ready"] = self._query_ready()
            data["index_levels_ready"] = self._ready_levels()
            data["cache_reused"] = stats.reused
            data["recommended_next_tool"] = None if data["query_ready"] else "build_index"
            data["recommended_build_level"] = None if data["query_ready"] else "full"
            summary = (
                f"Configured {self.vcd_path.name} metadata; indexes are not query-ready"
                if not data["query_ready"]
                else f"Loaded {self.vcd_path.name}: {stats.signal_count} signals, {stats.change_count} changes"
            )
            return ok("ok", summary, data)
        except Exception as exc:
            return fail("configure_failed", str(exc))

    def index_status(self, vcd_path: str | None = None, cache_dir: str | None = None) -> dict[str, Any]:
        try:
            target = Path(vcd_path).expanduser().resolve() if vcd_path else self.vcd_path
            if target is None:
                return fail("not_configured", "no waveform configured and no vcd_path supplied")
            status = cache_status(target, cache_dir if cache_dir is not None else self.cache_dir)
            return ok(str(status["status"]), f"Cache status is {status['status']}", status)
        except Exception as exc:
            return fail("index_status_failed", str(exc))

    def build_index(self, vcd_path: str | None = None, cache_dir: str | None = None, level: str = "full", signals: list[str] | None = None, rebuild: bool = False) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            if signals:
                return fail("unsupported", "signal-subset indexing is not supported yet; build the requested level for the whole VCD", {"signals": signals})
            target = str(Path(vcd_path).expanduser().resolve()) if vcd_path else (str(self.vcd_path) if self.vcd_path else None)
            use_cache_dir = cache_dir if cache_dir is not None else self.cache_dir
            if target is None:
                return fail("not_configured", "no waveform configured and no vcd_path supplied")
            self.close()
            stats = build_database(target, use_cache_dir, rebuild=rebuild, level=level)
            self._open_cache(stats.db_path, target, use_cache_dir)
            data = self._info_data()
            data.update({
                "db_path": str(stats.db_path),
                "requested_level": level,
                "built_level": stats.built_level,
                "elapsed_seconds": time.perf_counter() - started,
                "build_seconds": stats.build_seconds,
                "cache_reused": stats.reused,
                "index_levels_ready": self._ready_levels(),
                "query_ready": self._query_ready(),
                "signal_subset_indexing": "unsupported",
                "counts": {
                    "signals": stats.signal_count,
                    "changes": stats.change_count,
                    "runs": stats.run_count,
                    "edges": stats.edge_count,
                },
            })
            return ok("ok", f"Built {level} index for {Path(target).name}", data)
        except Exception as exc:
            return fail("build_index_failed", str(exc))

    def require(self) -> sqlite3.Connection:
        if self.con is None:
            raise RuntimeError("no waveform configured; call configure_waveform first")
        return self.con

    def _timescale(self) -> Timescale:
        if self.timescale is None:
            raise RuntimeError("no waveform configured; call configure_waveform first")
        return self.timescale

    def parse_tick(self, value: str | int) -> int:
        return parse_time_to_ticks(value, self._timescale())

    def format_tick(self, tick: int) -> str:
        return self._timescale().format_tick(tick)

    def _info_data(self) -> dict[str, Any]:
        size_data = self._size_data()
        return {
            "source_path": self.meta.get("source_path"),
            "timescale": f"{self.meta.get('timescale_number')}{self.meta.get('timescale_unit')}",
            "signal_count": int(self.meta.get("signal_count", 0)),
            "change_count": int(self.meta.get("change_count", 0)),
            "run_count": int(self.meta.get("run_count", 0)),
            "edge_count": int(self.meta.get("edge_count", 0)),
            "min_tick": int(self.meta.get("min_tick", 0)),
            "max_tick": int(self.meta.get("max_tick", 0)),
            "min_time": self.format_tick(int(self.meta.get("min_tick", 0))) if self.timescale else None,
            "max_time": self.format_tick(int(self.meta.get("max_tick", 0))) if self.timescale else None,
            "build_state": self.meta.get("build_state", "unknown"),
            "index_levels_ready": self._ready_levels(),
            "query_ready": self._query_ready(),
            **size_data,
        }

    def info(self) -> dict[str, Any]:
        try:
            self.require()
            data = self._info_data()
            data["db_path"] = str(self.db_path) if self.db_path else None
            return ok("ok", "Waveform is configured", data)
        except Exception as exc:
            return fail("not_configured", str(exc))

    def resolve_signals(self, query: str, scope_hint: str | None = None, limit: int = 10) -> dict[str, Any]:
        try:
            con = self.require()
            q = (query or "").strip().lower()
            if not q:
                return fail("bad_query", "query must be non-empty")
            limit = max(1, min(int(limit), 100))
            scope_lc = (scope_hint or "").strip().lower()
            tokens = [t for t in q.replace("/", ".").replace("_", " ").replace(".", " ").split() if t]
            candidates: dict[int, tuple[float, str, sqlite3.Row]] = {}

            def add_rows(rows: Iterable[sqlite3.Row], base: float, reason: str) -> None:
                for r in rows:
                    score = base
                    full = r["full_path_lc"]
                    leaf = r["leaf_lc"]
                    scope = r["scope_lc"]
                    if scope_lc and scope_lc in scope:
                        score += 10
                    if all(tok in full for tok in tokens):
                        score += 8
                    if q == full:
                        score += 40
                    elif q == leaf:
                        score += 30
                    elif scope and q == f"{scope}.{leaf}":
                        score += 30
                    elif full.endswith("." + q):
                        score += 22
                    prev = candidates.get(int(r["signal_id"]))
                    if prev is None or score > prev[0]:
                        candidates[int(r["signal_id"])] = (score, reason, r)

            like = f"%{q}%"
            add_rows(
                con.execute(
                    "SELECT * FROM signals WHERE full_path_lc=? OR leaf_lc=? LIMIT 200",
                    (q, q),
                ),
                80,
                "exact full-path or leaf match",
            )
            add_rows(
                con.execute(
                    "SELECT * FROM signals WHERE full_path_lc LIKE ? OR leaf_lc LIKE ? LIMIT 500",
                    (like, like),
                ),
                45,
                "substring match",
            )
            if tokens:
                where = " AND ".join(["full_path_lc LIKE ?" for _ in tokens])
                args = [f"%{tok}%" for tok in tokens]
                add_rows(
                    con.execute(f"SELECT * FROM signals WHERE {where} LIMIT 500", args),
                    55,
                    "all query tokens appear in full path",
                )
            ranked = sorted(candidates.values(), key=lambda x: (-x[0], x[2]["full_path"]))[:limit]
            matches = [
                {
                    "signal_id": int(r["signal_id"]),
                    "full_path": r["full_path"],
                    "scope": r["scope"],
                    "leaf": r["leaf"],
                    "kind": r["kind"],
                    "width": int(r["width"]),
                    "score": round(min(score / 120.0, 1.0), 3),
                    "reason": reason,
                }
                for score, reason, r in ranked
            ]
            if not matches:
                return fail("not_found", f"No signal matched {query!r}", {"query": query})
            return ok("ok", f"Found {len(matches)} candidate signal(s)", {"query": query, "matches": matches})
        except Exception as exc:
            return fail("resolve_failed", str(exc))

    def _get_signal_exact_rows(self, name: str) -> list[sqlite3.Row]:
        con = self.require()
        q = name.strip().lower()
        # scope + leaf lets "tb.u_apb.paddr" find the vector "tb.u_apb.paddr [7:0]".
        scope_q, _, leaf_q = q.rpartition(".")
        rows = list(con.execute(
            "SELECT * FROM signals WHERE full_path_lc=? OR leaf_lc=? OR (scope_lc=? AND leaf_lc=?) ORDER BY full_path",
            (q, q, scope_q, leaf_q),
        ))
        if not rows and "[" in q:
            # Tolerate range spacing differences: "data[7:0]" vs "data [7:0]". Fallback only (full scan).
            rows = list(con.execute(
                "SELECT * FROM signals WHERE REPLACE(full_path_lc, ' ', '')=? ORDER BY full_path",
                (q.replace(" ", ""),),
            ))
        return rows

    def get_signal(self, name: str) -> SignalRef | dict[str, Any]:
        key = name.strip().lower()
        cached = self._sig_cache.get(key)
        if cached is not None:
            return cached
        sig = self._lookup_signal(name)
        if isinstance(sig, SignalRef):
            self._sig_cache[key] = sig
        return sig

    def _lookup_signal(self, name: str) -> SignalRef | dict[str, Any]:
        rows = self._get_signal_exact_rows(name)
        if len(rows) == 1:
            return SignalRef.from_row(rows[0])
        if len(rows) > 1:
            q = name.strip().lower()
            exact_full = [r for r in rows if r["full_path_lc"] == q]
            if len(exact_full) == 1:
                return SignalRef.from_row(exact_full[0])
            scoped_leaf = [r for r in rows if f"{r['scope_lc']}.{r['leaf_lc']}" == q]
            if len(scoped_leaf) == 1:
                return SignalRef.from_row(scoped_leaf[0])
            return fail(
                "ambiguous_signal",
                f"Signal name {name!r} is ambiguous; use a full path",
                {"candidates": [{"full_path": r["full_path"], "width": int(r["width"])} for r in rows[:20]]},
            )
        resolved = self.resolve_signals(name, limit=5)
        if not resolved.get("ok"):
            return resolved
        matches = resolved["data"]["matches"]
        if matches and matches[0]["score"] >= 0.95:
            row = self.require().execute("SELECT * FROM signals WHERE signal_id=?", (matches[0]["signal_id"],)).fetchone()
            if row:
                return SignalRef.from_row(row)
        return fail("not_found", f"No exact signal matched {name!r}; call resolve_signals first", {"matches": matches})

    def _check_tick_range(self, tick: int) -> dict[str, Any] | None:
        max_tick = int(self.meta.get("max_tick", 0))
        min_tick = 0
        if tick < min_tick or tick > max_tick:
            return fail(
                "time_out_of_range",
                f"time {self.format_tick(tick)} is outside waveform range {self.format_tick(min_tick)}..{self.format_tick(max_tick)}",
                {"tick": tick, "min_tick": min_tick, "max_tick": max_tick},
            )
        return None

    def _check_window(self, a: int, b: int) -> dict[str, Any] | None:
        if b < a:
            return fail("bad_range", "end must be >= start")
        return self._check_tick_range(a) or self._check_tick_range(b)

    def _check_edge_kind(self, kind: str, allowed: tuple[str, ...]) -> dict[str, Any] | None:
        if kind in allowed:
            return None
        return fail("bad_edge_kind", f"edge kind {kind!r} is not one of: {', '.join(allowed)}", {"allowed": list(allowed)})

    def _clock_cycle_count(self, sig: SignalRef, edge: str) -> int:
        return int(self.require().execute(
            "SELECT COUNT(*) FROM clock_edges WHERE clock_signal_id=? AND edge_kind=?", (sig.signal_id, edge)
        ).fetchone()[0])

    def _require_clock(self, clock: str, edge: str) -> SignalRef | dict[str, Any]:
        bad = self._check_edge_kind(edge, CLOCK_EDGE_KINDS)
        if bad:
            return bad
        sig = self.get_signal(clock)
        if isinstance(sig, dict):
            return sig
        if (sig.signal_id, edge) in self._clock_ok:
            return sig
        if self.require().execute(
            "SELECT 1 FROM clock_edges WHERE clock_signal_id=? AND edge_kind=? LIMIT 1", (sig.signal_id, edge)
        ).fetchone() is None:
            return fail("clock_not_defined", f"{sig.full_path} has no {edge}-edge cycle index; call define_clock first", {"clock": sig.full_path, "edge": edge})
        self._clock_ok.add((sig.signal_id, edge))
        return sig

    def value_at(self, signal: str, time_value: str | int) -> dict[str, Any]:
        try:
            need = self._index_required("runs")
            if need:
                return need
            sig = self.get_signal(signal)
            if isinstance(sig, dict):
                return sig
            tick = self.parse_tick(time_value)
            bad = self._check_tick_range(tick)
            if bad:
                return bad
            row = self._held_rows([sig.signal_id], tick).get(sig.signal_id)
            if not row:
                return fail("not_found", f"No value recorded for {sig.full_path} at {self.format_tick(tick)}")
            return ok("ok", f"{sig.full_path}={row['raw_value']} at {self.format_tick(tick)}", self._value_data(sig, tick, row))
        except Exception as exc:
            return fail("value_at_failed", str(exc))

    def _held_rows(self, signal_ids: Iterable[int], tick: int) -> dict[int, sqlite3.Row]:
        """Held value for many signals at one tick, one indexed seek per signal in a single query."""
        ids = list(dict.fromkeys(signal_ids))
        out: dict[int, sqlite3.Row] = {}
        con = self.require()
        for i in range(0, len(ids), _SQL_CHUNK):
            chunk = ids[i:i + _SQL_CHUNK]
            rows = con.execute(
                f"""
                SELECT s.signal_id, v.raw_value, v.has_x, v.has_z, v.int_value, r.start_tick, r.end_tick
                FROM signals s
                JOIN signal_runs r ON r.signal_id=s.signal_id AND r.start_tick=(
                    SELECT start_tick FROM signal_runs
                    WHERE signal_id=s.signal_id AND start_tick<=? ORDER BY start_tick DESC LIMIT 1)
                JOIN values_interned v ON v.value_id=r.value_id
                WHERE s.signal_id IN ({",".join("?" * len(chunk))}) AND r.end_tick>?
                """,
                (tick, *chunk, tick),
            )
            out.update((int(r["signal_id"]), r) for r in rows)
        return out

    def _value_data(self, sig: SignalRef, tick: int, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "signal": sig.full_path,
            "tick": tick,
            "time": self.format_tick(tick),
            "value": row["raw_value"],
            "int_value": row["int_value"],
            "hex": hex_of(row["int_value"], sig.width) if sig.width > 1 else None,
            "has_x": bool(row["has_x"]),
            "has_z": bool(row["has_z"]),
            "held_from_tick": int(row["start_tick"]),
            "held_until_tick_exclusive": int(row["end_tick"]),
            "held_from_time": self.format_tick(int(row["start_tick"])),
            "held_until_time_exclusive": self.format_tick(int(row["end_tick"])),
        }

    def values_at(self, signals: list[str], time_value: str | int) -> dict[str, Any]:
        try:
            need = self._index_required("runs")
            if need:
                return need
            if not signals:
                return fail("bad_query", "signals must be non-empty")
            tick = self.parse_tick(time_value)
            bad = self._check_tick_range(tick)
            if bad:
                return bad
            values = []
            errors = []
            resolved: list[tuple[str, SignalRef]] = []
            for name in signals:
                sig = self.get_signal(name)
                if isinstance(sig, dict):
                    errors.append({"signal": name, "status": sig.get("status"), "summary": sig.get("summary"), "data": sig.get("data")})
                else:
                    resolved.append((name, sig))
            held = self._held_rows((sig.signal_id for _, sig in resolved), tick)
            for name, sig in resolved:
                row = held.get(sig.signal_id)
                if row is None:
                    errors.append({"signal": name, "status": "not_found", "summary": f"No value recorded for {sig.full_path} at {self.format_tick(tick)}", "data": {}})
                else:
                    values.append(self._value_data(sig, tick, row))
            if errors:
                return fail("partial_failure", "One or more signal lookups failed", {"tick": tick, "time": self.format_tick(tick), "values": values, "errors": errors})
            return ok("ok", f"Read {len(values)} signal value(s) at {self.format_tick(tick)}", {"tick": tick, "time": self.format_tick(tick), "values": values})
        except Exception as exc:
            return fail("values_at_failed", str(exc))

    def changes(self, signal: str, start: str | int, end: str | int, limit: int = 200) -> dict[str, Any]:
        try:
            need = self._index_required("changes")
            if need:
                return need
            sig = self.get_signal(signal)
            if isinstance(sig, dict):
                return sig
            a = self.parse_tick(start)
            b = self.parse_tick(end)
            bad = self._check_window(a, b)
            if bad:
                return bad
            limit = max(1, min(int(limit), 10000))
            rows = list(self.require().execute(
                """
                SELECT c.tick, c.ordinal, v.raw_value, v.has_x, v.has_z, v.int_value
                FROM changes c JOIN values_interned v ON v.value_id=c.value_id
                WHERE c.signal_id=? AND c.tick BETWEEN ? AND ?
                ORDER BY c.tick, c.ordinal LIMIT ?
                """,
                (sig.signal_id, a, b, limit + 1),
            ))
            truncated = len(rows) > limit
            rows = rows[:limit]
            data = {
                "signal": sig.full_path,
                "start_tick": a,
                "end_tick": b,
                "start_time": self.format_tick(a),
                "end_time": self.format_tick(b),
                "truncated": truncated,
                "changes": [
                    {
                        "tick": int(r["tick"]),
                        "time": self.format_tick(int(r["tick"])),
                        "value": r["raw_value"],
                        "int_value": r["int_value"],
                        "has_x": bool(r["has_x"]),
                        "has_z": bool(r["has_z"]),
                    }
                    for r in rows
                ],
            }
            return ok("ok", f"Found {len(rows)} change(s) for {sig.full_path}", data, limitations=["result truncated"] if truncated else [])
        except Exception as exc:
            return fail("changes_failed", str(exc))

    def changes_many(self, signals: list[str], start: str | int, end: str | int, limit_per_signal: int = 200) -> dict[str, Any]:
        if not signals:
            return fail("bad_query", "signals must be non-empty")
        out = []
        errors = []
        for s in signals:
            r = self.changes(s, start, end, limit_per_signal)
            if r.get("ok"):
                out.append(r["data"])
            else:
                errors.append({"signal": s, "status": r.get("status"), "summary": r.get("summary"), "data": r.get("data")})
        if errors:
            return fail("partial_failure", "One or more signal change queries failed", {"results": out, "errors": errors})
        return ok("ok", f"Queried changes for {len(out)} signal(s)", {"results": out})

    def edges(self, signal: str, kind: str | None = None, start: str | int | None = None, end: str | int | None = None, limit: int = 200) -> dict[str, Any]:
        try:
            need = self._index_required("edges")
            if need:
                return need
            if kind:
                bad = self._check_edge_kind(kind, EDGE_KINDS)
                if bad:
                    return bad
            sig = self.get_signal(signal)
            if isinstance(sig, dict):
                return sig
            a = self.parse_tick(start) if start is not None else 0
            b = self.parse_tick(end) if end is not None else int(self.meta.get("max_tick", 0))
            bad = self._check_window(a, b)
            if bad:
                return bad
            limit = max(1, min(int(limit), 10000))
            args: list[Any] = [sig.signal_id, a, b]
            where = "e.signal_id=? AND e.tick BETWEEN ? AND ?"
            if kind:
                where += " AND e.edge_kind=?"
                args.append(kind)
            args.append(limit + 1)
            rows = list(self.require().execute(
                f"""
                SELECT e.tick, e.edge_kind, vf.raw_value AS from_value, vt.raw_value AS to_value
                FROM signal_edges e
                JOIN values_interned vf ON vf.value_id=e.from_value_id
                JOIN values_interned vt ON vt.value_id=e.to_value_id
                WHERE {where}
                ORDER BY e.tick LIMIT ?
                """,
                args,
            ))
            truncated = len(rows) > limit
            rows = rows[:limit]
            data = {
                "signal": sig.full_path,
                "kind": kind,
                "start_tick": a,
                "end_tick": b,
                "edges": [
                    {
                        "tick": int(r["tick"]),
                        "time": self.format_tick(int(r["tick"])),
                        "kind": r["edge_kind"],
                        "from": r["from_value"],
                        "to": r["to_value"],
                    }
                    for r in rows
                ],
                "truncated": truncated,
            }
            return ok("ok", f"Found {len(rows)} edge(s) for {sig.full_path}", data, limitations=["result truncated"] if truncated else [])
        except Exception as exc:
            return fail("edges_failed", str(exc))

    def first_edge_after(self, signal: str, kind: str, time_value: str | int) -> dict[str, Any]:
        try:
            need = self._index_required("edges")
            if need:
                return need
            bad = self._check_edge_kind(kind, EDGE_KINDS)
            if bad:
                return bad
            sig = self.get_signal(signal)
            if isinstance(sig, dict):
                return sig
            tick = self.parse_tick(time_value)
            bad = self._check_tick_range(tick)
            if bad:
                return bad
            row = self.require().execute(
                """
                SELECT e.tick, e.edge_kind, vf.raw_value AS from_value, vt.raw_value AS to_value
                FROM signal_edges e
                JOIN values_interned vf ON vf.value_id=e.from_value_id
                JOIN values_interned vt ON vt.value_id=e.to_value_id
                WHERE e.signal_id=? AND e.edge_kind=? AND e.tick>?
                ORDER BY e.tick LIMIT 1
                """,
                (sig.signal_id, kind, tick),
            ).fetchone()
            if not row:
                return fail("not_found", f"No {kind} edge found for {sig.full_path} after {self.format_tick(tick)}")
            data = {
                "signal": sig.full_path,
                "kind": row["edge_kind"],
                "tick": int(row["tick"]),
                "time": self.format_tick(int(row["tick"])),
                "from": row["from_value"],
                "to": row["to_value"],
            }
            return ok("ok", f"First {kind} edge for {sig.full_path} after {self.format_tick(tick)} is at {data['time']}", data)
        except Exception as exc:
            return fail("first_edge_after_failed", str(exc))

    def stable_between(self, signal: str, start: str | int, end: str | int) -> dict[str, Any]:
        try:
            need = self._index_required("runs")
            if need:
                return need
            need = self._index_required("changes")
            if need:
                return need
            sig = self.get_signal(signal)
            if isinstance(sig, dict):
                return sig
            a = self.parse_tick(start)
            b = self.parse_tick(end)
            bad = self._check_window(a, b)
            if bad:
                return bad
            rows = list(self.require().execute(
                """
                SELECT DISTINCT v.raw_value
                FROM signal_runs r JOIN values_interned v ON v.value_id=r.value_id
                WHERE r.signal_id=? AND r.start_tick<=? AND r.end_tick>?
                """,
                (sig.signal_id, b, a),
            ))
            changes = int(self.require().execute(
                "SELECT COUNT(*) FROM changes WHERE signal_id=? AND tick>? AND tick<=?",
                (sig.signal_id, a, b),
            ).fetchone()[0])
            stable = changes == 0
            vals = [r["raw_value"] for r in rows]
            return ok(
                "ok",
                f"{sig.full_path} is {'stable' if stable else 'not stable'} between {self.format_tick(a)} and {self.format_tick(b)}",
                {"signal": sig.full_path, "start_tick": a, "end_tick": b, "stable": stable, "change_count": changes, "values_seen": vals},
            )
        except Exception as exc:
            return fail("stable_between_failed", str(exc))

    def window_digest(self, center_time: str | int, radius: str | int, signals: list[str] | None = None, scope: str | None = None, max_signals: int = 30) -> dict[str, Any]:
        try:
            for level in ("changes", "runs", "edges"):
                need = self._index_required(level)
                if need:
                    return need
            con = self.require()
            center = self.parse_tick(center_time)
            bad = self._check_tick_range(center)
            if bad:
                return bad
            rad = parse_radius_to_ticks(radius, self._timescale())
            a = max(0, center - rad)
            b = min(int(self.meta.get("max_tick", 0)), center + rad)
            max_signals = max(1, min(int(max_signals), 200))
            sigs: list[SignalRef] = []
            errors = []
            if signals:
                for name in signals:
                    sig = self.get_signal(name)
                    if isinstance(sig, dict):
                        errors.append({"signal": name, "status": sig.get("status"), "summary": sig.get("summary")})
                    else:
                        sigs.append(sig)
            else:
                args: list[Any] = [a, b]
                scope_clause = ""
                if scope:
                    scope_clause = " AND s.scope_lc LIKE ?"
                    args.append(f"%{scope.lower()}%")
                args.append(max_signals)
                rows = con.execute(
                    f"""
                    SELECT c.signal_id, s.full_path, s.scope, s.leaf, s.kind, s.width,
                           COUNT(*) AS n, MIN(c.tick) AS first_tick, MAX(c.tick) AS last_tick
                    FROM changes c JOIN signals s ON s.signal_id=c.signal_id
                    WHERE c.tick BETWEEN ? AND ? {scope_clause}
                    GROUP BY c.signal_id
                    ORDER BY n DESC, first_tick ASC
                    LIMIT ?
                    """,
                    args,
                )
                sigs = [SignalRef.from_row(r) for r in rows]
            # Dedupe while keeping order, then gather everything with a fixed number of batched queries.
            sigs = list({s.signal_id: s for s in sigs}.values())[:max_signals]
            ids = [s.signal_id for s in sigs]
            start_rows = self._held_rows(ids, a)
            end_rows = self._held_rows(ids, b)
            change_stats: dict[int, sqlite3.Row] = {}
            edge_counts: dict[int, int] = {}
            for i in range(0, len(ids), _SQL_CHUNK):
                chunk = ids[i:i + _SQL_CHUNK]
                marks = ",".join("?" * len(chunk))
                change_stats.update((int(r["signal_id"]), r) for r in con.execute(
                    f"""
                    SELECT signal_id, COUNT(*) AS n, MIN(tick) AS first_tick, MAX(tick) AS last_tick
                    FROM changes WHERE signal_id IN ({marks}) AND tick BETWEEN ? AND ? GROUP BY signal_id
                    """,
                    (*chunk, a, b),
                ))
                edge_counts.update((int(r[0]), int(r[1])) for r in con.execute(
                    f"SELECT signal_id, COUNT(*) FROM signal_edges WHERE signal_id IN ({marks}) AND tick BETWEEN ? AND ? GROUP BY signal_id",
                    (*chunk, a, b),
                ))
            summaries = []
            for sig in sigs:
                stats = change_stats.get(sig.signal_id)
                first_tick = int(stats["first_tick"]) if stats else None
                last_tick = int(stats["last_tick"]) if stats else None
                start_row = start_rows.get(sig.signal_id)
                end_row = end_rows.get(sig.signal_id)
                summaries.append(
                    {
                        "signal": sig.full_path,
                        "width": sig.width,
                        "change_count": int(stats["n"]) if stats else 0,
                        "edge_count": edge_counts.get(sig.signal_id, 0),
                        "first_change_tick": first_tick,
                        "first_change_time": self.format_tick(first_tick) if first_tick is not None else None,
                        "last_change_tick": last_tick,
                        "last_change_time": self.format_tick(last_tick) if last_tick is not None else None,
                        "start_value": start_row["raw_value"] if start_row else None,
                        "end_value": end_row["raw_value"] if end_row else None,
                    }
                )
            data = {
                "center_tick": center,
                "center_time": self.format_tick(center),
                "radius_ticks": rad,
                "start_tick": a,
                "end_tick": b,
                "start_time": self.format_tick(a),
                "end_time": self.format_tick(b),
                "signals": summaries,
                "errors": errors,
                "suggested_next_queries": [
                    "values_at(signals, center_time)",
                    "changes_many(signals, start_time, end_time)",
                    "edges(signal, kind, start_time, end_time)",
                ],
            }
            if errors:
                return fail("partial_failure", "Window digest completed with signal resolution errors", data)
            return ok("ok", f"Window digest for {len(summaries)} signal(s) from {self.format_tick(a)} to {self.format_tick(b)}", data)
        except Exception as exc:
            return fail("window_digest_failed", str(exc))

    def define_clock(self, signal: str, edge: str = "rise") -> dict[str, Any]:
        try:
            need = self._index_required("edges")
            if need:
                return need
            edge = edge or "rise"
            bad = self._check_edge_kind(edge, CLOCK_EDGE_KINDS)
            if bad:
                return bad
            sig = self.get_signal(signal)
            if isinstance(sig, dict):
                return sig
            con = self.require()
            available = con.execute(
                "SELECT 1 FROM signal_edges WHERE signal_id=? AND edge_kind=? LIMIT 1", (sig.signal_id, edge)
            ).fetchone()
            if available is None:
                return fail("no_clock_edges", f"{sig.full_path} has no {edge} edges; it cannot be used as a {edge}-edge clock", {"clock": sig.full_path, "edge": edge})
            cycles = rebuild_clock_edges(con, sig.signal_id, edge)
            con.commit()
            self._clock_ok.add((sig.signal_id, edge))
            return ok("ok", f"Defined clock {sig.full_path} on {edge} edges with {cycles} cycle(s)", {"clock": sig.full_path, "edge": edge, "cycles": cycles})
        except Exception as exc:
            return fail("define_clock_failed", str(exc))

    def time_to_cycle(self, clock: str, time_value: str | int, edge: str = "rise") -> dict[str, Any]:
        try:
            sig = self._require_clock(clock, edge)
            if isinstance(sig, dict):
                return sig
            tick = self.parse_tick(time_value)
            bad = self._check_tick_range(tick)
            if bad:
                return bad
            row = self.require().execute(
                "SELECT cycle_index, tick FROM clock_edges WHERE clock_signal_id=? AND edge_kind=? AND tick<=? ORDER BY tick DESC LIMIT 1",
                (sig.signal_id, edge, tick),
            ).fetchone()
            if not row:
                return fail("before_first_cycle", f"{self.format_tick(tick)} is before the first {edge} edge of {sig.full_path}")
            data = {"clock": sig.full_path, "edge": edge, "query_tick": tick, "query_time": self.format_tick(tick), "cycle_index": int(row["cycle_index"]), "cycle_tick": int(row["tick"]), "cycle_time": self.format_tick(int(row["tick"]))}
            return ok("ok", f"{self.format_tick(tick)} maps to cycle {data['cycle_index']} of {sig.full_path}", data)
        except Exception as exc:
            return fail("time_to_cycle_failed", str(exc))

    def cycle_to_time(self, clock: str, cycle_index: int, edge: str = "rise") -> dict[str, Any]:
        try:
            sig = self._require_clock(clock, edge)
            if isinstance(sig, dict):
                return sig
            row = self.require().execute(
                "SELECT tick FROM clock_edges WHERE clock_signal_id=? AND edge_kind=? AND cycle_index=?",
                (sig.signal_id, edge, int(cycle_index)),
            ).fetchone()
            if not row:
                cycles = self._clock_cycle_count(sig, edge)
                return fail("cycle_out_of_range", f"cycle {cycle_index} is outside 0..{cycles - 1} for {sig.full_path} ({edge} edges)", {"cycle_index": int(cycle_index), "cycles": cycles})
            tick = int(row["tick"])
            return ok("ok", f"Cycle {cycle_index} of {sig.full_path} is at {self.format_tick(tick)}", {"clock": sig.full_path, "edge": edge, "cycle_index": int(cycle_index), "tick": tick, "time": self.format_tick(tick)})
        except Exception as exc:
            return fail("cycle_to_time_failed", str(exc))

    def values_at_cycle(self, signals: list[str], clock: str, cycle_index: int, edge: str = "rise", sample: str = "at") -> dict[str, Any]:
        """Values at a clock cycle. sample="at" reads at the edge tick (post-edge, the historical
        default); sample="before" reads one tick earlier, the flip-flop view find_times uses."""
        if sample not in FIND_SAMPLES:
            return fail("bad_sample", f"sample must be one of: {', '.join(FIND_SAMPLES)}", {"allowed": list(FIND_SAMPLES)})
        cyc = self.cycle_to_time(clock, cycle_index, edge=edge)
        if not cyc.get("ok"):
            return cyc
        tick = int(cyc["data"]["tick"]) - (1 if sample == "before" else 0)
        if tick < 0:
            return fail("no_pre_edge_sample", f"cycle {cycle_index} is at tick 0, so there is no value just before its edge", {"cycle_index": int(cycle_index)})
        result = self.values_at(signals, tick)
        result.setdefault("data", {}).update({"clock": cyc["data"]["clock"], "edge": edge, "cycle_index": int(cycle_index), "edge_tick": int(cyc["data"]["tick"]), "sample": sample})
        return result

    def changes_by_cycle(self, signal: str, clock: str, start_cycle: int, end_cycle: int, edge: str = "rise", limit: int = 200) -> dict[str, Any]:
        a = self.cycle_to_time(clock, start_cycle, edge=edge)
        if not a.get("ok"):
            return a
        b = self.cycle_to_time(clock, end_cycle, edge=edge)
        if not b.get("ok"):
            return b
        return self.changes(signal, int(a["data"]["tick"]), int(b["data"]["tick"]), limit=limit)

    def _run_cursor(self, signal_id: int, a: int, b: int) -> sqlite3.Cursor:
        """Runs of one signal overlapping [a, b], in order, starting with the run that holds at a."""
        con = self.require()
        first = con.execute(
            "SELECT start_tick FROM signal_runs WHERE signal_id=? AND start_tick<=? ORDER BY start_tick DESC LIMIT 1",
            (signal_id, a),
        ).fetchone()
        lo = int(first[0]) if first else a
        return con.execute(
            """
            SELECT r.end_tick, r.value_id, v.raw_value, v.has_x, v.has_z, v.int_value
            FROM signal_runs r JOIN values_interned v ON v.value_id=r.value_id
            WHERE r.signal_id=? AND r.start_tick BETWEEN ? AND ? ORDER BY r.start_tick
            """,
            (signal_id, lo, b),
        )

    def _segments(self, signal_ids: list[int], a: int, b: int) -> Iterator[tuple[int, int, tuple[sqlite3.Row, ...]]]:
        """Yield (start, end_exclusive, rows) tiling [a, b] so no listed signal changes inside a segment.

        Streams each signal's runs (contiguous by construction), so callers can stop early.
        """
        cursors = [self._run_cursor(sid, a, b) for sid in signal_ids]
        current = [cur.fetchone() for cur in cursors]
        if any(row is None for row in current):
            raise RuntimeError("signal run data does not cover the requested window")
        t = a
        while t <= b:
            end = min(min(int(row["end_tick"]) for row in current), b + 1)
            yield t, end, tuple(current)
            if end > b:
                return
            for i, row in enumerate(current):
                if int(row["end_tick"]) == end:
                    nxt = cursors[i].fetchone()
                    if nxt is None:
                        raise RuntimeError("signal run data ended before the requested window")
                    current[i] = nxt
            t = end

    def _sampler(self, signal_ids: list[int], a: int, b: int) -> Callable[[int], tuple[sqlite3.Row, ...]]:
        """Return at(tick) giving each signal's held row; ticks must be non-decreasing within [a, b].

        Walks the runs forward once, so sampling many increasing ticks costs one pass, not one query each.
        """
        segments = self._segments(signal_ids, a, b)
        state = list(next(segments))

        def at(tick: int) -> tuple[sqlite3.Row, ...]:
            while state[1] <= tick:
                state[:] = next(segments)
            return state[2]
        return at

    def _resolve_payload(self, payload: list[str] | None) -> tuple[list[SignalRef], list[dict[str, Any]]]:
        sigs: list[SignalRef] = []
        errors: list[dict[str, Any]] = []
        for name in payload or []:
            sig = self.get_signal(str(name))
            if isinstance(sig, dict):
                errors.append({"signal": name, "status": sig.get("status"), "summary": sig.get("summary"), "data": sig.get("data")})
            elif all(s.signal_id != sig.signal_id for s in sigs):
                sigs.append(sig)
        return sigs, errors

    def find_times(
        self,
        conditions: list[dict[str, Any]],
        start: str | int | None = None,
        end: str | int | None = None,
        clock: str | None = None,
        edge: str = "rise",
        sample: str = "before",
        limit: int = 100,
        payload: list[str] | None = None,
    ) -> dict[str, Any]:
        """Find when all conditions hold: as time intervals, or as clock cycles when a clock is given.

        payload signals are sampled at each match (the interval start, or the cycle's sample tick)
        and returned per match, so "every handshake and its data" is one call.
        """
        return self._find_times(conditions, start, end, clock, edge, sample, limit, payload, max_limit=10000)

    def _find_times(
        self,
        conditions: list[dict[str, Any]],
        start: str | int | None,
        end: str | int | None,
        clock: str | None,
        edge: str,
        sample: str,
        limit: int,
        payload: list[str] | None,
        *,
        max_limit: int,
    ) -> dict[str, Any]:
        try:
            need = self._index_required("runs")
            if need:
                return need
            if not conditions:
                return fail("bad_query", "conditions must be non-empty")
            if len(conditions) > _MAX_FIND_CONDITIONS:
                return fail("bad_query", f"at most {_MAX_FIND_CONDITIONS} conditions are supported")
            sigs: list[SignalRef] = []
            tests: list[Callable[[sqlite3.Row], bool]] = []
            described: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []
            for cond in conditions:
                if not isinstance(cond, dict) or "signal" not in cond or "value" not in cond:
                    return fail("bad_condition", "each condition needs 'signal' and 'value' (and optional 'op')", {"condition": cond})
                op = str(cond.get("op") or "==")
                sig = self.get_signal(str(cond["signal"]))
                if isinstance(sig, dict):
                    errors.append({"signal": cond["signal"], "status": sig.get("status"), "summary": sig.get("summary"), "data": sig.get("data")})
                    continue
                if sig.kind.lower() in {"real", "realtime", "string"}:
                    errors.append({"signal": sig.full_path, "status": "unsupported_signal_kind", "summary": f"conditions on {sig.kind} signals are not supported"})
                    continue
                try:
                    tests.append(compile_condition(op, cond["value"], sig.width))
                except ValueError as exc:
                    errors.append({"signal": sig.full_path, "status": "bad_condition", "summary": str(exc)})
                    continue
                sigs.append(sig)
                described.append({"signal": sig.full_path, "op": op, "value": str(cond["value"])})
            if errors:
                return fail("bad_condition", "One or more conditions could not be evaluated", {"errors": errors})
            payload_sigs, payload_errors = self._resolve_payload(payload)
            if payload_errors:
                return fail("bad_payload", "One or more payload signals could not be resolved", {"errors": payload_errors})

            a = self.parse_tick(start) if start is not None else 0
            b = self.parse_tick(end) if end is not None else int(self.meta.get("max_tick", 0))
            bad = self._check_window(a, b)
            if bad:
                return bad
            limit = max(1, min(int(limit), max_limit))
            ids = [s.signal_id for s in sigs]
            payload_at: Callable[[int], tuple[sqlite3.Row, ...]] | None = None

            def payload_values(tick: int) -> dict[str, str]:
                nonlocal payload_at
                if payload_at is None:
                    payload_at = self._sampler([p.signal_id for p in payload_sigs], tick, b)
                return {p.full_path: display_value(row, p.width) for p, row in zip(payload_sigs, payload_at(tick))}
            # Each predicate depends only on the value, so cache its result per interned value id.
            caches: list[dict[int, bool]] = [{} for _ in tests]

            def holds(rows: tuple[sqlite3.Row, ...]) -> bool:
                for test, cache, row in zip(tests, caches, rows):
                    vid = int(row["value_id"])
                    hit = cache.get(vid)
                    if hit is None:
                        hit = cache[vid] = test(row)
                    if not hit:
                        return False
                return True

            cond_text = " && ".join(f"{d['signal']}{d['op']}{d['value']}" for d in described)
            data: dict[str, Any] = {
                "conditions": described,
                "start_tick": a,
                "end_tick": b,
                "start_time": self.format_tick(a),
                "end_time": self.format_tick(b),
            }
            if payload_sigs:
                data["payload_signals"] = [{"signal": p.full_path, "width": p.width} for p in payload_sigs]
                data["payload_format"] = "scalars 0/1/x/z; fully known vectors 0x-hex padded to width; vectors with x/z as raw VCD binary"
            limitations: list[str] = []
            truncated = False

            if clock is None:
                matches: list[dict[str, Any]] = []
                max_tick = int(self.meta.get("max_tick", 0))

                def emit(s: int, e: int) -> bool:
                    if len(matches) >= limit:
                        return False
                    match = {
                        "start_tick": s,
                        "end_tick_exclusive": e,
                        "start_time": self.format_tick(s),
                        "end_time_exclusive": self.format_tick(e),
                        "duration_ticks": e - s,
                        "may_start_earlier": s == a and a > 0,
                        "may_continue_later": e == b + 1 and b < max_tick,
                    }
                    if payload_sigs:
                        match["values"] = payload_values(s)
                    matches.append(match)
                    return True

                open_start: int | None = None
                open_end = a
                for seg_start, seg_end, rows in self._segments(ids, a, b):
                    if holds(rows):
                        if open_start is None:
                            open_start = seg_start
                        open_end = seg_end
                    elif open_start is not None:
                        if not emit(open_start, open_end):
                            truncated = True
                            break
                        open_start = None
                if open_start is not None and not truncated and not emit(open_start, open_end):
                    truncated = True
                data.update({"mode": "intervals", "matches": matches, "match_count": len(matches), "truncated": truncated})
                found = f"Found {len(matches)} interval(s) where {cond_text}"
            else:
                if sample not in FIND_SAMPLES:
                    return fail("bad_sample", f"sample must be one of: {', '.join(FIND_SAMPLES)}", {"allowed": list(FIND_SAMPLES)})
                clk = self._require_clock(clock, edge)
                if isinstance(clk, dict):
                    return clk
                offset = 1 if sample == "before" else 0
                edge_rows = self.require().execute(
                    "SELECT cycle_index, tick FROM clock_edges WHERE clock_signal_id=? AND edge_kind=? AND tick BETWEEN ? AND ? ORDER BY tick",
                    (clk.signal_id, edge, a, b),
                )
                cycles: list[dict[str, Any]] = []
                segments = self._segments(ids, max(0, a - offset), b)
                seg_start, seg_end, rows = next(segments)
                for edge_row in edge_rows:
                    edge_tick = int(edge_row["tick"])
                    sample_tick = edge_tick - offset
                    if sample_tick < 0:
                        limitations.append(f"cycle {int(edge_row['cycle_index'])} at tick 0 has no pre-edge sample and was skipped")
                        continue
                    while seg_end <= sample_tick:
                        seg_start, seg_end, rows = next(segments)
                    if holds(rows):
                        if len(cycles) >= limit:
                            truncated = True
                            break
                        cycle = {
                            "cycle_index": int(edge_row["cycle_index"]),
                            "edge_tick": edge_tick,
                            "edge_time": self.format_tick(edge_tick),
                            "sample_tick": sample_tick,
                        }
                        if payload_sigs:
                            cycle["values"] = payload_values(sample_tick)
                        cycles.append(cycle)
                data.update({
                    "mode": "clock_cycles",
                    "clock": clk.full_path,
                    "edge": edge,
                    "sample": sample,
                    "matches": cycles,
                    "match_count": len(cycles),
                    "truncated": truncated,
                })
                when = "just before" if sample == "before" else "at"
                found = f"Found {len(cycles)} {clk.full_path} {edge}-edge cycle(s) where {cond_text} {when} the edge"
            if truncated:
                limitations.append("result truncated at limit")
            if data["match_count"] == 0:
                return ok("no_match", f"No match for {cond_text} between {self.format_tick(a)} and {self.format_tick(b)}", data, limitations=limitations)
            return ok("ok", found, data, limitations=limitations)
        except Exception as exc:
            return fail("find_times_failed", str(exc))

    # ------------------------------------------------------------------ scope helpers and debug tools

    def _window(self, start: str | int | None, end: str | int | None) -> tuple[int, int] | dict[str, Any]:
        a = self.parse_tick(start) if start is not None else 0
        b = self.parse_tick(end) if end is not None else int(self.meta.get("max_tick", 0))
        bad = self._check_window(a, b)
        return bad if bad else (a, b)

    def _scope_rows(self) -> list[tuple[str, int]]:
        return [(r[0], int(r[1])) for r in self.require().execute("SELECT scope, COUNT(*) FROM signals GROUP BY scope")]

    @staticmethod
    def _scope_matches(scope: str, query: str) -> bool:
        """Segment-aligned match: 'u_apb' matches tb.u_apb and tb.u_apb.sub, never tb.u_apb2."""
        q = query.strip().strip(".").lower()
        return not q or f".{q}." in f".{scope.lower()}."

    def _signals_in_scope(self, scope: str | None) -> list[SignalRef] | dict[str, Any]:
        con = self.require()
        if not scope:
            return [SignalRef.from_row(r) for r in con.execute("SELECT * FROM signals ORDER BY full_path")]
        scopes = [s for s, _ in self._scope_rows() if self._scope_matches(s, scope)]
        if not scopes:
            return fail("scope_not_found", f"No scope matched {scope!r}; call list_scopes to browse the hierarchy", {"scope": scope})
        out: list[SignalRef] = []
        for i in range(0, len(scopes), _SQL_CHUNK):
            chunk = scopes[i:i + _SQL_CHUNK]
            out.extend(SignalRef.from_row(r) for r in con.execute(
                f"SELECT * FROM signals WHERE scope IN ({','.join('?' * len(chunk))}) ORDER BY full_path", chunk
            ))
        return out

    def list_scopes(self, prefix: str | None = None, max_depth: int = 1, limit: int = 200) -> dict[str, Any]:
        """Browse the design hierarchy. Works on metadata alone, before any index build."""
        try:
            self.require()
            limit = max(1, min(int(limit), 2000))
            max_depth = max(0, int(max_depth))
            direct = dict(self._scope_rows())
            # Every ancestor scope exists in the hierarchy even if it declares no signals itself.
            all_scopes: dict[str, int] = {}
            for scope, n in direct.items():
                parts = scope.split(".")
                for k in range(1, len(parts) + 1):
                    all_scopes.setdefault(".".join(parts[:k]), 0)
                all_scopes[scope] = n
            total: dict[str, int] = dict.fromkeys(all_scopes, 0)
            for scope, n in direct.items():
                parts = scope.split(".")
                for k in range(1, len(parts) + 1):
                    total[".".join(parts[:k])] += n
            q = (prefix or "").strip().strip(".").lower()
            if q:
                roots = [s for s in all_scopes if s.lower() == q or s.lower().endswith("." + q)]
                if not roots:
                    return fail("scope_not_found", f"No scope matched {prefix!r}", {"prefix": prefix, "top_level": sorted(s for s in all_scopes if "." not in s)[:50]})
            else:
                roots = [s for s in all_scopes if "." not in s]
            rows = []
            for root in sorted(roots):
                depth0 = root.count(".")
                for scope in sorted(all_scopes):
                    if scope == root or scope.startswith(root + "."):
                        depth = scope.count(".") - depth0
                        if depth <= max_depth:
                            rows.append({"scope": scope, "depth": depth, "signals": all_scopes[scope], "signals_in_subtree": total[scope]})
            truncated = len(rows) > limit
            data: dict[str, Any] = {"prefix": prefix, "max_depth": max_depth, "scopes": rows[:limit], "scope_count": len(rows), "truncated": truncated}
            if len(roots) == 1:
                sigs = [r for r in self.require().execute(
                    "SELECT leaf, width, kind FROM signals WHERE scope=? ORDER BY leaf LIMIT 201", (roots[0],)
                )]
                data["root_signals"] = [{"leaf": r["leaf"], "width": int(r["width"]), "kind": r["kind"]} for r in sigs[:200]]
                data["root_signals_truncated"] = len(sigs) > 200
            return ok("ok", f"Found {len(rows)} scope(s)", data, limitations=["result truncated"] if truncated else [])
        except Exception as exc:
            return fail("list_scopes_failed", str(exc))

    def x_report(self, time_value: str | int | None = None, scope: str | None = None, limit: int = 100) -> dict[str, Any]:
        """Signals holding x/z at a time, earliest-to-go-unknown first (the likely origin of an X)."""
        try:
            for level in ("runs", "edges"):
                need = self._index_required(level)
                if need:
                    return need
            con = self.require()
            tick = self.parse_tick(time_value) if time_value is not None else int(self.meta.get("max_tick", 0))
            bad = self._check_tick_range(tick)
            if bad:
                return bad
            limit = max(1, min(int(limit), 5000))
            sigs = self._signals_in_scope(scope)
            if isinstance(sigs, dict):
                return sigs
            # String values can contain the letters x/z; they are not 4-state unknowns.
            sigs = [s for s in sigs if s.kind.lower() not in {"string", "real", "realtime"}]
            held = self._held_rows([s.signal_id for s in sigs], tick)
            unknown = [(s, held[s.signal_id]) for s in sigs if s.signal_id in held and (held[s.signal_id]["has_x"] or held[s.signal_id]["has_z"])]
            ids = [s.signal_id for s, _ in unknown]
            x_since: dict[int, int] = {}
            for i in range(0, len(ids), _SQL_CHUNK):
                chunk = ids[i:i + _SQL_CHUNK]
                # An x_enter edge is exactly a no-x -> has-x transition, so the last one is when the current x began.
                x_since.update((int(r[0]), int(r[1])) for r in con.execute(
                    f"SELECT signal_id, MAX(tick) FROM signal_edges WHERE signal_id IN ({','.join('?' * len(chunk))}) AND edge_kind='x_enter' AND tick<=? GROUP BY signal_id",
                    (*chunk, tick),
                ))
            entries = []
            for sig, row in unknown:
                if row["has_x"]:
                    since = x_since.get(sig.signal_id, 0)
                else:
                    # z-only: walk back to the end of the last run with no z (exact even when x_enter masked z_enter).
                    r = con.execute(
                        """SELECT MAX(r.end_tick) FROM signal_runs r JOIN values_interned v ON v.value_id=r.value_id
                           WHERE r.signal_id=? AND r.start_tick<=? AND v.has_z=0""",
                        (sig.signal_id, tick),
                    ).fetchone()
                    since = int(r[0]) if r and r[0] is not None and int(r[0]) <= tick else 0
                entries.append({
                    "signal": sig.full_path,
                    "width": sig.width,
                    "value": row["raw_value"],
                    "has_x": bool(row["has_x"]),
                    "has_z": bool(row["has_z"]),
                    "unknown_since_tick": since,
                    "unknown_since_time": self.format_tick(since),
                    "unknown_since_start": since == 0,
                })
            entries.sort(key=lambda e: (e["unknown_since_tick"], e["signal"]))
            truncated = len(entries) > limit
            data = {
                "tick": tick,
                "time": self.format_tick(tick),
                "scope": scope,
                "checked_signals": len(sigs),
                "unknown_count": len(entries),
                "x_count": sum(1 for e in entries if e["has_x"]),
                "z_count": sum(1 for e in entries if e["has_z"]),
                "signals": entries[:limit],
                "truncated": truncated,
            }
            limitations = ["unknown_since is when the current unknown stretch began; unknown_since_start means unknown since time 0"]
            if truncated:
                limitations.append("result truncated at limit")
            if not entries:
                return ok("no_match", f"No x/z values in {len(sigs)} signal(s) at {self.format_tick(tick)}", data, limitations=limitations)
            first = entries[0]
            return ok(
                "ok",
                f"{len(entries)} signal(s) hold x/z at {self.format_tick(tick)}; earliest: {first['signal']} since {first['unknown_since_time']}",
                data,
                limitations=limitations,
            )
        except Exception as exc:
            return fail("x_report_failed", str(exc))

    def activity(self, start: str | int | None = None, end: str | int | None = None, scope: str | None = None, ignore: list[str] | None = None, limit: int = 50) -> dict[str, Any]:
        """Which signals changed after start (up to end), when each last changed, and which stayed idle.

        Answers "where did the design go quiet?" for hangs; pass clocks in ignore so they do not mask it.
        """
        try:
            need = self._index_required("changes")
            if need:
                return need
            con = self.require()
            window = self._window(start, end)
            if isinstance(window, dict):
                return window
            a, b = window
            limit = max(1, min(int(limit), 5000))
            sigs = self._signals_in_scope(scope)
            if isinstance(sigs, dict):
                return sigs
            ignored_ids: set[int] = set()
            ignore_errors = []
            for name in ignore or []:
                ref = self.get_signal(name)
                if isinstance(ref, dict):
                    ignore_errors.append({"signal": name, "status": ref.get("status"), "summary": ref.get("summary")})
                else:
                    ignored_ids.add(ref.signal_id)
            if ignore_errors:
                return fail("bad_ignore", "One or more ignore signals could not be resolved", {"errors": ignore_errors})
            sigs = [s for s in sigs if s.signal_id not in ignored_ids]
            stats: dict[int, tuple[int, int]] = {}
            ids = [s.signal_id for s in sigs]
            for i in range(0, len(ids), _SQL_CHUNK):
                chunk = ids[i:i + _SQL_CHUNK]
                stats.update((int(r[0]), (int(r[1]), int(r[2]))) for r in con.execute(
                    f"SELECT signal_id, COUNT(*), MAX(tick) FROM changes WHERE signal_id IN ({','.join('?' * len(chunk))}) AND tick>? AND tick<=? GROUP BY signal_id",
                    (*chunk, a, b),
                ))
            active = sorted((s for s in sigs if s.signal_id in stats), key=lambda s: (-stats[s.signal_id][1], s.full_path))
            idle = [s for s in sigs if s.signal_id not in stats]
            shown = active[:limit]
            end_vals = self._held_rows([s.signal_id for s in shown + idle[:limit]], b) if self.meta.get("ready_runs") == "1" else {}

            def end_value(s: SignalRef) -> str | None:
                row = end_vals.get(s.signal_id)
                return display_value(row, s.width) if row is not None else None

            last_tick = stats[active[0].signal_id][1] if active else None
            data = {
                "start_tick": a,
                "end_tick": b,
                "start_time": self.format_tick(a),
                "end_time": self.format_tick(b),
                "scope": scope,
                "ignored": sorted(s.full_path for s in self._signals_by_ids(ignored_ids)),
                "checked_signals": len(sigs),
                "active_count": len(active),
                "idle_count": len(idle),
                "last_activity_tick": last_tick,
                "last_activity_time": self.format_tick(last_tick) if last_tick is not None else None,
                "most_recent": [
                    {
                        "signal": s.full_path,
                        "change_count": stats[s.signal_id][0],
                        "last_change_tick": stats[s.signal_id][1],
                        "last_change_time": self.format_tick(stats[s.signal_id][1]),
                        "end_value": end_value(s),
                    }
                    for s in shown
                ],
                "idle": [{"signal": s.full_path, "end_value": end_value(s)} for s in idle[:limit]],
                "truncated": len(active) > limit or len(idle) > limit,
            }
            limitations = ["counts changes strictly after start and at or before end"]
            if data["truncated"]:
                limitations.append("lists truncated at limit")
            if last_tick is None:
                summary = f"No signal changed after {self.format_tick(a)} up to {self.format_tick(b)} ({len(sigs)} checked)"
            else:
                summary = f"{len(active)} of {len(sigs)} signal(s) changed; last activity at {self.format_tick(last_tick)} ({active[0].full_path}); {len(idle)} idle"
            return ok("ok", summary, data, limitations=limitations)
        except Exception as exc:
            return fail("activity_failed", str(exc))

    def _signals_by_ids(self, ids: Iterable[int]) -> list[SignalRef]:
        ids = list(ids)
        if not ids:
            return []
        return [SignalRef.from_row(r) for r in self.require().execute(
            f"SELECT * FROM signals WHERE signal_id IN ({','.join('?' * len(ids))})", ids
        )]

    def state_transitions(self, signal: str, start: str | int | None = None, end: str | int | None = None, limit: int = 50) -> dict[str, Any]:
        """Transition counts (from -> to) and time spent in each value: an FSM summary in one call."""
        try:
            for level in ("runs", "edges"):
                need = self._index_required(level)
                if need:
                    return need
            sig = self.get_signal(signal)
            if isinstance(sig, dict):
                return sig
            window = self._window(start, end)
            if isinstance(window, dict):
                return window
            a, b = window
            limit = max(1, min(int(limit), 1000))
            con = self.require()
            trans = list(con.execute(
                """
                SELECT vf.raw_value AS f_raw, vf.int_value AS f_int, vt.raw_value AS t_raw, vt.int_value AS t_int,
                       COUNT(*) AS n, MIN(e.tick) AS first_tick, MAX(e.tick) AS last_tick
                FROM signal_edges e
                JOIN values_interned vf ON vf.value_id=e.from_value_id
                JOIN values_interned vt ON vt.value_id=e.to_value_id
                WHERE e.signal_id=? AND e.tick BETWEEN ? AND ?
                GROUP BY e.from_value_id, e.to_value_id
                ORDER BY n DESC, first_tick
                """,
                (sig.signal_id, a, b),
            ))
            first = con.execute(
                "SELECT start_tick FROM signal_runs WHERE signal_id=? AND start_tick<=? ORDER BY start_tick DESC LIMIT 1",
                (sig.signal_id, a),
            ).fetchone()
            lo = int(first[0]) if first else a
            dwell = list(con.execute(
                """
                SELECT v.raw_value, v.int_value, SUM(MIN(r.end_tick, ?) - MAX(r.start_tick, ?)) AS ticks, COUNT(*) AS visits
                FROM signal_runs r JOIN values_interned v ON v.value_id=r.value_id
                WHERE r.signal_id=? AND r.start_tick BETWEEN ? AND ?
                GROUP BY r.value_id ORDER BY ticks DESC
                """,
                (b + 1, a, sig.signal_id, lo, b),
            ))
            span = b + 1 - a

            def show(raw: str, int_value: Any) -> str:
                if sig.width > 1:
                    h = hex_of(int_value, sig.width)
                    if h is not None:
                        return h
                return str(raw)

            data = {
                "signal": sig.full_path,
                "width": sig.width,
                "start_tick": a,
                "end_tick": b,
                "start_time": self.format_tick(a),
                "end_time": self.format_tick(b),
                "transition_count": sum(int(r["n"]) for r in trans),
                "distinct_transitions": len(trans),
                "transitions": [
                    {
                        "from": show(r["f_raw"], r["f_int"]),
                        "to": show(r["t_raw"], r["t_int"]),
                        "count": int(r["n"]),
                        "first_time": self.format_tick(int(r["first_tick"])),
                        "last_time": self.format_tick(int(r["last_tick"])),
                        "first_tick": int(r["first_tick"]),
                        "last_tick": int(r["last_tick"]),
                    }
                    for r in trans[:limit]
                ],
                "distinct_values": len(dwell),
                "values": [
                    {
                        "value": show(r["raw_value"], r["int_value"]),
                        "ticks": int(r["ticks"]),
                        "fraction": round(int(r["ticks"]) / span, 6) if span else 0.0,
                        "visits": int(r["visits"]),
                    }
                    for r in dwell[:limit]
                ],
                "truncated": len(trans) > limit or len(dwell) > limit,
            }
            limitations = ["values are end-of-timestep; a change and change-back inside one timestep is not a transition"]
            if data["truncated"]:
                limitations.append("lists truncated at limit")
            return ok(
                "ok",
                f"{sig.full_path}: {data['transition_count']} transition(s) over {len(trans)} distinct from->to pair(s), {len(dwell)} distinct value(s)",
                data,
                limitations=limitations,
            )
        except Exception as exc:
            return fail("state_transitions_failed", str(exc))

    def pair_events(
        self,
        clock: str,
        request: list[dict[str, Any]],
        response: list[dict[str, Any]],
        edge: str = "rise",
        sample: str = "before",
        start: str | int | None = None,
        end: str | int | None = None,
        request_id: str | None = None,
        response_id: str | None = None,
        allow_same_cycle: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Pair each request cycle with a later response cycle (in order, or in order per ID).

        Reports latencies and, most usefully for hangs, requests that never got a response.
        """
        try:
            if (request_id is None) != (response_id is None):
                return fail("bad_query", "give both request_id and response_id, or neither")
            limit = max(1, min(int(limit), 10000))
            req = self._find_times(request, start, end, clock, edge, sample, _PAIR_MAX_EVENTS, [request_id] if request_id else None, max_limit=_PAIR_MAX_EVENTS)
            if not req.get("ok"):
                return req
            rsp = self._find_times(response, start, end, clock, edge, sample, _PAIR_MAX_EVENTS, [response_id] if response_id else None, max_limit=_PAIR_MAX_EVENTS)
            if not rsp.get("ok"):
                return rsp
            if req["data"].get("truncated") or rsp["data"].get("truncated"):
                return fail("too_many_events", f"more than {_PAIR_MAX_EVENTS} request or response cycles; narrow start/end", {"max_events": _PAIR_MAX_EVENTS})
            req_id_path = req["data"]["payload_signals"][0]["signal"] if request_id else None
            rsp_id_path = rsp["data"]["payload_signals"][0]["signal"] if response_id else None

            def key_of(match: dict[str, Any], path: str | None) -> int | str | None:
                if path is None:
                    return "*"
                text = match["values"][path]
                if text.startswith("0x"):
                    return int(text, 16)
                if text in ("0", "1"):
                    return int(text)
                return None  # x/z ID: cannot be matched

            # One stream of (cycle, order, kind, match); order decides same-cycle handling.
            events: list[tuple[int, int, str, dict[str, Any]]] = []
            req_order, rsp_order = (0, 1) if allow_same_cycle else (1, 0)
            events.extend((m["cycle_index"], req_order, "req", m) for m in req["data"]["matches"])
            events.extend((m["cycle_index"], rsp_order, "rsp", m) for m in rsp["data"]["matches"])
            events.sort(key=lambda e: (e[0], e[1]))
            outstanding: dict[Any, deque[dict[str, Any]]] = {}
            pairs: list[dict[str, Any]] = []
            orphans: list[dict[str, Any]] = []
            unkeyed_requests: list[dict[str, Any]] = []
            for cycle, _order, kind, m in events:
                if kind == "req":
                    k = key_of(m, req_id_path)
                    if k is None:
                        unkeyed_requests.append(m)
                    else:
                        outstanding.setdefault(k, deque()).append(m)
                    continue
                k = key_of(m, rsp_id_path)
                queue = outstanding.get(k) if k is not None else None
                if not queue:
                    orphans.append(m)
                    continue
                r = queue.popleft()
                pairs.append({
                    "id": (m["values"][rsp_id_path] if rsp_id_path else None),
                    "request_cycle": r["cycle_index"],
                    "response_cycle": cycle,
                    "latency_cycles": cycle - r["cycle_index"],
                    "request_time": r["edge_time"],
                    "response_time": m["edge_time"],
                    "latency_ticks": m["edge_tick"] - r["edge_tick"],
                })
            unmatched = sorted((m for q in outstanding.values() for m in q), key=lambda m: m["cycle_index"])
            lat = sorted(p["latency_cycles"] for p in pairs)
            stats = None
            if lat:
                mid = len(lat) // 2
                median = lat[mid] if len(lat) % 2 else (lat[mid - 1] + lat[mid]) / 2
                worst = max(pairs, key=lambda p: (p["latency_cycles"], -p["request_cycle"]))
                stats = {"min": lat[0], "max": lat[-1], "mean": round(sum(lat) / len(lat), 3), "median": median, "worst": worst}

            def brief(m: dict[str, Any], path: str | None) -> dict[str, Any]:
                out = {"cycle_index": m["cycle_index"], "time": m["edge_time"]}
                if path:
                    out["id"] = m["values"][path]
                return out

            b_tick = int(rsp["data"]["end_tick"])
            data = {
                "clock": req["data"]["clock"],
                "edge": edge,
                "sample": sample,
                "pairing": "in order per ID" if request_id else "in order",
                "request_count": len(req["data"]["matches"]),
                "response_count": len(rsp["data"]["matches"]),
                "pair_count": len(pairs),
                "latency_cycles": stats,
                "pairs": pairs[:limit],
                "unmatched_request_count": len(unmatched),
                "unmatched_requests": [brief(m, req_id_path) for m in unmatched[:limit]],
                "orphan_response_count": len(orphans),
                "orphan_responses": [brief(m, rsp_id_path) for m in orphans[:limit]],
                "unknown_id_request_count": len(unkeyed_requests),
                "window_ends_before_sim_end": b_tick < int(self.meta.get("max_tick", 0)),
                "truncated": len(pairs) > limit or len(unmatched) > limit or len(orphans) > limit,
            }
            limitations = [
                "pairing assumes responses return in request order" + (" within each ID" if request_id else "") + "; out-of-order protocols need request_id/response_id",
                "same-cycle responses " + ("may" if allow_same_cycle else "may not") + " complete a request issued that cycle",
            ]
            if data["window_ends_before_sim_end"] and unmatched:
                limitations.append("window ends before the simulation does; unmatched requests may complete later")
            if data["truncated"]:
                limitations.append("lists truncated at limit")
            summary = f"{len(pairs)} request/response pair(s)"
            if stats:
                summary += f", latency {stats['min']}..{stats['max']} cycles (mean {stats['mean']})"
            summary += f"; {len(unmatched)} unmatched request(s), {len(orphans)} orphan response(s)"
            if unkeyed_requests:
                summary += f", {len(unkeyed_requests)} request(s) with x/z ID"
            return ok("ok", summary, data, limitations=limitations)
        except Exception as exc:
            return fail("pair_events_failed", str(exc))
