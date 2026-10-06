"""工作进程集成测试：冻结快照导出、临时件->核验->原子发布、崩溃恢复/收敛。"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: E402
from app.config import Config  # noqa: E402
from app.storage import Storage  # noqa: E402
from app.worker import Worker  # noqa: E402


def make_config(worker_id="w1", lease_seconds=30):
    return Config(data_dir=Path(tempfile.mkdtemp()), worker_id=worker_id,
                  lease_seconds=lease_seconds, heartbeat_seconds=100, poll_seconds=0.05)


def records(note="例行航迹"):
    return models.normalize_payload([
        {"ship_id": "HC-1", "track_id": "T1", "timestamp": "2026-10-06T00:00:00Z",
         "longitude": 121.5, "latitude": 31.2, "sog": 7.0, "depth": 20.0, "note": note}
    ])


class WorkerFlowTests(unittest.TestCase):
    def test_happy_path_publishes_verified_artifact(self):
        cfg = make_config()
        storage = Storage(cfg.connect()); storage.init_db()
        rules = models.normalize_rules({"mask_fields": ["note"]})
        eid = storage.adjudicate_submission(records(), rules)["export_id"]

        worker = Worker(cfg)
        worker.process_once(eid)

        row = storage.get(eid)
        self.assertEqual(row["stage"], "PUBLISHED")
        final = cfg.artifact_dir / "published" / row["artifact_name"]
        body = json.loads(final.read_bytes())
        self.assertEqual(body["records"][0]["note"], "***MASKED***")
        self.assertEqual(body["records"][0]["depth"], 20.0)
        self.assertEqual(models.sha256_bytes(final.read_bytes()), row["content_sha256"])
        # 临时目录已清空
        self.assertEqual(list((cfg.artifact_dir / "tmp").iterdir()), [])
        stages = [r["stage"] for r in storage.conn.execute(
            "SELECT stage FROM stage_log WHERE export_id=? ORDER BY id", (eid,))]
        self.assertEqual(stages, ["ACCEPTED", "LEASED", "TEMP_WRITTEN", "VERIFIED", "PUBLISHED"])

    def test_rule_change_after_submit_does_not_change_export(self):
        cfg = make_config()
        storage = Storage(cfg.connect()); storage.init_db()
        rules = models.normalize_rules({"mask_fields": ["note"]})
        recs = records()
        eid = storage.adjudicate_submission(recs, rules)["export_id"]

        # 提交后值班员改动当前规则（扩大遮蔽范围）
        storage.set_current_rules(models.normalize_rules({"mask_fields": ["note", "depth", "sog"]}))

        worker = Worker(cfg)
        worker.process_once(eid)

        row = storage.get(eid)
        self.assertEqual(row["stage"], "PUBLISHED")
        body = json.loads((cfg.artifact_dir / "published" / row["artifact_name"]).read_bytes())
        # 仍按冻结快照：只遮蔽 note，depth/sog 明文
        self.assertEqual(body["records"][0]["note"], "***MASKED***")
        self.assertEqual(body["records"][0]["depth"], 20.0)
        self.assertEqual(body["records"][0]["sog"], 7.0)
        self.assertEqual(body["rule_snapshot"]["mask_fields"], ["note"])

    def test_recovery_after_crash_removes_partial_and_converges(self):
        cfg = make_config(lease_seconds=0.3)
        storage = Storage(cfg.connect()); storage.init_db()
        rules = models.normalize_rules({"mask_fields": ["depth"]})
        recs = records()
        eid = storage.adjudicate_submission(recs, rules)["export_id"]

        # 第一代 worker：写临时件后崩溃
        w1 = Worker(cfg)
        self.assertTrue(w1.storage.acquire_lease(eid, w1.owner, 0.3))
        content = models.render_artifact(eid, recs, rules)
        w1._write_temp(w1._tmp_path(eid), content)
        w1.storage.advance(eid, w1.owner, "TEMP_WRITTEN", content_sha256=models.sha256_bytes(content))
        # 模拟崩溃：不释放租约、不发布、残留临时件（再加一个被截断的残缺件）
        partial = w1._tmp_path(eid)
        partial.write_bytes(content[:50])
        tmp_dir = cfg.artifact_dir / "tmp"
        self.assertEqual(len(list(tmp_dir.iterdir())), 2)

        # 重启：新 worker，租约过期后领取并恢复
        time.sleep(0.4)
        cfg2 = Config(data_dir=cfg.data_dir, worker_id="w1", lease_seconds=30,
                      heartbeat_seconds=100, poll_seconds=0.05)
        w2 = Worker(cfg2)
        claimed = w2.storage.claim_next_pending(w2.owner, 30)
        self.assertEqual(claimed, eid)
        w2.process_once(eid)

        row = storage.get(eid)
        self.assertEqual(row["stage"], "PUBLISHED")
        final = cfg.artifact_dir / "published" / f"{eid}.json"
        self.assertTrue(final.exists())
        self.assertEqual(models.sha256_bytes(final.read_bytes()), row["content_sha256"])
        # 残缺临时件被清理，只剩唯一发布件
        self.assertEqual(list(tmp_dir.iterdir()), [])
        body = json.loads(final.read_bytes())
        self.assertEqual(body["records"][0]["depth"], "***MASKED***")

    def test_recovery_converges_to_existing_complete_temp(self):
        cfg = make_config()
        storage = Storage(cfg.connect()); storage.init_db()
        rules = models.normalize_rules({"mask_fields": []})
        recs = records()
        eid = storage.adjudicate_submission(recs, rules)["export_id"]

        w1 = Worker(cfg)
        w1.storage.acquire_lease(eid, w1.owner, 30)
        content = models.render_artifact(eid, recs, rules)
        sha = models.sha256_bytes(content)
        tmp = w1._tmp_path(eid)
        w1._write_temp(tmp, content)
        w1.storage.advance(eid, w1.owner, "TEMP_WRITTEN", content_sha256=sha)
        w1.storage.advance(eid, w1.owner, "VERIFIED", content_sha256=sha)
        # 崩溃发生在 rename 之前

        cfg2 = Config(data_dir=cfg.data_dir, worker_id="w2", lease_seconds=30,
                      heartbeat_seconds=100, poll_seconds=0.05)
        w2 = Worker(cfg2)
        # 手动以恢复者身份处理（租约过期后 claim）
        w2.storage.release_lease(eid, w1.owner)
        claimed = w2.storage.claim_next_pending(w2.owner, 30)
        self.assertEqual(claimed, eid)
        w2.process_once(eid)

        row = storage.get(eid)
        self.assertEqual(row["stage"], "PUBLISHED")
        self.assertEqual(row["content_sha256"], sha)
        # 恢复时复用了同一个完整临时件（收敛），最终只有一个工件
        published = list((cfg.artifact_dir / "published").iterdir())
        self.assertEqual(len(published), 1)
        self.assertEqual(list((cfg.artifact_dir / "tmp").iterdir()), [])

    def test_crash_marker_then_restart_publishes_once(self):
        """端到端：通过 crash marker 让进程在临时件后 os._exit，重启后发布。"""
        cfg = make_config()
        storage = Storage(cfg.connect()); storage.init_db()
        rules = models.normalize_rules({"mask_fields": ["note"]})
        eid = storage.adjudicate_submission(records(), rules)["export_id"]

        import os
        pid = os.fork()
        if pid == 0:
            cfg_child = Config(data_dir=cfg.data_dir, worker_id="crash-child",
                               lease_seconds=0.3, heartbeat_seconds=100)
            cfg_child.crash_marker.write_text(eid)
            wc = Worker(cfg_child)
            claimed = wc.storage.claim_next_pending(wc.owner, 0.3)
            assert claimed == eid
            wc.process_once(eid)  # 应在临时件写入后 os._exit(3)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 3)

        # 子进程已死：临时件残留、阶段停在 TEMP_WRITTEN
        row = storage.get(eid)
        self.assertEqual(row["stage"], "TEMP_WRITTEN")
        self.assertTrue(any((cfg.artifact_dir / "tmp").iterdir()))

        # 重启（新进程身份），等租约过期后收敛发布
        time.sleep(0.4)
        cfg2 = Config(data_dir=cfg.data_dir, worker_id="restart", lease_seconds=30,
                      heartbeat_seconds=100, poll_seconds=0.05)
        w2 = Worker(cfg2)
        self.assertEqual(w2.storage.claim_next_pending(w2.owner, 30), eid)
        w2.process_once(eid)
        row = storage.get(eid)
        self.assertEqual(row["stage"], "PUBLISHED")
        self.assertEqual(list((cfg.artifact_dir / "tmp").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
