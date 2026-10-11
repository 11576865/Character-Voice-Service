"""Disposable cross-process GPU-group activity worker for lifecycle tests."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from server.runtime_inference_admission import runtime_group_guard


def main(args: list[str]) -> None:
    slot_dir, group, engine, entered, done = args
    with runtime_group_guard(group, Path(slot_dir)) as guard:
        assert guard is not None
        transaction = guard.begin_inference(engine)
        Path(entered).write_text(transaction, encoding="utf-8")
        deadline = time.monotonic() + 30
        while not Path(done).exists() and time.monotonic() < deadline:
            time.sleep(0.03)
        if not Path(done).exists():
            raise TimeoutError("fixture still waiting on inference completion")
        guard.finish_inference(transaction)


if __name__ == "__main__":
    main(sys.argv[1:])
