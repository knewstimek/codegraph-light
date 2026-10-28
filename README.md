# CodeGraph Light

CodeGraph Light is a local code-graph MCP server for coding agents. It keeps a
small, predictable tool surface while reusing the mature parsing, graph, and
query engine from
[`DeusData/codebase-memory-mcp`](https://github.com/DeusData/codebase-memory-mcp).

The project is designed around three constraints:

- standard MCP over stdio for Codex and other MCP clients;
- native Windows and Linux executables with no hosted service or API key;
- a complete MCP tool schema below 2,000 tokens.

## How it differs from upstream

CodeGraph Light is a focused fork for agents that need code navigation through
MCP. It reuses upstream parsing, symbol extraction, call resolution, graph
storage, and query code, with a smaller public interface and a different stdio
session runtime.

| Area | CodeGraph Light | Upstream codebase-memory-mcp |
| --- | --- | --- |
| Public MCP interface | Seven tools with a complete schema below 2,000 tokens | A broader toolset and more operation-specific options |
| Session runtime | Each stdio client has an independent server; no shared MCP daemon | Shared daemon and client coordination |
| Indexing | Graph-only `fast` mode, with no public mode selector | Additional indexing modes and semantic enrichment |
| Refresh | Run `index` explicitly; background tasks are disabled for Light stdio sessions | Background synchronization is available |
| Product scope | Native code-navigation MCP workflow and manual client configuration | UI, installer, client integrations, hooks, and administrative features |

Indexing still runs in supervised worker processes, and cross-process
publication locks protect shared indexes. A worker failure is reported without
closing the MCP transport; closing one client does not own the lifetime of
other clients.

"Light" describes the tool interface and operating scope. Vendored language
parsers remain in the native binary, so the executable can still be large.
The 2,000-token budget applies to tool definitions, not query responses or
index size. Some inherited UI, daemon, and installer source remains in the
repository for selective maintenance; its presence does not imply feature
parity with upstream.

## Download

Download the Windows x86-64 executable from this fork's
[Releases](https://github.com/knewstimek/codegraph-light/releases), and compare
its SHA-256 with the release notes. Configure your MCP client to run the local
executable using the examples under [Profiles](#profiles).

Current published binaries are for Windows x86-64. Linux users can
[build from source](#build). Reconnect the MCP client after replacing the
executable: an existing connection keeps its already-running server image.

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

Read tools accept either an indexed project ID or an absolute repository path
as `project`. This allows one session to query a known repository outside its
current working root without adding another discovery tool.

## Indexing scope and refresh

The `index` tool uses graph-only fast indexing. It applies file and directory
filters and omits semantic enrichment. Directories such as documentation,
examples, and fixtures can therefore be outside the indexed graph. Review the
index response's excluded, skipped, partial-parse, and unusable-parse diagnostics
before treating an empty symbol search as evidence that code is absent.

Run `index` before querying a new repository, and run it again after source or
build-configuration changes. Light stdio sessions do not start background
auto-indexing or file watching.

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
CI repeats the build, schema-budget check, and functional smoke test on Windows
and Linux.

The inherited package wrappers under `pkg/` remain sync references only. They
still describe the upstream distribution and must not be used to publish
CodeGraph Light. Use the native build and manual MCP configuration above until
derivative packages are added.

## Upstream and license

CodeGraph Light is derived from
[`DeusData/codebase-memory-mcp`](https://github.com/DeusData/codebase-memory-mcp).
Upstream updates are reviewed and ported selectively, especially parser,
extraction, graph-quality, traversal, and storage fixes. Daemon, watcher,
installer, updater, UI, hook, and release changes are reviewed separately against
Light's product scope and session-lifetime requirements. This fork does not
automatically track every upstream change or promise full upstream feature
parity.

The original project and this derivative are distributed under the MIT License.
Original copyright and license notices are preserved in [LICENSE](LICENSE).

Bundled components retain their own licenses and notices. See
[THIRD_PARTY.md](THIRD_PARTY.md) before redistributing binaries.
