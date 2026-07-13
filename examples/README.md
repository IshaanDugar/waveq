# Examples

- `sample.vcd`: deterministic fixture used by the README, tests, and smoke checks.

Useful checks:

```bash
python -m waveq.cli info examples/sample.vcd
python -m waveq.cli value examples/sample.vcd tb.valid 15ns
python -m waveq.cli values examples/sample.vcd 30ns tb.u_apb.psel tb.u_apb.penable "tb.u_apb.paddr [7:0]"
python -m waveq.cli digest examples/sample.vcd 30ns 10ns --scope u_apb
```
