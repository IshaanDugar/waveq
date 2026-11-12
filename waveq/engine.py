from __future__ import annotations

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
_MAX_FIND_CONDITIONS = 16


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

    def configure(self, vcd_path: str, cache_dir: str | None = None, rebuild: bool = False) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            source = Path(vcd_path).expanduser().resolve()
            size_class = size_class_for_bytes(source.stat().st_size)
            prior = cache_status(source, cache_dir)
            if not rebuild and prior.get("cache_valid") and "metadata" in prior.get("ready_levels", []):
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
            elif size_class == "small":
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
        if self._clock_cycle_count(sig, edge) == 0:
            return fail("clock_not_defined", f"{sig.full_path} has no {edge}-edge cycle index; call define_clock first", {"clock": sig.full_path, "edge": edge})
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
            available = int(con.execute(
                "SELECT COUNT(*) FROM signal_edges WHERE signal_id=? AND edge_kind=?", (sig.signal_id, edge)
            ).fetchone()[0])
            if available == 0:
                return fail("no_clock_edges", f"{sig.full_path} has no {edge} edges; it cannot be used as a {edge}-edge clock", {"clock": sig.full_path, "edge": edge})
            cycles = rebuild_clock_edges(con, sig.signal_id, edge)
            con.commit()
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

    def values_at_cycle(self, signals: list[str], clock: str, cycle_index: int, edge: str = "rise") -> dict[str, Any]:
        cyc = self.cycle_to_time(clock, cycle_index, edge=edge)
        if not cyc.get("ok"):
            return cyc
        return self.values_at(signals, int(cyc["data"]["tick"]))

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

    def find_times(
        self,
        conditions: list[dict[str, Any]],
        start: str | int | None = None,
        end: str | int | None = None,
        clock: str | None = None,
        edge: str = "rise",
        sample: str = "before",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Find when all conditions hold: as time intervals, or as clock cycles when a clock is given."""
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

            a = self.parse_tick(start) if start is not None else 0
            b = self.parse_tick(end) if end is not None else int(self.meta.get("max_tick", 0))
            bad = self._check_window(a, b)
            if bad:
                return bad
            limit = max(1, min(int(limit), 10000))
            ids = [s.signal_id for s in sigs]
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
            limitations: list[str] = []
            truncated = False

            if clock is None:
                matches: list[dict[str, Any]] = []
                max_tick = int(self.meta.get("max_tick", 0))

                def emit(s: int, e: int) -> bool:
                    if len(matches) >= limit:
                        return False
                    matches.append({
                        "start_tick": s,
                        "end_tick_exclusive": e,
                        "start_time": self.format_tick(s),
                        "end_time_exclusive": self.format_tick(e),
                        "duration_ticks": e - s,
                        "may_start_earlier": s == a and a > 0,
                        "may_continue_later": e == b + 1 and b < max_tick,
                    })
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
                        cycles.append({
                            "cycle_index": int(edge_row["cycle_index"]),
                            "edge_tick": edge_tick,
                            "edge_time": self.format_tick(edge_tick),
                            "sample_tick": sample_tick,
                        })
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
