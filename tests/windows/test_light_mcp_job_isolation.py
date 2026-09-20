#!/usr/bin/env python3
"""CodeGraph Light MCP sessions and index crashes stay process-isolated."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes"
    )]


class BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", wt.DWORD),
        ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t),
        ("active_process_limit", wt.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wt.DWORD),
        ("scheduling", wt.DWORD),
    ]


class ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimitInformation),
        ("io", IoCounters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("size", wt.DWORD),
        ("usage", wt.DWORD),
        ("thread_id", wt.DWORD),
        ("owner_pid", wt.DWORD),
        ("base_priority", ctypes.c_long),
        ("delta_priority", ctypes.c_long),
        ("flags", wt.DWORD),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
kernel32.CreateJobObjectW.restype = wt.HANDLE
kernel32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
kernel32.SetInformationJobObject.restype = wt.BOOL
kernel32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
kernel32.AssignProcessToJobObject.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.Thread32First.argtypes = [wt.HANDLE, ctypes.POINTER(ThreadEntry)]
kernel32.Thread32First.restype = wt.BOOL
kernel32.Thread32Next.argtypes = [wt.HANDLE, ctypes.POINTER(ThreadEntry)]
kernel32.Thread32Next.restype = wt.BOOL
kernel32.OpenThread.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenThread.restype = wt.HANDLE
kernel32.ResumeThread.argtypes = [wt.HANDLE]
kernel32.ResumeThread.restype = wt.DWORD


def send(proc: subprocess.Popen[str], payload: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def receive(proc: subprocess.Popen[str]) -> dict:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line:
        stderr = proc.stderr.read() if proc.stderr else ""
        raise AssertionError(
            f"MCP transport closed (exit={proc.poll()}): {stderr[-2000:]}"
        )
    return json.loads(line)


def request(proc: subprocess.Popen[str], request_id: int, method: str, params: dict) -> dict:
    send(proc, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    while True:
        response = receive(proc)
        if response.get("id") == request_id:
            return response


def assert_ok(response: dict, label: str) -> None:
    if "error" in response or response.get("result", {}).get("isError"):
        raise AssertionError(f"{label} failed: {response!r}")


def tool_text(response: dict) -> str:
    return "".join(
        item.get("text", "")
        for item in response.get("result", {}).get("content", [])
        if item.get("type") == "text"
    )


def resume_process_thread(pid: int) -> None:
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ThreadEntry()
        entry.size = ctypes.sizeof(entry)
        present = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while present:
            if entry.owner_pid == pid:
                thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, entry.thread_id)
                if not thread:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    kernel32.CloseHandle(thread)
                return
            present = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    raise AssertionError(f"suspended thread for pid {pid} was not found")


def create_kill_on_close_job() -> int:
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimitInformation()
    limits.basic.flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise ctypes.WinError(error)
    return job


def initialize(proc: subprocess.Popen[str], request_id: int) -> None:
    response = request(
        proc,
        request_id,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "job-isolation", "version": "1"},
        },
    )
    assert_ok(response, "initialize")


def spawn(binary: Path, cwd: Path, env: dict[str, str], suspended: bool = False) -> subprocess.Popen[str]:
    flags = CREATE_NO_WINDOW | (CREATE_SUSPENDED if suspended else 0)
    return subprocess.Popen(
        [str(binary)],
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=flags,
    )


def stop(proc: subprocess.Popen[str] | None) -> None:
    if not proc or proc.poll() is not None:
        return
    proc.kill()
    proc.wait(timeout=10)


def main() -> int:
    if sys.platform != "win32":
        print("SKIP: Windows-only job isolation regression")
        return 0
    if len(sys.argv) not in {2, 3} or (len(sys.argv) == 3 and sys.argv[2] != "--crash-seam"):
        print(
            "usage: test_light_mcp_job_isolation.py "
            "<codegraph-light.exe> [--crash-seam]"
        )
        return 2
    binary = Path(sys.argv[1]).resolve()
    exercise_crash = len(sys.argv) == 3
    if not binary.is_file():
        print(f"FAIL: binary not found: {binary}")
        return 2

    secure_parent = os.environ.get("CBM_CACHE_DIR")
    parent = secure_parent if secure_parent and os.path.isdir(secure_parent) else None
    owner: subprocess.Popen[str] | None = None
    peer: subprocess.Popen[str] | None = None
    job: int | None = None
    with tempfile.TemporaryDirectory(prefix="codegraph-light-job-", dir=parent) as work_text:
        work = Path(work_text)
        repository = work / "repo"
        cache = work / "cache"
        runtime = work / "runtime"
        repository.mkdir()
        cache.mkdir()
        runtime.mkdir()
        (repository / "moves.py").write_text(
            "def _leaf_move():\n    return 1\n\n"
            "def _leaf_use():\n    return _leaf_move()\n\n"
            "def dispatch_move():\n    return _leaf_move()\n\n"
            "def dispatch_use():\n    return _leaf_use()\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["CBM_CACHE_DIR"] = str(cache)
        env["CBM_RUNTIME_DIR"] = str(runtime)
        if exercise_crash:
            env["CBM_INDEX_MAX_RESTARTS"] = "5"
            env["CBM_TEST_CRASH_ON"] = "synthetic_index_crash.py"
        try:
            owner = spawn(binary, repository, env, suspended=True)
            job = create_kill_on_close_job()
            if not kernel32.AssignProcessToJobObject(job, int(owner._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            resume_process_thread(owner.pid)
            initialize(owner, 1)
            assert_ok(
                request(
                    owner,
                    2,
                    "tools/call",
                    {"name": "index", "arguments": {"repo_path": str(repository)}},
                ),
                "initial index",
            )
            if exercise_crash:
                (repository / "synthetic_index_crash.py").write_text(
                    "def should_never_publish():\n    return 0\n",
                    encoding="utf-8",
                )
                contained_response = request(
                    owner,
                    5,
                    "tools/call",
                    {"name": "index", "arguments": {"repo_path": str(repository)}},
                )
                contained_text = tool_text(contained_response)
                contained_result = contained_response.get("result", {})
                contained_detail = contained_result.get("structuredContent", {})
                recovered = (
                    not contained_result.get("isError")
                    and '"status":"indexed"' in contained_text
                    and "synthetic_index_crash.py" in contained_text
                )
                structured_failure = (
                    contained_result.get("isError")
                    and contained_detail.get("status")
                    in {"error", "aborted_no_previous_index", "aborted_previous_preserved"}
                )
                if not (recovered or structured_failure) or owner.poll() is not None:
                    raise AssertionError(
                        "index crash did not cross the supervised-worker boundary: "
                        f"response={contained_text[-2000:]!r}, exit={owner.poll()}"
                    )
                tools_label = "post-crash owner tools/list"
            else:
                tools_label = "post-index owner tools/list"
            assert_ok(request(owner, 6, "tools/list", {}), tools_label)

            peer = spawn(binary, repository, env)
            initialize(peer, 3)
            names = ["_leaf_move", "_leaf_use", "dispatch_move", "dispatch_use"]
            for offset, name in enumerate(names):
                send(
                    peer,
                    {
                        "jsonrpc": "2.0",
                        "id": 10 + offset,
                        "method": "tools/call",
                        "params": {
                            "name": "trace",
                            "arguments": {
                                "project": repository.name,
                                "function_name": name,
                                "direction": "both",
                                "depth": 4,
                                "limit": 100,
                            },
                        },
                    },
                )

            # RED before the fix: this closes the owner's restrictive job and
            # kills the shared daemon, so the peer receives zero responses and
            # exits 1. GREEN: each MCP owns its server; only owner is killed.
            kernel32.CloseHandle(job)
            job = None
            responses = [receive(peer) for _ in names]
            for response in responses:
                assert_ok(response, f"parallel trace {response.get('id')}")
            ids = sorted(response.get("id") for response in responses)
            if ids != [10, 11, 12, 13] or peer.poll() is not None:
                raise AssertionError(f"peer did not remain serviceable: ids={ids}, exit={peer.poll()}")
            detail = "index crash stayed in its worker and " if exercise_crash else ""
            print(
                f"PASS: {detail}closing one restrictive MCP job preserved all four peer "
                "trace responses"
            )
            return 0
        finally:
            if job:
                kernel32.CloseHandle(job)
            stop(owner)
            stop(peer)


if __name__ == "__main__":
    raise SystemExit(main())
