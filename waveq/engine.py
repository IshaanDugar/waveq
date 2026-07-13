from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
import sqlite3
import time

from .db import BuildStats, build_database, build_metadata_database, cache_path_for, cache_status, connect, size_class_for_bytes
from .timeparse import Timescale, parse_radius_to_ticks, parse_time_to_ticks


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
        return list(con.execute("SELECT * FROM signals WHERE full_path_lc=? OR leaf_lc=? ORDER BY full_path", (q, q)))

    def get_signal(self, name: str) -> SignalRef | dict[str, Any]:
        rows = self._get_signal_exact_rows(name)
        if len(rows) == 1:
            return SignalRef.from_row(rows[0])
        if len(rows) > 1:
            exact_full = [r for r in rows if r["full_path_lc"] == name.strip().lower()]
            if len(exact_full) == 1:
                return SignalRef.from_row(exact_full[0])
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
            row = self.require().execute(
                """
                SELECT v.raw_value, v.has_x, v.has_z, v.int_value, r.start_tick, r.end_tick
                FROM signal_runs r JOIN values_interned v ON v.value_id=r.value_id
                WHERE r.signal_id=? AND r.start_tick<=? AND r.end_tick>?
                ORDER BY r.start_tick DESC LIMIT 1
                """,
                (sig.signal_id, tick, tick),
            ).fetchone()
            if not row:
                return fail("not_found", f"No value recorded for {sig.full_path} at {self.format_tick(tick)}")
            data = {
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
            return ok("ok", f"{sig.full_path}={row['raw_value']} at {self.format_tick(tick)}", data)
        except Exception as exc:
            return fail("value_at_failed", str(exc))

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
            for name in signals:
                r = self.value_at(name, tick)
                if r.get("ok"):
                    values.append(r["data"])
                else:
                    errors.append({"signal": name, "status": r.get("status"), "summary": r.get("summary"), "data": r.get("data")})
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
            if b < a:
                return fail("bad_range", "end must be >= start")
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
            sig = self.get_signal(signal)
            if isinstance(sig, dict):
                return sig
            a = self.parse_tick(start) if start is not None else 0
            b = self.parse_tick(end) if end is not None else int(self.meta.get("max_tick", 0))
            if b < a:
                return fail("bad_range", "end must be >= start")
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
            sig = self.get_signal(signal)
            if isinstance(sig, dict):
                return sig
            tick = self.parse_tick(time_value)
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
            if b < a:
                return fail("bad_range", "end must be >= start")
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
            summaries = []
            for sig in sigs[:max_signals]:
                start_v = self.value_at(sig.full_path, a)
                end_v = self.value_at(sig.full_path, b)
                cnt_row = con.execute(
                    "SELECT COUNT(*) AS n, MIN(tick) AS first_tick, MAX(tick) AS last_tick FROM changes WHERE signal_id=? AND tick BETWEEN ? AND ?",
                    (sig.signal_id, a, b),
                ).fetchone()
                edge_cnt = int(con.execute(
                    "SELECT COUNT(*) FROM signal_edges WHERE signal_id=? AND tick BETWEEN ? AND ?",
                    (sig.signal_id, a, b),
                ).fetchone()[0])
                summaries.append(
                    {
                        "signal": sig.full_path,
                        "width": sig.width,
                        "change_count": int(cnt_row["n"]),
                        "edge_count": edge_cnt,
                        "first_change_tick": int(cnt_row["first_tick"]) if cnt_row["first_tick"] is not None else None,
                        "first_change_time": self.format_tick(int(cnt_row["first_tick"])) if cnt_row["first_tick"] is not None else None,
                        "last_change_tick": int(cnt_row["last_tick"]) if cnt_row["last_tick"] is not None else None,
                        "last_change_time": self.format_tick(int(cnt_row["last_tick"])) if cnt_row["last_tick"] is not None else None,
                        "start_value": start_v.get("data", {}).get("value") if start_v.get("ok") else None,
                        "end_value": end_v.get("data", {}).get("value") if end_v.get("ok") else None,
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
            sig = self.get_signal(signal)
            if isinstance(sig, dict):
                return sig
            edge = edge or "rise"
            con = self.require()
            con.execute("DELETE FROM clock_edges WHERE clock_signal_id=? AND edge_kind=?", (sig.signal_id, edge))
            ticks = [int(r["tick"]) for r in con.execute(
                "SELECT tick FROM signal_edges WHERE signal_id=? AND edge_kind=? ORDER BY tick",
                (sig.signal_id, edge),
            )]
            con.executemany(
                "INSERT INTO clock_edges(clock_signal_id, edge_kind, cycle_index, tick) VALUES(?, ?, ?, ?)",
                ((sig.signal_id, edge, i, t) for i, t in enumerate(ticks)),
            )
            con.commit()
            return ok("ok", f"Defined clock {sig.full_path} on {edge} edges with {len(ticks)} cycle(s)", {"clock": sig.full_path, "edge": edge, "cycles": len(ticks)})
        except Exception as exc:
            return fail("define_clock_failed", str(exc))

    def time_to_cycle(self, clock: str, time_value: str | int, edge: str = "rise") -> dict[str, Any]:
        try:
            sig = self.get_signal(clock)
            if isinstance(sig, dict):
                return sig
            tick = self.parse_tick(time_value)
            row = self.require().execute(
                "SELECT cycle_index, tick FROM clock_edges WHERE clock_signal_id=? AND edge_kind=? AND tick<=? ORDER BY tick DESC LIMIT 1",
                (sig.signal_id, edge, tick),
            ).fetchone()
            if not row:
                return fail("not_found", f"No {edge} clock cycle exists at or before {self.format_tick(tick)}; call define_clock first")
            data = {"clock": sig.full_path, "edge": edge, "query_tick": tick, "query_time": self.format_tick(tick), "cycle_index": int(row["cycle_index"]), "cycle_tick": int(row["tick"]), "cycle_time": self.format_tick(int(row["tick"]))}
            return ok("ok", f"{self.format_tick(tick)} maps to cycle {data['cycle_index']} of {sig.full_path}", data)
        except Exception as exc:
            return fail("time_to_cycle_failed", str(exc))

    def cycle_to_time(self, clock: str, cycle_index: int, edge: str = "rise") -> dict[str, Any]:
        try:
            sig = self.get_signal(clock)
            if isinstance(sig, dict):
                return sig
            row = self.require().execute(
                "SELECT tick FROM clock_edges WHERE clock_signal_id=? AND edge_kind=? AND cycle_index=?",
                (sig.signal_id, edge, int(cycle_index)),
            ).fetchone()
            if not row:
                return fail("not_found", f"No cycle {cycle_index} for {sig.full_path}; call define_clock first")
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
