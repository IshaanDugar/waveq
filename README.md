# WaveQ

[![CI](https://github.com/IshaanDugar/waveq/actions/workflows/ci.yml/badge.svg)](https://github.com/IshaanDugar/waveq/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

**Your AI assistant can read code — WaveQ lets it read waveforms too.**

WaveQ is a local [MCP](https://modelcontextprotocol.io/) server and CLI that indexes `.vcd` / `.vcd.gz` files into SQLite, then answers precise questions about signal values, transitions, edges, and clock cycles. Instead of dumping megabytes of raw VCD text into context, your agent gets compact, structured answers — and honest failures when data is missing or ambiguous.

Zero runtime dependencies. Pure Python. Runs entirely on your machine.

## Quick start

**Requirements:** Python 3.10+

```bash
git clone https://github.com/IshaanDugar/waveq.git
cd waveq
python -m venv .venv

# Linux / macOS
source .venv/bin/activate
pip install -e .

# Windows
.\.venv\Scripts\activate
pip install -e .
```

Try the included fixture:

```bash
waveq info examples/sample.vcd
waveq value examples/sample.vcd tb.valid 16ns
waveq digest examples/sample.vcd 30ns 10ns --scope u_apb
```

Start the MCP server (stdio JSON-RPC):

```bash
waveq-mcp
# or: python -m waveq.mcp_server
```

## Connect to your AI tool

WaveQ speaks MCP over stdio. Point any MCP-capable client at `python -m waveq.mcp_server` (or the `waveq-mcp` script after install).

| Client | Setup guide |
|--------|-------------|
| **Cursor** | [docs/install/mcp-clients.md](docs/install/mcp-clients.md#cursor) |
| **Claude Desktop** | [docs/install/mcp-clients.md](docs/install/mcp-clients.md#claude-desktop) |
| **OpenAI Codex** | [docs/install/codex-windows.md](docs/install/codex-windows.md) |
| **Other MCP hosts** | [docs/install/mcp-clients.md](docs/install/mcp-clients.md) |

Example agent prompt once connected:

```text
Use WaveQ on examples/sample.vcd. Check index_status; if query_ready is false,
build_index(level="full"). Resolve clk and reset, define clk as rising-edge clock,
and summarize activity around 10ns.
```

See [AGENTS.md](AGENTS.md) for the recommended tool workflow.

## What it does

- Parse VCD/VCD.GZ into a reusable local SQLite cache
- Resolve fuzzy signal names to exact hierarchical paths
- Query values at a time, changes in a window, edges, and stability
- Summarize a debug window with `window_digest` instead of raw dumps
- Build a clock cycle index and query by cycle
- Return `ok: false` on missing signals, bad times, or partial batch failures — no silent success

## What it does not do

WaveQ is **not** a simulator, waveform viewer, assertion engine, or protocol checker. It reports observed VCD facts. Do not use it alone to prove APB/AHB/AXI correctness, CDC safety, or formal properties. It does not read FSDB, VPD, or FST.

## How it works

WaveQ is size-aware. Small VCDs may auto-index on configure; larger files stay metadata-only until you explicitly call `build_index`.

| VCD size | Class | Default on `configure_waveform` |
|---------:|-------|----------------------------------|
| < 10 MB | small | May build full indexes |
| 10–50 MB | medium | Metadata only |
| 50–250 MB | large | Metadata only |
| > 250 MB | huge | Metadata only |

Recommended agent workflow:

1. `configure_waveform` → 2. `index_status` → 3. `build_index` if needed → 4. `resolve_signals` → 5. `define_clock` (for cycles) → 6. `window_digest` near the failure → 7. follow up with `values_at`, `changes_many`, `edges`

### MCP tools (18)

| Tool | Purpose |
|------|---------|
| `configure_waveform` | Load VCD metadata and cache |
| `index_status` | Check which index levels are ready |
| `build_index` | Build `changes`, `runs`, `edges`, or `full` |
| `waveform_info` | Timescale, signal count, time range |
| `resolve_signals` | Match approximate names to exact paths |
| `value_at` / `values_at` | Signal value(s) at one time |
| `changes` / `changes_many` | Transition lists in a window |
| `edges` / `first_edge_after` | Precomputed edge facts |
| `stable_between` | Held-value check over a range |
| `window_digest` | Compact "what changed here?" summary |
| `define_clock` | Build cycle index from clock edges |
| `time_to_cycle` / `cycle_to_time` | Time ↔ cycle conversion |
| `values_at_cycle` / `changes_by_cycle` | Cycle-based queries |

### Result shape

Success:

```json
{"ok": true, "status": "ok", "summary": "tb.valid=1 at 16ns", "data": {}, "confidence": 1.0}
```

Failure (batch tools do not hide partial errors):

```json
{"ok": false, "status": "partial_failure", "summary": "One or more signal lookups failed", "confidence": 0.0}
```

### Time syntax

Raw ticks (`42`) or unit times (`42ns`, `1.5us`). Unit times convert exactly via rational arithmetic — non-integral ticks fail instead of rounding silently.

## Performance

Synthetic benchmarks on a typical dev laptop (Windows, Python 3.x). Regenerate anytime:

```bash
python benchmarks/benchmark_waveq.py --output reports
```

### Query speed (median, after full index)

| Case | VCD size | Signals | Changes | `value_at` | `values_at` ×8 | `window_digest` | Cache reuse |
|------|---------:|--------:|--------:|-----------:|---------------:|----------------:|------------:|
| small | 45 KB | 32 | 6,871 | 0.06 ms | 0.49 ms | 5.6 ms | 1.6 ms |
| medium | 12.6 MB | 128 | 92,815 | 0.06 ms | 0.47 ms | 6.1 ms | 1.5 ms |
| large | 55.0 MB | 32 | 6,871 | 0.06 ms | 0.47 ms | 5.9 ms | 1.4 ms |

### Index build cost (one-time per VCD)

| Case | `configure_waveform` | Query-ready after configure? | Full `build_index` |
|------|---------------------:|------------------------------|-------------------:|
| small | 72 ms | yes (auto-indexed) | 57 ms |
| medium | 27 ms | no (metadata only) | 672 ms |
| large | 32 ms | no (metadata only) | 148 ms |

Large files stay metadata-only at configure time — you pay for indexing only when you need it.

### Why this matters for AI-assisted debug

| Without WaveQ | With WaveQ |
|---------------|------------|
| A **12 MB VCD** is millions of tokens — it cannot fit in any agent context window | `window_digest` returns a **compact JSON summary** in ~6 ms |
| Agent reads raw text, guesses signal hierarchy, may silently miss data | `resolve_signals` + `values_at` return **exact paths and values** with `ok: false` on failure |
| Re-parsing the same VCD on every question | SQLite cache **reattaches in ~1.5 ms** — index once, query many times |
| 8 separate signal lookups = 8 file scans | `values_at` / `changes_many` batch **8 signals in under 1 ms** |
| "What happened near the failure?" → dump entire time range | `window_digest(center, radius)` returns only what changed in that window |

**Rule of thumb:** after the one-time index build, point queries are sub-millisecond and debug-window summaries land in single-digit milliseconds — even on 50+ MB files. The real win is not raw speed alone; it is making waveform evidence **queryable and context-sized** for an AI agent instead of dumping unusable VCD text.

## CLI reference

```bash
waveq info <vcd>
waveq configure <vcd>
waveq index-status <vcd>
waveq build-index <vcd> --level full
waveq resolve <vcd> <query>
waveq value <vcd> <signal> <time>
waveq values <vcd> <time> <signal> [...]
waveq changes <vcd> <signal> <start> <end>
waveq edges <vcd> <signal> --kind rise --start 0ns --end 50ns
waveq digest <vcd> <center> <radius> [--scope <hier>]
```

## Development

```bash
python -m py_compile waveq/*.py          # PowerShell: py_compile (Get-ChildItem waveq\*.py)
python -m unittest discover -s tests -v
python benchmarks/benchmark_waveq.py --output reports   # optional; writes to reports/
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## Repository layout

```text
waveq/           VCD parser, SQLite indexer, query engine, MCP server, CLI
tests/           Unit and MCP behavior tests
benchmarks/      Synthetic VCD performance + correctness checks
examples/        Sample VCD and smoke commands
docs/install/    MCP client setup guides
```

## License

MIT — see [LICENSE](LICENSE).

Originally developed by Ishaan Dugar during an internship at Ambiq Micro. Ambiq Micro is not the author or maintainer of this project.

## Contributing

Issues and pull requests welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
