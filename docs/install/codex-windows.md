# WaveQ with OpenAI Codex on Windows

This guide wires WaveQ into [OpenAI Codex](https://openai.com/codex/) on a Windows laptop. It uses your existing Codex `config.toml` — no global MCP registration required.

## 1. Install WaveQ

Requires Python 3.10+. [Download Python for Windows](https://www.python.org/downloads/windows/) if needed.

```cmd
git clone https://github.com/IshaanDugar/waveq.git
cd waveq
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m waveq.mcp_server --version
```

## 2. Update Codex `config.toml`

Open your Codex config:

```cmd
notepad %USERPROFILE%\.codex\config.toml
```

Remove any old `[mcp_servers.waveq]` block and tool overrides. Add:

```toml
[mcp_servers.waveq]
command = "C:\\path\\to\\waveq\\.venv\\Scripts\\python.exe"
args = ["-m", "waveq.mcp_server"]
cwd = "C:\\path\\to\\waveq"
enabled = true
required = false
startup_timeout_sec = 30
tool_timeout_sec = 120
```

Replace `C:\\path\\to\\waveq` with your actual clone path (use doubled backslashes).

Optional per-tool approval (recommended for first use):

```toml
[mcp_servers.waveq.tools.configure_waveform]
approval_mode = "approve"

[mcp_servers.waveq.tools.build_index]
approval_mode = "approve"
```

Repeat for other tools as needed. See the [README tool table](../../README.md#mcp-tools-19) for the full list.

## 3. Verify in Codex

```cmd
codex
```

Inside Codex, run `/mcp` — you should see `waveq` listed.

## 4. Test prompt

```text
Use the waveq MCP server. Load examples/sample.vcd from the WaveQ repo.
Check index_status; if query_ready is false, build_index(level="full").
Resolve clk and reset, define clk as rising-edge clock, and summarize activity around 10ns.
```

## Troubleshooting

**Codex does not show WaveQ under `/mcp`**

Verify the server starts manually:

```cmd
C:\path\to\waveq\.venv\Scripts\python.exe -m waveq.mcp_server --version
```

If that works, re-check `command` and `cwd` in `config.toml`.

**Tool call times out on first VCD load**

`configure_waveform` returns quickly for medium/large/huge files (metadata only). If indexed queries fail with `index_required`, ask the agent to call `build_index(level="full")` first.

For very large VCDs, increase the timeout:

```toml
tool_timeout_sec = 300
```

**Signal not found**

Ask the agent to call `resolve_signals` before querying. Do not guess exact VCD hierarchy by hand.
