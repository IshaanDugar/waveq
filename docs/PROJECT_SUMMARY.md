# WaveQ: project summary

WaveQ lets an AI agent ask exact questions about a digital simulation waveform (a `.vcd` or `.vcd.gz` file) instead of reading the raw dump. It is a local MCP server and a CLI, written in pure Python with no runtime dependencies, licensed AGPL-3.0-or-later, by Ishaan Dugar.

- Repository: https://github.com/IshaanDugar/waveq
- Site: https://ishaandugar.github.io/waveq/
- Package version: 3.0.0 (the work below is in `[Unreleased]` in the changelog)

## The problem

A simulator writes a VCD as one line per signal change, under timestamp lines. A modest testbench makes tens of megabytes. That does not fit in an agent's context, and an agent that reads slices of it guesses at hierarchy, misreads which value holds at a time, and silently misses changes.

WaveQ turns the dump into an indexed SQLite cache once, then answers questions with small JSON results: the value of a signal at a time, its changes in a window, when a condition held, what was X and since when. Every answer is exact, or it fails explicitly.

## Design rules

1. **Exact or fail.** Every result is `{ok, status, summary, data, confidence, limitations}`. A missing signal, an ambiguous name, a time outside the dump, a non-integral time, a signal not in the index, or one failure inside a batch gives `ok: false`. "Nothing matched" is `ok: true` with status `no_match`, never an empty success that looks like a pass.
2. **Exact time.** Times are raw ticks or unit strings (`16ns`, `1.5us`), converted with rational arithmetic against the dump's `$timescale`; a time that is not a whole number of ticks is rejected, not rounded.
3. **Observations, not proofs.** WaveQ reports what the waveform shows. Tools that pair or summarise (for example `pair_events`) say what they assume in `limitations`. It is not a protocol checker.
4. **Zero dependencies, local only.** The standard library only (`sqlite3`, `gzip`, `argparse`, `json`). Nothing leaves the machine.
5. **Pay for what you query.** Small files index fully on configure; large files stay metadata-only until asked, and can index only the signals needed.

## How it works

### Parsing (`waveq/db.py`)

- The header parser is token-based: `$scope`, `$var` (including multi-line and packed directives), `$timescale`, `$upscope`, `$enddefinitions` (required). Each `$var` becomes a signal with its full dotted path, scope, leaf, kind and width. Aliases (several signals sharing one id code) are supported.
- The body parser streams line by line. It handles `#time`, scalar changes (`0 1 x z`), vectors (`b…`), reals (`r…`) and strings (`s…`); skips `$comment` and other non-dump blocks to their `$end`; treats `$dumpvars`, `$dumpall`, `$dumpon` and `$dumpoff` contents as value changes; and drops re-dumps of a value already held, so they never count as changes.
- Several changes to one signal inside one timestep are all kept in `changes` (with an ordinal), while runs and edges use the value at the end of the timestep.

### The cache

One SQLite file per VCD, keyed by the VCD's path and schema version, validated against its size and mtime (a changed VCD reads as `stale` and is rebuilt). Tables:

| Table | Holds | Used for |
|---|---|---|
| `signals` | path, scope, leaf, kind, width, lower-case copies | name resolution, scopes |
| `values_interned` | each distinct raw value once, with `has_x`, `has_z`, integer value | compact rows, fast predicates |
| `changes` | (signal, tick, ordinal, value) | change lists, stability, activity, digests |
| `signal_runs` | (signal, start, end, value): the value held over each interval | `value_at`, windows, `find_times`, diffs |
| `signal_edges` | (signal, tick, from, to, kind): rise, fall, x/z enter/exit, scalar/vector change | edges, clocks, X tracing, FSMs |
| `clock_edges` | (clock, edge kind, cycle index, tick), built by `define_clock` | cycle tools |
| `indexed_signals` | only in subset caches: which signals were indexed | the `signal_not_indexed` check |
| `meta` | source path/size/mtime, schema, timescale, counts, readiness per level, subset flag | validation and status |

The data tables are `WITHOUT ROWID` with primary keys that start at `signal_id`, so per-signal lookups in either direction read the table itself.

**Index levels:** `metadata` (header only), `changes`, `runs`, `edges`, `full`. **Size classes:** small under 10 MB (fully indexed on configure by default), medium to 50 MB, large to 250 MB, huge above (metadata only on configure unless `index="full"`).

**Builds** go to a temporary file with no journal and no fsync, then are renamed over the cache only when complete, so a crashed build never leaves a half-valid cache. Clock definitions survive rebuilds.

**Subset caches:** `build_index(signals=[…])` still parses the whole dump but stores rows only for the named signals. Queries on any other signal fail with `signal_not_indexed`; scope-wide tools list how many they skipped; asking for more signals rebuilds with the union; a full cache serves any subset request.

**Where the cache lives:** `cache_dir` if given, else `WAVEQ_CACHE_DIR`, else `.waveq_cache` beside the VCD, unless that folder is synced (OneDrive, Dropbox, Google Drive, iCloud) or read-only, in which case the per-user cache directory (`%LOCALAPPDATA%\waveq\cache`, `~/Library/Caches/waveq`, `~/.cache/waveq`).

### The engine (`waveq/engine.py`)

`WaveQEngine` holds one open cache. Names resolve by exact path, unique leaf, scope plus leaf, a vector path without its range, then fuzzy matching (only a near-certain fuzzy match is accepted silently; otherwise `ambiguous_signal` or `not_found` with candidates). Resolved names and defined clocks are memoised per open cache.

**Clock sampling:** with a clock, `find_times` and `pair_events` sample each signal one tick before the edge by default (what a flip-flop sees), or at the edge with `sample="at"`. `values_at_cycle` reads at the edge by default and takes `sample="before"` to match.

**Value display:** raw VCD values are always available; vectors are also shown as width-padded hex (`0x0a5`) when every bit is known.

### Interfaces

- **MCP server** (`waveq/mcp_server.py`, `python -m waveq.mcp_server` or `waveq mcp`): JSON-RPC over stdio, `tools/list` with input and output schemas, server instructions that steer agents to the batch and debug tools.
- **CLI** (`waveq/cli.py`): one subcommand per tool family, JSON output, exit code 1 on `ok: false`.

## The 25 MCP tools

| Group | Tools | What they answer |
|---|---|---|
| Setup | `configure_waveform`, `index_status`, `build_index`, `waveform_info` | open or build the cache; readiness; timescale and range |
| Names | `resolve_signals`, `list_scopes` | which exact signal did you mean; browse the hierarchy (metadata only) |
| Values | `value_at`, `values_at`, `changes`, `changes_many`, `stable_between` | value held at a time (with the run it sits in); changes in a window; did it change |
| Edges | `edges`, `first_edge_after` | rises, falls, x/z entries and exits |
| Overview | `window_digest` | what changed around a failure time, signal by signal |
| Search | `find_times` | when did all these conditions hold, as intervals or clock cycles, with optional payload values at each match |
| Cycles | `define_clock`, `time_to_cycle`, `cycle_to_time`, `values_at_cycle`, `changes_by_cycle` | work in clock cycles instead of ticks |
| Debug | `x_report`, `activity`, `state_transitions`, `pair_events`, `first_divergence` | where an X came from; where a hung design went quiet; FSM transitions and dwell; request to response latency and requests that never completed; where two runs first differ |

Typical agent flow: `configure_waveform` (with `index="full"`, or metadata plus a signal subset on a big dump) → `list_scopes` / `resolve_signals` → `define_clock` → `window_digest` near the failure → `find_times`, `values_at`, `changes_many`, or the debug tools.

## Performance (benchmark in `benchmarks/benchmark_waveq.py`, this machine)

| | small (45 KB, 32 signals) | medium (12.6 MB, 128 signals) |
|---|---|---|
| full index build | 0.10 s | 1.27 s |
| cache size | 0.66 MB | 8.2 MB |
| `value_at` | 0.23 ms | 0.23 ms |
| `values_at`, 8 signals | 0.31 ms | 0.14 ms |
| `changes_many`, 8 signals | 3.1 ms | 2.9 ms |
| `window_digest` | 2.3 ms | 2.9 ms |
| `find_times` on a clock | 4.5 ms | 22 ms |

Indexing 4 of 128 signals on the medium dump: 0.13 s and 1.3 MB, against 0.80 s and 15.6 MB for all of them (same run, padded file). The README's own table came from a different machine and has not been refreshed.

## Quality

- 72 unit tests (`python -m unittest discover -s tests`): parser regressions, exact time handling, strict failures, batch equivalence against single queries, brute-force cross-checks for `find_times` and `x_report`, subset rows equal to full-build rows, MCP round trips.
- CI on GitHub Actions; the index build was checked row-for-row against the previous build on every fixture after the performance changes.

## The site (`site/`)

A single static page published with GitHub Pages (`.github/workflows/pages.yml`), built with the Hallmark design skill in its Cobalt theme. The hero shows raw `sample.vcd` lines joined to the JSON answer they become, and a live waveform of the same dump: a cursor travels to 16 ns, the run of `valid` it lands in lights up, a readout says "valid is 1 from 15 to 40 ns", and the matching lines glow. Every diagram is drawn from `examples/sample.vcd` by WaveQ itself (`python site/build_diagrams.py`). Motion is one 9-second loop using transform and opacity only, with a pause control; it pauses off screen and shows a still frame with reduced motion or no script.

## Repository layout

```
waveq/        engine.py (queries), db.py (parse, cache, build), mcp_server.py, cli.py, timeparse.py
tests/        test_waveq.py and fixtures/*.vcd
benchmarks/   benchmark_waveq.py (generates synthetic dumps, writes reports/)
examples/     sample.vcd used by the docs, the site and the tests
site/         index.html, site.css, tokens.css, base.css, build_diagrams.py
docs/         install guides (MCP clients, Codex on Windows) and this summary
AGENTS.md     how an AI agent should use the tools
```

## Limits and open work

- **Formats:** VCD and VCD.GZ only; no FST, FSDB or VPD.
- **One pass over the whole file:** a one-off question on a huge dump still pays a full parse (a subset index cuts the storing, not the reading). A streaming no-SQLite mode for single questions is the next performance step.
- **Conditions:** `==` and `!=` against whole signals, ANDed. No bit-selects (`data[7:4]`), ranges, ORs or temporal sequences yet.
- **Time-window indexing** (index only around the failure) is not built.
- **MCP protocol version** is echoed from the client instead of negotiated.
- **Not a checker:** `pair_events` assumes in-order responses (per ID when IDs are given) and says so.
