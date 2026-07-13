# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
