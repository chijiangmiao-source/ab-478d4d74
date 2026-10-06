"""独立后台工作进程入口：可在 Compose 中启动多个副本来验证租约互斥。"""
from __future__ import annotations

import signal
import time

from .config import Config
from .worker import Worker


def main() -> None:
    config = Config()
    worker = Worker(config)

    def _stop(signum, frame):  # noqa: ANN001
        worker.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(f"[worker] {worker.owner} started, lease={config.lease_seconds}s", flush=True)
    worker.run_forever()


if __name__ == "__main__":
    main()
