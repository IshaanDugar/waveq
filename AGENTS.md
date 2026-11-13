# WaveQ usage guidance for AI agents

Use WaveQ for exact VCD observations, not for protocol proof.

Preferred flow:

1. Call `configure_waveform` once for the target `.vcd` or `.vcd.gz`. Pass `index="full"` when you will run value queries, so a large file is query-ready in one call.
2. Call `waveform_info` to learn the timescale and range.
3. Use `resolve_signals` before querying approximate or leaf-only names.
4. Prefer `values_at`, `changes_many`, and `window_digest` over many single-signal calls.
5. Call `define_clock` before cycle-based tools.
6. Use `find_times` for "when/whether did X happen" questions instead of scanning `changes`. With a clock it samples just before each edge (flip-flop view); pass `sample="at"` for the post-edge value.
   Add `payload=[...]` to get the data of every match in the same call instead of following up with `values_at`.
   `values_at_cycle` reads at the edge by default; pass `sample="before"` to see what `find_times` saw for that cycle.
7. Use the debug helpers before hand-rolled scans: `list_scopes` (hierarchy), `x_report` (X origin), `activity` with `ignore=[clock]` (hangs),
   `state_transitions` (FSMs), `pair_events` (request/response latency and requests that never completed).
8. Treat `ok: false` as a hard failure. Do not continue as though the query succeeded.
9. Do not infer protocol correctness unless the queried evidence directly supports it. `pair_events` pairs in order (per ID when given); it is evidence, not a protocol check.

Good first query near a failure:

```text
window_digest(center_time=<failure time>, radius=<small window>, scope=<suspected block>)
```

Then follow up with exact values and changes for the small set of relevant signals.
