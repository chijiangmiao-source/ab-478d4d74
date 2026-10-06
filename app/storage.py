"""SQLite 持久化：提交裁决（原子冻结）、租约、阶段机、发布登记。"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Optional

from . import models

# 阶段机：只允许沿该顺序前进，永不回退。
STAGES = ["ACCEPTED", "LEASED", "TEMP_WRITTEN", "VERIFIED", "PUBLISHED", "FAILED"]


class Storage:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---- 初始化 -------------------------------------------------------

    def init_db(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS exports (
                    export_id        TEXT PRIMARY KEY,
                    receipt_id       TEXT NOT NULL,
                    business_key     TEXT NOT NULL,
                    records_norm     TEXT NOT NULL,
                    rules_snapshot   TEXT NOT NULL,
                    input_hash       TEXT NOT NULL,
                    rules_hash       TEXT NOT NULL,
                    stage            TEXT NOT NULL,
                    content_sha256   TEXT,
                    artifact_name    TEXT,
                    lease_owner      TEXT,
                    lease_expires_at REAL,
                    attempts         INTEGER NOT NULL DEFAULT 0,
                    last_error       TEXT,
                    created_at       REAL NOT NULL,
                    updated_at       REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_exports_bkey ON exports(business_key);
                CREATE TABLE IF NOT EXISTS stage_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    export_id  TEXT NOT NULL,
                    stage      TEXT NOT NULL,
                    note       TEXT,
                    at         REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_stage_log_exp ON stage_log(export_id, id);
                CREATE TABLE IF NOT EXISTS app_settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO app_settings(key, value) VALUES('current_rules', ?)",
                (json.dumps({"version": models.CANONICAL_VERSION, "mask_fields": []}),),
            )

    def get_current_rules(self) -> dict:
        row = self.conn.execute(
            "SELECT value FROM app_settings WHERE key='current_rules'"
        ).fetchone()
        return json.loads(row["value"]) if row else {"mask_fields": []}

    def set_current_rules(self, rules_norm: dict) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE app_settings SET value=? WHERE key='current_rules'",
                (json.dumps(rules_norm, ensure_ascii=False),),
            )

    def _log(self, export_id: str, stage: str, note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO stage_log(export_id, stage, note, at) VALUES (?,?,?,?)",
            (export_id, stage, note, time.time()),
        )

    # ---- 提交裁决 -----------------------------------------------------

    def adjudicate_submission(
        self, records_norm: list[dict], rules_norm: dict
    ) -> dict[str, Any]:
        """同一持久化事务中冻结规范化输入与规则快照。

        返回:
          {"outcome": "created"|"duplicate"|"conflict", "export_id": ..., "conflict": {...}}
        """
        input_hash = models.stable_hash(records_norm)
        rules_hash = models.stable_hash(rules_norm)
        export_id = models.make_export_id(records_norm, rules_norm)
        business_key = models.make_business_key(records_norm)
        receipt_id = f"rcpt_{uuid.uuid4().hex}"
        now = time.time()

        with self.conn:  # BEGIN IMMEDIATE 语义由 sqlite 隐式提供，先查后写在同一事务
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT * FROM exports WHERE business_key = ? ORDER BY rowid ASC LIMIT 1",
                (business_key,),
            ).fetchone()

            if row is not None:
                if row["input_hash"] == input_hash and row["rules_hash"] == rules_hash:
                    return {
                        "outcome": "duplicate",
                        "export_id": row["export_id"],
                        "receipt_id": row["receipt_id"],
                        "conflict": None,
                    }
                # 业务等价（航迹相同）但记录或规则快照不同 => 明确冲突，保留原证据
                return {
                    "outcome": "conflict",
                    "export_id": row["export_id"],
                    "receipt_id": row["receipt_id"],
                    "conflict": {
                        "existing_export_id": row["export_id"],
                        "existing_stage": row["stage"],
                        "existing_input_hash": row["input_hash"],
                        "existing_rules_hash": row["rules_hash"],
                        "incoming_input_hash": input_hash,
                        "incoming_rules_hash": rules_hash,
                        "input_changed": row["input_hash"] != input_hash,
                        "rules_changed": row["rules_hash"] != rules_hash,
                    },
                }

            self.conn.execute(
                """INSERT INTO exports(export_id, receipt_id, business_key, records_norm,
                       rules_snapshot, input_hash, rules_hash, stage, attempts,
                       created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?, 'ACCEPTED', 0, ?, ?)""",
                (
                    export_id,
                    receipt_id,
                    business_key,
                    json.dumps(records_norm, ensure_ascii=False),
                    json.dumps(rules_norm, ensure_ascii=False),
                    input_hash,
                    rules_hash,
                    now,
                    now,
                ),
            )
            self._log(export_id, "ACCEPTED", "frozen input + rule snapshot")
            return {"outcome": "created", "export_id": export_id, "receipt_id": receipt_id, "conflict": None}

    # ---- 查询 ---------------------------------------------------------

    def get(self, export_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM exports WHERE export_id = ?", (export_id,)
        ).fetchone()

    def get_status(self, export_id: str) -> Optional[dict[str, Any]]:
        row = self.get(export_id)
        if row is None:
            return None
        logs = self.conn.execute(
            "SELECT stage, note, at FROM stage_log WHERE export_id = ? ORDER BY id",
            (export_id,),
        ).fetchall()
        return {
            "export_id": row["export_id"],
            "receipt_id": row["receipt_id"],
            "stage": row["stage"],
            "attempts": row["attempts"],
            "last_error": row["last_error"],
            "content_sha256": row["content_sha256"],
            "artifact_name": row["artifact_name"],
            "lease_owner": row["lease_owner"],
            "rule_snapshot": json.loads(row["rules_snapshot"]),
            "input_hash": row["input_hash"],
            "rules_hash": row["rules_hash"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "stages": [
                {"stage": r["stage"], "note": r["note"], "at": r["at"]} for r in logs
            ],
            "download_available": row["stage"] == "PUBLISHED",
        }

    def get_frozen(self, export_id: str) -> Optional[tuple[list[dict], dict]]:
        row = self.get(export_id)
        if row is None:
            return None
        return json.loads(row["records_norm"]), json.loads(row["rules_snapshot"])

    # ---- 租约 ---------------------------------------------------------

    def acquire_lease(self, export_id: str, owner: str, ttl_seconds: float) -> bool:
        """尝试获取处理租约。仅未持有有效租约者可获取，可重入续租。"""
        now = time.time()
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT stage, lease_owner, lease_expires_at, attempts FROM exports WHERE export_id=?",
                (export_id,),
            ).fetchone()
            if row is None:
                return False
            if row["stage"] in ("PUBLISHED", "FAILED"):
                return False
            held = row["lease_owner"] is not None and (row["lease_expires_at"] or 0) > now
            if held and row["lease_owner"] != owner:
                return False
            self.conn.execute(
                """UPDATE exports SET lease_owner=?, lease_expires_at=?, attempts=?,
                       stage=CASE WHEN stage='ACCEPTED' THEN 'LEASED' ELSE stage END,
                       updated_at=? WHERE export_id=?""",
                (owner, now + ttl_seconds, row["attempts"] + 1, now, export_id),
            )
            if row["stage"] == "ACCEPTED":
                self._log(export_id, "LEASED", f"owner={owner}")
            return True

    def heartbeat(self, export_id: str, owner: str, ttl_seconds: float) -> bool:
        now = time.time()
        cur = self.conn.execute(
            "UPDATE exports SET lease_expires_at=? WHERE export_id=? AND lease_owner=?",
            (now + ttl_seconds, export_id, owner),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def release_lease(self, export_id: str, owner: str) -> None:
        self.conn.execute(
            "UPDATE exports SET lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
            "WHERE export_id=? AND lease_owner=?",
            (time.time(), export_id, owner),
        )
        self.conn.commit()

    def claim_next_pending(self, owner: str, ttl_seconds: float) -> Optional[str]:
        """领取一个待处理导出（含租约过期的崩溃恢复项）。"""
        now = time.time()
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                """SELECT export_id, stage FROM exports
                   WHERE stage IN ('ACCEPTED','LEASED','TEMP_WRITTEN','VERIFIED')
                     AND (lease_owner IS NULL OR lease_expires_at <= ?)
                   ORDER BY created_at ASC LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            export_id = row["export_id"]
            prev_stage = row["stage"]
            attempts = self.conn.execute(
                "SELECT attempts FROM exports WHERE export_id=?", (export_id,)
            ).fetchone()["attempts"]
            self.conn.execute(
                """UPDATE exports
                   SET lease_owner=?, lease_expires_at=?, attempts=?,
                       stage=CASE WHEN stage='ACCEPTED' THEN 'LEASED' ELSE stage END,
                       updated_at=?
                   WHERE export_id=?""",
                (owner, now + ttl_seconds, attempts + 1, now, export_id),
            )
            if prev_stage == "ACCEPTED":
                self._log(export_id, "LEASED", f"owner={owner}")
            else:
                # 崩溃恢复：阶段本身不回退，只记录在当前阶段上重新认领
                self._log(export_id, prev_stage, f"lease reclaimed by {owner} after recovery")
            return export_id

    # ---- 阶段推进（单调，禁止回退）-----------------------------------

    _STAGE_RANK = {s: i for i, s in enumerate(STAGES)}

    def advance(self, export_id: str, owner: str, target: str, note: str = "", **fields: Any) -> bool:
        """仅租约持有者可推进；阶段只能前进；可附带更新字段。"""
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT stage, lease_owner, lease_expires_at FROM exports WHERE export_id=?",
                (export_id,),
            ).fetchone()
            if row is None:
                return False
            now = time.time()
            if row["lease_owner"] != owner or (row["lease_expires_at"] or 0) <= now:
                return False
            cur_rank = self._STAGE_RANK[row["stage"]]
            new_rank = self._STAGE_RANK[target]
            if new_rank <= cur_rank:
                # 同阶段幂等写入也必须是当前租约持有者
                if row["lease_owner"] != owner or (row["lease_expires_at"] or 0) <= now:
                    return False
                if new_rank == cur_rank:
                    if fields:
                        sets = ", ".join(f"{k}=?" for k in fields)
                        vals = list(fields.values()) + [now, export_id]
                        self.conn.execute(
                            f"UPDATE exports SET {sets}, updated_at=? WHERE export_id=?", vals
                        )
                    return True
                return False
            sets = "stage=?"
            vals: list[Any] = [target]
            for k, v in fields.items():
                sets += f", {k}=?"
                vals.append(v)
            vals += [now, export_id]
            self.conn.execute(
                f"UPDATE exports SET {sets}, updated_at=? WHERE export_id=?", vals
            )
            self._log(export_id, target, note)
            return True

    def mark_failed(self, export_id: str, owner: str, error: str) -> None:
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT stage, lease_owner FROM exports WHERE export_id=?", (export_id,)
            ).fetchone()
            if row is None or row["stage"] == "PUBLISHED":
                return
            # 仅当前租约持有者可标记失败，防止旧持有者误伤已被恢复接管的导出
            if row["lease_owner"] is not None and row["lease_owner"] != owner:
                return
            self.conn.execute(
                "UPDATE exports SET stage='FAILED', last_error=?, lease_owner=NULL, "
                "lease_expires_at=NULL, updated_at=? WHERE export_id=?",
                (error[:2000], time.time(), export_id),
            )
            self._log(export_id, "FAILED", error[:500])

    # ---- 发布：唯一一次，原子登记 -------------------------------------

    def commit_publish(
        self, export_id: str, owner: str, artifact_name: str, content_sha256: str
    ) -> str:
        """把 VERIFIED 推进到 PUBLISHED，返回发布时所在阶段。

        PUBLISHED 是吸收态：并发的第二个发布会看到已发布并保持幂等返回。
        """
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT stage, lease_owner, lease_expires_at, content_sha256, artifact_name "
                "FROM exports WHERE export_id=?",
                (export_id,),
            ).fetchone()
            if row is None:
                return "MISSING"
            if row["stage"] == "PUBLISHED":
                # 同一工件幂等；若摘要不同说明异常，保留原证据
                if row["content_sha256"] == content_sha256:
                    return "PUBLISHED"
                return "DIGEST_MISMATCH"
            now = time.time()
            if row["lease_owner"] != owner or (row["lease_expires_at"] or 0) <= now:
                return "LEASE_LOST"
            if row["stage"] != "VERIFIED":
                return f"BAD_STAGE:{row['stage']}"
            self.conn.execute(
                """UPDATE exports SET stage='PUBLISHED', artifact_name=?, content_sha256=?,
                       lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE export_id=?""",
                (artifact_name, content_sha256, now, export_id),
            )
            self._log(export_id, "PUBLISHED", f"artifact={artifact_name} sha256={content_sha256[:16]}")
            return "PUBLISHED"
