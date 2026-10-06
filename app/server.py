"""HTTP API 与静态页面：仅依赖标准库。"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from . import models
from .config import Config
from .storage import Storage

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "MarineTrackExport/1.0"

    # 每个请求使用自己的 SQLite 连接（WAL 支持并发，写事务串行裁决）
    def _storage(self) -> Storage:
        storage = getattr(self, "_db_storage", None)
        if storage is None:
            cfg: Config = self.server.config  # type: ignore[attr-defined]
            conn = cfg.connect()
            storage = Storage(conn)
            storage.init_db()
            self._db_storage = storage
        return storage

    def finish(self) -> None:
        super().finish()
        storage = getattr(self, "_db_storage", None)
        if storage is not None:
            storage.conn.close()

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "quiet", False):
            return
        super().log_message(fmt, *args)

    # ---- 响应工具 -----------------------------------------------------

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, name: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    # ---- 路由 ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json({"status": "ok", "service": "marine-track-export"})
            return
        if path == "/api/rules":
            self._send_json({"rules": self._storage().get_current_rules()})
            return
        if path == "/api/exports":
            self._list_exports()
            return
        if path.startswith("/api/exports/"):
            rest = path[len("/api/exports/"):]
            parts = [p for p in rest.split("/") if p]
            if len(parts) == 1:
                self._get_status(parts[0])
                return
            if len(parts) == 2 and parts[1] == "artifact":
                self._download(parts[0])
                return
        if path in ("/", "/index.html"):
            self._serve_static("index.html", "text/html; charset=utf-8")
            return
        if path == "/app.js":
            self._serve_static("app.js", "application/javascript; charset=utf-8")
            return
        self._send_json({"error": "not_found"}, 404)

    def do_PUT(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/rules":
            self._edit_rules()
            return
        self._send_json({"error": "not_found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/exports":
            self._submit()
            return
        self._send_json({"error": "not_found"}, 404)

    # ---- 业务 ---------------------------------------------------------

    def _edit_rules(self) -> None:
        try:
            payload = self._read_json()
            rules_norm = models.normalize_rules(payload.get("rules", payload))
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": "invalid_rules", "detail": str(exc)}, 400)
            return
        self._storage().set_current_rules(rules_norm)
        self._send_json({"ok": True, "rules": rules_norm})

    def _submit(self) -> None:
        try:
            payload = self._read_json()
            records_norm = models.normalize_payload(payload.get("records"))
            if "rules" in payload and payload["rules"] is not None:
                rules_norm = models.normalize_rules(payload["rules"])
            else:
                rules_norm = self._storage().get_current_rules()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": "invalid_submission", "detail": str(exc)}, 400)
            return

        result = self._storage().adjudicate_submission(records_norm, rules_norm)
        if result["outcome"] == "created":
            self._send_json(
                {
                    "outcome": "created",
                    "export_id": result["export_id"],
                    "receipt_id": result["receipt_id"],
                    "frozen_rules": rules_norm,
                    "message": "已受理：规范化输入与规则快照已在同一事务中冻结",
                },
                201,
            )
        elif result["outcome"] == "duplicate":
            self._send_json(
                {
                    "outcome": "duplicate",
                    "export_id": result["export_id"],
                    "receipt_id": result["receipt_id"],
                    "message": "业务等价重传：返回首次回执，不产生第二个工件",
                },
                200,
            )
        else:
            self._send_json(
                {
                    "outcome": "conflict",
                    "message": "业务键相同但记录或规则快照不一致，请求被拒绝；原有证据保留",
                    **result["conflict"],
                    "receipt_id": result["receipt_id"],
                },
                409,
            )

    def _get_status(self, export_id: str) -> None:
        status = self._storage().get_status(export_id)
        if status is None:
            self._send_json({"error": "not_found", "export_id": export_id}, 404)
            return
        self._send_json(status)

    def _list_exports(self) -> None:
        conn = self._storage().conn
        rows = conn.execute(
            "SELECT export_id, receipt_id, stage, content_sha256, artifact_name, "
            "rules_snapshot, created_at, updated_at, attempts "
            "FROM exports ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        items = [
            {
                "export_id": r["export_id"],
                "receipt_id": r["receipt_id"],
                "stage": r["stage"],
                "attempts": r["attempts"],
                "content_sha256": r["content_sha256"],
                "artifact_name": r["artifact_name"],
                "frozen_rules_summary": json.loads(r["rules_snapshot"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
        self._send_json({"exports": items, "current_rules": self._storage().get_current_rules()})

    def _download(self, export_id: str) -> None:
        cfg: Config = self.server.config  # type: ignore[attr-defined]
        storage = self._storage()
        row = storage.get(export_id)
        if row is None:
            self._send_json({"error": "not_found"}, 404)
            return
        # 下载接口绝不暴露未核验内容：只有 PUBLISHED 且磁盘摘要与登记摘要一致才可下载
        if row["stage"] != "PUBLISHED" or not row["artifact_name"] or not row["content_sha256"]:
            self._send_json(
                {"error": "not_published", "stage": row["stage"],
                 "message": "工件尚未完成核验与发布，拒绝下载"},
                409,
            )
            return
        final = cfg.artifact_dir / "published" / row["artifact_name"]
        try:
            body = final.read_bytes()
        except FileNotFoundError:
            self._send_json({"error": "artifact_missing"}, 410)
            return
        if models.sha256_bytes(body) != row["content_sha256"]:
            self._send_json(
                {"error": "artifact_tampered", "message": "磁盘工件摘要与发布登记不一致，拒绝下载"},
                409,
            )
            return
        self._send_bytes(body, "application/json; charset=utf-8", row["artifact_name"])

    def _serve_static(self, name: str, content_type: str) -> None:
        try:
            body = (WEB_ROOT / name).read_bytes()
        except FileNotFoundError:
            self._send_json({"error": "not_found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_server(config: Config, quiet: bool = False) -> ThreadingHTTPServer:
    config.ensure_dirs()
    Storage(config.connect()).init_db()
    server = ThreadingHTTPServer((config.host, config.port), ApiHandler)
    server.config = config
    server.quiet = quiet
    return server
