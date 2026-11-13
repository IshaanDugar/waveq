# Contributing to WaveQ

Thanks for your interest. WaveQ is a small, focused tool — contributions that keep it fast, deterministic, and honest about failures are especially welcome.

## Development setup

```bash
git clone https://github.com/IshaanDugar/waveq.git
cd waveq
python -m venv .venv
source .venv/bin/activate   # Windows: .\.venv\Scripts\activate
pip install -e .
```

## Running tests

```bash
python -m py_compile waveq/*.py
python -m unittest discover -s tests -v
```

PowerShell `py_compile`:

```powershell
python -m py_compile (Get-ChildItem waveq\*.py)
```

Optional benchmark (writes to `reports/`, gitignored):

```bash
python benchmarks/benchmark_waveq.py --output reports
```

## Scope

**In scope:**

- VCD / VCD.GZ parsing and indexing
- Exact waveform queries (values, changes, edges, cycles)
- MCP and CLI interfaces
- Performance and correctness improvements

**Out of scope:**

- Protocol proof (APB/AHB/AXI checkers)
- Non-VCD formats (FSDB, VPD, FST)
- Waveform GUIs or simulators

## Pull requests

1. Fork and branch from `main`
2. Add or update tests for behavior changes
3. Keep the diff focused — prefer extending precomputed tables and batch tools over NL parsing in the server
4. Ensure `python -m unittest discover -s tests -v` passes
5. Open a PR with a clear description of what changed and why

## Code style

- Python 3.10+ with type hints
- Stdlib only — no new runtime dependencies without discussion
- Treat `ok: false` as a hard failure in agent workflows; never paper over missing data

## Questions

Open a [GitHub issue](https://github.com/IshaanDugar/waveq/issues) for bugs, feature ideas, or setup help.

## License

WaveQ is licensed under the GNU AGPL-3.0-or-later. By contributing, you agree that your contribution is licensed under the same terms.
