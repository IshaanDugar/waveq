# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 3.0.x   | Yes       |

## Reporting a vulnerability

If you find a security issue, please **do not** open a public GitHub issue.

Email the maintainer via the contact on their [GitHub profile](https://github.com/IshaanDugar), or open a private security advisory on the repository once it is published.

## Threat model

WaveQ is designed to run **locally**:

- It reads VCD files you point it at and writes SQLite caches alongside them
- The MCP server uses stdio JSON-RPC — no network listener
- It does not send waveform data to external services

**Residual risks to be aware of:**

- Maliciously crafted VCD files could stress parsing or SQLite storage (denial of service on your machine)
- Absolute paths to VCD files are stored in local cache metadata
- MCP clients can invoke any registered tool — only connect WaveQ to clients you trust

We welcome responsible disclosure of parsing or query-engine issues that could affect local safety or integrity.
