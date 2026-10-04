"""Bounded real-MCP regression for duplicate C++ receiver definitions.

Run: python tests/test_cpp_duplicate_calls.py <binary> [--report <json-path>]
Fixtures/cache/runtime are fresh and isolated beneath the user's cache directory.
"""
import argparse
import hashlib
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent / "windows"))
from mcp_stdio import McpServer

HEADER = "#pragma once\nclass Surface { public: bool Lock(); void LockWords(); };\n"
IMPL = '#include "surface.h"\nbool Surface::Lock() { return true; }\nvoid Surface::LockWords() { if (!Lock()) return; }\n'
CALLS = "void Build(Surface& s) { if (!s.Lock()) return; }\nvoid Draw(Surface* s) { if (!s->Lock()) return; }\n"


def names(table):
    return {g["qn_prefix"] + "." + row[0] if g["qn_prefix"] else row[0]
            for g in table.get("groups", []) for row in g["rows"]}


def run(binary):
    parent = pathlib.Path.home() / ".cache"
    parent.mkdir(exist_ok=True)
    results = {"binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "cases": []}
    # Root include, explicit mirror, and negative controls for ambiguous visibility.
    cases = [("control", False, '#include "surface.h"\n', "surface"),
             ("duplicate", True, '#include "surface.h"\n', "surface"),
             ("mirror", True, '#include "mirror/surface.h"\n', "mirror.surface"),
             ("both", True, '#include "surface.h"\n#include "mirror/surface.h"\n', None),
             ("none", True, "", None),
             ("namespace", False, '#include "surface.h"\n', "surface.Alpha"),
             ("namespace_missing", False, '#include "surface.h"\n', None)]
    with tempfile.TemporaryDirectory(prefix="cpp-resolution-", dir=parent) as tmp:
        base = pathlib.Path(tmp)
        for label, duplicate, includes, target in cases:
            repo = base / label
            repo.mkdir()
            namespace_case = label.startswith("namespace")
            if namespace_case:
                header = "".join(
                    "namespace %s { class Surface { public: "
                    "bool Lock() { return true; } void LockWords() { Lock(); } }; }\n" % ns
                    for ns in ("Alpha", "Beta"))
                impl = '#include "surface.h"\n'
                calls = CALLS.replace("Surface", ("Alpha" if target else "Gamma") + "::Surface")
            else:
                header, impl, calls = HEADER, IMPL, CALLS
            (repo / "surface.h").write_text(header, encoding="utf-8")
            (repo / "surface.cpp").write_text(impl, encoding="utf-8")
            (repo / "caller.cpp").write_text(includes + calls, encoding="utf-8")
            if duplicate:
                (repo / "mirror").mkdir()
                for filename, text in [("surface.h", HEADER), ("surface.cpp", IMPL)]:
                    (repo / "mirror" / filename).write_text(text, encoding="utf-8")
            # Cross the extraction-worker threshold in one positive and one
            # ambiguity case; the small cases also cover the ordinary path.
            if label in ("duplicate", "both"):
                for i in range(51):
                    (repo / ("filler_%02d.cpp" % i)).write_text(
                        "int filler_%02d() { return %d; }\n" % (i, i), encoding="utf-8")
            cache, runtime = base / (label + "-cache"), base / (label + "-runtime")
            cache.mkdir()
            runtime.mkdir()
            with McpServer(str(binary), cache_dir=str(cache),
                           extra_env={"CBM_RUNTIME_DIR": str(runtime), "CBM_MAX_THREADS": "2"},
                           cwd=str(base)) as server:
                initialized = server.initialize(timeout=40)
                info = initialized.get("result", {}).get("serverInfo", {})
                tool_names = ({"index": "index_repository", "trace": "trace_path"}
                              if info.get("name") == "codebase-memory-mcp" else {})

                def call(tool, args):
                    response = server.call_tool(tool_names.get(tool, tool),
                                                dict(args, format="json"), timeout=90)
                    text, error = server.tool_text(response)
                    assert not error and not response.get("result", {}).get("isError"), text
                    return json.loads(text)

                indexed = call("index", {"repo_path": str(repo)})
                project = indexed["project"]
                for key in ("not_indexed_files_count", "skipped_count",
                            "parse_partial_count", "parse_unusable_count"):
                    assert indexed[key] == 0, (label, key, indexed[key])

                def trace(qn, direction):
                    return call("trace", {"project": project, "function_name": project + "." + qn,
                                          "direction": direction, "depth": 1, "limit": 10})

                observed = {}
                modules = (["surface.Alpha", "surface.Beta"] if namespace_case else
                           ["surface"] + (["mirror.surface"] if duplicate else []))
                for module in modules:
                    incoming = trace(module + ".Surface.Lock", "inbound")
                    expected = {project + "." + module + ".Surface.LockWords"}
                    if module == target:
                        expected |= {project + ".caller.Build", project + ".caller.Draw"}
                    actual = names(incoming.get("callers", {}))
                    assert actual == expected, (label, module, actual, expected)
                    assert incoming["callers_total"] == len(expected)
                    observed[module] = incoming["callers_total"]
                for caller in ("Build", "Draw"):
                    outgoing = trace("caller." + caller, "outbound")
                    actual = names(outgoing.get("callees", {}))
                    expected = {project + "." + target + ".Surface.Lock"} if target else set()
                    assert actual == expected, (label, caller, actual, expected)
                results["cases"].append({"case": label, "callers": observed,
                                         "direct_target": target, "parse_failures": 0})
                print(label + ": PASS", flush=True)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=pathlib.Path)
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    result = run(args.binary.resolve())
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(str(len(result["cases"])) + " cases passed")
