"""运行期配置：全部可通过环境变量覆盖，便于 Compose 与测试使用。"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", "./data")))
    host: str = field(default_factory=lambda: os.environ.get("WEB_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.environ.get("WEB_PORT", "8080")))
    lease_seconds: float = field(default_factory=lambda: float(os.environ.get("LEASE_SECONDS", "30")))
    heartbeat_seconds: float = field(default_factory=lambda: float(os.environ.get("HEARTBEAT_SECONDS", "10")))
    poll_seconds: float = field(default_factory=lambda: float(os.environ.get("POLL_SECONDS", "0.5")))
    worker_id: str = field(default_factory=lambda: os.environ.get("WORKER_ID", "worker"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def artifact_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def crash_marker(self) -> Path:
        return self.data_dir / "crash_after_temp"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        (self.artifact_dir / "tmp").mkdir(parents=True, exist_ok=True)
        (self.artifact_dir / "published").mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        self.ensure_dirs()
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
