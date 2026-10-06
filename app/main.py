"""入口：启动 HTTP API（同进程内附带一个后台工作线程）。"""
from __future__ import annotations

import argparse
import threading
import time

from .config import Config
from .server import build_server
from .worker import Worker


def main() -> None:
    parser = argparse.ArgumentParser(description="海洋测绘航迹导出服务")
    parser.add_argument("--no-worker", action="store_true", help="仅启动 API，不启动工作进程（测试用）")
    args = parser.parse_args()

    config = Config()
    server = build_server(config)

    workers: list[Worker] = []
    threads: list[threading.Thread] = []
    if not args.no_worker:
        worker = Worker(config)
        workers.append(worker)
        t = threading.Thread(target=worker.run_forever, name="worker-1", daemon=True)
        t.start()
        threads.append(t)

    print(f"[web] listening on {config.host}:{config.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        for w in workers:
            w.stop()
        time.sleep(0.2)


if __name__ == "__main__":
    main()
