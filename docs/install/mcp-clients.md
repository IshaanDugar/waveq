# MCP client setup

WaveQ is an MCP server over stdio. Every client needs:

- **Command:** path to Python in your venv, or `waveq-mcp` if on PATH
- **Args:** `["-m", "waveq.mcp_server"]` (if using Python directly)
- **Cwd:** the WaveQ repository root (recommended)

Install first:

```bash
git clone https://github.com/IshaanDugar/waveq.git
cd waveq
python -m venv .venv
pip install -e .
```

Verify:

```bash
python -m waveq.mcp_server --version
```

---

## Cursor

Edit your MCP config file:

- **macOS / Linux:** `~/.cursor/mcp.json`
- **Windows:** `%USERPROFILE%\.cursor\mcp.json`

```json
{
  "mcpServers": {
    "waveq": {
      "command": "/absolute/path/to/waveq/.venv/bin/python",
      "args": ["-m", "waveq.mcp_server"],
      "cwd": "/absolute/path/to/waveq"
    }
  }
}
```

On Windows, use backslashes escaped in JSON:

```json
{
  "mcpServers": {
    "waveq": {
      "command": "C:\\Users\\you\\waveq\\.venv\\Scripts\\python.exe",
      "args": ["-m", "waveq.mcp_server"],
      "cwd": "C:\\Users\\you\\waveq"
    }
  }
}
```

Restart Cursor (or reload MCP servers). WaveQ should appear as `waveq` with 19 tools.

**Tip:** Add [AGENTS.md](../../AGENTS.md) or its contents to your project rules so the agent follows the recommended WaveQ workflow.

---

## Claude Desktop

Edit the Claude Desktop config:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "waveq": {
      "command": "/absolute/path/to/waveq/.venv/bin/python",
      "args": ["-m", "waveq.mcp_server"],
      "cwd": "/absolute/path/to/waveq"
    }
  }
}
```

Restart Claude Desktop after saving.

---

## OpenAI Codex

See [codex-windows.md](codex-windows.md) for Windows `config.toml` setup.

---

## Other MCP hosts

Any client that supports stdio MCP servers can run WaveQ. The pattern is always:

| Field | Value |
|-------|-------|
| Transport | stdio |
| Command | `python` (venv) or `waveq-mcp` |
| Args | `-m waveq.mcp_server` (if using python) |
| Working directory | WaveQ repo root |

WaveQ writes **only** JSON-RPC responses to stdout. Logs and errors go to stderr.

---

## Example agent prompts

**Quick smoke test:**

```text
Use WaveQ on examples/sample.vcd. Report waveform_info after configuring.
```

**Debug near a failure:**

```text
Use WaveQ on sim/fail.vcd. build_index(level="full") if needed.
Resolve reset, clk, valid, ready, and err. Define clk as rising-edge clock.
Summarize the 50 ns window around 1850 ns.
```

**Cycle-based inspection:**

```text
Use WaveQ to report valid, ready, addr, data, and err at cycles 84 through 90.
```

See [AGENTS.md](../../AGENTS.md) for the full recommended workflow.
