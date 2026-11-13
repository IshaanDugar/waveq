# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ishaan Dugar
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, TextIO
import gzip
import hashlib
import os
import re
import sqlite3
import sys
import time

from . import __version__
from .formats import is_native
from .timeparse import Timescale

# v3: same-value re-dumps are no longer stored as changes; header parsing is token-based.
_SCHEMA_VERSION = 3
_UNIT_RE = re.compile(r"\b(1|10|100)\s*(fs|ps|ns|us|ms|s)\b", re.IGNORECASE)
_INDEX_LEVELS = ("metadata", "changes", "runs", "edges")
# Body directives whose contents are ordinary value changes; their closing $end is a no-op.
_DUMP_BLOCKS = frozenset({"$dumpvars", "$dumpall", "$dumpon", "$dumpoff", "$dumpports", "$dumpportsall", "$dumpportson", "$dumpportsoff", "$end"})

# IEEE 1364 extended VCD port states -> 4-state values (driven or sensed, any strength).
_EVCD_STATE = {**dict.fromkeys("DdLl0", "0"), **dict.fromkeys("UuHh1", "1"), **dict.fromkeys("ZTFf", "z")}

EDGE_KINDS = ("rise", "fall", "x_enter", "x_exit", "z_enter", "z_exit", "scalar_change", "vector_change")
CLOCK_EDGE_KINDS = ("rise", "fall")

@dataclass(frozen=True)
class SignalDef:
    signal_id: int
    id_code: str
    full_path: str
    scope: str
    leaf: str
    kind: str
    width: int

@dataclass(frozen=True)
class BuildStats:
    db_path: Path
    signal_count: int
    change_count: int
    run_count: int
    edge_count: int
    min_tick: int
    max_tick: int
    build_seconds: float
    source_size_bytes: int
    size_class: str = "small"
    built_level: str = "full"
    reused: bool = False
    indexed_signals: int | None = None  # None: every signal is indexed


def size_class_for_bytes(size: int) -> str:
    mb = size / (1024 * 1024)
    if mb < 10:
        return "small"
    if mb < 50:
        return "medium"
    if mb <= 250:
        return "large"
    return "huge"


def connect(db_path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    path = Path(db_path)
    if readonly:
        uri = f"file:{path}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
    else:
        con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    if not readonly:
        # Long-lived query connection: a bigger page cache and memory-mapped reads keep repeated
        # lookups off the file-read path.
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("PRAGMA cache_size=-65536")
        con.execute("PRAGMA mmap_size=268435456")
    return con


_SYNCED_ROOT_ENV = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
_SYNCED_DIR_NAMES = ("onedrive", "dropbox", "google drive", "icloud drive", "icloud~", "mobile documents")


def user_cache_dir() -> Path:
    """Per-user cache root (used when the VCD's own folder is a poor place for a SQLite cache)."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "waveq" / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "waveq"
    return Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "waveq"


def _in_synced_folder(path: Path) -> bool:
    """True for folders that a file-sync client watches (OneDrive, Dropbox, Google Drive, iCloud).

    SQLite writes there are slow and sync clients can lock the file mid-build.
    """
    for var in _SYNCED_ROOT_ENV:
        root = os.environ.get(var)
        if root:
            try:
                if path.is_relative_to(Path(root).resolve()):
                    return True
            except (OSError, ValueError):
                pass
    parts = [part.lower() for part in path.parts]
    return any(name in part for part in parts for name in _SYNCED_DIR_NAMES)


def default_cache_dir_for(vcd_path: str | Path) -> Path:
    """Where the cache lives when no cache_dir is given.

    WAVEQ_CACHE_DIR wins; otherwise `.waveq_cache` next to the VCD, unless that folder is synced
    or not writable, in which case the per-user cache directory is used.
    """
    env = os.environ.get("WAVEQ_CACHE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    p = Path(vcd_path).resolve()
    if _in_synced_folder(p.parent) or not os.access(p.parent, os.W_OK):
        return user_cache_dir()
    return p.parent / ".waveq_cache"


def cache_path_for(vcd_path: str | Path, cache_dir: str | Path | None = None) -> Path:
    p = Path(vcd_path).resolve()
    key = f"{p}\0{_SCHEMA_VERSION}".encode()
    digest = hashlib.sha256(key).hexdigest()[:16]
    name = p.name
    if name.endswith(".gz"):
        name = name[:-3]
    if name.endswith(".vcd"):
        name = name[:-4]
    root = Path(cache_dir).expanduser().resolve() if cache_dir else default_cache_dir_for(p)
    return root / f"{name}.{digest}.waveq.sqlite"


def is_cache_valid(db_path: str | Path, vcd_path: str | Path) -> bool:
    db = Path(db_path)
    src = Path(vcd_path).resolve()
    if not db.exists():
        return False
    try:
        con = connect(db, readonly=True)
        try:
            meta = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}
        finally:
            con.close()
        st = src.stat()
        return (
            meta.get("schema_version") == str(_SCHEMA_VERSION)
            and meta.get("source_path") == str(src)
            and meta.get("source_size_bytes") == str(st.st_size)
            and meta.get("source_mtime_ns") == str(st.st_mtime_ns)
        )
    except Exception:
        return False


def _meta_dict(con: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}


def _ready_levels(meta: dict[str, str]) -> list[str]:
    return [level for level in _INDEX_LEVELS if meta.get(f"ready_{level}") == "1"]


def cache_status(vcd_path: str | Path, cache_dir: str | Path | None = None) -> dict[str, object]:
    source = Path(vcd_path).expanduser().resolve()
    db_path = cache_path_for(source, cache_dir)
    source_exists = source.exists()
    source_size = source.stat().st_size if source_exists else None
    source_mtime_ns = source.stat().st_mtime_ns if source_exists else None
    source_size_mb = (float(source_size) / (1024 * 1024)) if source_size is not None else None
    status: dict[str, object] = {
        "status": "missing",
        "db_path": str(db_path),
        "cache_exists": db_path.exists(),
        "source_path": str(source),
        "source_exists": source_exists,
        "vcd_size_bytes": source_size,
        "vcd_size_mb": source_size_mb,
        "size_class": size_class_for_bytes(source_size or 0),
        "source_mtime_ns": source_mtime_ns,
        "cache_valid": False,
        "build_state": "missing",
        "ready_levels": [],
        "query_ready": False,
        "metadata": {},
    }
    if not source_exists:
        status["status"] = "missing_source"
        return status
    if not db_path.exists():
        return status
    try:
        con = connect(db_path, readonly=True)
        try:
            meta = _meta_dict(con)
        finally:
            con.close()
    except Exception as exc:
        status.update({"status": "partial", "build_state": "unreadable", "error": str(exc)})
        return status
    status["metadata"] = meta
    status["build_state"] = meta.get("build_state", "unknown")
    valid = (
        meta.get("schema_version") == str(_SCHEMA_VERSION)
        and meta.get("source_path") == str(source)
        and meta.get("source_size_bytes") == str(source_size)
        and meta.get("source_mtime_ns") == str(source_mtime_ns)
    )
    status["cache_valid"] = valid
    ready = _ready_levels(meta) if valid else []
    status["ready_levels"] = ready
    status["query_ready"] = "changes" in ready or "runs" in ready or "edges" in ready
    if not valid:
        status["status"] = "stale"
    elif meta.get("build_state", "").endswith("_building"):
        status["status"] = "partial"
    elif all(level in ready for level in _INDEX_LEVELS):
        status["status"] = "ready"
    elif ready:
        status["status"] = "partial"
    else:
        status["status"] = "missing"
    return status


def _open_text(path: Path) -> TextIO:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "rt", encoding="utf-8", errors="replace")


def _iter_header_directives(fh: TextIO) -> Iterator[tuple[str, list[str]]]:
    """Yield (keyword, args) for each header directive up to and including $enddefinitions.

    Tokenizes on whitespace, so directives may span several lines or share one line, and
    text inside $comment/$date/$version can never be mistaken for a $var or $scope.
    """
    keyword: str | None = None
    args: list[str] = []
    for raw_line in fh:
        for tok in raw_line.split():
            if keyword is None:
                if tok.startswith("$"):
                    keyword, args = tok, []
                continue
            if tok == "$end":
                yield keyword, args
                if keyword == "$enddefinitions":
                    return
                keyword = None
            else:
                args.append(tok)
    if keyword is not None:
        raise ValueError(f"unterminated {keyword} directive in VCD header")
    raise ValueError("VCD header has no $enddefinitions")


def _parse_timescale(args: list[str]) -> Timescale:
    text = " ".join(args)
    m = _UNIT_RE.search(text)
    if not m:
        raise ValueError(f"unsupported or malformed timescale directive: $timescale {text} $end")
    return Timescale(int(m.group(1)), m.group(2).lower())


def _scope_path(stack: list[str]) -> str:
    return ".".join(s for s in stack if s)


def _leaf_from_ref(reference: str) -> str:
    ref = reference.strip()
    # Keep vector ranges on full paths, but leaf-name matching is cleaner without them.
    ref = re.sub(r"\s+", " ", ref)
    return re.sub(r"\s*\[[^\]]+\]\s*$", "", ref)


def _parse_var(args: list[str], stack: list[str], signal_id: int) -> SignalDef:
    text = f"$var {' '.join(args)} $end"
    if len(args) < 4:
        raise ValueError(f"malformed $var directive: {text}")
    kind = args[0]
    try:
        if args[1].startswith("["):
            # Extended VCD ports declare a range instead of a width: $var port [7:0] <0 data $end
            msb, _, lsb = args[1].strip("[]").partition(":")
            width = abs(int(msb) - int(lsb or msb)) + 1
        else:
            width = int(args[1])
    except ValueError as exc:
        raise ValueError(f"malformed $var width in: {text}") from exc
    id_code = args[2]
    reference = " ".join(args[3:])
    scope = _scope_path(stack)
    full = f"{scope}.{reference}" if scope else reference
    leaf = _leaf_from_ref(reference)
    return SignalDef(signal_id, id_code, full, scope, leaf, kind, width)


def _parse_header_stream(fh: TextIO) -> tuple[Timescale, list[SignalDef]]:
    """Consume the VCD header from fh, leaving it positioned at the first body line."""
    timescale: Timescale | None = None
    signal_defs: list[SignalDef] = []
    scope_stack: list[str] = []
    for keyword, args in _iter_header_directives(fh):
        if keyword == "$timescale":
            timescale = _parse_timescale(args)
        elif keyword == "$scope":
            # Always push (even an unnamed scope) so $upscope stays balanced.
            scope_stack.append(" ".join(args[1:]))
        elif keyword == "$upscope":
            if scope_stack:
                scope_stack.pop()
        elif keyword == "$var":
            signal_defs.append(_parse_var(args, scope_stack, len(signal_defs) + 1))
    if timescale is None:
        raise ValueError("VCD has no $timescale")
    return timescale, signal_defs


def _canonical_scalar(ch: str) -> str:
    ch = ch.lower()
    if ch in {"0", "1", "x", "z"}:
        return ch
    raise ValueError(f"invalid scalar VCD value: {ch!r}")


def _canonical_vector(kind: str, payload: str) -> str:
    kind = kind.lower()
    payload = payload.strip().lower()
    if kind == "b":
        return "b" + payload
    if kind == "r":
        return "r" + payload
    if kind == "s":
        return "s" + payload
    raise ValueError(f"unsupported VCD vector kind: {kind!r}")


def _value_flags(raw: str) -> tuple[int, int, str | None]:
    lower = raw.lower()
    has_x = 1 if "x" in lower else 0
    has_z = 1 if "z" in lower else 0
    int_value = None
    try:
        if lower in {"0", "1"}:
            int_value = str(int(lower, 2))
        elif lower.startswith("b") and not has_x and not has_z:
            bits = lower[1:]
            if bits:
                int_value = str(int(bits, 2))
    except ValueError:
        int_value = None
    return has_x, has_z, int_value


def _open_build_db(tmp_path: Path) -> sqlite3.Connection:
    """Connection for building a cache into a throwaway temp file.

    The file is renamed over the real cache only after a complete build, so a crash just leaves
    a .tmp to discard: journaling and fsync buy nothing here and cost a lot of insert time.
    """
    con = sqlite3.connect(tmp_path)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA locking_mode=EXCLUSIVE;
        PRAGMA temp_store=MEMORY;
        PRAGMA cache_size=-131072;
        """
    )
    return con


def _finish_build_db(con: sqlite3.Connection) -> None:
    con.execute("ANALYZE")
    con.execute("PRAGMA optimize")
    con.commit()
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()


def _create_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE signals (
            signal_id INTEGER PRIMARY KEY,
            id_code TEXT NOT NULL,
            full_path TEXT NOT NULL,
            scope TEXT NOT NULL,
            leaf TEXT NOT NULL,
            kind TEXT NOT NULL,
            width INTEGER NOT NULL,
            full_path_lc TEXT NOT NULL,
            scope_lc TEXT NOT NULL,
            leaf_lc TEXT NOT NULL
        );

        CREATE TABLE values_interned (
            value_id INTEGER PRIMARY KEY,
            raw_value TEXT NOT NULL UNIQUE,
            has_x INTEGER NOT NULL,
            has_z INTEGER NOT NULL,
            int_value TEXT
        );

        CREATE TABLE changes (
            signal_id INTEGER NOT NULL,
            tick INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            value_id INTEGER NOT NULL,
            PRIMARY KEY(signal_id, tick, ordinal)
        ) WITHOUT ROWID;

        CREATE TABLE signal_runs (
            signal_id INTEGER NOT NULL,
            start_tick INTEGER NOT NULL,
            end_tick INTEGER NOT NULL,
            value_id INTEGER NOT NULL,
            PRIMARY KEY(signal_id, start_tick)
        ) WITHOUT ROWID;

        CREATE TABLE signal_edges (
            signal_id INTEGER NOT NULL,
            tick INTEGER NOT NULL,
            from_value_id INTEGER NOT NULL,
            to_value_id INTEGER NOT NULL,
            edge_kind TEXT NOT NULL,
            PRIMARY KEY(signal_id, tick, to_value_id)
        ) WITHOUT ROWID;

        CREATE TABLE clock_edges (
            clock_signal_id INTEGER NOT NULL,
            edge_kind TEXT NOT NULL,
            cycle_index INTEGER NOT NULL,
            tick INTEGER NOT NULL,
            PRIMARY KEY(clock_signal_id, edge_kind, cycle_index)
        ) WITHOUT ROWID;
        """
    )


def _create_indexes(con: sqlite3.Connection, level: str) -> None:
    con.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_signals_full_lc ON signals(full_path_lc);
        CREATE INDEX IF NOT EXISTS idx_signals_leaf_lc ON signals(leaf_lc);
        CREATE INDEX IF NOT EXISTS idx_signals_scope_lc ON signals(scope_lc);
        """
    )
    if level in {"changes", "runs", "edges", "full"}:
        con.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_changes_tick_sig_value ON changes(tick, signal_id, value_id);
            """
        )
    # changes, signal_runs and signal_edges are WITHOUT ROWID tables whose primary keys start with
    # signal_id, so per-signal lookups (forward or backward) are already covered by the table itself.
    if level in {"edges", "full"}:
        con.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_edges_sig_kind_tick ON signal_edges(signal_id, edge_kind, tick);
            CREATE INDEX IF NOT EXISTS idx_clock_tick ON clock_edges(clock_signal_id, edge_kind, tick, cycle_index);
            """
        )


def rebuild_clock_edges(con: sqlite3.Connection, signal_id: int, edge_kind: str) -> int:
    """(Re)derive the cycle index for one clock from signal_edges. Returns the cycle count."""
    con.execute("DELETE FROM clock_edges WHERE clock_signal_id=? AND edge_kind=?", (signal_id, edge_kind))
    con.execute(
        """
        INSERT INTO clock_edges(clock_signal_id, edge_kind, cycle_index, tick)
        SELECT signal_id, edge_kind, ROW_NUMBER() OVER (ORDER BY tick) - 1, tick
        FROM signal_edges WHERE signal_id=? AND edge_kind=?
        """,
        (signal_id, edge_kind),
    )
    return int(con.execute(
        "SELECT COUNT(*) FROM clock_edges WHERE clock_signal_id=? AND edge_kind=?", (signal_id, edge_kind)
    ).fetchone()[0])


def _existing_clock_defs(db_path: Path) -> list[tuple[str, str]]:
    """Clock definitions (full_path, edge_kind) in an existing cache, so rebuilds can keep them."""
    if not db_path.exists():
        return []
    try:
        con = connect(db_path, readonly=True)
        try:
            return [
                (r["full_path"], r["edge_kind"])
                for r in con.execute(
                    "SELECT DISTINCT s.full_path, c.edge_kind FROM clock_edges c JOIN signals s ON s.signal_id=c.clock_signal_id"
                )
            ]
        finally:
            con.close()
    except sqlite3.Error:
        return []


def _insert_meta(con: sqlite3.Connection, rows: dict[str, str]) -> None:
    con.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", rows.items())


def _replace_cache(tmp_path: Path, db_path: Path) -> None:
    # Windows refuses to replace a SQLite DB while stale WAL sidecars exist.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(tmp_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    if db_path.exists():
        db_path.unlink()
    os.replace(tmp_path, db_path)


def _insert_signals(con: sqlite3.Connection, signals: Iterable[SignalDef]) -> None:
    con.executemany(
        """
        INSERT INTO signals(signal_id, id_code, full_path, scope, leaf, kind, width,
                            full_path_lc, scope_lc, leaf_lc)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                s.signal_id,
                s.id_code,
                s.full_path,
                s.scope,
                s.leaf,
                s.kind,
                s.width,
                s.full_path.lower(),
                s.scope.lower(),
                s.leaf.lower(),
            )
            for s in signals
        ),
    )


def _base_meta(source: Path, source_stat: os.stat_result, timescale: Timescale | None, signal_count: int) -> dict[str, str]:
    meta = {
        "schema_version": str(_SCHEMA_VERSION),
        "source_path": str(source),
        "vcd_path": str(source),
        "source_size_bytes": str(source_stat.st_size),
        "vcd_size": str(source_stat.st_size),
        "source_mtime_ns": str(source_stat.st_mtime_ns),
        "vcd_mtime": str(source_stat.st_mtime_ns),
        "waveq_version": __version__,
        "signal_count": str(signal_count),
        "change_count": "0",
        "run_count": "0",
        "edge_count": "0",
        "min_tick": "0",
        "max_tick": "0",
        "ready_metadata": "1" if timescale is not None else "0",
        "ready_changes": "0",
        "ready_runs": "0",
        "ready_edges": "0",
    }
    if timescale is not None:
        meta["timescale_number"] = str(timescale.number)
        meta["timescale_unit"] = timescale.unit
    return meta


def _parse_header(source: Path) -> tuple[Timescale, list[SignalDef]]:
    with _open_text(source) as fh:
        return _parse_header_stream(fh)


def build_metadata_database(vcd_path: str | Path, cache_dir: str | Path | None = None, *, rebuild: bool = False) -> BuildStats:
    source = Path(vcd_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    if not is_native(source):
        raise ValueError("WaveQ parses .vcd and .evcd (optionally .gz); open formats such as .fst are converted first by the engine")

    db_path = cache_path_for(source, cache_dir)
    status = cache_status(source, cache_dir)
    if not rebuild and status.get("cache_valid") and "metadata" in status.get("ready_levels", []):
        meta = status.get("metadata", {})
        return BuildStats(
            db_path=db_path,
            signal_count=int(meta.get("signal_count", 0)),
            change_count=int(meta.get("change_count", 0)),
            run_count=int(meta.get("run_count", 0)),
            edge_count=int(meta.get("edge_count", 0)),
            min_tick=int(meta.get("min_tick", 0)),
            max_tick=int(meta.get("max_tick", 0)),
            build_seconds=0.0,
            source_size_bytes=source.stat().st_size,
            size_class=size_class_for_bytes(source.stat().st_size),
            built_level="metadata",
            reused=True,
        )

    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    start = time.perf_counter()
    con = _open_build_db(tmp_path)
    _create_schema(con)
    source_stat = source.stat()
    _insert_meta(con, {
        "schema_version": str(_SCHEMA_VERSION),
        "source_path": str(source),
        "source_size_bytes": str(source_stat.st_size),
        "source_mtime_ns": str(source_stat.st_mtime_ns),
        "build_state": "metadata_building",
        "ready_metadata": "0",
        "ready_changes": "0",
        "ready_runs": "0",
        "ready_edges": "0",
    })
    timescale, signal_defs = _parse_header(source)
    _insert_signals(con, signal_defs)
    meta = _base_meta(source, source_stat, timescale, len(signal_defs))
    meta["build_state"] = "metadata_ready"
    _insert_meta(con, meta)
    _create_indexes(con, "metadata")
    _finish_build_db(con)
    _replace_cache(tmp_path, db_path)
    return BuildStats(
        db_path=db_path,
        signal_count=len(signal_defs),
        change_count=0,
        run_count=0,
        edge_count=0,
        min_tick=0,
        max_tick=0,
        build_seconds=time.perf_counter() - start,
        source_size_bytes=source_stat.st_size,
        size_class=size_class_for_bytes(source_stat.st_size),
        built_level="metadata",
        reused=False,
    )


def indexed_subset(db_path: str | Path) -> set[str] | None:
    """Full paths a subset cache indexed, or None when the cache indexes every signal (or is unreadable)."""
    try:
        con = connect(db_path, readonly=True)
        try:
            meta = _meta_dict(con)
            if meta.get("index_subset") != "1":
                return None
            return {r[0] for r in con.execute("SELECT s.full_path FROM indexed_signals i JOIN signals s ON s.signal_id=i.signal_id")}
        finally:
            con.close()
    except sqlite3.Error:
        return None


def build_database(
    vcd_path: str | Path,
    cache_dir: str | Path | None = None,
    *,
    rebuild: bool = False,
    level: str = "full",
    signals: Iterable[str] | None = None,
) -> BuildStats:
    """Build (or reuse) the indexed cache.

    signals: exact full paths to index. The whole dump is still parsed, but only these signals'
    changes, runs and edges are stored, and the cache records the subset so queries on any other
    signal fail instead of reading as never-changing. An existing cache is reused when it covers the
    request; asking for more signals rebuilds with the union of old and new.
    """
    if level not in {"changes", "runs", "edges", "full"}:
        raise ValueError("index level must be one of: changes, runs, edges, full")
    build_runs = level in {"runs", "full"}
    build_edges = level in {"edges", "full"}
    source = Path(vcd_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    if not is_native(source):
        raise ValueError("WaveQ parses .vcd and .evcd (optionally .gz); open formats such as .fst are converted first by the engine")

    db_path = cache_path_for(source, cache_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    requested_ready = ["metadata", "changes"]
    if level in {"runs", "full"}:
        requested_ready.append("runs")
    if level in {"edges", "full"}:
        requested_ready.append("edges")
    status = cache_status(source, cache_dir)
    wanted: set[str] | None = set(signals) if signals else None
    existing_subset = indexed_subset(db_path) if status.get("cache_valid") else None
    covers = existing_subset is None or (wanted is not None and wanted <= existing_subset)
    if wanted is not None and existing_subset is not None and not covers:
        wanted |= existing_subset
    if not rebuild and status.get("cache_valid") and covers and all(x in status.get("ready_levels", []) for x in requested_ready):
        meta = status.get("metadata", {})
        return BuildStats(
            db_path=db_path,
            signal_count=int(meta.get("signal_count", 0)),
            change_count=int(meta.get("change_count", 0)),
            run_count=int(meta.get("run_count", 0)),
            edge_count=int(meta.get("edge_count", 0)),
            min_tick=int(meta.get("min_tick", 0)),
            max_tick=int(meta.get("max_tick", 0)),
            build_seconds=0.0,
            source_size_bytes=source.stat().st_size,
            size_class=size_class_for_bytes(source.stat().st_size),
            built_level=level,
            reused=True,
            indexed_signals=len(existing_subset) if existing_subset is not None else None,
        )

    carried_clocks = _existing_clock_defs(db_path) if build_edges else []
    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    start = time.perf_counter()
    con = _open_build_db(tmp_path)
    _create_schema(con)

    source_stat = source.stat()
    _insert_meta(con, {
        "schema_version": str(_SCHEMA_VERSION),
        "source_path": str(source),
        "source_size_bytes": str(source_stat.st_size),
        "source_mtime_ns": str(source_stat.st_mtime_ns),
        "build_state": f"{level}_building",
        "ready_metadata": "0",
        "ready_changes": "0",
        "ready_runs": "0",
        "ready_edges": "0",
    })
    id_to_signals: dict[str, list[SignalDef]] = {}
    current_tick = 0
    min_tick: int | None = None
    max_tick = 0
    ordinal = 0
    change_count = 0
    run_count = 0
    edge_count = 0
    value_ids: dict[str, int] = {}
    value_xz: dict[int, tuple[int, int]] = {}
    current_value: dict[int, str] = {}
    current_run_start: dict[int, int] = {}
    pending_final_by_id: dict[str, str] = {}
    last_raw_by_id: dict[str, str] = {}
    changes_batch: list[tuple[int, int, int, int]] = []
    runs_batch: list[tuple[int, int, int, int]] = []
    edges_batch: list[tuple[int, int, int, int, str]] = []
    VALUE_BATCH = 10000

    values_batch: list[tuple[int, str, int, int, str | None]] = []

    def value_id(raw: str) -> int:
        cached = value_ids.get(raw)
        if cached is not None:
            return cached
        # The table starts empty and every value goes through this dict, so ids can be assigned
        # here and inserted in batches instead of one INSERT per new value.
        has_x, has_z, int_value = _value_flags(raw)
        vid = len(value_ids) + 1
        values_batch.append((vid, raw, has_x, has_z, int_value))
        value_ids[raw] = vid
        value_xz[vid] = (has_x, has_z)
        return vid

    def classify(prev: str, new: str, prev_vid: int, new_vid: int, scalar: bool) -> str:
        # Edge kind from canonical raw values; x/z flags are computed once per interned value.
        if scalar:
            if prev == "0" and new == "1":
                return "rise"
            if prev == "1" and new == "0":
                return "fall"
        p_has_x, p_has_z = value_xz[prev_vid]
        n_has_x, n_has_z = value_xz[new_vid]
        if not p_has_x and n_has_x:
            return "x_enter"
        if p_has_x and not n_has_x:
            return "x_exit"
        if not p_has_z and n_has_z:
            return "z_enter"
        if p_has_z and not n_has_z:
            return "z_exit"
        return "scalar_change" if scalar else "vector_change"

    value_id("x")  # every signal starts as x before its first dump

    def flush_batches(force: bool = False) -> None:
        nonlocal changes_batch, runs_batch, edges_batch, values_batch
        if values_batch and (force or len(values_batch) >= VALUE_BATCH):
            con.executemany("INSERT INTO values_interned(value_id, raw_value, has_x, has_z, int_value) VALUES(?, ?, ?, ?, ?)", values_batch)
            values_batch = []
        if changes_batch and (force or len(changes_batch) >= VALUE_BATCH):
            con.executemany("INSERT INTO changes(signal_id, tick, ordinal, value_id) VALUES(?, ?, ?, ?)", changes_batch)
            changes_batch = []
        if runs_batch and (force or len(runs_batch) >= VALUE_BATCH):
            con.executemany("INSERT INTO signal_runs(signal_id, start_tick, end_tick, value_id) VALUES(?, ?, ?, ?)", runs_batch)
            runs_batch = []
        if edges_batch and (force or len(edges_batch) >= VALUE_BATCH):
            con.executemany(
                "INSERT INTO signal_edges(signal_id, tick, from_value_id, to_value_id, edge_kind) VALUES(?, ?, ?, ?, ?)",
                edges_batch,
            )
            edges_batch = []

    def flush_pending(final_tick: int) -> None:
        nonlocal run_count, edge_count, pending_final_by_id
        if not pending_final_by_id:
            return
        for id_code, raw in list(pending_final_by_id.items()):
            for sig in id_to_signals.get(id_code, []):
                prev = current_value.get(sig.signal_id, "x")
                if raw == prev:
                    continue
                start_tick = current_run_start[sig.signal_id]
                if build_runs and final_tick > start_tick:
                    runs_batch.append((sig.signal_id, start_tick, final_tick, value_id(prev)))
                    run_count += 1
                if build_edges:
                    prev_vid = value_id(prev)
                    new_vid = value_id(raw)
                    edges_batch.append((sig.signal_id, final_tick, prev_vid, new_vid, classify(prev, raw, prev_vid, new_vid, sig.width == 1)))
                    edge_count += 1
                current_value[sig.signal_id] = raw
                current_run_start[sig.signal_id] = final_tick
        pending_final_by_id = {}
        flush_batches()

    def record_change(raw: str, id_code: str) -> None:
        nonlocal change_count, ordinal, min_tick
        sigs = id_to_signals.get(id_code)
        if not sigs:
            return
        if last_raw_by_id.get(id_code) == raw:
            # Re-dump of the value already held ($dumpall, $dumpon, redundant writes): not a change.
            return
        last_raw_by_id[id_code] = raw
        if min_tick is None:
            min_tick = current_tick
        vid = value_id(raw)
        for sig in sigs:
            ordinal += 1
            changes_batch.append((sig.signal_id, current_tick, ordinal, vid))
            change_count += 1
        pending_final_by_id[id_code] = raw
        flush_batches()

    with _open_text(source) as fh:
        timescale, signal_defs = _parse_header_stream(fh)
        if wanted is not None:
            unknown = sorted(wanted - {sig.full_path for sig in signal_defs})
            if unknown:
                con.close()
                tmp_path.unlink(missing_ok=True)
                raise ValueError(f"signals not in the VCD header: {', '.join(unknown[:20])}")
        indexed_defs = [sig for sig in signal_defs if wanted is None or sig.full_path in wanted]
        # Only indexed signals are wired to their id codes, so every other value change is skipped
        # at the first dictionary lookup.
        for sig in indexed_defs:
            id_to_signals.setdefault(sig.id_code, []).append(sig)
            current_value[sig.signal_id] = "x"
            current_run_start[sig.signal_id] = 0
        _insert_signals(con, signal_defs)

        skip_until_end = False
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            if skip_until_end:
                if line == "$end" or line.endswith((" $end", "	$end")):
                    skip_until_end = False
                continue
            first = line[0]
            if first == "#":
                flush_pending(current_tick)
                try:
                    current_tick = int(line[1:].strip())
                except ValueError as exc:
                    raise ValueError(f"malformed VCD timestamp: {line!r}") from exc
                max_tick = max(max_tick, current_tick)
                continue
            if first == "$":
                # Skip non-dump directives such as a multi-line $comment through their $end,
                # so their text is never misread as value changes.
                keyword = line.split(maxsplit=1)[0]
                if keyword not in _DUMP_BLOCKS and not line.endswith((" $end", "	$end")):
                    skip_until_end = True
                continue
            if first in "01xXzZ":
                record_change(_canonical_scalar(first), line[1:].strip())
                continue
            if first == "p":
                # Extended VCD port change: p<one state char per bit> <strength0> <strength1> <id>
                parts = line.split()
                if len(parts) >= 2:
                    bits = "".join(_EVCD_STATE.get(ch, "x") for ch in parts[0][1:])
                    if len(bits) == 1:
                        record_change(bits, parts[-1])
                    elif bits:
                        record_change("b" + bits, parts[-1])
                continue
            if first in "bBrRsS":
                parts = line.split(maxsplit=1)
                if len(parts) != 2:
                    continue
                record_change(_canonical_vector(first, parts[0][1:]), parts[1].strip())
                continue
            # Ignore anything else that is not a recognizable value change.

    flush_pending(current_tick)
    end_tick = max_tick + 1
    if build_runs:
        for sig in indexed_defs:
            start_tick = current_run_start.get(sig.signal_id, 0)
            raw = current_value.get(sig.signal_id, "x")
            if end_tick > start_tick:
                runs_batch.append((sig.signal_id, start_tick, end_tick, value_id(raw)))
                run_count += 1
    flush_batches(force=True)

    meta = _base_meta(source, source_stat, timescale, len(signal_defs))
    meta.update({
        "build_state": "ready" if level == "full" else f"{level}_ready",
        "ready_changes": "1",
        "ready_runs": "1" if build_runs else "0",
        "ready_edges": "1" if build_edges else "0",
        "change_count": str(change_count),
        "run_count": str(run_count),
        "edge_count": str(edge_count),
        "min_tick": str(min_tick if min_tick is not None else 0),
        "max_tick": str(max_tick),
        "index_subset": "1" if wanted is not None else "0",
        "indexed_signal_count": str(len(indexed_defs)),
    })
    if wanted is not None:
        con.execute("CREATE TABLE indexed_signals (signal_id INTEGER PRIMARY KEY)")
        con.executemany("INSERT INTO indexed_signals(signal_id) VALUES(?)", ((sig.signal_id,) for sig in indexed_defs))
    _insert_meta(con, meta)
    _create_indexes(con, level)
    # Keep clocks defined against the previous cache; re-derive them from the new edges.
    for full_path, edge_kind in carried_clocks:
        row = con.execute("SELECT signal_id FROM signals WHERE full_path=?", (full_path,)).fetchone()
        if row:
            rebuild_clock_edges(con, int(row[0]), edge_kind)
    _finish_build_db(con)
    _replace_cache(tmp_path, db_path)
    elapsed = time.perf_counter() - start
    return BuildStats(
        db_path=db_path,
        signal_count=len(signal_defs),
        change_count=change_count,
        run_count=run_count,
        edge_count=edge_count,
        min_tick=min_tick if min_tick is not None else 0,
        max_tick=max_tick,
        build_seconds=elapsed,
        source_size_bytes=source_stat.st_size,
        size_class=size_class_for_bytes(source_stat.st_size),
        built_level=level,
        reused=False,
        indexed_signals=len(indexed_defs) if wanted is not None else None,
    )
