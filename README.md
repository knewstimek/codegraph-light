# CodeGraph Light

CodeGraph Light is a local code-graph MCP server for coding agents. It keeps a
small, predictable tool surface while reusing the mature parsing, graph, and
query engine from
[`DeusData/codebase-memory-mcp`](https://github.com/DeusData/codebase-memory-mcp).

The project is designed around three constraints:

- standard MCP over stdio for Codex and other MCP clients;
- native Windows and Linux executables with no hosted service or API key;
- a complete MCP tool schema below 2,000 tokens.

## Tools

The default profile exposes seven tools:

| Tool | Purpose |
| --- | --- |
| `index` | Build or refresh a repository graph. |
| `search` | Find code symbols by name, kind, or path. |
| `trace` | Find callers, callees, and call paths. |
| `source` | Return source code for a graph symbol. |
| `overview` | Summarize repository structure and entry points. |
| `schema` | Describe graph nodes, properties, and relationships. |
| `query` | Run a bounded, read-only graph query. |

## Profiles

`default` is selected when `--tool-profile` is omitted.

| Profile | Tools |
| --- | --- |
| `default` | All seven tools |
| `minimal` | `index`, `search`, `trace`, `source` |
| `analysis` | `search`, `trace`, `source`, `overview`, `schema`, `query` |

Example MCP configuration:

```json
{
  "mcpServers": {
    "codegraph-light": {
      "command": "/path/to/codegraph-light",
      "args": []
    }
  }
}
```

Use a restricted profile when desired:

```json
{
  "mcpServers": {
    "codegraph-light-minimal": {
      "command": "/path/to/codegraph-light",
      "args": ["--tool-profile=minimal"]
    }
  }
}
```

## Build

The inherited native build currently uses `Makefile.cbm`:

```sh
gmake -j -f Makefile.cbm cbm SANITIZE=
```

The output is `build/c/codegraph-light` (`.exe` on Windows). Use a current
compiler and SDK supported by the upstream project.

Run the cross-platform MCP smoke test after building:

```sh
python tests/light_mcp_smoke.py --functional build/c/codegraph-light
```

Installing the optional `tiktoken` package and adding `--require-tokenizer`
also verifies the 2,000-token budget against `cl100k_base` and `o200k_base`.
The default profile currently measures 691 tokens or fewer with those two
encodings. CI repeats the build and functional smoke test on Windows and Linux.

The inherited package wrappers under `pkg/` remain sync references only. They
still describe the upstream distribution and must not be used to publish
CodeGraph Light. Use the native build and manual MCP configuration above until
derivative packages are added.

## Upstream and license

CodeGraph Light is based on and synchronized from
[`DeusData/codebase-memory-mcp`](https://github.com/DeusData/codebase-memory-mcp).
The original project and this derivative are distributed under the MIT License.
Original copyright and license notices are preserved in [LICENSE](LICENSE).

Bundled components retain their own licenses and notices. See
[THIRD_PARTY.md](THIRD_PARTY.md) before redistributing binaries.
