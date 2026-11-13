# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `find_times` MCP tool and `waveq find` CLI command: find when ANDed conditions (`==`/`!=` against x, z, decimal, hex, or binary) hold, as time intervals or as clock cycles sampled just before (default) or at each edge
- `find_times` `payload`: sample extra signals (address, data, IDs) at every match in the same pass, returned as width-padded hex
- `list_scopes`: browse the hierarchy with direct and subtree signal counts; works on metadata alone, before any index build
- `x_report`: signals holding x/z at a time, ordered by when their unknown stretch began, to trace an X back to its origin
- `activity`: per-signal last-change times and idle signals in a window (with `ignore` for clocks) to locate where a hung design went quiet
- `state_transitions`: from→to transition counts with first/last times, and time spent in each value
- `pair_events`: pair request and response cycles in order (or in order per ID) and report latency stats, unmatched requests, and orphan responses
- `configure_waveform` `index` option (`auto`, `full`, `metadata`): `full` makes any file query-ready in one call
- `values_at_cycle` `sample` option: `before` reads just before the edge, matching `find_times`
- `value_at` / `values_at` results include a `hex` field for fully known vectors
- `WAVEQ_CACHE_DIR` environment variable; the default cache moves to the per-user cache directory when the VCD's folder is synced (OneDrive, Dropbox, Google Drive, iCloud) or read-only
- CLI commands `scopes`, `xreport`, `activity`, `transitions`, `pair`; `find --payload`; `--index` on every command that configures
- `build_index` `signals`: index only the named signals. The whole dump is still parsed but only their rows are stored (6x faster build, 12x smaller cache for 4 of 128 signals in the benchmark dump); queries on other signals fail with `signal_not_indexed`, scope-wide tools say how many signals they skipped, and asking for more signals rebuilds with the union
- Input formats: Extended VCD (`.evcd`, IEEE 1364 port dumps) is read natively; FST, LXT, LXT2 and VZT are converted with GTKWave's open tools (`fst2vcd`, `lxt2vcd`, `vzt2vcd`) when on PATH, cached, and reused until the source changes; GHW and closed formats get a one-line pointer to the right converter or simulator flag
- `benchmarks/realistic_vcd.py`: generates an SoC-style dump with known answers (out-of-order AXI-style reads with one hung request, an FSM, an injected X, filler registers) and checks `pair_events`, `x_report`, `state_transitions`, `activity` and `first_divergence` against them; runs in CI at small size
- Distribution: Claude Code plugin and marketplace (`/plugin install waveq --marketplace IshaanDugar/waveq`) with a waveform-debugging skill; `server.json` for the official MCP registry and a release workflow that publishes to PyPI and the registry; `uvx` one-line MCP config; site metadata (OpenGraph image, JSON-LD, `llms.txt`, sitemap)
- `first_divergence` MCP tool and `waveq diff` CLI command: compare the configured waveform with a second VCD and report the first time they differ, on which signals, and the values just before

### Performance

- `window_digest` gathers values and counts for all signals in a fixed number of batched queries instead of several per signal (~5x faster in the benchmark)
- `values_at` reads all held values in one query
- Index builds write the temporary database without journaling or fsync (it is renamed into place only when complete) and classify edges from per-value x/z flags computed once: full builds ~2x faster in the benchmark
- Dropped four indexes that duplicated `WITHOUT ROWID` primary keys or were never used: caches ~40% smaller
- Resolved signal names and defined clocks are memoized per open cache; cycle tools no longer count every clock cycle on each call
- Query connection uses a larger page cache and memory-mapped reads
- New values are interned in batches with ids assigned in Python, instead of one INSERT per new value (matters for random data buses)

### Fixed

- Unknown edge kinds (e.g. `posedge`) now fail with `bad_edge_kind` instead of returning zero results; MCP schemas and the CLI advertise the valid kinds
- `define_clock` fails with `no_clock_edges` when the signal has no edges of the requested kind instead of defining a 0-cycle clock
- `window_digest`, `changes`, `changes_many`, `stable_between`, `edges`, `first_edge_after`, and `time_to_cycle` reject times outside the waveform with `time_out_of_range`
- Cycle tools distinguish `clock_not_defined`, `cycle_out_of_range`, and `before_first_cycle`
- Vector signals resolve by path without their range (`tb.u_apb.paddr`) and with unspaced ranges (`tb.u_apb.paddr[7:0]`)
- Re-dumps of an unchanged value (`$dumpall`, redundant writes) are no longer stored as changes, so `stable_between` and change counts are accurate
- Clocks defined with `define_clock` survive `build_index(rebuild=True)`
- Text inside multi-line `$comment` blocks in the dump body is no longer parsed as value changes
- Header directives may now span multiple lines or share a line (multi-line `$var`, `$scope`); a missing `$enddefinitions` is reported as an error
- Cache metadata records the real package version

### Changed

- License changed from MIT to GNU AGPL-3.0-or-later. Copies obtained up to commit `bb239b3` remain under MIT.
- Cache schema version bumped to 3; existing caches are rebuilt automatically on next use

## [3.0.0] - 2026-07-14

### Added

- Local MCP server with 18 waveform query tools over stdio JSON-RPC
- CLI (`waveq`) for configure, index, resolve, value, changes, edges, and digest commands
- SQLite-backed VCD indexer with size-aware configure behavior
- Signal run and edge precomputation for fast `value_at`, `stable_between`, and edge queries
- Clock cycle index via `define_clock` and cycle-based query tools
- Exact rational timescale conversion — rejects non-integral unit times
- Strict `ok: false` semantics for missing signals, ambiguous names, bad times, and partial batch failures
- `window_digest` for compact local debug summaries
- Unit tests and synthetic VCD benchmark suite

### Notes

- Zero runtime dependencies (Python 3.10+ stdlib only)
- Supports `.vcd` and `.vcd.gz`

[3.0.0]: https://github.com/IshaanDugar/waveq/releases/tag/v3.0.0
