from __future__ import annotations

import argparse
import json
import os
import signal
import time
from pathlib import Path

from .control import process_expired_workers
from .store import Store, utcnow


def serve(home: Path, interval: float = 2.0) -> int:
    store = Store(home)
    store.initialize()
    marker = store.home / "aosd.json"
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    store.event("daemon.started", "daemon", "aosd", {"pid": os.getpid()})
    try:
        while running:
            marker.write_text(json.dumps({"pid": os.getpid(), "heartbeat": utcnow()}))
            os.chmod(marker, 0o600)
            process_expired_workers(store)
            time.sleep(interval)
    finally:
        marker.unlink(missing_ok=True)
        store.event("daemon.stopped", "daemon", "aosd", {"pid": os.getpid()})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the aOS recovery and lease supervisor")
    parser.add_argument("--home", type=Path, default=Path(os.getenv("AOS_HOME", "state")))
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()
    return serve(args.home, args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
