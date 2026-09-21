"""Keep supervised MCP usable after >26 failures or one recovery setup failure.

Run with a TEST_SEAMS=1 binary and an owner-only CBM_CACHE_DIR. The injected
worker exits normally with a nonzero code, so Windows shows no crash dialog.
"""

import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

from test_light_mcp_job_isolation import (
    CREATE_NO_WINDOW,
    CREATE_SUSPENDED,
    create_kill_on_close_job,
    initialize,
    kernel32,
    request,
    resume_process_thread,
    stop,
    tool_text,
)


def bounded_request(proc, request_id: int, repo: Path, seconds: int = 180) -> dict:
    result: dict = {}

    def run() -> None:
        try:
            result["value"] = request(
                proc, request_id, "tools/call",
                {"name": "index", "arguments": {"repo_path": str(repo)}},
            )
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        stop(proc)
        thread.join(5)
        raise AssertionError("supervised index request timed out")
    if "error" in result:
        raise result["error"]
    return result["value"]


def main() -> int:
    if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3 and sys.argv[2] != "--fail-artifact"):
        print("usage: test_worker_log_namespace.py <test-seam-binary> [--fail-artifact]")
        return 2
    fail_artifact = len(sys.argv) == 3
    binary = Path(sys.argv[1]).resolve()
    secure_root_text = os.environ.get("CBM_CACHE_DIR")
    if not secure_root_text or not Path(secure_root_text).is_dir():
        print("CBM_CACHE_DIR must name an existing owner-only directory")
        return 2
    ctypes.windll.kernel32.SetErrorMode(0x8003)
    owner = None
    job = None
    with tempfile.TemporaryDirectory(prefix="cbm-log-namespace-", dir=secure_root_text) as tmp:
        work = Path(tmp)
        repo = work / "repo"
        cache = work / "cache"
        runtime = work / "runtime"
        for path in (repo, cache, runtime):
            path.mkdir()
        exit_marker = work / "synthetic_worker_exit.marker"
        exit_marker.write_text("exit before indexing\n", encoding="utf-8")
        (repo / "synthetic_worker_exit.cpp").write_text(
            "int synthetic_worker_exit(void) { return 1; }\n", encoding="utf-8"
        )
        env = dict(os.environ)
        env["CBM_CACHE_DIR"] = str(cache)
        env["CBM_RUNTIME_DIR"] = str(runtime)
        env["CBM_INDEX_MAX_RESTARTS"] = "27" if not fail_artifact else "2"
        env["CBM_TEST_WORKER_EXIT_MARKER"] = str(exit_marker)
        if fail_artifact:
            env["CBM_TEST_ARTIFACT_FAIL_AT"] = "3"
        else:
            env["CBM_TEST_RETRY_UNATTRIBUTABLE"] = "1"
        try:
            # The retry loop can emit more than a pipe buffer of diagnostics.
            # Do not let an unread stderr pipe stall the MCP host mid-test.
            owner = subprocess.Popen(
                [str(binary)], cwd=repo, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                encoding="utf-8", creationflags=CREATE_NO_WINDOW | CREATE_SUSPENDED,
            )
            job = create_kill_on_close_job()
            if not kernel32.AssignProcessToJobObject(job, int(owner._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            resume_process_thread(owner.pid)
            initialize(owner, 1)
            first = bounded_request(owner, 2, repo)
            first_result = first.get("result", {})
            first_detail = first_result.get("structuredContent", {})
            if not first_result.get("isError"):
                raise AssertionError(
                    "injected worker exits were not reported: "
                    f"status={first_detail.get('status')!r} "
                    f"outcome={first_detail.get('outcome')!r} "
                    f"nodes={first_detail.get('nodes')!r} "
                    f"skipped={first_detail.get('skipped_count')!r} "
                    f"detail={str(first_detail.get('not_indexed_files'))[:350]!r}"
                )
            first_text = tool_text(first)
            if "spawn_failed" in first_text or "exit_nonzero" not in first_text:
                raise AssertionError("worker exit was overwritten by spawn_failed")
            retained = sum(1 for _ in work.rglob(".worker-log-*"))
            if not fail_artifact and retained <= 26:
                raise AssertionError(f"only {retained} failed worker logs were retained")

            exit_marker.unlink()
            (repo / "synthetic_recovery.cpp").write_text(
                "int synthetic_recovery(void) { return 2; }\n", encoding="utf-8"
            )
            second = bounded_request(owner, 3, repo)
            second_result = second.get("result", {})
            second_detail = second_result.get("structuredContent", {})
            if second_result.get("isError") or second_detail.get("status") != "indexed":
                raise AssertionError("same MCP session could not index after recovery")
            if owner.poll() is not None:
                raise AssertionError("MCP transport ended after worker failures")
            if fail_artifact:
                print("PASS: recovery artifact failure preserved exit_nonzero; "
                      "same-session recovery indexed")
            else:
                print(f"PASS: retained_logs={retained}; same-session recovery indexed")
        finally:
            stop(owner)
            if job:
                kernel32.CloseHandle(job)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
