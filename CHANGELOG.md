# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `find_times` MCP tool and `waveq find` CLI command: find when ANDed conditions (`==`/`!=` against x, z, decimal, hex, or binary) hold, as time intervals or as clock cycles sampled just before (default) or at each edge

### Performance

- `window_digest` gathers values and counts for all signals in a fixed number of batched queries instead of several per signal (~5x faster in the benchmark)
- `values_at` reads all held values in one query

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
