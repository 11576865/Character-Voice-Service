"""Windows-only supervised TTS launcher using a kill-on-close Job Object.

This wrapper intentionally assigns ITSELF to the job BEFORE starting
the engine, so a child cannot escape during the post-Popen assignment
race. The handle is kept solely in the wrapper process, not inherited.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _require(result, call):
    if not result:
        raise OSError(ctypes.get_last_error(), f"Windows {call} failed")
    return result


def main(argv: list[str]) -> int:
    if os.name != "nt":
        print("Windows Job Object wrapper cannot run on non-Windows OS", file=sys.stderr)
        return 86
    if not argv:
        print("Windows Job Object wrapper: missing engine command", file=sys.stderr)
        return 87

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.GetCurrentProcess.argtypes = []
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    job = _require(k32.CreateJobObjectW(None, None), "CreateJobObjectW")
    try:
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _require(
            k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)),
            "SetInformationJobObject",
        )
        # If a parent OS job disallows nesting, fail closed BEFORE spawning.
        _require(k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()), "AssignProcessToJobObject")

        # Job membership is inherited by descendants at CreateProcess time.
        # Do NOT permit a breakaway creation flag.
        engine = subprocess.Popen(argv, stdin=subprocess.DEVNULL, close_fds=True)
        code = engine.wait()
        # This process's ExitProcess will close its last Job Object handle.
        # All surviving descendants of the engine are killed at that point.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code & 0xFFFFFFFF)
    except Exception as exc:
        print(f"Windows managed job launch failed: {exc}", file=sys.stderr, flush=True)
        k32.CloseHandle(job)
        return 88


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
