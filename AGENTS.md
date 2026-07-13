# WaveQ usage guidance for AI agents

Use WaveQ for exact VCD observations, not for protocol proof.

Preferred flow:

1. Call `configure_waveform` once for the target `.vcd` or `.vcd.gz`.
2. Call `waveform_info` to learn the timescale and range.
3. Use `resolve_signals` before querying approximate or leaf-only names.
4. Prefer `values_at`, `changes_many`, and `window_digest` over many single-signal calls.
5. Call `define_clock` before cycle-based tools.
6. Treat `ok: false` as a hard failure. Do not continue as though the query succeeded.
7. Do not infer protocol correctness unless the queried evidence directly supports it.

Good first query near a failure:

```text
window_digest(center_time=<failure time>, radius=<small window>, scope=<suspected block>)
```

Then follow up with exact values and changes for the small set of relevant signals.
