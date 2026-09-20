#!/usr/bin/env python3
"""Cross-platform smoke test for the public CodeGraph Light MCP surface."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


PROFILES = {
    "default": ["index", "search", "trace", "source", "overview", "schema", "query"],
    "minimal": ["index", "search", "trace", "source"],
    "analysis": ["search", "trace", "source", "overview", "schema", "query"],
}


def request(proc: subprocess.Popen[str], request_id: int, method: str, params: dict) -> dict:
    assert proc.stdin is not None and proc.stdout is not None
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        if not line:
            stderr = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"MCP process exited before response {request_id}: {stderr[-2000:]}")
        response = json.loads(line)
        if response.get("id") == request_id:
            return response


def inspect_profile(binary: Path, profile: str, encodings: list[object]) -> tuple[int, list[int]]:
    args = [str(binary)]
    if profile != "default":
        args.append(f"--tool-profile={profile}")
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        initialized = request(
            proc,
            1,
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}},
        )
        info = initialized.get("result", {}).get("serverInfo", {})
        if info.get("name") != "codegraph-light":
            raise AssertionError(f"unexpected server name for {profile}: {info!r}")

        listed = request(proc, 2, "tools/list", {})
        result = listed.get("result", {})
        tools = result.get("tools", [])
        names = [tool.get("name") for tool in tools]
        if len(names) != len(PROFILES[profile]) or set(names) != set(PROFILES[profile]):
            raise AssertionError(f"{profile}: expected {PROFILES[profile]}, got {names}")
        encoded = json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) >= 5000:
            raise AssertionError(f"{profile}: tools/list result is {len(encoded)} bytes (limit: 4999)")
        token_counts = [len(encoding.encode(encoded.decode("utf-8"))) for encoding in encodings]
        if any(count >= 2000 for count in token_counts):
            raise AssertionError(f"{profile}: tools/list token counts {token_counts} exceed 1,999")
        return len(encoded), token_counts
    finally:
        if proc.stdin:
            proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=10)


def assert_call_ok(response: dict, tool: str) -> dict:
    if "error" in response:
        raise AssertionError(f"{tool}: JSON-RPC error: {response['error']!r}")
    result = response.get("result", {})
    if result.get("isError"):
        raise AssertionError(f"{tool}: tool error: {result!r}")
    return result


def inspect_graph_roundtrip(binary: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="codegraph-light-smoke-") as directory:
        fixture = Path(directory)
        (fixture / "mathbox.py").write_text(
            "def add(left, right):\n"
            "    return left + right\n\n"
            "def twice(value):\n"
            "    return add(value, value)\n",
            encoding="utf-8",
        )
        project = fixture.name
        proc = subprocess.Popen(
            [str(binary)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            request(
                proc,
                10,
                "initialize",
                {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}},
            )
            assert_call_ok(
                request(proc, 11, "tools/call", {"name": "index", "arguments": {"repo_path": str(fixture)}}),
                "index",
            )
            searched = assert_call_ok(
                request(
                    proc,
                    12,
                    "tools/call",
                    {"name": "search", "arguments": {"project": project, "query": "add", "limit": 5}},
                ),
                "search",
            )
            if "add" not in json.dumps(searched, ensure_ascii=False):
                raise AssertionError(f"search: indexed function was not returned: {searched!r}")
            assert_call_ok(
                request(
                    proc,
                    13,
                    "tools/call",
                    {
                        "name": "trace",
                        "arguments": {
                            "project": project,
                            "function_name": "twice",
                            "direction": "outbound",
                            "depth": 2,
                            "limit": 10,
                        },
                    },
                ),
                "trace",
            )
            source = assert_call_ok(
                request(
                    proc,
                    14,
                    "tools/call",
                    {
                        "name": "source",
                        "arguments": {
                            "project": project,
                            "qualified_name": "mathbox.add",
                            "max_lines": 20,
                        },
                    },
                ),
                "source",
            )
            if "return left + right" not in json.dumps(source, ensure_ascii=False):
                raise AssertionError(f"source: function body missing: {source!r}")
            assert_call_ok(
                request(proc, 15, "tools/call", {"name": "overview", "arguments": {"project": project}}),
                "overview",
            )
            schema = assert_call_ok(
                request(proc, 16, "tools/call", {"name": "schema", "arguments": {"project": project}}),
                "schema",
            )
            if "edge_types" not in json.dumps(schema, ensure_ascii=False).lower():
                raise AssertionError(f"schema: relationship metadata missing: {schema!r}")
            queried = assert_call_ok(
                request(
                    proc,
                    17,
                    "tools/call",
                    {
                        "name": "query",
                        "arguments": {
                            "project": project,
                            "query": "MATCH (n) RETURN n LIMIT 1",
                            "max_rows": 10,
                        },
                    },
                ),
                "query",
            )
            if "rows" not in json.dumps(queried, ensure_ascii=False):
                raise AssertionError(f"query: graph rows missing: {queried!r}")
        finally:
            if proc.stdin:
                proc.stdin.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=10)
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument(
        "--require-tokenizer",
        action="store_true",
        help="require tiktoken and verify cl100k_base and o200k_base stay below 2,000 tokens",
    )
    parser.add_argument("--functional", action="store_true", help="index and query a temporary repository")
    args = parser.parse_args()
    binary = args.binary.resolve()
    if not binary.is_file():
        parser.error(f"binary does not exist: {binary}")
    encodings: list[object] = []
    try:
        import tiktoken

        encodings = [tiktoken.get_encoding("cl100k_base"), tiktoken.get_encoding("o200k_base")]
    except ImportError:
        if args.require_tokenizer:
            parser.error("--require-tokenizer needs the optional tiktoken package")

    sizes = {profile: inspect_profile(binary, profile, encodings) for profile in PROFILES}
    if args.functional:
        inspect_graph_roundtrip(binary)
    rendered = ", ".join(
        f"{profile}={size}B" + (f"/{max(tokens)}tok" if tokens else "")
        for profile, (size, tokens) in sizes.items()
    )
    print(f"OK: CodeGraph Light MCP profiles and schema budget ({rendered})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
