---
name: waveq
description: Debug digital simulations from VCD, EVCD or FST waveform dumps with the WaveQ MCP tools. Use when the user mentions a .vcd/.fst file, a waveform, a failing testbench, an X/Z value, a hang, a handshake, latency, or wants to know a signal's value at a time.
---

# Reading waveforms with WaveQ

WaveQ answers exact questions about a waveform dump. Never read the raw dump into context.

1. `configure_waveform(vcd_path, index="full")`. On a very large dump where only a few signals matter, use `index="metadata"` then `build_index(signals=[...])`.
2. `list_scopes` to see the hierarchy, `resolve_signals` for approximate names.
3. `define_clock(clk)` before any cycle-based question.
4. Near a failure time: `window_digest(center_time, radius, scope=...)`.
5. "When did X happen": `find_times(conditions, clock=..., payload=[data signals])`. It samples just before each clock edge by default.
6. Debug helpers:
   - `x_report(time, scope)`: what is X/Z, earliest first (likely origin).
   - `activity(ignore=[clk])`: where a hung design went quiet.
   - `state_transitions(state_signal)`: FSM transitions and time in each state.
   - `pair_events(clock, request, response, request_id=, response_id=)`: latency and requests that never completed.
   - `first_divergence(other_vcd)`: where a passing and a failing run first differ.
7. Treat `ok: false` as a failure, never as an answer. Report observations; do not claim protocol correctness from them.
